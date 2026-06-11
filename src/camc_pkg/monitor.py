"""Background monitor loop (~1s poll cycle).

Pure screen-based, tool-agnostic design:
  1. Capture screen -> detect state/confirm/idle
  2. Auto-confirm: TOML [[confirm]] rule match -> send response
  3. Idle: screen hash stable 60s + prompt visible -> idle
  4. Auto-exit: idle + not attached + auto_exit enabled -> kill

No probe mechanism. The only characters the monitor sends are
auto-confirm responses driven by adapter TOML rules. The previous
"stuck fallback" that injected a bare "1" when the screen froze with
no prompt visible was removed on 2026-06-10 — see
``docs/legacy/monitor-auto-confirm-v1.md`` for the archived rationale.

Auxiliary screen signals (optional, from TOML config):
  busy_pattern: "ing…" style → definitely busy, reset idle
  done_pattern: "ed for Xs" style → task done, fast-track idle with prompt

Logging levels:
  DEBUG  - every cycle decision (hash, idle_for, why skipped, screen tail)
  INFO   - state changes, auto-confirm, idle, auto-exit
  WARNING- unexpected situations
  ERROR  - crashes, session errors

Refactor note: the per-tick logic lives in ``camc_pkg.monitor_features``
as a coarse-grained 3-phase pipeline (before_confirm → confirm →
after_confirm). This loop captures the screen, builds a MonitorSnapshot,
calls each registered+enabled feature's phase hooks in order, and
applies the returned action dicts (send_input, send_key, store_update,
event, log, halt_cycle). A halt_cycle from the confirm phase skips the
after_confirm phase entirely. Health-check and auto-exit stay inline.
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
from camc_pkg.detection import detect_completion, is_ready_for_input
from camc_pkg.monitor_features import (
    MonitorSnapshot, MonitorRuntime, build_features,
)


def _screen_tail(output, n=3):
    """Last n non-empty lines of screen output, for logging."""
    lines = [l.strip() for l in output.rstrip("\n").split("\n") if l.strip()]
    return " | ".join(lines[-n:]) if lines else "(empty)"


# ANSI CSI (`ESC[...letter`) + OSC (`ESC]...BEL`). Sequences arriving with
# stray bytes will fall through and be stripped by the printable-ASCII /
# CJK pass below — this regex covers the structured cases only.
import re as _re
_ANSI_RE = _re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
# [0-9]+ — strictly ASCII. We MUST NOT use ``\d`` here: Python's
# ``\d`` is Unicode-aware and would also strip e.g. fullwidth digits
# (０-９, U+FF10..U+FF19), Arabic-Indic digits, and other decimal
# digit codepoints. The user spec keeps non-ASCII characters
# (Chinese/CJK and anything else printable) in hash1; only the
# Western 0-9 run gets removed.
_DIGITS_RE = _re.compile(r"[0-9]+")


def _normalize_screen(text):
    """Return a stable, content-only view of a tmux capture.

    Keeps only printable ASCII (space..tilde) and CJK characters
    (CJK Unified Ideographs / Extension A, CJK Symbols and
    Punctuation, halfwidth+fullwidth forms). Preserves ``\\n`` and
    ``\\t`` so line / column structure survives for hash comparison.

    Strips:
      * the trailing status-bar line (Claude's `? for shortcuts` ↔
        `esc to interrupt` flicker — same trim the legacy hash did);
      * ANSI CSI/OSC escape sequences;
      * everything else (control chars, emoji, box-drawing, dingbats,
        sparklines, all other Unicode blocks).

    Python 3.6+, stdlib only. UTF-8 throughout.
    """
    if not text:
        return ""
    body = text.rsplit("\n", 1)[0] if "\n" in text else text
    body = _ANSI_RE.sub("", body)
    out = []
    append = out.append
    for ch in body:
        if ch == "\n" or ch == "\t":
            append(ch)
            continue
        cp = ord(ch)
        if 0x20 <= cp <= 0x7e:
            append(ch)
            continue
        if (0x4e00 <= cp <= 0x9fff or       # CJK Unified Ideographs
            0x3400 <= cp <= 0x4dbf or       # CJK Extension A
            0x3000 <= cp <= 0x303f or       # CJK Symbols and Punctuation
            0xff00 <= cp <= 0xffef):        # Halfwidth + Fullwidth Forms
            append(ch)
    return "".join(out)


def _strip_ascii_digits(text):
    """Remove ASCII digit runs. CJK digits (一二三 / 零) are untouched
    because they are not in 0-9; they stay in the hash so genuine
    Chinese-language progress text is not flattened."""
    return _DIGITS_RE.sub("", text)


def _content_hash(text):
    """Short stable MD5 hex digest used as a screen-content fingerprint."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def _apply_action(action, *, session, agent_id, store, events_fn):
    """Apply a single step action dict to the real world. Returns
    (halt_cycle, sleep_seconds). halt_cycle=True means the loop must
    stop running further steps in this cycle and skip the inline
    stuck-fallback / auto-exit blocks (matching the original
    ``time.sleep(confirm_sleep); continue`` semantics)."""
    kind = action.get("kind")
    if kind == "log":
        getattr(log, action.get("level", "debug"))(action.get("msg", ""))
    elif kind == "send_input":
        tmux_send_input(session, action["text"],
                        send_enter=action.get("send_enter", True))
    elif kind == "send_key":
        tmux_send_key(session, action["key"])
    elif kind == "store_update":
        store.update(agent_id, **action.get("fields", {}))
    elif kind == "event":
        events_fn(action["name"], action.get("detail"))
    elif kind == "halt_cycle":
        return True, float(action.get("sleep", 0) or 0)
    return False, 0.0


def run_monitor_loop(session, agent_id, config, store, pid_path=None, events=None):
    if pid_path:
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))

    running = [True]
    signal.signal(signal.SIGTERM, lambda s, f: running.__setitem__(0, False))

    def _event(etype, detail=None):
        if events:
            events.append(agent_id, etype, detail)

    _event("monitor_start")
    log.info("Config: confirm_cooldown=%.1f confirm_sleep=%.1f health_check=%.1f",
             config.confirm_cooldown, config.confirm_sleep, config.health_check_interval)

    # Build the per-tick feature pipeline once. Stuck-fallback /
    # auto-exit / health-check are NOT in the pipeline yet — they
    # remain inline below per the v1 scope. Disabled features
    # (currently MailboxFeature + CronFeature) are still in the list
    # so test code can introspect them, but the driver skips them.
    runtime = MonitorRuntime(agent_id, config, now=time.time())
    runtime.last_health = runtime.last_change
    features = build_features()
    prev_output = ""

    def _run_phase(snap, phase_name):
        """Apply the given phase hook on every enabled feature, in
        ``order`` ascending. Returns (halted, sleep_seconds) — halted
        signals the driver to skip remaining phases for this cycle."""
        for feat in features:
            if not feat.enabled:
                continue
            hook = getattr(feat, phase_name, None)
            if hook is None:
                continue
            for action in hook(snap, runtime):
                halt, slp = _apply_action(
                    action,
                    session=session, agent_id=agent_id,
                    store=store, events_fn=_event,
                )
                if halt:
                    return True, slp
        return False, 0.0

    try:
        while running[0]:
            now = time.time()
            runtime.cycle += 1
            cycle = runtime.cycle

            # --- 1. Health check (every 15s) ---
            if now - runtime.last_health >= config.health_check_interval:
                runtime.last_health = now
                alive = tmux_session_exists(session)
                log.debug("[%d] Health check: session=%s alive=%s", cycle, session, alive)
                if not alive:
                    # Retry to avoid transient false positives (tmux server hiccup,
                    # heavy load, etc.).  A single failed check previously caused
                    # agents to be incorrectly marked completed.
                    confirmed_dead = True
                    for retry in range(3):
                        time.sleep(2)
                        if tmux_session_exists(session):
                            log.info("Session reappeared on retry %d — false alarm", retry + 1)
                            confirmed_dead = False
                            break
                    if not confirmed_dead:
                        continue
                    if prev_output:
                        done = detect_completion(prev_output, config)
                        status = "completed" if (done or runtime.has_worked) else "failed"
                    else:
                        status = "completed" if runtime.has_worked else "failed"
                    reason = "Session ended cleanly" if status == "completed" else "Session exited before agent started working"
                    log.info("Session gone (confirmed after retries) -> %s (%s) [has_worked=%s]",
                             status, reason, runtime.has_worked)
                    log.info("Last screen: %s", _screen_tail(prev_output, 5))
                    store.update(agent_id, status=status,
                                 exit_reason=reason, completed_at=_now_iso())
                    _event("completed", {"status": status, "reason": reason})
                    return

            # --- 2. Capture screen ---
            output = capture_tmux(session)
            if output.strip():
                prev_output = output

            # Content hashes:
            #   hash0 = normalized screen (printable ASCII + CJK,
            #           status-bar line stripped, ANSI/control stripped).
            #           This is the canonical idle signal.
            #   hash1 = hash0 with ASCII digits removed. If hash1 is
            #           stable while hash0 keeps changing, only numbers
            #           are churning (timer / progress / spinner): the
            #           agent is still working, NOT idle. Used as a
            #           diagnostic and a guard against numeric churn
            #           starving idle detection.
            normalized = _normalize_screen(output)
            h0 = _content_hash(normalized)
            h1 = _content_hash(_strip_ascii_digits(normalized))
            prev_h_for_log = runtime.prev_hash
            changed = h0 != runtime.prev_hash
            runtime.prev_hash = h0
            # Per-tick hash1 churn tracking (separate from hash0's
            # runtime.last_change which drives the idle decision).
            if h1 != getattr(runtime, "prev_hash1", ""):
                runtime.last_change_hash1 = now
            runtime.prev_hash1 = h1
            idle_for_hash1 = now - getattr(
                runtime, "last_change_hash1", now)

            if not output.strip():
                log.debug("[%d] Empty screen, skipping", cycle)
                time.sleep(1)
                continue

            # Compute auxiliary screen signals + tail_lines for the snapshot.
            tail_lines = [l for l in output.rstrip("\n").split("\n") if l.strip()][-5:]
            tail_text = "\n".join(tail_lines)
            screen_busy = (config.busy_pattern and
                           bool(config.busy_pattern.search(tail_text)))
            screen_done = (config.done_pattern and
                           bool(config.done_pattern.search(tail_text)))
            bare_prompt = any(
                l.strip() in ("❯", ">", "›")  # ❯  >  ›
                for l in tail_lines
            )
            prompt_visible = is_ready_for_input(output, config)
            idle_for = now - runtime.last_change

            snap = MonitorSnapshot(
                output=output, hash=h0, prev_hash=prev_h_for_log,
                changed=changed, now=now, cycle=cycle,
                prompt_visible=prompt_visible,
                screen_busy=bool(screen_busy),
                screen_done=bool(screen_done),
                bare_prompt=bare_prompt,
                tail_lines=tail_lines, idle_for=idle_for,
                hash0=h0, hash1=h1,
                idle_for_hash1=idle_for_hash1,
            )

            # Periodic debug summary (every 30 cycles ≈ 30s)
            if cycle % 30 == 0:
                log.debug(
                    "[%d] h0=%s h1=%s changed=%s idle_for=%.0fs "
                    "idle_for_h1=%.0fs prompt=%s state=%s "
                    "has_worked=%s idle_confirmed=%s",
                    cycle, h0, h1, changed, idle_for, idle_for_hash1,
                    prompt_visible, runtime.current_state,
                    runtime.has_worked, runtime.idle_confirmed)
                log.debug("[%d] screen: %s", cycle, _screen_tail(output))

            # --- Feature pipeline: 3 phases per cycle ---
            # Phase A (before_confirm): busy/done signals.
            # Phase B (confirm):        auto-confirm rules. May halt.
            # Phase C (after_confirm):  state detect, output change, idle.
            # A halt from any earlier phase skips later phases for this
            # cycle, matching the legacy `time.sleep(...); continue`.
            halted, halt_sleep = _run_phase(snap, "before_confirm")
            if not halted:
                halted, halt_sleep = _run_phase(snap, "confirm")
            if not halted:
                halted, halt_sleep = _run_phase(snap, "after_confirm")
            if halted:
                if halt_sleep > 0:
                    time.sleep(halt_sleep)
                continue

            # --- 6b. (Removed 2026-06-10) Legacy stuck fallback ---
            # The previous code sent a hardcoded "1" into the pane when
            # the screen had been frozen for >=120s and no prompt was
            # visible. That nudge was not driven by any TOML rule and
            # has been removed as part of the TOML-only auto-confirm
            # simplification. See docs/legacy/monitor-auto-confirm-v1.md.
            # Tools that need a nudge in this state should declare it
            # as a TOML [[confirm]] rule instead.

            # --- 7. Auto-exit ---
            if runtime.idle_confirmed:
                attached = tmux_is_attached(session)
                if attached:
                    log.debug("[%d] Idle but user attached, waiting", cycle)
                else:
                    agent_rec = store.get(agent_id)
                    ae = None
                    aee = False  # auto_exit_enable — safety arm, default False
                    if agent_rec:
                        t = agent_rec.get("task")
                        if isinstance(t, dict):
                            ae = t.get("auto_exit")
                            aee = bool(t.get("auto_exit_enable", False))
                        else:
                            ae = agent_rec.get("auto_exit")
                            aee = bool(agent_rec.get("auto_exit_enable", False))
                    if ae is None:
                        ae = getattr(config, "auto_exit", False)
                    # Two-key safety: auto_exit alone is treated as a no-op
                    # unless the agent record also has auto_exit_enable=True.
                    # The idle detector is heuristic (prompt visible + hash
                    # stable 60s); "thinking for a minute" and "done" look
                    # identical on screen, and the false-positive cost is
                    # lost work. The arming flag is intentionally hidden
                    # (only power-users who understand the trade-off should
                    # reach for it) — keep this log at DEBUG so we don't
                    # advertise it.
                    if ae and not aee:
                        log.debug("[%d] idle + auto_exit set, but not armed", cycle)
                        time.sleep(1)
                        continue
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
