"""Background monitor loop: unified confirm/probe via "1"+BSpace, state detection, auto-exit.

Ported from cam server's monitor.py + probe.py (deleted in Phase 4).
Key features preserved:
  - Unified "1"+BSpace for both auto-confirm and idle probe
  - Smart probe returns "idle" or "busy" (no ambiguous "confirmed" state)
  - Probe-caused output change filter (avoids false reset)
  - Max probe limit (threshold * 3)
  - Completion + idle_confirmed + auto-exit flow
"""

import hashlib
import logging
import os
import signal
import sys
import time

from camc_pkg import LOGS_DIR, log
from camc_pkg.utils import _now_iso
from camc_pkg.adapters import _load_config
from camc_pkg.storage import AgentStore, EventStore
from camc_pkg.transport import (
    capture_tmux, tmux_session_exists, tmux_send_input, tmux_send_key,
    tmux_kill_session,
)
from camc_pkg.detection import detect_state, should_auto_confirm, detect_completion


def _smart_probe(session, config):
    """Smart probe: send probe char, observe terminal echo, classify, clean up.

    Sends the adapter's probe_char (default "1") without Enter, then checks
    what happened:
      - "idle": char appeared in output (echoed at prompt) -> waiting for input
      - "busy": output unchanged (agent in raw mode) OR output changed but
        char not echoed (consumed by a dialog, agent resumes work)
      - "error": capture or send failed

    BSpace follows to clean up the probe char from the terminal.

    The probe char is configurable per adapter (config.probe_char). Default "1"
    works for Claude (selects option 1 in permission menus). Other agents may
    use a different char.

    Returns: "idle", "busy", or "error"
    """
    char = config.probe_char

    # 1. Capture baseline
    baseline = capture_tmux(session)
    if not baseline.strip():
        return "error"

    # 2. Send probe char (no Enter)
    if not tmux_send_input(session, char, send_enter=False):
        return "error"

    # 3. Wait and recapture
    time.sleep(config.probe_wait)
    after = capture_tmux(session)

    # 4. Classify — compare all non-empty lines, not just the last one.
    # Claude Code's TUI has status/separator lines below the prompt, so the
    # probe char may appear on any line (e.g. "❯\xa01"), not necessarily the last.
    result = "busy"
    baseline_stripped = baseline.rstrip("\n")
    after_stripped = after.rstrip("\n")

    if baseline_stripped != after_stripped:
        # Output changed — check if the probe char was echoed (idle)
        # or consumed by a dialog (busy)
        baseline_lines = set(baseline_stripped.splitlines())
        after_lines = after_stripped.splitlines()
        for line in after_lines:
            if line not in baseline_lines and char in line:
                result = "idle"
                break

    # 5. BSpace to clean up probe char
    tmux_send_key(session, "BSpace")
    time.sleep(0.15)

    return result


def run_monitor_loop(session, agent_id, config, store, pid_path=None, events=None):
    """Monitor a tmux session: unified confirm/probe, state detection, auto-exit.

    Uses a single "1"+BSpace mechanism for both confirmation and idle detection.

    Auto-confirm flow:
      Confirm pattern matched -> send "1" (no BSpace, dialog consumes it).

    Probe flow (after completion detected):
      _smart_probe() -> classify result:
        "idle" (echoed)  -> agent at prompt, consecutive_idle++
        "busy" (else)    -> agent working or dialog consumed, reset idle state

    Idle confirmed when consecutive_idle >= threshold (default 2).
    """
    if pid_path:
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))

    running = [True]
    signal.signal(signal.SIGTERM, lambda s, f: running.__setitem__(0, False))

    prev_hash = ""
    prev_output = ""
    last_change = last_health = time.time()
    last_confirm = 0.0
    current_state = None
    has_worked = False
    empty_count = 0

    # Probe state (ported from cam server monitor)
    last_probe = 0.0
    probe_count = 0                    # total probes sent
    consecutive_idle = 0          # consecutive idle probes
    idle_confirmed = False             # set True once probe confirms idle
    completion_detected = False        # set True when detect_completion matches
    max_probes = config.probe_idle_threshold * 3  # give up after this many

    def _event(event_type, detail=None):
        if events:
            events.append(agent_id, event_type, detail)

    _event("monitor_start")

    try:
        while running[0]:
            now = time.time()

            # --- Health check (periodic) ---
            if now - last_health >= config.health_check_interval:
                last_health = now
                if not tmux_session_exists(session):
                    # Session disappeared — check last output for completion
                    # signals before deciding completed vs failed.
                    if prev_output:
                        done = detect_completion(prev_output, config)
                        if done:
                            status = "completed"
                        elif has_worked:
                            status = "completed"
                        else:
                            status = "failed"
                    else:
                        status = "completed" if has_worked else "failed"
                    reason = "Session ended cleanly" if status == "completed" else "Session exited before agent started working"
                    log.info("Session gone: %s -> %s (%s)", session, status, reason)
                    store.update(agent_id, status=status,
                                 exit_reason=reason, completed_at=_now_iso())
                    _event("completed", {"status": status, "reason": reason})
                    return

            # --- Capture output ---
            output = capture_tmux(session)
            if output.strip():
                prev_output = output
            h = hashlib.md5(output.encode()).hexdigest()[:8]
            changed = h != prev_hash

            # --- Output change detection with probe-caused filter ---
            # If output changed shortly after a probe, it was likely caused
            # by the probe itself (e.g. "1" echoing or BSpace clearing).
            # Don't reset idle state for probe-caused changes.
            probe_caused = (
                last_probe > 0
                and now - last_probe < config.probe_wait + 2.0
            )

            if changed:
                if not probe_caused:
                    # Real change — agent is working, reset everything
                    last_change = now
                    idle_confirmed = False
                    consecutive_idle = 0
                    completion_detected = False
                else:
                    # Probe-caused change — update hash but don't reset idle state
                    log.debug("Probe-caused output change, not resetting")
            prev_hash = h

            if not output.strip():
                empty_count += 1
                if empty_count >= config.empty_threshold and not tmux_session_exists(session):
                    status = "completed" if has_worked else "failed"
                    store.update(agent_id, status=status,
                                 exit_reason="Session exited", completed_at=_now_iso())
                    _event("completed", {"status": status, "reason": "Session exited"})
                    return
                time.sleep(1)
                continue
            empty_count = 0

            # --- Auto-confirm: send "1" when confirm pattern detected ---
            # Pattern already matched a dialog, so "1" will be consumed by the
            # menu (select option 1). No Enter, no BSpace needed.
            if now - last_confirm >= config.confirm_cooldown:
                confirm = should_auto_confirm(output, config)
                if confirm:
                    log.info("Auto-confirm: pattern matched, sending '1'")
                    tmux_send_input(session, "1", send_enter=False)
                    last_confirm = now
                    _event("auto_confirm", {"pattern": "matched"})
                    time.sleep(config.confirm_sleep)
                    continue

            # --- State detection ---
            ns = detect_state(output, config)
            if ns and ns != current_state:
                if ns != "initializing":
                    has_worked = True
                log.info("State: %s -> %s", current_state, ns)
                _event("state_change", {"from": current_state, "to": ns})
                current_state = ns
                store.update(agent_id, state=ns)

            # --- Completion detection ---
            idle_for = now - last_change
            if not changed and idle_for >= config.completion_stable:
                done = detect_completion(output, config)
                if done:
                    has_worked = True
                    if not completion_detected:
                        completion_detected = True
                        log.info("Completion detected (output stable %.1fs)", idle_for)
                        _event("completion_detected", {"idle_for": idle_for})

            # --- Probe-based idle detection ---
            # Only probe after completion detected, agent has worked,
            # idle not yet confirmed, and haven't exceeded max probes.
            if (has_worked
                    and completion_detected
                    and not idle_confirmed
                    and probe_count < max_probes):
                if (idle_for >= config.probe_stable
                        and now - last_probe >= config.probe_cooldown):
                    last_probe = now
                    probe_count += 1
                    probe_result = _smart_probe(session, config)

                    log.info("Probe #%d: %s (consecutive=%d/%d)",
                             probe_count, probe_result,
                             consecutive_idle + (1 if probe_result == "idle" else 0),
                             config.probe_idle_threshold)
                    _event("probe", {
                        "result": probe_result,
                        "probe_count": probe_count,
                        "consecutive_idle": consecutive_idle,
                    })

                    if probe_result == "idle":
                        consecutive_idle += 1
                    else:
                        # busy or error — agent working, reset
                        last_change = now
                        consecutive_idle = 0
                        completion_detected = False

                    if consecutive_idle >= config.probe_idle_threshold:
                        idle_confirmed = True
                        log.info("Idle confirmed (%d consecutive probes)",
                                 consecutive_idle)
                        _event("idle_confirmed", {"probe_count": probe_count})
                        store.update(agent_id, state="idle")

            # --- Auto-exit on completion + idle confirmed ---
            # Two paths:
            # 1. Probe confirmed: completion_detected + idle_confirmed
            # 2. Long stable fallback: completion_detected + idle >= completion_stable * 3
            #    (for trivial tasks where probes all return busy/error)
            completion_stable_enough = (
                completion_detected
                and idle_for >= config.completion_stable * 3
            )
            if completion_detected and (idle_confirmed or completion_stable_enough):
                if not idle_confirmed:
                    store.update(agent_id, state="idle")

                agent_rec = store.get(agent_id)
                ae = None
                if agent_rec:
                    t = agent_rec.get("task")
                    if isinstance(t, dict):
                        ae = t.get("auto_exit")
                    else:
                        ae = agent_rec.get("auto_exit")
                if ae is None:
                    ae = getattr(config, "auto_exit", False)
                if ae:
                    exit_action = getattr(config, "exit_action", "kill_session")
                    log.info("Auto-exit: action=%s", exit_action)
                    if exit_action == "kill_session":
                        tmux_kill_session(session)
                    elif exit_action == "send_exit":
                        tmux_send_input(session, getattr(config, "exit_command", "/exit"), send_enter=True)
                        for _ in range(10):
                            time.sleep(1)
                            if not tmux_session_exists(session):
                                break
                        else:
                            tmux_kill_session(session)
                    store.update(agent_id, status="completed",
                                 exit_reason="Task completed (auto-exit)",
                                 completed_at=_now_iso())
                    _event("completed", {"status": "completed", "reason": "auto-exit"})
                    return

            time.sleep(1)
    finally:
        if pid_path:
            try:
                os.unlink(pid_path)
            except OSError:
                pass


def _run_monitor(agent_id):
    # Reconfigure logging to file for background monitor
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
    except OSError:
        pass
    log_path = os.path.join(LOGS_DIR, "monitor-%s.log" % agent_id)
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [monitor] %(levelname)s %(message)s",
        filename=log_path,
    )
    log.info("Monitor starting for agent %s", agent_id)

    store = AgentStore()
    events = EventStore()
    agent = store.get(agent_id)
    if not agent:
        log.error("Agent %s not found", agent_id)
        sys.exit(1)
    # Support both new nested task format and legacy flat format
    _t = agent.get("task")
    _tool = _t.get("tool", "claude") if isinstance(_t, dict) else agent.get("tool", "claude")
    _session = agent.get("tmux_session") or agent.get("session", "")
    _path = agent.get("context_path") or agent.get("path", "")
    config = _load_config(_tool)
    log.info("Tool=%s session=%s path=%s", _tool, _session, _path)
    from camc_pkg import PIDS_DIR
    try:
        os.makedirs(PIDS_DIR, exist_ok=True)
    except OSError:
        pass
    pid_path = os.path.join(PIDS_DIR, "%s.pid" % agent_id)

    # Auto-restart on crash (e.g. database locked, transient errors)
    max_restarts = 5
    for attempt in range(max_restarts + 1):
        try:
            run_monitor_loop(_session, agent_id, config, store, pid_path=pid_path, events=events)
            break  # clean exit
        except Exception as e:
            log.error("Monitor crashed (attempt %d/%d): %s", attempt + 1, max_restarts, e)
            if attempt >= max_restarts:
                log.error("Max restarts reached, giving up")
                store.update(agent_id, status="failed", exit_reason="Monitor crashed: %s" % e,
                             completed_at=_now_iso())
                break
            time.sleep(5 * (attempt + 1))  # backoff: 5s, 10s, 15s...
            # Re-check if agent is still running
            agent = store.get(agent_id)
            if not agent or agent.get("status") != "running":
                log.info("Agent no longer running, stopping monitor")
                break
            log.info("Restarting monitor...")

    log.info("Monitor finished for agent %s", agent_id)
