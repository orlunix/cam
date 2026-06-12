"""Focused tests for the generic selection-cursor strip in
``clean_for_confirm``.

The cleanup must let an author write a tool-agnostic TOML
``[[confirm]]`` regex (e.g. ``^1\\.\\s*Yes``) and still match real UI
screens where the option is prefixed by a selection cursor like
``❯``, ``›`` or ``>``.
"""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg.utils import (                              # noqa: E402
    clean_for_confirm,
    _strip_selection_cursor_before_numbered,
)
from camc_pkg.detection import should_auto_confirm        # noqa: E402


# ---------------------------------------------------------------------------
# Direct helper behavior
# ---------------------------------------------------------------------------

class TestStripSelectionCursor:
    @pytest.mark.parametrize("cursor", [u"❯", u"›", u">"])
    def test_strips_cursor_before_numbered_option(self, cursor):
        line = cursor + " 1. Yes"
        assert _strip_selection_cursor_before_numbered(line) == "1. Yes"

    def test_preserves_leading_indent(self):
        assert _strip_selection_cursor_before_numbered(u"    ❯ 1. Allow once") == \
            "    1. Allow once"

    def test_multiline_only_affects_cursor_lines(self):
        src = (
            u"Do you want to proceed?\n"
            u"❯ 1. Yes, allow\n"
            u"  2. No, deny\n"
            u"❯ write the code\n"            # prose — must NOT change
        )
        out = _strip_selection_cursor_before_numbered(src)
        assert "1. Yes, allow" in out
        assert "  2. No, deny" in out          # unchanged
        assert u"❯ write the code" in out      # negative — prose retained

    def test_does_not_touch_double_or_triple_gt(self):
        # `>>` (shell prompt) and `>>>` (REPL) must be left alone.
        for prefix in (">>", ">>>"):
            line = prefix + " 1. Yes"
            assert _strip_selection_cursor_before_numbered(line) == line

    def test_does_not_strip_when_no_numbered_option_follows(self):
        for line in (u"❯ write code", u"› proceed", "> hello world"):
            assert _strip_selection_cursor_before_numbered(line) == line

    def test_only_strips_cursor_and_following_whitespace_not_indent(self):
        # Cursor + multiple spaces -> all those spaces collapse, indent
        # preserved.
        assert _strip_selection_cursor_before_numbered(u"  ❯   1. Yes") == \
            "  1. Yes"


# ---------------------------------------------------------------------------
# clean_for_confirm integrates the strip
# ---------------------------------------------------------------------------

class TestCleanForConfirmIntegration:
    def test_includes_cursor_strip(self):
        screen = (
            u"╭─ menu ─╮\n"
            u"│ Do you want to proceed? │\n"
            u"│ ❯ 1. Yes                │\n"
            u"│   2. No                 │\n"
            u"╰─────────╯\n"
        )
        cleaned = clean_for_confirm(screen)
        # The box-drawing strip + cursor strip together should expose
        # "1. Yes" at line start (modulo the inner space the box left).
        assert re.search(r"^\s*1\.\s*Yes", cleaned, re.MULTILINE)


# ---------------------------------------------------------------------------
# End-to-end via should_auto_confirm: a generic TOML rule fires on
# screens with cursor-prefixed options.
# ---------------------------------------------------------------------------

class _Cfg(object):
    """Minimal AdapterConfig stand-in for should_auto_confirm()."""

    def __init__(self, rules):
        self.confirm_rules = rules
        self.strip_ansi = False
        self.confirm_recent_lines = 16


def _rule(pattern, response="1", send_enter=False, flags=re.MULTILINE):
    return (re.compile(pattern, flags), response, send_enter)


class TestShouldAutoConfirmWithCursorPrefixedOption:
    @pytest.mark.parametrize("cursor", [u"❯", u"›", u">"])
    def test_generic_rule_fires_on_each_cursor_variant(self, cursor):
        cfg = _Cfg([_rule(r"^1\.\s*Yes")])
        screen = (
            u"Some agent prose here\n"
            u"Do you want to proceed?\n"
            u"%s 1. Yes\n"
            u"  2. No\n"
        ) % cursor
        out = should_auto_confirm(screen, cfg)
        assert out is not None, "rule did not fire for cursor %r" % cursor
        response, send_enter, _pat_str, _matched = out
        assert response == "1"
        assert send_enter is False

    def test_indented_cursor_variant_also_fires(self):
        cfg = _Cfg([_rule(r"^\s*1\.\s*Allow once")])
        screen = (
            u"Select an option:\n"
            u"    ❯ 1. Allow once\n"
            u"      2. Allow always\n"
            u"      3. Deny\n"
        )
        out = should_auto_confirm(screen, cfg)
        assert out is not None
        response, _send_enter, _pat, _matched = out
        assert response == "1"

    def test_does_not_fire_on_cursor_with_prose(self):
        # Negative: `❯ write the function` must not satisfy a numbered
        # rule even though the line starts with a cursor.
        cfg = _Cfg([_rule(r"^1\.\s*write")])
        screen = (
            u"What should I do?\n"
            u"❯ write the function\n"
            u"  2. read the function\n"
        )
        assert should_auto_confirm(screen, cfg) is None


# ---------------------------------------------------------------------------
# Existing adapter TOMLs still work for screens that have the cursor.
# (Sanity: the embedded claude TOML rule `1\.\s*(Yes,?\s*)?allow` should
# now also match `❯ 1. Yes, allow ...` thanks to the generic cleanup.)
# ---------------------------------------------------------------------------

class TestEmbeddedClaudeRuleWithCursor:
    def test_claude_allow_codex_to_work_with_cursor_prefix(self):
        # Use the legacy CAM client's AdapterConfig + a minimal config
        # carrying the canonical Claude trust-dialog rule. This proves
        # the cleanup also benefits live adapter rules.
        cfg = _Cfg([
            _rule(r"^[^\w\s]?\s*1\.\s*(Yes,?\s*)?allow", flags=re.IGNORECASE | re.MULTILINE),
        ])
        screen = (
            u"Do you trust the contents of this directory?\n"
            u"❯ 1. Yes, allow Codex to work in this folder\n"
            u"  2. No, quit\n"
        )
        out = should_auto_confirm(screen, cfg)
        # Even without the cursor strip, this specific anchor would
        # match (the regex tolerates one leading non-word char). The
        # important assertion is that the cleanup did NOT break it.
        assert out is not None
