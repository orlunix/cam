"""Claude Code adapter implementation.

This module provides the ToolAdapter implementation for Claude Code CLI,
using interactive mode with --allowed-tools for reliable auto-confirmation.
"""

from __future__ import annotations

import re

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition
from cam.utils.ansi import strip_ansi


class ClaudeAdapter(ToolAdapter):
    """Adapter for Claude Code CLI tool.

    Runs Claude in interactive mode (not headless -p) with pre-authorized
    tools via --allowed-tools. Permission prompts show a numbered menu
    (1. Yes, 2. Yes don't ask again, 3. No) — the adapter sends "1"
    without Enter to confirm.
    """

    name = "claude"
    display_name = "Claude Code"

    # Compile regex patterns for efficiency
    _STATE_PATTERNS = {
        AgentState.PLANNING: re.compile(
            r"(Thinking|Analyzing|Planning|Reading|Searching)",
            re.IGNORECASE
        ),
        AgentState.EDITING: re.compile(
            r"(Editing|Writing|Creating|Modifying|Updated)",
            re.IGNORECASE
        ),
        AgentState.TESTING: re.compile(
            r"(Running tests|Testing|Executing|pytest|npm test)",
            re.IGNORECASE
        ),
        AgentState.COMMITTING: re.compile(
            r"(Committing|Pushing|Creating PR|git commit)",
            re.IGNORECASE
        ),
    }

    # Auto-confirm patterns for Claude's numbered menu UI.
    # Claude shows: "1. Yes  2. Yes, don't ask again  3. No"
    # or "1. Yes, I trust this folder  2. No, exit"
    # We send "1" without Enter (Claude reads single keypresses).
    _AUTO_CONFIRM_PATTERNS = [
        # Direct permission prompt
        (re.compile(r"Do\s+you\s+want\s+to\s+proceed", re.IGNORECASE),
         ConfirmAction(response="1", send_enter=False)),
        # Trust folder prompt
        (re.compile(r"1\.\s*Yes,?\s*I?\s*trust", re.IGNORECASE),
         ConfirmAction(response="1", send_enter=False)),
        # Numbered menu with "1. Yes" or "1. Allow" visible
        (re.compile(r"1\.\s*(Yes|Allow)", re.IGNORECASE),
         ConfirmAction(response="1", send_enter=False)),
    ]

    # Claude Code cost/token output pattern
    _COST_PATTERN = re.compile(
        r"Total cost:\s*\$?([\d.]+)",
        re.IGNORECASE,
    )

    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Launch Claude Code in interactive mode with pre-authorized tools.

        Uses --allowed-tools to pre-authorize key tools, reducing permission
        prompts. The prompt is sent separately via stdin after launch
        (see needs_prompt_after_launch).

        Args:
            task: Task containing the prompt to execute
            context: Context with working directory and environment

        Returns:
            Command list for interactive mode execution
        """
        return [
            "claude",
            "--allowed-tools",
            "Bash,Edit,Read,Write,Glob,Grep,WebFetch,TodoWrite,NotebookEdit",
        ]

    def detect_state(self, output: str) -> AgentState | None:
        """Detect agent state from recent output.

        Strips ANSI codes before checking patterns.

        Args:
            output: Recent TMUX output

        Returns:
            Detected AgentState or None if no patterns match
        """
        recent = output[-2000:] if len(output) > 2000 else output
        clean = strip_ansi(recent)

        for state, pattern in self._STATE_PATTERNS.items():
            if pattern.search(clean):
                return state

        return None

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Check if output contains a Claude permission prompt.

        Strips ANSI codes before pattern matching. Checks the last ~500
        characters for numbered menu prompts.

        Args:
            output: Recent TMUX output

        Returns:
            ConfirmAction(response="1", send_enter=False) or None
        """
        recent = output[-500:] if len(output) > 500 else output
        clean = strip_ansi(recent)

        for pattern, action in self._AUTO_CONFIRM_PATTERNS:
            if pattern.search(clean):
                return action

        return None

    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect if Claude Code has completed execution.

        In interactive mode, Claude doesn't auto-exit. Completion is detected
        by the monitor via session_exists() when the session ends, or via
        idle timeout. This method returns None to avoid false positives.

        Args:
            output: Recent TMUX output

        Returns:
            None — completion is detected by session exit, not output parsing
        """
        return None

    # Claude's input prompt: "❯" at the start of a line (between ──── borders).
    # May have placeholder text like "Try "fix lint errors"" or be empty.
    _READY_PATTERN = re.compile(r"^❯", re.MULTILINE)

    def is_ready_for_input(self, output: str) -> bool:
        """Check if Claude's TUI shows the input prompt (❯).

        The input prompt appears as a line starting with ❯ between
        horizontal rule borders (────). This indicates Claude has
        finished loading and is ready to accept input.
        """
        clean = strip_ansi(output)
        return bool(self._READY_PATTERN.search(clean))

    def get_startup_wait(self) -> float:
        """Maximum seconds to wait for Claude's TUI to become ready."""
        return 30.0

    def needs_prompt_after_launch(self) -> bool:
        """Prompt must be sent via stdin after Claude is ready."""
        return True

    def estimate_cost(self, output: str) -> float | None:
        """Estimate cost from Claude Code output."""
        return None

    def parse_files_changed(self, output: str) -> list[str]:
        """Extract modified file paths from Claude Code output."""
        return []
