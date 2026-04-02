"""Background monitor loop: unified confirm/probe via "1"+BSpace, state detection, auto-exit.

v2 design (single-probe, attachment-aware):
  - Auto-confirm: always runs (even when user attached)
  - Single probe: screen stable 5s → send "1" → echoed = idle, consumed = busy
  - Attachment check: only blocks auto-exit (don't kill session user is in)
  - Idle revival: any real output change resets idle_confirmed → full cycle resumes
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
    tmux_kill_session, tmux_is_attached,
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
    """Monitor a tmux session: auto-confirm, single-probe idle detection, auto-exit.

    Phase 1 - Auto-confirm: always runs, sends response when confirm pattern matched.
    Phase 2 - State detection: pattern match on output → planning/editing/testing/etc.
    Phase 3 - Idle probe: screen stable 5s → send "1" → echoed = idle (single probe).
    Phase 4 - Auto-exit: idle_confirmed + not attached + auto_exit → kill session.
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
    last_confirm_matched = ""  # suppress duplicate matches on same text
    last_confirm_false = False  # was last confirm a false positive?
    confirm_suppress = set()  # confirmed patterns — suppress scrollback re-triggers
    current_state = None
    has_worked = False
    empty_count = 0

    # Probe state
    last_probe = 0.0
    idle_confirmed = False

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
            # Also filter confirm-caused changes: "1"+BSpace cycle changes
            # the output hash but isn't real agent work.
            confirm_caused = (
                last_confirm > 0
                and now - last_confirm < config.confirm_cooldown
            )

            if changed:
                if not probe_caused and not confirm_caused:
                    # Real change — agent is working, reset idle state
                    last_change = now
                    idle_confirmed = False
                    # Clear suppress set — scrollback has scrolled away,
                    # new dialogs with the same text are real.
                    confirm_suppress.clear()
                    # Don't reset false-positive state here — if the same
                    # pattern keeps matching across output changes, it's
                    # agent prose scrolling past, not a new dialog appearing.
                    # The false-positive flag is only reset when the confirm
                    # pattern stops matching (output no longer contains it).
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

            # --- Auto-confirm: send response when confirm pattern detected ---
            # Real permission dialogs consume the keystroke (e.g. "1" selects
            # option 1). If the pattern matched agent prose instead, the char
            # would echo at the prompt. To prevent "1" accumulation:
            #   1. Capture baseline before sending
            #   2. Send response
            #   3. Wait briefly, recapture
            #   4. If output didn't change (false positive), BSpace to clean up
            if now - last_confirm >= config.confirm_cooldown:
                confirm = should_auto_confirm(output, config)
                if confirm:
                    response, send_enter, pat_str, matched = confirm
                    confirm_key = "%s:%s" % (pat_str, matched)
                    # After a successful confirm, the old dialog text lingers
                    # in scrollback. Suppress re-matches until real output change.
                    if confirm_key in confirm_suppress:
                        time.sleep(1)
                        continue
                    if confirm_key == last_confirm_matched and last_confirm_false:
                        log.debug("Auto-confirm: suppressed (known false positive)")
                        time.sleep(1)
                    else:
                        log.info("Auto-confirm: pattern=%r matched=%r -> response=%r enter=%s",
                                 pat_str, matched, response, send_enter)
                        if response:
                            tmux_send_input(session, response, send_enter=send_enter)
                        elif send_enter:
                            tmux_send_key(session, "Enter")
                        last_confirm = now
                        _event("auto_confirm", {"pattern": pat_str, "matched": matched,
                                                "response": response})
                        time.sleep(config.confirm_sleep)
                        # Check if dialog consumed the input.
                        # Real dialog: screen redraws (agent resumes).
                        # False positive: "1" echoes at prompt, pattern still visible.
                        post = capture_tmux(session)
                        last_confirm_false = False
                        if response and not send_enter:
                            still_matches = should_auto_confirm(post, config)
                            if still_matches and still_matches[2] == pat_str:
                                # Pattern still there → false positive, clean up.
                                tmux_send_key(session, "BSpace")
                                last_confirm_false = True
                                log.info("Auto-confirm: false positive (pattern still visible), BSpace cleanup")
                                _event("auto_confirm_cleanup", {"pattern": pat_str})
                            else:
                                # Dialog consumed → suppress this pattern until
                                # real output change clears the scrollback.
                                confirm_suppress.add(confirm_key)
                                log.info("Auto-confirm: success, suppressing scrollback re-trigger")
                        last_confirm_matched = confirm_key
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

            # --- Idle probe: single probe when screen stable ---
            idle_for = now - last_change
            if (not idle_confirmed
                    and not changed
                    and idle_for >= config.probe_stable
                    and now - last_probe >= config.probe_cooldown):
                last_probe = now
                probe_result = _smart_probe(session, config)

                log.info("Probe: %s (idle_for=%.1fs)", probe_result, idle_for)
                _event("probe", {"result": probe_result, "idle_for": idle_for})

                if probe_result == "idle":
                    idle_confirmed = True
                    log.info("Idle confirmed (single probe)")
                    _event("idle_confirmed")
                    store.update(agent_id, state="idle")
                else:
                    # busy or error — agent working or dialog consumed "1"
                    last_change = now

            # --- Fallback: long stable without probe confirmation ---
            fallback_stable = getattr(config, "fallback_stable", 30.0)
            if not idle_confirmed and idle_for >= fallback_stable:
                idle_confirmed = True
                log.info("Idle confirmed (fallback: stable %.1fs)", idle_for)
                _event("idle_confirmed", {"reason": "fallback"})
                store.update(agent_id, state="idle")

            # --- Auto-exit: only when idle + not attached ---
            if idle_confirmed:
                # Don't kill a session a user is sitting in
                if tmux_is_attached(session):
                    pass  # wait for detach
                else:
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
