"""Tests for clean_for_confirm and cursor-anchored runtime confirm rules."""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg.utils import clean_for_confirm  # noqa: E402
from camc_pkg.detection import should_auto_confirm  # noqa: E402


class TestCleanForConfirm:
    def test_strips_box_drawing_edges(self):
        screen = (
            u"╭─ menu ─╮\n"
            u"│ Do you want to proceed? │\n"
            u"│ ❯ 1. Yes                │\n"
            u"│   2. No                 │\n"
            u"╰─────────╯\n"
        )
        cleaned = clean_for_confirm(screen)
        assert "Do you want to proceed?" in cleaned
        assert u"❯ 1. Yes" in cleaned

    def test_preserves_selection_cursor_for_toml_anchors(self):
        line = u"❯ 1. Yes  2. No"
        assert u"❯" in clean_for_confirm(line)


class _Cfg(object):
    def __init__(self, rules):
        self.confirm_rules = rules
        self.strip_ansi = False
        self.confirm_recent_lines = 16


def _rule(pattern, response="1", send_enter=False, flags=re.MULTILINE):
    return (re.compile(pattern, flags), response, send_enter)


class TestRuntimeCursorAnchoredConfirm:
    """Runtime rules anchor on the active menu cursor (see claude.toml)."""

    def test_claude_numbered_menu_fires(self):
        cfg = _Cfg([_rule(r"^❯\s+1\.\s*(Yes|Allow).*$", flags=re.IGNORECASE | re.MULTILINE)])
        screen = (
            "Do you want to proceed?\n"
            "❯ 1. Yes  2. Yes, don't ask again  3. No\n"
        )
        out = should_auto_confirm(screen, cfg)
        assert out is not None
        assert out[0] == "1"

    def test_prose_list_does_not_fire_without_cursor_anchor(self):
        cfg = _Cfg([_rule(r"^❯\s+1\.\s*(Yes|Allow).*$", flags=re.IGNORECASE | re.MULTILINE)])
        screen = (
            "Here is some markdown prose:\n"
            "  1. Yes, do it\n"
            "  2. No, skip it\n"
        )
        assert should_auto_confirm(screen, cfg) is None

    def test_bare_input_cursor_blocks_confirm(self):
        cfg = _Cfg([_rule(r"^❯\s+1\.\s*Yes")])
        screen = (
            "Do you want to proceed?\n"
            "1. Yes\n"
            "2. No\n"
            "❯ \n"
        )
        assert should_auto_confirm(screen, cfg) is None
