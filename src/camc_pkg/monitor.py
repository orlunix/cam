"""Background monitor loop (~1s poll cycle).

Pure screen-based, tool-agnostic design:
  1. Capture screen -> detect state/confirm/idle
  2. Auto-confirm: pattern match on last 8 lines -> send response
  3. Idle: screen hash stable 60s + prompt visible -> idle
  4. Stuck fallback: screen frozen 120s + prompt NOT visible -> send "1"
  5. Auto-exit: idle + not attached + auto_exit enabled -> kill

No probe mechanism.  No characters sent except auto-confirm responses
and stuck fallback.  All tool differences handled via TOML config patterns.

Auxiliary screen signals (optional, from TOML config):
  busy_pattern: "ing…" style → definitely busy, skip confirm, reset idle
  done_pattern: "ed for Xs" style → task done, fast-track idle with prompt

Logging levels:
  DEBUG  - every cycle decision (hash, idle_for, why skipped, screen tail)
  INFO   - state changes, auto-confirm, idle, auto-exit, stuck
  WARNING- stuck fallback, unexpected situations
  ERROR  - crashes, session errors
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
from camc_pkg.detection import (
    detect_state, should_auto_confirm, detect_completion, is_ready_for_input,
)


def _screen_tail(output, n=3):
    """Last n non-empty lines of screen output, for logging."""
    lines = [l.strip() for l in output.rstrip("\n").split("\n") if l.strip()]
    return " | ".join(lines[-n:]) if lines else "(empty)"


def run_monitor_loop(session, agent_id, config, store, pid_path=None, events=None):
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
    idle_confirmed = False
    cycle = 0

    def _event(etype, detail=None):
        if events:
            events.append(agent_id, etype, detail)

    _event("monitor_start")
    log.info("Config: confirm_cooldown=%.1f confirm_sleep=%.1f health_check=%.1f",
             config.confirm_cooldown, config.confirm_sleep, config.health_check_interval)

    try:
        while running[0]:
            now = time.time()
            cycle += 1

            # --- 1. Health check (every 15s) ---
            if now - last_health >= config.health_check_interval:
                last_health = now
                alive = tmux_session_exists(session)
                log.debug("[%d] Health check: session=%s alive=%s", cycle, session, alive)
                if not alive:
                    if prev_output:
                        done = detect_completion(prev_output, config)
                        status = "completed" if (done or has_worked) else "failed"
                    else:
                        status = "completed" if has_worked else "failed"
                    reason = "Session ended cleanly" if status == "completed" else "Session exited before agent started working"
                    log.info("Session gone -> %s (%s) [has_worked=%s]", status, reason, has_worked)
                    log.info("Last screen: %s", _screen_tail(prev_output, 5))
                    store.update(agent_id, status=status,
                                 exit_reason=reason, completed_at=_now_iso())
                    _event("completed", {"status": status, "reason": reason})
                    return

            # --- 2. Capture screen ---
            output = capture_tmux(session)
            if output.strip():
                prev_output = output

            # Hash: strip last line (status bar flicker) before hashing
            hash_input = output.rsplit("\n", 1)[0] if "\n" in output else output
            h = hashlib.md5(hash_input.encode()).hexdigest()[:8]
            changed = h != prev_hash
            prev_hash = h

            if not output.strip():
                log.debug("[%d] Empty screen, skipping", cycle)
                time.sleep(1)
                continue

            idle_for = now - last_change
            prompt_visible = is_ready_for_input(output, config)

            # --- 2b. Auxiliary screen signals (busy/done) ---
            tail_text = "\n".join(
                [l for l in output.rstrip("\n").split("\n") if l.strip()][-5:]
            )
            screen_busy = (config.busy_pattern and
                           bool(config.busy_pattern.search(tail_text)))
            screen_done = (config.done_pattern and
                           bool(config.done_pattern.search(tail_text)))

            if screen_busy:
                # Definitely working — reset idle, mark has_worked, skip confirm
                if not has_worked:
                    has_worked = True
                    log.info("Busy signal detected, has_worked=True")
                last_change = now
                idle_confirmed = False

            if screen_done and not has_worked:
                has_worked = True
                log.info("Done signal detected, has_worked=True")

            # Periodic debug summary (every 30 cycles ≈ 30s)
            if cycle % 30 == 0:
                log.debug("[%d] hash=%s changed=%s idle_for=%.0fs prompt=%s state=%s has_worked=%s idle_confirmed=%s",
                          cycle, h, changed, idle_for, prompt_visible, current_state, has_worked, idle_confirmed)
                log.debug("[%d] screen: %s", cycle, _screen_tail(output))

            # --- 3. Auto-confirm (cooldown-gated) ---
            # Skip when: busy signal (agent is working, not at a dialog),
            # or bare prompt (agent at input, confirm text is stale history).
            confirm_cd = now - last_confirm
            tail_lines = [l for l in output.rstrip("\n").split("\n") if l.strip()][-5:]
            bare_prompt = any(
                l.strip() in ("\u276f", ">", "\u203a")  # ❯  >  ›
                for l in tail_lines
            )
            skip_confirm = screen_busy or bare_prompt
            if confirm_cd >= config.confirm_cooldown and not skip_confirm:
                confirm = should_auto_confirm(output, config)
                if confirm:
                    response, send_enter, pat_str, matched = confirm
                    log.info("Auto-confirm: pattern=%r matched=%r -> %r (enter=%s)",
                             pat_str, matched, response, send_enter)
                    log.debug("[%d] Confirm screen: %s", cycle, _screen_tail(output, 5))
                    if response:
                        tmux_send_input(session, response, send_enter=send_enter)
                    elif send_enter:
                        tmux_send_key(session, "Enter")
                    last_confirm = now
                    last_change = now
                    idle_confirmed = False
                    has_worked = True
                    _event("auto_confirm", {"pattern": pat_str, "response": response})
                    time.sleep(config.confirm_sleep)
                    continue
            else:
                log.debug("[%d] Confirm cooldown (%.1fs remaining)", cycle, config.confirm_cooldown - confirm_cd)

            # --- 4. State detection ---
            ns = detect_state(output, config)
            if ns and ns != current_state:
                if ns != "initializing":
                    has_worked = True
                log.info("State: %s -> %s", current_state, ns)
                _event("state_change", {"from": current_state, "to": ns})
                current_state = ns
                store.update(agent_id, state=ns)

            # --- 5. Output change ---
            if changed:
                log.debug("[%d] Output changed (hash %s -> %s), reset idle timer", cycle, prev_hash, h)
                last_change = now
                idle_confirmed = False

            # --- 6. Idle detection (pure screen: stable hash + prompt visible) ---
            # Fast-track: done signal + bare prompt → idle after 5s (not 60s).
            # The done signal ("ed for Xs") is a definitive completion marker.
            idle_threshold = 5 if (screen_done and bare_prompt) else 60
            if has_worked and not idle_confirmed and idle_for >= idle_threshold:
                if prompt_visible:
                    idle_confirmed = True
                    if idle_threshold < 60:
                        log.info("Idle confirmed (done signal + prompt, %.0fs stable)", idle_for)
                    else:
                        log.info("Idle confirmed (screen stable %.0fs, prompt visible)", idle_for)
                    _event("idle_confirmed", {"idle_for": idle_for})
                    store.update(agent_id, state="idle")
                else:
                    log.debug("[%d] Idle candidate (%.0fs stable) but prompt not visible", cycle, idle_for)

            # --- 6b. Stuck fallback: screen frozen but prompt NOT visible ---
            stuck_threshold = getattr(config, "stuck_timeout", 120)
            if (has_worked and not idle_confirmed
                    and idle_for >= stuck_threshold
                    and not prompt_visible):
                log.warning("Stuck fallback (%.0fs frozen, prompt not visible) — sending '1'", idle_for)
                log.warning("Stuck screen: %s", _screen_tail(output, 5))
                tmux_send_input(session, "1", send_enter=False)
                last_change = now
                _event("stuck_fallback", {"idle_for": idle_for})

            # --- 7. Auto-exit ---
            if idle_confirmed:
                attached = tmux_is_attached(session)
                if attached:
                    log.debug("[%d] Idle but user attached, waiting", cycle)
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
                        log.info("Auto-exit: action=%s (idle_for=%.0fs)", exit_action, idle_for)
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
                    else:
                        log.debug("[%d] Idle but auto_exit disabled", cycle)

            time.sleep(1)
    finally:
        if pid_path:
            try:
                os.unlink(pid_path)
            except OSError:
                pass


def _run_monitor(agent_id):
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
    except OSError:
        pass
    log_path = os.path.join(LOGS_DIR, "monitor-%s.log" % agent_id)
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [monitor] %(levelname)s %(message)s",
        filename=log_path,
    )
    from camc_pkg import __version__, __build__
    log.info("Monitor starting for agent %s (pid=%d) camc=%s (%s)",
             agent_id, os.getpid(), __version__, __build__ or "dev")

    store = AgentStore()
    events = EventStore()
    agent = store.get(agent_id)
    if not agent:
        log.error("Agent %s not found", agent_id)
        sys.exit(1)
    _t = agent.get("task")
    _tool = _t.get("tool", "claude") if isinstance(_t, dict) else agent.get("tool", "claude")
    _session = agent.get("tmux_session") or agent.get("session", "")
    _path = agent.get("context_path") or agent.get("path", "")
    config = _load_config(_tool)
    log.info("Tool=%s session=%s path=%s", _tool, _session, _path)
    log.info("Confirm rules: %s", [(p.pattern, r, e) for p, r, e in config.confirm_rules])
    from camc_pkg import PIDS_DIR
    try:
        os.makedirs(PIDS_DIR, exist_ok=True)
    except OSError:
        pass
    pid_path = os.path.join(PIDS_DIR, "%s.pid" % agent_id)

    max_restarts = 5
    for attempt in range(max_restarts + 1):
        try:
            run_monitor_loop(_session, agent_id, config, store, pid_path=pid_path, events=events)
            break
        except Exception as e:
            log.error("Monitor crashed (attempt %d/%d): %s", attempt + 1, max_restarts, e, exc_info=True)
            if attempt >= max_restarts:
                log.error("Max restarts reached, giving up")
                store.update(agent_id, status="failed", exit_reason="Monitor crashed: %s" % e,
                             completed_at=_now_iso())
                break
            time.sleep(5 * (attempt + 1))
            agent = store.get(agent_id)
            if not agent or agent.get("status") != "running":
                log.info("Agent no longer running, stopping monitor")
                break
            log.info("Restarting monitor...")

    log.info("Monitor finished for agent %s", agent_id)
