"""Tests for tool adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from cam.adapters.base import ConfirmAction
from cam.adapters.configurable import ConfigurableAdapter
from cam.adapters.generic import GenericAdapter
from cam.core.models import (
    AgentState,
    AgentStatus,
    Context,
    MachineConfig,
    TaskDefinition,
)

# Claude and Codex adapters are TOML-configured (not Python classes)
_CONFIGS_DIR = Path(__file__).resolve().parent.parent / "src" / "cam" / "adapters" / "configs"


def ClaudeAdapter():
    """Load Claude adapter from TOML config."""
    return ConfigurableAdapter.from_toml(_CONFIGS_DIR / "claude.toml")


def ClaudeBootAdapter():
    """Load Claude boot config for initializing-phase detection tests."""
    from camc_pkg.adapters import AdapterConfig, _parse_toml
    text = (_CONFIGS_DIR / "claude.boot.toml").read_text(encoding="utf-8")
    return AdapterConfig(_parse_toml(text))


def CodexAdapter():
    """Load Codex adapter from TOML config."""
    return ConfigurableAdapter.from_toml(_CONFIGS_DIR / "codex.toml")


@pytest.fixture
def task():
    return TaskDefinition(name="test", tool="claude", prompt="Write tests")


@pytest.fixture
def context():
    from datetime import datetime, timezone
    from uuid import uuid4
    return Context(
        id=str(uuid4()),
        name="test",
        path="/tmp/test",
        created_at=datetime.now(timezone.utc),
    )


class TestConfirmAction:
    def test_default_send_enter(self):
        action = ConfirmAction(response="y")
        assert action.response == "y"
        assert action.send_enter is True

    def test_explicit_no_enter(self):
        action = ConfirmAction(response="1", send_enter=False)
        assert action.response == "1"
        assert action.send_enter is False

    def test_is_tuple(self):
        action = ConfirmAction(response="y", send_enter=True)
        assert isinstance(action, tuple)
        assert action[0] == "y"
        assert action[1] is True


class TestClaudeAdapter:
    def test_launch_command(self, task, context):
        adapter = ClaudeAdapter()
        cmd = adapter.get_launch_command(task, context)
        assert cmd[:4] == [
            "env", "CLAUDE_CODE_DISABLE_MOUSE=1",
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1", "claude",
        ]
        assert "--allowed-tools" not in cmd
        assert "--permission-mode" not in cmd
        # Prompt is NOT in launch command — sent via stdin after launch
        assert task.prompt not in cmd
        # Should NOT use headless mode
        assert "-p" not in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_launch_command_keeps_default_permissions(self, task, context):
        adapter = ClaudeAdapter()
        cmd = adapter.get_launch_command(task, context)
        assert "--allowed-tools" not in cmd
        assert "--permission-mode" not in cmd
        assert "--dangerously-skip-permissions" not in cmd












    def test_auto_confirm_proceed(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm(
            "Do you want to proceed?\n❯ 1. Yes  2. No"
        )
        assert result is not None
        assert isinstance(result, ConfirmAction)
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_yes_menu(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm(
            "❯ 1. Yes  2. Yes, don't ask again  3. No")
        assert result is not None
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_allow_menu(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("❯ 1. Allow for this session")
        assert result is not None
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_strips_ansi(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm(
            "\x1B[1m❯ 1. Yes\x1B[0m  2. No"
        )
        assert result is not None
        assert result.response == "1"

    def test_no_auto_confirm_plan_mode_interview(self):
        adapter = ClaudeAdapter()
        output = (
            "❯ 1. ~/confluence.token.txt\n"
            "  2. .env 文件\n"
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"
        )
        result = adapter.should_auto_confirm(output)
        assert result is None

    def test_no_auto_confirm(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("Just some normal output")
        assert result is None

    def test_is_interactive(self):
        adapter = ClaudeAdapter()
        assert adapter.needs_prompt_after_launch()
        assert adapter.get_startup_wait() > 0

    def test_auto_confirm_trust_folder_is_boot_only(self):
        adapter = ClaudeAdapter()
        screen = "1. Yes, I trust this folder  2. No, exit"
        assert adapter.should_auto_confirm(screen) is None









    def test_is_ready_for_input_with_prompt(self):
        adapter = ClaudeAdapter()
        output = "Welcome back!\n❯ \n"
        assert adapter.is_ready_for_input(output) is True

    def test_is_ready_for_input_without_prompt(self):
        adapter = ClaudeAdapter()
        output = "Loading Claude Code...\n"
        assert adapter.is_ready_for_input(output) is False

    def test_is_ready_for_input_with_ansi(self):
        adapter = ClaudeAdapter()
        output = "\x1b[32mReady\x1b[0m\n❯ \n"
        assert adapter.is_ready_for_input(output) is True

    def test_is_ready_for_input_with_placeholder(self):
        adapter = ClaudeAdapter()
        output = '────\n❯\xa0Try "fix lint errors"\n────\n'
        assert adapter.is_ready_for_input(output) is True

    def test_startup_wait_is_90s(self):
        adapter = ClaudeAdapter()
        assert adapter.get_startup_wait() == 90.0

    def test_is_ready_not_on_ink_menu(self):
        adapter = ClaudeAdapter()
        theme = "Choose the text style\n ❯ 2. Dark mode ✔\n"
        assert adapter.is_ready_for_input(theme) is False

    def test_auto_confirm_theme_picker(self):
        from camc_pkg.detection import should_boot_confirm
        adapter = ClaudeBootAdapter()
        screen = "Let's get started.\nChoose the text style that looks best\n ❯ 2. Dark mode ✔\n"
        confirm = should_boot_confirm(screen, adapter)
        assert confirm is not None
        response, send_enter, _pat, _matched = confirm
        assert response == ""
        assert send_enter is True

    def test_auto_confirm_api_key_dialog(self):
        from camc_pkg.detection import should_boot_confirm
        adapter = ClaudeBootAdapter()
        screen = (
            "Detected a custom API key in your environment\n"
            "Do you want to use this API key?\n"
            "  1. Yes\n"
            "❯ 2. No (recommended)\n"
        )
        confirm = should_boot_confirm(screen, adapter)
        assert confirm is not None
        response, send_enter, _pat, _matched = confirm
        assert response == "1"
        assert send_enter is False


class TestCodexAdapter:
    def test_launch_command(self, task, context):
        task.tool = "codex"
        adapter = CodexAdapter()
        cmd = adapter.get_launch_command(task, context)
        assert cmd[0] == "codex"
        assert "--no-alt-screen" in cmd
        assert "--full-auto" not in cmd
        assert task.prompt not in cmd

    def test_interactive_prompt_after_launch(self):
        adapter = CodexAdapter()
        assert adapter.needs_prompt_after_launch()

    def test_auto_confirm_trust_dialog_is_boot_only(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm(
            "1. Yes, allow Codex to work in this folder without asking for approval\n"
            "2. No, ask me to approve edits and commands"
        )
        assert result is None

    def test_auto_confirm_new_trust_directory_dialog_is_boot_only(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm(
            "Do you trust the contents of this directory?\n\n"
            "You are in /tmp/project\n\n"
            "> 1. Yes, continue\n"
            "  2. No, quit\n\n"
            "Press enter to continue"
        )
        assert result is None

    def test_auto_confirm_numbered_menu_at_runtime(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm(
            "Reason: command failed; retry without sandbox?\n"
            "› 1. Yes, proceed (y)\n"
            "  2. Yes, and don't ask again for these files (a)\n"
            "  3. No, and tell Codex what to do differently (esc)\n"
            "Press enter to confirm or esc to cancel"
        )
        assert result is not None
        assert isinstance(result, ConfirmAction)
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_runtime_menu_from_no_option(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm(
            "Reason: command failed; retry without sandbox?\n"
            "  1. Yes, proceed (y)\n"
            "  2. Yes, and don't ask again for these files (a)\n"
            "  3. No, and tell Codex what to do differently (esc)\n"
            "Press enter to confirm or esc to cancel"
        )
        assert result is not None
        assert isinstance(result, ConfirmAction)
        assert result.response == "1"
        assert result.send_enter is False

    def test_no_auto_confirm_press_enter_stale_scrollback(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm(
            "  Press enter to continue\n\n"
            "╭────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex                        │\n"
            "╰────────────────────────────────────────╯\n"
            "Tip: Press Tab to queue a message.\n\n"
            "› Improve documentation in @filename"
        )
        assert result is None

    # test_auto_confirm_apply removed on 2026-06-11 along with
    # codex.toml rule #5 (the loose bracket-y/n rule). Modern Codex
    # uses numbered menus.

    def test_no_auto_confirm(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm("Just output")
        assert result is None





class TestGenericAdapter:
    def test_launch_command(self, task, context):
        adapter = GenericAdapter()
        cmd = adapter.get_launch_command(task, context)
        assert cmd == [task.tool, task.prompt]

    def test_no_state_detection(self):
        adapter = GenericAdapter()
        assert adapter.detect_state("anything") is None

    def test_no_auto_confirm(self):
        adapter = GenericAdapter()
        assert adapter.should_auto_confirm("anything") is None


class TestAdapterRegistry:
    def test_builtins_registered(self, adapter_registry):
        assert "claude" in adapter_registry
        assert "codex" in adapter_registry
        assert "generic" in adapter_registry
        assert len(adapter_registry) >= 3

    def test_get(self, adapter_registry):
        adapter = adapter_registry.get("claude")
        assert adapter is not None
        assert adapter.name == "claude"
        assert adapter.display_name == "Claude Code"

    def test_get_unknown(self, adapter_registry):
        assert adapter_registry.get("nonexistent") is None

    def test_names(self, adapter_registry):
        names = adapter_registry.names()
        assert "claude" in names
        assert "codex" in names
        assert "aider" not in names
