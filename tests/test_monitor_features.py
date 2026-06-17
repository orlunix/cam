"""Focused tests for the feature pipeline v1.

Behavior-preserving refactor: production behavior is also covered by
tests/test_monitor_loop.py; these tests pin the pipeline SHAPE so a
future contributor can't quietly rename, re-order, drop, or enable a
feature without flagging the change.
"""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import monitor_features as mf  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal stand-ins (no monitor.py / no tmux / no store).
# ---------------------------------------------------------------------------

class _Cfg(object):
    def __init__(self, confirm_cooldown=5.0, confirm_sleep=0.5,
                 confirm_rules=None, busy_pattern=None, done_pattern=None,
                 ready_pattern=None, state_patterns=None,
                 state_strategy="last", state_recent_chars=2000,
                 strip_ansi=False, confirm_recent_lines=8):
        self.confirm_cooldown = confirm_cooldown
        self.confirm_sleep = confirm_sleep
        self.confirm_rules = confirm_rules or []
        self.busy_pattern = busy_pattern
        self.done_pattern = done_pattern
        self.ready_pattern = ready_pattern
        self.state_patterns = state_patterns or []
        self.state_strategy = state_strategy
        self.state_recent_chars = state_recent_chars
        self.strip_ansi = strip_ansi
        self.confirm_recent_lines = confirm_recent_lines


def _mk_snap(output="hello\n❯ ", **overrides):
    tail_lines = [l for l in output.rstrip("\n").split("\n") if l.strip()][-5:]
    fields = dict(
        output=output, hash="aa", prev_hash="bb",
        changed=False, now=100.0, cycle=1,
        prompt_visible=True, screen_busy=False,
        screen_done=False, bare_prompt=False,
        tail_lines=tail_lines, idle_for=0.0,
    )
    fields.update(overrides)
    return mf.MonitorSnapshot(**fields)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

def test_registry_includes_state_manager_and_auto_confirmation_and_placeholders():
    """The default v1 set is StateManagerFeature(10) → BootPromptFeature(15) →
    AutoConfirmationFeature(20) → MailboxFeature(30, disabled) →
    CronFeature(40, disabled). build_features() returns DISABLED
    features too so the driver / introspection code can see them."""
    feats = mf.build_features()
    by_name = {f.name: f for f in feats}
    assert {"state_manager", "boot_prompt", "auto_confirm", "mailbox", "cron"} <= set(by_name)
    # Order ascending.
    names = [f.name for f in feats]
    assert names == ["state_manager", "boot_prompt", "auto_confirm", "mailbox", "cron"]
    assert [f.order for f in feats] == [10, 15, 20, 30, 40]


def test_placeholders_are_disabled_by_default():
    feats = {f.name: f for f in mf.build_features()}
    assert feats["mailbox"].enabled is False, \
        "MailboxFeature must ship disabled — no mailbox behavior in this slice"
    assert feats["cron"].enabled is False, \
        "CronFeature must ship disabled — no cron behavior in this slice"
    assert feats["state_manager"].enabled is True
    assert feats["auto_confirm"].enabled is True


def test_placeholder_phase_hooks_are_noops():
    """Even if a future change accidentally flips enabled=True on a
    placeholder, its phase hooks must remain empty until the placeholder
    is explicitly fleshed out. Right now the base-class default is `return
    []`; this test catches a future override that forgets to stay a
    no-op."""
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    snap = _mk_snap()
    for name in ("mailbox", "cron"):
        feat = next(f for f in mf.build_features() if f.name == name)
        assert feat.before_confirm(snap, runtime) == []
        assert feat.confirm(snap, runtime)        == []
        assert feat.after_confirm(snap, runtime)  == []


def test_build_features_enabled_dict_overrides():
    feats = mf.build_features(enabled={"mailbox": True, "auto_confirm": False})
    by_name = {f.name: f for f in feats}
    assert by_name["mailbox"].enabled is True
    assert by_name["auto_confirm"].enabled is False
    # Untouched names use the class default.
    assert by_name["state_manager"].enabled is True
    assert by_name["cron"].enabled is False


def test_build_features_allow_list_only_enables_named():
    feats = mf.build_features(enabled=["state_manager"])
    by_name = {f.name: f for f in feats}
    assert by_name["state_manager"].enabled is True
    assert by_name["auto_confirm"].enabled is False
    assert by_name["mailbox"].enabled is False
    assert by_name["cron"].enabled is False


def test_register_feature_is_idempotent_for_repeat_calls():
    before = len(mf.registered_features())
    mf.register_feature(mf.AutoConfirmationFeature)
    after = len(mf.registered_features())
    assert before == after


# ---------------------------------------------------------------------------
# Per-feature state persists across ticks
# ---------------------------------------------------------------------------

def test_feature_state_persists_across_ticks_via_get_state():
    class _Counter(mf.MonitorFeature):
        name = "counter"
        order = 99
        def init_state(self):
            return {"hits": 0}
        def before_confirm(self, snap, runtime):
            st = self.get_state(runtime)
            st["hits"] += 1
            return []

    runtime = mf.MonitorRuntime("aid", _Cfg(), now=0.0)
    feat = _Counter()
    snap = _mk_snap()
    feat.before_confirm(snap, runtime)
    feat.before_confirm(snap, runtime)
    feat.before_confirm(snap, runtime)
    assert runtime.feature_state["counter"]["hits"] == 3
    # Back-compat alias still points at the same dict.
    assert runtime.step_state is runtime.feature_state


# ---------------------------------------------------------------------------
# StateManagerFeature internals — busy/done, state change, output change, idle
# ---------------------------------------------------------------------------

def test_state_manager_before_confirm_busy_signal_resets_idle_and_marks_has_worked():
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    runtime.idle_confirmed = True
    runtime.last_change = 0.0
    feat = mf.StateManagerFeature()
    snap = _mk_snap(screen_busy=True, now=100.0)
    actions = feat.before_confirm(snap, runtime)
    assert runtime.has_worked is True
    assert runtime.idle_confirmed is False
    assert runtime.last_change == 100.0
    assert any(a["kind"] == "log" and "Busy signal" in a["msg"] for a in actions)


def test_state_manager_before_confirm_done_marks_has_worked_only():
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    runtime.last_change = 5.0
    feat = mf.StateManagerFeature()
    snap = _mk_snap(screen_done=True, screen_busy=False, now=999.0)
    actions = feat.before_confirm(snap, runtime)
    assert runtime.has_worked is True
    # done alone must NOT touch last_change.
    assert runtime.last_change == 5.0
    assert any(a["kind"] == "log" and "Done signal" in a["msg"] for a in actions)


def test_state_manager_after_confirm_state_change_emits_event_and_store_update(monkeypatch):
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    feat = mf.StateManagerFeature()
    # Stub detect_state to drive a transition.
    monkeypatch.setattr("camc_pkg.detection.detect_state",
                        lambda output, c: "editing")
    snap = _mk_snap()
    actions = feat.after_confirm(snap, runtime)
    kinds = [a["kind"] for a in actions]
    assert "event" in kinds
    assert "store_update" in kinds
    assert runtime.current_state == "editing"
    assert runtime.has_worked is True   # transition to non-'initializing'
    upd = next(a for a in actions if a["kind"] == "store_update")
    assert upd["fields"] == {"state": "editing"}


def test_state_manager_after_confirm_output_change_resets_last_change(monkeypatch):
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    runtime.last_change = 0.0
    runtime.idle_confirmed = True
    feat = mf.StateManagerFeature()
    monkeypatch.setattr("camc_pkg.detection.detect_state", lambda o, c: None)
    snap = _mk_snap(changed=True, now=42.0,
                    hash="ab12", prev_hash="cd34")
    actions = feat.after_confirm(snap, runtime)
    assert runtime.last_change == 42.0
    assert runtime.idle_confirmed is False
    assert any(a["kind"] == "log" and "Output changed" in a["msg"]
               and "ab12" in a["msg"] and "cd34" in a["msg"]
               for a in actions)


def test_state_manager_after_confirm_idle_detect_requires_stable_ticks(monkeypatch):
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    runtime.has_worked = True
    feat = mf.StateManagerFeature()
    monkeypatch.setattr("camc_pkg.detection.detect_state", lambda o, c: None)

    # 10s stable → below 60s threshold → no idle fire.
    snap = _mk_snap(idle_for=10.0, prompt_visible=True)
    actions = feat.after_confirm(snap, runtime)
    assert all(a["kind"] != "store_update" for a in actions)
    assert runtime.idle_confirmed is False

    # 60s stable + prompt visible → idle fires.
    snap = _mk_snap(idle_for=60.0, prompt_visible=True)
    actions = feat.after_confirm(snap, runtime)
    upd = [a for a in actions if a["kind"] == "store_update"]
    assert upd and upd[0]["fields"] == {"state": "idle"}
    assert runtime.idle_confirmed is True


def test_state_manager_after_confirm_fast_track_idle_threshold(monkeypatch):
    """screen_done AND bare_prompt → threshold drops from 60s to 5s."""
    cfg = _Cfg()
    runtime = mf.MonitorRuntime("aid", cfg, now=0.0)
    runtime.has_worked = True
    feat = mf.StateManagerFeature()
    monkeypatch.setattr("camc_pkg.detection.detect_state", lambda o, c: None)
    snap = _mk_snap(idle_for=5.0, prompt_visible=True,
                    screen_done=True, bare_prompt=True)
    actions = feat.after_confirm(snap, runtime)
    assert runtime.idle_confirmed is True
    assert any(a["kind"] == "store_update" for a in actions)


# ---------------------------------------------------------------------------
# AutoConfirmationFeature — cooldown, 1-spam guard, send_input, halt
# ---------------------------------------------------------------------------

def test_auto_confirmation_returns_send_input_and_halt_cycle():
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=1000.0)
    runtime.last_confirm = 0.0
    feat = mf.AutoConfirmationFeature()
    snap = _mk_snap(
        output="Do you want to proceed?\n1. Yes\n2. No\n",
        now=1000.0, prompt_visible=False, bare_prompt=False,
    )
    actions = feat.confirm(snap, runtime)
    kinds = [a["kind"] for a in actions]
    assert "send_input" in kinds
    assert "event" in kinds
    assert "halt_cycle" in kinds
    # Ordering: send_input must precede event + halt_cycle.
    si = kinds.index("send_input")
    ev = kinds.index("event")
    ht = kinds.index("halt_cycle")
    assert si < ev < ht
    assert runtime.last_confirm == 1000.0
    assert runtime.last_change == 1000.0
    assert runtime.has_worked is True
    assert runtime.idle_confirmed is False
    halt = next(a for a in actions if a["kind"] == "halt_cycle")
    assert halt["sleep"] == pytest.approx(cfg.confirm_sleep)


def test_auto_confirmation_cooldown_window_suppresses_fire():
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_cooldown=10.0, confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=100.0)
    runtime.last_confirm = 95.0   # only 5s elapsed
    feat = mf.AutoConfirmationFeature()
    snap = _mk_snap(output="Do you want to proceed?\n1. Yes\n", now=100.0)
    kinds = [a["kind"] for a in feat.confirm(snap, runtime)]
    assert "send_input" not in kinds
    assert "halt_cycle" not in kinds
    assert runtime.last_confirm == 95.0


def test_auto_confirmation_no_1_spam_guard_python_side():
    """v2 is TOML-only: a screen full of '1' characters MUST NOT
    suppress a matching TOML [[confirm]] rule. The legacy 1-spam guard
    was archived on 2026-06-10 (see docs/legacy/monitor-auto-confirm-v1.md).
    If a user needs to suppress in that screen state, the TOML rule
    must encode it."""
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_cooldown=5.0, confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=200.0)
    runtime.last_confirm = 0.0
    feat = mf.AutoConfirmationFeature()
    snap = _mk_snap(
        output="1111111\n❯ Do you want to proceed?\n1. Yes\n",
        tail_lines=["1111111", "❯ Do you want to proceed?", "1. Yes"],
        now=200.0,
    )
    actions = feat.confirm(snap, runtime)
    kinds = [a["kind"] for a in actions]
    # TOML rule fired; no Python-side suppression.
    assert "send_input" in kinds
    assert "halt_cycle" in kinds
    halt = next(a for a in actions if a["kind"] == "halt_cycle")
    assert halt["sleep"] == pytest.approx(cfg.confirm_sleep)
    # last_confirm advanced to NOW (normal fire), not now + (60-cooldown).
    assert runtime.last_confirm == 200.0


def test_auto_confirmation_bare_input_cursor_blocks_python_side():
    """Bare input cursor at the bottom blocks confirm — keystrokes would
    land in the user's input box instead of the menu."""
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=500.0)
    runtime.last_confirm = 0.0
    feat = mf.AutoConfirmationFeature()
    snap = _mk_snap(output="\n1. Yes\n❯ ", bare_prompt=True, now=500.0)
    kinds = [a["kind"] for a in feat.confirm(snap, runtime)]
    assert "send_input" not in kinds
    assert "halt_cycle" not in kinds


# ---------------------------------------------------------------------------
# Driver-level halt semantics — phase B halt skips phase C entirely
# ---------------------------------------------------------------------------

def _drive_three_phases(features, snap, runtime):
    """Mimics monitor.py's loop: run before_confirm → confirm →
    after_confirm phases, breaking on halt_cycle. Returns
    (applied_actions_with_phase_tags, halted_bool, phases_actually_run)."""
    applied = []
    phases_run = []
    halted = False
    for phase in ("before_confirm", "confirm", "after_confirm"):
        phases_run.append(phase)
        for feat in features:
            if not feat.enabled:
                continue
            hook = getattr(feat, phase)
            for action in hook(snap, runtime):
                applied.append((phase, feat.name, action))
                if action.get("kind") == "halt_cycle":
                    halted = True
                    break
            if halted:
                break
        if halted:
            break
    return applied, halted, phases_run


def test_successful_confirm_with_1_spam_screen_still_halts_phase_c():
    """v2 TOML-only: even when the screen tail has '1' spam, a matching
    TOML rule still fires and the resulting halt skips after_confirm.
    last_change is set by AutoConfirmationFeature's successful fire
    path (not by the legacy 1-spam suppression which used to halt with
    sleep=0)."""
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_cooldown=5.0, confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=200.0)
    runtime.last_change = 50.0
    runtime.last_confirm = 0.0
    runtime.idle_confirmed = False
    features = mf.build_features()
    snap = _mk_snap(
        output="1111111\n❯ Do you want to proceed?\n1. Yes\n",
        tail_lines=["1111111", "❯ Do you want to proceed?", "1. Yes"],
        changed=True, now=200.0,
    )
    applied, halted, phases = _drive_three_phases(features, snap, runtime)
    assert halted
    assert "after_confirm" not in phases
    # Successful fire updates last_change to now (NOT preserved at 50.0
    # as in the archived v1 1-spam suppression path).
    assert runtime.last_change == 200.0


def test_successful_confirm_halt_also_skips_after_confirm():
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_cooldown=5.0, confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=300.0)
    runtime.last_change = 50.0
    runtime.last_confirm = 0.0
    runtime.idle_confirmed = True
    features = mf.build_features()
    snap = _mk_snap(
        output="Do you want to proceed?\n1. Yes\n2. No\n",
        changed=True, now=300.0,
    )
    applied, halted, phases = _drive_three_phases(features, snap, runtime)
    assert halted
    assert "after_confirm" not in phases
    # AutoConfirmationFeature itself sets last_change=now on a successful
    # fire; OutputChangeStep would have done the same — both paths yield
    # the same answer, but we ASSERT after_confirm did not run via the
    # phases_run check above.
    assert runtime.last_change == 300.0


def test_no_halt_runs_all_three_phases():
    """If neither auto-confirm nor 1-spam fires, phase C must run."""
    cfg = _Cfg()   # no confirm_rules → never matches → never halts
    runtime = mf.MonitorRuntime("aid", cfg, now=400.0)
    runtime.last_change = 0.0
    runtime.has_worked = True
    runtime.last_confirm = 0.0
    features = mf.build_features()
    snap = _mk_snap(changed=True, idle_for=120.0, prompt_visible=True,
                    now=400.0)
    _, halted, phases = _drive_three_phases(features, snap, runtime)
    assert halted is False
    assert phases == ["before_confirm", "confirm", "after_confirm"]
    # OutputChangeStep equivalent ran: last_change advanced.
    assert runtime.last_change == 400.0
    # IdleDetectStep equivalent ran: idle confirmed.
    assert runtime.idle_confirmed is True


# ---------------------------------------------------------------------------
# TOML-only invariant — monitor.py must not carry the legacy stuck-fallback
# (which injected a bare "1" into the pane when frozen with no prompt).
# This is a static check against the source so a future contributor can't
# reintroduce the Python-side nudge without renaming the symbol.
# ---------------------------------------------------------------------------

def test_monitor_module_has_no_stuck_fallback_send():
    monitor_path = os.path.join(SRC, "camc_pkg", "monitor.py")
    with open(monitor_path, "r") as f:
        src = f.read()
    # The legacy event name and the hardcoded '1' send must both be
    # absent from live code. We tolerate appearances inside the
    # comment block that documents the removal.
    code_lines = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        code_lines.append(line)
    body = "\n".join(code_lines)
    assert "stuck_fallback" not in body, \
        "stuck_fallback event must not be emitted (TOML-only auto-confirm)"
    assert 'tmux_send_input(session, "1"' not in body, \
        "hardcoded '1' send must not appear in monitor.py"


def test_auto_confirmation_fires_purely_from_toml_rule():
    """End-to-end pin: only the TOML rule decides. No bare_prompt, no
    1-spam, no busy/done overrides on the confirm path."""
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    cfg = _Cfg(confirm_rules=confirm_rules)
    runtime = mf.MonitorRuntime("aid", cfg, now=10.0)
    runtime.last_confirm = 0.0
    feat = mf.AutoConfirmationFeature()
    snap = _mk_snap(
        output="Do you want to proceed?\n1. Yes\n2. No\n",
        bare_prompt=True, screen_busy=True, screen_done=True,
        tail_lines=["1111", "❯", "1. Yes"],
        now=10.0,
    )
    kinds = [a["kind"] for a in feat.confirm(snap, runtime)]
    assert "send_input" in kinds
    assert "halt_cycle" in kinds


def test_auto_confirmation_no_rule_matches_no_action():
    """No TOML rule => no send_input, no halt. Empty rule set must be
    a clean no-op; no implicit Python-side fallback."""
    cfg = _Cfg(confirm_rules=[])
    runtime = mf.MonitorRuntime("aid", cfg, now=10.0)
    runtime.last_confirm = 0.0
    feat = mf.AutoConfirmationFeature()
    snap = _mk_snap(
        output="some random output with nothing matching",
        idle_for=300.0, prompt_visible=False, now=10.0,
    )
    actions = feat.confirm(snap, runtime)
    kinds = [a["kind"] for a in actions]
    assert "send_input" not in kinds
    assert "send_key" not in kinds
    assert "halt_cycle" not in kinds
