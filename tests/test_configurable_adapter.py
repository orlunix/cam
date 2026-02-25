"""Tests for TOML-configurable adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from cam.adapters.base import ConfirmAction
from cam.adapters.codex import CodexAdapter
from cam.adapters.configurable import ConfigurableAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition

CONFIGS_DIR = Path(__file__).parent.parent / "src" / "cam" / "adapters" / "configs"
CODEX_TOML = CONFIGS_DIR / "codex.toml"
CURSOR_TOML = CONFIGS_DIR / "cursor.toml"


@pytest.fixture
def task():
    return TaskDefinition(name="test", tool="codex", prompt="Fix the bug")


@pytest.fixture
def context():
    return Context(
        id=str(uuid4()),
        name="test",
        path="/tmp/test",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def codex_toml_adapter():
    return ConfigurableAdapter.from_toml(CODEX_TOML)


@pytest.fixture
def codex_python_adapter():
    return CodexAdapter()


@pytest.fixture
def cursor_adapter():
    return ConfigurableAdapter.from_toml(CURSOR_TOML)


# ── Loading tests ──────────────────────────────────────────────────


class TestLoading:
    def test_load_codex_toml(self):
        adapter = ConfigurableAdapter.from_toml(CODEX_TOML)
        assert adapter.name == "codex"
        assert adapter.display_name == "OpenAI Codex"

    def test_load_cursor_toml(self):
        adapter = ConfigurableAdapter.from_toml(CURSOR_TOML)
        assert adapter.name == "cursor"
        assert adapter.display_name == "Cursor Agent"

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="adapter.name is required"):
            ConfigurableAdapter({"adapter": {"display_name": "Foo"}})

    def test_missing_display_name_raises(self):
        with pytest.raises(ValueError, match="adapter.display_name is required"):
            ConfigurableAdapter({"adapter": {"name": "foo"}})

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="Unknown state"):
            ConfigurableAdapter({
                "adapter": {"name": "x", "display_name": "X"},
                "state": {"patterns": [{"state": "flying", "pattern": "fly"}]},
            })

    def test_unknown_regex_flag_raises(self):
        with pytest.raises(ValueError, match="Unknown regex flag"):
            ConfigurableAdapter({
                "adapter": {"name": "x", "display_name": "X"},
                "state": {"patterns": [
                    {"state": "planning", "pattern": "foo", "flags": ["BADFLG"]},
                ]},
            })

    def test_empty_config_minimal(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "bare", "display_name": "Bare"},
        })
        assert adapter.name == "bare"
        assert adapter.needs_prompt_after_launch() is False
        assert adapter.get_startup_wait() == 2.0
        assert adapter.is_ready_for_input("anything") is True

    def test_unknown_state_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown state.strategy"):
            ConfigurableAdapter({
                "adapter": {"name": "x", "display_name": "X"},
                "state": {"strategy": "middle"},
            })

    def test_unknown_completion_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown completion.strategy"):
            ConfigurableAdapter({
                "adapter": {"name": "x", "display_name": "X"},
                "completion": {"strategy": "magic"},
            })

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            ConfigurableAdapter.from_toml(Path("/nonexistent/adapter.toml"))

    def test_prompt_after_launch_config(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "x", "display_name": "X"},
            "launch": {"prompt_after_launch": True, "startup_wait": 10.0},
        })
        assert adapter.needs_prompt_after_launch() is True
        assert adapter.get_startup_wait() == 10.0


# ── Codex behavior equivalence tests ──────────────────────────────


class TestCodexEquivalence:
    """Verify ConfigurableAdapter(codex.toml) behaves identically to CodexAdapter."""

    def test_launch_command(self, codex_toml_adapter, codex_python_adapter, task, context):
        toml_cmd = codex_toml_adapter.get_launch_command(task, context)
        py_cmd = codex_python_adapter.get_launch_command(task, context)
        assert toml_cmd == py_cmd

    def test_launch_no_double_substitution(self):
        """Prompt containing {path} must not get replaced by context.path."""
        adapter = ConfigurableAdapter({
            "adapter": {"name": "x", "display_name": "X"},
            "launch": {"command": ["tool", "--prompt", "{prompt}"]},
        })
        task = TaskDefinition(name="t", tool="x", prompt="fix {path} in code")
        ctx = Context(
            id=str(uuid4()), name="c", path="/project",
            created_at=datetime.now(timezone.utc),
        )
        cmd = adapter.get_launch_command(task, ctx)
        assert cmd == ["tool", "--prompt", "fix {path} in code"]

    def test_startup_wait(self, codex_toml_adapter, codex_python_adapter):
        assert codex_toml_adapter.get_startup_wait() == codex_python_adapter.get_startup_wait()

    def test_needs_prompt_after_launch(self, codex_toml_adapter, codex_python_adapter):
        assert codex_toml_adapter.needs_prompt_after_launch() == codex_python_adapter.needs_prompt_after_launch()

    # ── detect_state ──

    @pytest.mark.parametrize("output,expected", [
        ("Thinking about the code...", AgentState.PLANNING),
        ("Planning the approach", AgentState.PLANNING),
        ("Analyzing the repo", AgentState.PLANNING),
        ("Reading the file", AgentState.PLANNING),
        ("Searching for matches", AgentState.PLANNING),
        ("Reviewing changes", AgentState.PLANNING),
        ("Editing main.py", AgentState.EDITING),
        ("Writing new file", AgentState.EDITING),
        ("Creating test.py", AgentState.EDITING),
        ("Modifying config", AgentState.EDITING),
        ("Applying patch", AgentState.EDITING),
        ("Patching file", AgentState.EDITING),
        ("Running tests", AgentState.TESTING),
        ("Testing the feature", AgentState.TESTING),
        ("Executing command", AgentState.TESTING),
        ("Verifying output", AgentState.TESTING),
        ("npm test", AgentState.TESTING),
        ("pytest tests/", AgentState.TESTING),
        ("cargo test", AgentState.TESTING),
        ("Committing changes", AgentState.COMMITTING),
        ("Pushing to remote", AgentState.COMMITTING),
        ("git commit -m 'fix'", AgentState.COMMITTING),
        ("git push origin main", AgentState.COMMITTING),
        ("some random output", None),
    ])
    def test_detect_state(self, codex_toml_adapter, codex_python_adapter, output, expected):
        toml_result = codex_toml_adapter.detect_state(output)
        py_result = codex_python_adapter.detect_state(output)
        assert toml_result == py_result == expected

    # ── should_auto_confirm ──

    @pytest.mark.parametrize("output,expect_match", [
        ("Apply changes? [Y/n]", True),
        ("Accept suggestion? [Y/n]", True),
        ("Approve all? [Y/n]", True),
        ("Continue with changes? [Y/n]", True),
        ("Proceed? [Y/n]", True),
        ("Apply changes? [y/N]", True),
        ("Press Enter to continue", True),
        ("Just some normal output", False),
    ])
    def test_auto_confirm(self, codex_toml_adapter, codex_python_adapter, output, expect_match):
        toml_result = codex_toml_adapter.should_auto_confirm(output)
        py_result = codex_python_adapter.should_auto_confirm(output)
        if expect_match:
            assert toml_result is not None and py_result is not None
            assert toml_result.response == py_result.response
            assert toml_result.send_enter == py_result.send_enter
        else:
            assert toml_result is None and py_result is None

    # ── detect_completion ──

    def test_completion_done(self, codex_toml_adapter, codex_python_adapter):
        output = "x" * 200 + "\nDone"
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.COMPLETED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_completion_completed(self, codex_toml_adapter, codex_python_adapter):
        output = "x" * 200 + "\nCompleted successfully"
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.COMPLETED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_completion_finished(self, codex_toml_adapter, codex_python_adapter):
        output = "x" * 200 + "\nFinished all tasks"
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.COMPLETED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_completion_all_changes_applied(self, codex_toml_adapter, codex_python_adapter):
        output = "x" * 200 + "\nAll changes applied"
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.COMPLETED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_error_detected(self, codex_toml_adapter, codex_python_adapter):
        output = "Error: something went wrong"
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.FAILED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.FAILED

    def test_fatal_error(self, codex_toml_adapter, codex_python_adapter):
        output = "fatal: not a git repository"
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.FAILED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.FAILED

    def test_shell_prompt_completion(self, codex_toml_adapter, codex_python_adapter):
        output = "a" * 200 + "\nsome output\n$ "
        assert codex_toml_adapter.detect_completion(output) == AgentStatus.COMPLETED
        assert codex_python_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_shell_prompt_too_short(self, codex_toml_adapter, codex_python_adapter):
        output = "$ "
        assert codex_toml_adapter.detect_completion(output) is None
        assert codex_python_adapter.detect_completion(output) is None

    def test_no_completion(self, codex_toml_adapter, codex_python_adapter):
        output = "Working on things..."
        assert codex_toml_adapter.detect_completion(output) is None
        assert codex_python_adapter.detect_completion(output) is None


# ── Cursor adapter tests ──────────────────────────────────────────


class TestCursorAdapter:
    def test_launch_command(self, cursor_adapter, task, context):
        cmd = cursor_adapter.get_launch_command(task, context)
        assert cmd == ["agent", "--workspace", "/tmp/test"]

    def test_is_interactive(self, cursor_adapter):
        assert cursor_adapter.needs_prompt_after_launch() is True
        assert cursor_adapter.get_startup_wait() == 15.0

    def test_ready_detection(self, cursor_adapter):
        assert cursor_adapter.is_ready_for_input("→ Plan, search, build anything") is True
        assert cursor_adapter.is_ready_for_input("Loading...") is False

    def test_detect_state_planning(self, cursor_adapter):
        assert cursor_adapter.detect_state("⬢ Read /tmp/test/main.py") == AgentState.PLANNING
        assert cursor_adapter.detect_state("⬢ Grep something") == AgentState.PLANNING
        assert cursor_adapter.detect_state("Analyzing the code") == AgentState.PLANNING

    def test_detect_state_editing(self, cursor_adapter):
        assert cursor_adapter.detect_state("⬢ Edit /tmp/test/main.py") == AgentState.EDITING
        assert cursor_adapter.detect_state("⬢ Write /tmp/test/new.py") == AgentState.EDITING

    def test_detect_state_testing(self, cursor_adapter):
        assert cursor_adapter.detect_state("$ python3 test.py 5s in /tmp") == AgentState.TESTING
        assert cursor_adapter.detect_state("$ pytest tests/ 2s in /project") == AgentState.TESTING

    def test_detect_state_committing(self, cursor_adapter):
        assert cursor_adapter.detect_state("git commit -m fix") == AgentState.COMMITTING

    def test_detect_state_last_wins(self, cursor_adapter):
        """Last match wins (strategy=last), like Claude adapter."""
        output = "⬢ Read config.py\nAnalyzing...\n⬢ Edit config.py\nDone"
        assert cursor_adapter.detect_state(output) == AgentState.EDITING

    def test_detect_state_none(self, cursor_adapter):
        assert cursor_adapter.detect_state("random stuff") is None

    def test_completion_two_prompts(self, cursor_adapter):
        # Two → = task prompt + return to input = completed
        output = "→ Fix the bug\n⬢ Read main.py\nFixed it.\n→ Add a follow-up"
        assert cursor_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_completion_single_prompt_not_done(self, cursor_adapter):
        output = "→ Fix the bug\n⬢ Read main.py\nWorking..."
        assert cursor_adapter.detect_completion(output) is None

    def test_completion_fallback_with_percentage(self, cursor_adapter):
        # Single → + usage percentage = completion
        output = "Fixed it.\n→ Add a follow-up\nClaude 4.6 Opus (Thinking) · 6.7%"
        assert cursor_adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_auto_confirm_run_once(self, cursor_adapter):
        output = "Run this command?\n → Run (once) (y)\n   Skip (esc or n)"
        result = cursor_adapter.should_auto_confirm(output)
        assert result is not None
        assert result.response == ""
        assert result.send_enter is True

    def test_auto_confirm_run_always(self, cursor_adapter):
        output = "Run this command?\n → Run (always for this command)\n   Skip"
        result = cursor_adapter.should_auto_confirm(output)
        assert result is not None
        assert result.send_enter is True

    def test_auto_confirm_trust_workspace(self, cursor_adapter):
        output = "▶ [a] Trust this workspace\n  [q] Quit\n  Use arrow keys to navigate"
        result = cursor_adapter.should_auto_confirm(output)
        assert result is not None
        assert result.response == ""
        assert result.send_enter is True

    def test_trust_dialog_not_detected_as_ready(self, cursor_adapter):
        """Trust dialog has ▶ not →, so is_ready_for_input must return False."""
        output = "▶ [a] Trust this workspace\n  [q] Quit"
        assert cursor_adapter.is_ready_for_input(output) is False

    def test_no_auto_confirm(self, cursor_adapter):
        assert cursor_adapter.should_auto_confirm("Just normal output") is None


# ── Prompt-count strategy tests ───────────────────────────────────


class TestPromptCountStrategy:
    def test_prompt_count_completion(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "pc", "display_name": "PC"},
            "completion": {
                "strategy": "prompt_count",
                "prompt_pattern": "^>",
                "prompt_flags": ["MULTILINE"],
                "prompt_count_threshold": 2,
            },
        })
        assert adapter.detect_completion("> hello\nwork\n> done") == AgentStatus.COMPLETED
        assert adapter.detect_completion("> hello\nworking...") is None

    def test_prompt_count_with_fallback(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "pc2", "display_name": "PC2"},
            "completion": {
                "strategy": "prompt_count",
                "prompt_pattern": "^>",
                "prompt_flags": ["MULTILINE"],
                "prompt_count_threshold": 2,
                "fallback_summary_pattern": "DONE in \\d+s",
            },
        })
        # Single prompt + summary = completion
        assert adapter.detect_completion("> hello\nDONE in 5s") == AgentStatus.COMPLETED
        # Single prompt without summary = still running
        assert adapter.detect_completion("> hello\nworking") is None


# ── Process-exit strategy tests ───────────────────────────────────


class TestProcessExitStrategy:
    def test_always_returns_none(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "pe", "display_name": "PE"},
            "completion": {"strategy": "process_exit"},
        })
        assert adapter.detect_completion("Error: boom") is None
        assert adapter.detect_completion("Done!") is None


# ── Strip ANSI tests ──────────────────────────────────────────────


class TestStripAnsi:
    def test_strip_ansi_in_state_detection(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "a", "display_name": "A"},
            "launch": {"strip_ansi": True},
            "state": {"patterns": [
                {"state": "planning", "pattern": "Thinking", "flags": ["IGNORECASE"]},
            ]},
        })
        assert adapter.detect_state("\x1b[32mThinking\x1b[0m") == AgentState.PLANNING

    def test_strip_ansi_in_pattern_completion(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "a", "display_name": "A"},
            "launch": {"strip_ansi": True},
            "completion": {
                "strategy": "pattern",
                "completion_pattern": "^Done$",
                "completion_flags": ["MULTILINE"],
                "min_output_length": 10,
            },
        })
        output = "x" * 50 + "\n\x1b[32mDone\x1b[0m"
        assert adapter.detect_completion(output) == AgentStatus.COMPLETED

    def test_strip_ansi_in_ready_check(self):
        adapter = ConfigurableAdapter({
            "adapter": {"name": "a", "display_name": "A"},
            "launch": {
                "strip_ansi": True,
                "ready_pattern": "^ready>",
                "ready_flags": ["MULTILINE"],
            },
        })
        assert adapter.is_ready_for_input("\x1b[1mready>\x1b[0m") is True
        assert adapter.is_ready_for_input("\x1b[1mloading\x1b[0m") is False


# ── Registry integration tests ────────────────────────────────────


class TestRegistryIntegration:
    def test_cursor_agent_registered(self):
        from cam.adapters.registry import AdapterRegistry
        reg = AdapterRegistry()
        assert "cursor" in reg
        adapter = reg.get("cursor")
        assert adapter is not None
        assert adapter.display_name == "Cursor Agent"

    def test_python_adapters_still_present(self):
        from cam.adapters.registry import AdapterRegistry
        reg = AdapterRegistry()
        for name in ["claude", "codex", "aider", "generic"]:
            assert name in reg

    def test_codex_toml_skipped(self):
        """Python CodexAdapter wins over codex.toml."""
        from cam.adapters.registry import AdapterRegistry
        reg = AdapterRegistry()
        adapter = reg.get("codex")
        assert adapter is not None
        assert isinstance(adapter, CodexAdapter)

    def test_total_adapter_count(self):
        from cam.adapters.registry import AdapterRegistry
        reg = AdapterRegistry()
        assert len(reg) == 5  # 4 Python + cursor

    def test_names_includes_cursor(self):
        from cam.adapters.registry import AdapterRegistry
        reg = AdapterRegistry()
        names = reg.names()
        assert "cursor" in names
        assert "claude" in names
        assert "codex" in names

    def test_invalid_toml_does_not_crash(self, tmp_path):
        """Bad TOML files in the scan directory are skipped gracefully."""
        from cam.adapters.configurable import ConfigurableAdapter
        from cam.adapters.registry import AdapterRegistry

        # Create a bad TOML file
        bad_toml = tmp_path / "bad.toml"
        bad_toml.write_text("[adapter]\n# missing name and display_name\n")

        # This should log a warning but not crash
        reg = AdapterRegistry()
        # The bad file isn't in the scan path, but we can test from_toml directly
        with pytest.raises(ValueError):
            ConfigurableAdapter.from_toml(bad_toml)
