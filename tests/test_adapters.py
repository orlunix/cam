"""Tests for tool adapters."""

from __future__ import annotations

import pytest

from cam.adapters.base import ConfirmAction
from cam.adapters.claude import ClaudeAdapter
from cam.adapters.codex import CodexAdapter
from cam.adapters.aider import AiderAdapter
from cam.adapters.generic import GenericAdapter
from cam.core.models import (
    AgentState,
    AgentStatus,
    Context,
    MachineConfig,
    TaskDefinition,
)


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
        assert cmd[0] == "claude"
        assert "--allowed-tools" in cmd
        # Prompt is NOT in launch command — sent via stdin after launch
        assert task.prompt not in cmd
        # Should NOT use headless mode
        assert "-p" not in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_launch_command_has_tools(self, task, context):
        adapter = ClaudeAdapter()
        cmd = adapter.get_launch_command(task, context)
        tools_idx = cmd.index("--allowed-tools")
        tools_arg = cmd[tools_idx + 1]
        assert "Bash" in tools_arg
        assert "Edit" in tools_arg
        assert "Write" in tools_arg

    def test_detect_state_planning_thinking(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("Thinking about the approach...") == AgentState.PLANNING

    def test_detect_state_planning_tool_marker(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("● Read(src/main.py)") == AgentState.PLANNING

    def test_detect_state_editing_tool_marker(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("● Edit(src/main.py)") == AgentState.EDITING

    def test_detect_state_editing_write_marker(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("● Write(new_file.py)") == AgentState.EDITING

    def test_detect_state_testing_bash_marker(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("● Bash(pytest tests/)") == AgentState.TESTING

    def test_detect_state_testing_keyword(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("Running tests with pytest...") == AgentState.TESTING

    def test_detect_state_committing(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("git commit -m 'fix bug'") == AgentState.COMMITTING

    def test_detect_no_state(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("Some random output") is None

    def test_detect_state_strips_ansi(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_state("\x1B[1;32mThinking\x1B[0m about the approach...") == AgentState.PLANNING

    def test_detect_state_last_match_wins(self):
        """When multiple patterns match, the last occurrence in output wins."""
        adapter = ClaudeAdapter()
        output = "● Read(config.py)\nAnalyzing...\n● Edit(config.py)\nDone"
        assert adapter.detect_state(output) == AgentState.EDITING

    def test_detect_state_last_match_testing_after_editing(self):
        adapter = ClaudeAdapter()
        output = "● Edit(main.py)\nUpdated file\n● Bash(pytest tests/)"
        assert adapter.detect_state(output) == AgentState.TESTING

    def test_auto_confirm_proceed(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("Do you want to proceed?")
        assert result is not None
        assert isinstance(result, ConfirmAction)
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_yes_menu(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("1. Yes  2. Yes, don't ask again  3. No")
        assert result is not None
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_allow_menu(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("1. Allow for this session")
        assert result is not None
        assert result.response == "1"
        assert result.send_enter is False

    def test_auto_confirm_strips_ansi(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm(
            "\x1B[1m1. Yes\x1B[0m  2. No"
        )
        assert result is not None
        assert result.response == "1"

    def test_no_auto_confirm(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("Just some normal output")
        assert result is None

    def test_is_interactive(self):
        adapter = ClaudeAdapter()
        assert adapter.needs_prompt_after_launch()
        assert adapter.get_startup_wait() > 0

    def test_auto_confirm_trust_folder(self):
        adapter = ClaudeAdapter()
        result = adapter.should_auto_confirm("1. Yes, I trust this folder  2. No, exit")
        assert result is not None
        assert result.response == "1"
        assert result.send_enter is False

    def test_completion_returns_none_for_single_prompt(self):
        adapter = ClaudeAdapter()
        # Single ❯ = still working (initial prompt sent)
        assert adapter.detect_completion("❯ Do something\nWorking...") is None

    def test_completion_returns_none_for_no_prompt(self):
        adapter = ClaudeAdapter()
        assert adapter.detect_completion("anything") is None

    def test_completion_detected_with_two_prompts(self):
        adapter = ClaudeAdapter()
        # Two ❯ = task prompt + return to input = completed
        output = '❯ Do something\n● Write(file.py)\n────\n❯\xa0Try "fix lint"\n────'
        assert adapter.detect_completion(output) == AgentStatus.COMPLETED

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

    def test_startup_wait_is_30s(self):
        adapter = ClaudeAdapter()
        assert adapter.get_startup_wait() == 30.0


class TestCodexAdapter:
    def test_launch_command(self, task, context):
        task.tool = "codex"
        adapter = CodexAdapter()
        cmd = adapter.get_launch_command(task, context)
        assert cmd[0] == "codex"
        assert "--full-auto" in cmd

    def test_not_interactive(self):
        adapter = CodexAdapter()
        assert not adapter.needs_prompt_after_launch()

    def test_auto_confirm_returns_confirm_action(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm("Apply changes? [Y/n]")
        assert result is not None
        assert isinstance(result, ConfirmAction)
        assert result.response == "y"
        assert result.send_enter is True

    def test_no_auto_confirm(self):
        adapter = CodexAdapter()
        result = adapter.should_auto_confirm("Just output")
        assert result is None


class TestAiderAdapter:
    def test_launch_command(self, task, context):
        task.tool = "aider"
        adapter = AiderAdapter()
        cmd = adapter.get_launch_command(task, context)
        assert cmd[0] == "aider"
        assert "--yes" in cmd

    def test_is_interactive(self):
        adapter = AiderAdapter()
        assert adapter.needs_prompt_after_launch()
        assert adapter.get_startup_wait() > 0

    def test_auto_confirm_returns_confirm_action(self):
        adapter = AiderAdapter()
        result = adapter.should_auto_confirm("Create new file test.py? y/n")
        assert result is not None
        assert isinstance(result, ConfirmAction)
        assert result.response == "y"
        assert result.send_enter is True


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
        assert "aider" in adapter_registry
        assert "generic" in adapter_registry
        assert len(adapter_registry) == 4

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
