"""Background monitor loop: auto-confirm, state detection, completion, auto-exit."""

import hashlib
import logging
import os
import signal
import sys
import time

from camc_pkg import LOGS_DIR, log
from camc_pkg.utils import _now_iso
from camc_pkg.adapters import _load_config
from camc_pkg.storage import AgentStore
from camc_pkg.transport import (
    capture_tmux, tmux_session_exists, tmux_send_input, tmux_kill_session,
)
from camc_pkg.detection import detect_state, should_auto_confirm, detect_completion


def run_monitor_loop(session, agent_id, config, store, pid_path=None):
    """Monitor a tmux session: auto-confirm, state detection, completion, auto-exit."""
    if pid_path:
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))

    running = [True]
    signal.signal(signal.SIGTERM, lambda s, f: running.__setitem__(0, False))

    prev_hash = ""
    last_change = last_health = time.time()
    last_confirm = 0.0
    current_state = None
    has_worked = False
    empty_count = 0

    try:
        while running[0]:
            now = time.time()

            if now - last_health >= config.health_check_interval:
                last_health = now
                if not tmux_session_exists(session):
                    status = "completed" if has_worked else "failed"
                    log.info("Session gone: %s -> %s", session, status)
                    store.update(agent_id, status=status,
                                 exit_reason="Session exited", completed_at=_now_iso())
                    return

            output = capture_tmux(session)
            h = hashlib.md5(output.encode()).hexdigest()[:8]
            changed = h != prev_hash
            if changed:
                last_change = now
            prev_hash = h

            if not output.strip():
                empty_count += 1
                if empty_count >= config.empty_threshold and not tmux_session_exists(session):
                    status = "completed" if has_worked else "failed"
                    store.update(agent_id, status=status,
                                 exit_reason="Session exited", completed_at=_now_iso())
                    return
                time.sleep(1)
                continue
            empty_count = 0

            if now - last_confirm >= config.confirm_cooldown:
                confirm = should_auto_confirm(output, config)
                if confirm:
                    log.info("Auto-confirm: %r enter=%s", confirm[0], confirm[1])
                    tmux_send_input(session, confirm[0], send_enter=confirm[1])
                    last_confirm = now
                    time.sleep(config.confirm_sleep)
                    continue

            ns = detect_state(output, config)
            if ns and ns != current_state:
                if ns != "initializing":
                    has_worked = True
                log.info("State: %s -> %s", current_state, ns)
                current_state = ns
                store.update(agent_id, state=ns)

            if not changed and now - last_change >= config.completion_stable:
                done = detect_completion(output, config)
                if done:
                    has_worked = True  # completion signal = agent did work
                    store.update(agent_id, state="idle")
                    # Auto-exit when completion detected + output stable for 3x completion_stable
                    if now - last_change >= config.completion_stable * 3:
                        agent_rec = store.get(agent_id)
                        ae = agent_rec.get("auto_exit") if agent_rec else None
                        if ae is None:
                            ae = getattr(config, "auto_exit", False)
                        if ae:
                            exit_action = getattr(config, "exit_action", "kill_session")
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
    agent = store.get(agent_id)
    if not agent:
        log.error("Agent %s not found", agent_id)
        sys.exit(1)
    config = _load_config(agent["tool"])
    log.info("Tool=%s session=%s path=%s", agent["tool"], agent["session"], agent.get("path"))
    pid_path = "/tmp/camc-%s.pid" % agent_id

    # Auto-restart on crash (e.g. database locked, transient errors)
    max_restarts = 5
    for attempt in range(max_restarts + 1):
        try:
            run_monitor_loop(agent["session"], agent_id, config, store, pid_path=pid_path)
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
