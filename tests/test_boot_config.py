"""Tests for boot vs runtime adapter config during initializing."""

from pathlib import Path

from camc_pkg.adapters import AdapterConfig, _parse_toml
from camc_pkg.detection import (
    is_ready_for_boot,
    should_boot_confirm,
    should_confirm_initializing,
)

_CONFIGS_DIR = Path(__file__).resolve().parent.parent / "src" / "cam" / "adapters" / "configs"


def _boot_config(name):
    text = (_CONFIGS_DIR / name).read_text(encoding="utf-8")
    return AdapterConfig(_parse_toml(text))


def test_boot_confirm_theme_picker_without_cursor_guard():
    boot = _boot_config("claude.boot.toml")
    screen = "Choose the text style that looks best\n ❯ 2. Dark mode ✔\n"
    hit = should_boot_confirm(screen, boot)
    assert hit is not None
    assert hit[0] == ""
    assert hit[1] is True


def test_initializing_uses_boot_before_tool():
    boot = _boot_config("claude.boot.toml")
    tool = _boot_config("claude.toml")
    screen = "Press Enter to continue\n"
    hit, cfg = should_confirm_initializing(screen, boot, tool)
    assert hit is not None
    assert cfg is boot


def test_initializing_falls_back_to_tool_confirm():
    boot = _boot_config("claude.boot.toml")
    tool = _boot_config("claude.toml")
    screen = "Confirm paste\n[Pasted text #1 +3 lines]\n"
    hit, cfg = should_confirm_initializing(screen, boot, tool)
    assert hit is not None
    assert cfg is tool


def test_ready_accepts_boot_or_tool_pattern():
    boot = _boot_config("claude.boot.toml")
    tool = _boot_config("claude.toml")
    assert is_ready_for_boot("❯ \n", boot, tool) is True
    assert is_ready_for_boot("Choose the text style\n ❯ 2. Dark\n", boot, tool) is False


def test_cursor_boot_trust_rule():
    boot = _boot_config("cursor.boot.toml")
    screen = "[a] Trust this workspace\n"
    hit = should_boot_confirm(screen, boot)
    assert hit is not None
    assert hit[0] == "a"
