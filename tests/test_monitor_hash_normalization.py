"""Focused tests for hash0 / hash1 screen-content fingerprints.

Covers the 2026-06-10 monitor change:
  * `_normalize_screen()` keeps printable ASCII + CJK; strips ANSI,
    control chars, emoji, decorative noise; trims the trailing
    status-bar line.
  * `_strip_ascii_digits()` removes ASCII digit runs (`\\d+`) but keeps
    CJK characters (including CJK numerals like 一/二/三).
  * `_content_hash()` is stable across calls.
  * The idle decision uses hash0 stability gated by
    `cfg.idle_stable_seconds` from TOML, with default 60 preserving
    prior behavior.
  * Numeric-only churn (timer-like content) changes hash0 every tick
    while hash1 stays stable (digits are stripped from hash1) — the
    agent must NOT be marked idle, because hash0 keeps resetting
    runtime.last_change.
  * AutoConfirmationFeature semantics unchanged.
"""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import monitor as _mon          # noqa: E402
from camc_pkg import monitor_features as _mf  # noqa: E402


# ---------------------------------------------------------------------------
# _normalize_screen
# ---------------------------------------------------------------------------

class TestNormalizeScreen:
    def test_keeps_printable_ascii(self):
        out = _mon._normalize_screen("hello world 123\nplain text\nfoot")
        # Trailing "foot" is the status bar — stripped.
        assert out == "hello world 123\nplain text"

    def test_strips_ansi_escapes(self):
        raw = "\x1b[31mred text\x1b[0m here\nsecond\nbar"
        out = _mon._normalize_screen(raw)
        assert "\x1b" not in out
        assert "red text here" in out

    def test_strips_control_chars_except_newline_and_tab(self):
        raw = "alpha\x07\x08beta\n\ttab kept\nbar"
        out = _mon._normalize_screen(raw)
        assert "\x07" not in out and "\x08" not in out
        assert "\t" in out
        assert "tab kept" in out

    def test_strips_emoji_and_decorative_unicode(self):
        raw = "rocket 🚀 dingbat ✓ box ╔══╗\nactual content\nbar"
        out = _mon._normalize_screen(raw)
        assert "🚀" not in out
        assert "✓" not in out
        assert "╔" not in out and "═" not in out and "╗" not in out
        assert "actual content" in out

    def test_keeps_chinese_cjk_characters(self):
        raw = "正在思考: 已用时 12 秒\ndone line\nbar"
        out = _mon._normalize_screen(raw)
        assert "正在思考" in out
        assert "已用时" in out
        assert "秒" in out

    def test_status_bar_trim_matches_legacy_behavior(self):
        # Two-line: hashes the first line; the second is dropped.
        out = _mon._normalize_screen("real content\nstatus_bar_flicker")
        assert out == "real content"

    def test_empty_text_is_empty(self):
        assert _mon._normalize_screen("") == ""
        assert _mon._normalize_screen(None) == ""


# ---------------------------------------------------------------------------
# _strip_ascii_digits
# ---------------------------------------------------------------------------

class TestStripAsciiDigits:
    def test_removes_ascii_digit_runs(self):
        assert _mon._strip_ascii_digits("elapsed 12:34") == "elapsed :"

    def test_keeps_cjk_numerals(self):
        # CJK digit characters are NOT in ASCII 0-9; they stay.
        assert _mon._strip_ascii_digits("一二三 12 步") == "一二三  步"

    def test_keeps_fullwidth_digits(self):
        # Fullwidth digits ０-９ (U+FF10..U+FF19) are Unicode decimal
        # digits and would be matched by Python's \\d. We must use
        # ASCII-only [0-9] so only Western 0-9 are stripped.
        # _normalize_screen() drops the fullwidth digits anyway (they
        # are not in the ASCII or CJK retain set, but FF10-FF19 IS
        # inside the Halfwidth+Fullwidth Forms block, so they DO
        # survive normalization). Check both layers in isolation:
        raw = "已用时 ２３ 秒"   # fullwidth 2,3
        # Direct strip: fullwidth digits must stay.
        assert _mon._strip_ascii_digits(raw) == raw
        # And via the full pipeline: hash1 only differs from hash0 if
        # an ASCII digit was removed. Fullwidth digits alone -> equal.
        norm = _mon._normalize_screen(raw)
        assert "２３" in norm, "fullwidth digits dropped during normalization"
        h0 = _mon._content_hash(norm)
        h1 = _mon._content_hash(_mon._strip_ascii_digits(norm))
        assert h0 == h1, "fullwidth digits were treated as ASCII digits"

    def test_keeps_arabic_indic_digits(self):
        # Arabic-Indic digits ٠-٩ (U+0660..U+0669) are Unicode decimal
        # digits but NOT ASCII. They are also outside our keep set
        # (no CJK), so _normalize_screen drops them — but at the
        # _strip_ascii_digits layer alone they MUST stay so a future
        # caller using a different normalizer is safe.
        raw = "elapsed ٣٢١ ticks"
        assert _mon._strip_ascii_digits(raw) == raw

    def test_no_digits_is_noop(self):
        assert _mon._strip_ascii_digits("no numbers here") == "no numbers here"


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_hash_is_stable_across_calls(self):
        text = "some normalized screen"
        assert _mon._content_hash(text) == _mon._content_hash(text)

    def test_different_text_yields_different_hash(self):
        assert _mon._content_hash("a") != _mon._content_hash("b")

    def test_hash_is_short_hex(self):
        h = _mon._content_hash("hello")
        assert re.match(r"^[0-9a-f]{8}$", h)


# ---------------------------------------------------------------------------
# Timer-like content: hash1 stable, hash0 changes
# ---------------------------------------------------------------------------

class TestTimerChurn:
    def test_elapsed_timer_changes_hash0_not_hash1(self):
        screens = [
            "Working: elapsed 12:01\nstatus_bar",
            "Working: elapsed 12:02\nstatus_bar",
            "Working: elapsed 12:03\nstatus_bar",
        ]
        norm = [_mon._normalize_screen(s) for s in screens]
        h0 = [_mon._content_hash(n) for n in norm]
        h1 = [_mon._content_hash(_mon._strip_ascii_digits(n)) for n in norm]
        # hash0 must change every cycle (timer ticks).
        assert h0[0] != h0[1] != h0[2]
        # hash1 must be identical across the three (numbers stripped).
        assert h1[0] == h1[1] == h1[2]

    def test_spinner_with_emoji_strip_and_chinese_status_stable_h0(self):
        # The spinner glyph is non-CJK / non-ASCII → stripped by hash0.
        # The Chinese content is the same across cycles → hash0 stable.
        screens = [
            "⠋ 正在生成代码... 13 秒\nstatus_bar",
            "⠙ 正在生成代码... 13 秒\nstatus_bar",
            "⠹ 正在生成代码... 13 秒\nstatus_bar",
        ]
        h0 = [_mon._content_hash(_mon._normalize_screen(s)) for s in screens]
        # Spinner glyphs are decoration → hash0 unchanged.
        assert h0[0] == h0[1] == h0[2]


# ---------------------------------------------------------------------------
# Idle rule uses cfg.idle_stable_seconds and hash0 stability
# ---------------------------------------------------------------------------

class _Cfg(object):
    def __init__(self, idle_stable_seconds=60.0):
        self.confirm_cooldown = 5.0
        self.confirm_sleep = 0.5
        self.confirm_rules = []
        self.busy_pattern = None
        self.done_pattern = None
        self.ready_pattern = None
        self.state_patterns = []
        self.recent_chars = 2000
        self.state_recent_chars = 2000
        self.state_strategy = "first"
        self.state_patterns = []
        self.strip_ansi = False
        self.confirm_recent_lines = 8
        self.idle_stable_seconds = float(idle_stable_seconds)


def _mk_snap(**kw):
    defaults = dict(
        output="", hash="x", prev_hash="x", changed=False,
        now=0.0, cycle=1, prompt_visible=True,
        screen_busy=False, screen_done=False, bare_prompt=False,
        tail_lines=[], idle_for=0.0,
        hash0="x", hash1="x", idle_for_hash1=0.0,
    )
    defaults.update(kw)
    return _mf.MonitorSnapshot(**defaults)


class TestIdleStableSeconds:
    def test_default_60s_threshold_preserves_prior_behavior(self):
        cfg = _Cfg()  # default 60.0
        rt = _mf.MonitorRuntime("aid", cfg, now=0.0)
        rt.has_worked = True
        feat = _mf.StateManagerFeature()
        # 59s stable: NOT idle yet (matches legacy 60s threshold).
        snap = _mk_snap(idle_for=59.0, prompt_visible=True)
        feat.after_confirm(snap, rt)
        assert rt.idle_confirmed is False
        # 60s stable: idle.
        snap2 = _mk_snap(idle_for=60.0, prompt_visible=True)
        feat.after_confirm(snap2, rt)
        assert rt.idle_confirmed is True

    def test_custom_idle_stable_seconds_from_toml(self):
        cfg = _Cfg(idle_stable_seconds=15.0)
        rt = _mf.MonitorRuntime("aid", cfg, now=0.0)
        rt.has_worked = True
        feat = _mf.StateManagerFeature()
        snap = _mk_snap(idle_for=14.0, prompt_visible=True)
        feat.after_confirm(snap, rt)
        assert rt.idle_confirmed is False
        snap2 = _mk_snap(idle_for=15.0, prompt_visible=True)
        feat.after_confirm(snap2, rt)
        assert rt.idle_confirmed is True

    def test_timer_churn_does_not_become_idle(self):
        """Driver-level: simulate three cycles where hash0 keeps changing
        because of timer ticks. runtime.last_change must advance each
        cycle, so idle_for never reaches the threshold."""
        cfg = _Cfg(idle_stable_seconds=10.0)
        rt = _mf.MonitorRuntime("aid", cfg, now=0.0)
        rt.has_worked = True
        rt.last_change = 0.0
        feat = _mf.StateManagerFeature()

        # Cycle 1: t=5, hash0 just changed from initial.
        snap1 = _mk_snap(
            output="Working: elapsed 12:01",
            hash0="h_a", changed=True,
            now=5.0, idle_for=5.0, prompt_visible=True,
        )
        feat.after_confirm(snap1, rt)
        # OutputChangeStep should have reset last_change.
        assert rt.last_change == 5.0
        assert rt.idle_confirmed is False

        # Cycle 2: t=11, hash0 changed again (timer tick). idle_for=6.
        snap2 = _mk_snap(
            output="Working: elapsed 12:02",
            hash0="h_b", changed=True,
            now=11.0, idle_for=6.0, prompt_visible=True,
        )
        feat.after_confirm(snap2, rt)
        assert rt.last_change == 11.0
        assert rt.idle_confirmed is False, \
            "timer churn must NOT trigger idle (hash0 keeps changing)"

        # Cycle 3: t=17, hash0 changed AGAIN. Still not idle.
        snap3 = _mk_snap(
            output="Working: elapsed 12:03",
            hash0="h_c", changed=True,
            now=17.0, idle_for=6.0, prompt_visible=True,
        )
        feat.after_confirm(snap3, rt)
        assert rt.idle_confirmed is False

    def test_stable_hash0_for_threshold_seconds_becomes_idle(self):
        cfg = _Cfg(idle_stable_seconds=10.0)
        rt = _mf.MonitorRuntime("aid", cfg, now=0.0)
        rt.has_worked = True
        rt.last_change = 0.0
        feat = _mf.StateManagerFeature()
        # First snap: changed=False, idle_for grows to 10.
        snap = _mk_snap(
            output="static content",
            hash0="h_same", changed=False,
            now=10.0, idle_for=10.0, prompt_visible=True,
        )
        feat.after_confirm(snap, rt)
        assert rt.idle_confirmed is True


# ---------------------------------------------------------------------------
# Snapshot exposes both hashes; AutoConfirmationFeature unchanged
# ---------------------------------------------------------------------------

class TestSnapshotShapeAndAutoConfirm:
    def test_snapshot_exposes_hash0_hash1_and_idle_for_hash1(self):
        snap = _mk_snap(hash0="aaa", hash1="bbb", idle_for_hash1=12.5)
        assert snap.hash0 == "aaa"
        assert snap.hash1 == "bbb"
        assert snap.idle_for_hash1 == 12.5

    def test_runtime_initializes_last_change_hash1(self):
        cfg = _Cfg()
        rt = _mf.MonitorRuntime("aid", cfg, now=42.0)
        assert rt.last_change_hash1 == 42.0
        assert rt.prev_hash1 == ""

    def test_left_initializing_latches_on_first_busy_and_blocks_fallback(self):
        """Addendum: once the screen has been busy at least once, the
        state detector must refuse to fall back to 'initializing'."""
        # Build a config that maps a specific marker to 'initializing'.
        # Use the real AdapterConfig path so detect_state() sees the
        # state patterns we declare.
        from camc_pkg.adapters import AdapterConfig
        cfg = AdapterConfig({
            "adapter": {"name": "x", "display_name": "X"},
            "state": {
                "strategy": "first",
                "recent_chars": 2000,
                "patterns": [
                    {"state": "initializing", "pattern": r"INIT_MARKER"},
                    {"state": "editing",      "pattern": r"EDIT_MARKER"},
                ],
            },
            "monitor": {},
        })
        rt = _mf.MonitorRuntime("aid", cfg, now=0.0)
        feat = _mf.StateManagerFeature()

        # Cycle 1: screen says INIT_MARKER. left_initializing still
        # False → transition to initializing is allowed.
        snap1 = _mk_snap(output="INIT_MARKER", screen_busy=False, now=1.0)
        feat.after_confirm(snap1, rt)
        assert rt.current_state == "initializing"
        assert rt.left_initializing is False

        # Cycle 2: screen goes busy → latch fires.
        snap2 = _mk_snap(output="ignored", screen_busy=True, now=2.0)
        feat.before_confirm(snap2, rt)
        assert rt.left_initializing is True

        # Cycle 3: detect_state() would say "initializing" again, but
        # the latch must SUPPRESS the fallback — state stays whatever
        # it last was (here: still 'initializing' from cycle 1, but
        # crucially no new store_update / event is emitted).
        rt.current_state = "editing"  # pretend we'd moved to editing
        snap3 = _mk_snap(output="INIT_MARKER", screen_busy=False, now=3.0)
        actions = feat.after_confirm(snap3, rt)
        kinds = [a.get("kind") for a in actions]
        # No state_change event, no store_update for state.
        assert not any(
            a.get("kind") == "store_update"
            and "state" in (a.get("fields") or {})
            for a in actions
        )
        # current_state must NOT have been clobbered back to initializing.
        assert rt.current_state == "editing"

    def test_auto_confirm_unchanged_by_normalization_changes(self):
        # A matching [[confirm]] rule must still fire — the new hash
        # plumbing must not touch auto-confirm semantics.
        confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
        cfg = _Cfg()
        cfg.confirm_rules = confirm_rules
        rt = _mf.MonitorRuntime("aid", cfg, now=100.0)
        rt.last_confirm = 0.0
        feat = _mf.AutoConfirmationFeature()
        snap = _mk_snap(
            # Trailing ❯ so the global input-cursor guard allows the rule.
            output="Do you want to proceed?\n1. Yes\n2. No\n❯ \n",
            now=100.0,
        )
        kinds = [a["kind"] for a in feat.confirm(snap, rt)]
        assert "send_input" in kinds
        assert "halt_cycle" in kinds
