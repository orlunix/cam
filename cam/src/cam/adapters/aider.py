"""Aider adapter implementation.

Provides the ToolAdapter for Aider, the AI pair programming tool.
Aider runs interactively, so the prompt is sent as input after launch.
"""

from __future__ import annotations

import re

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition


class AiderAdapter(ToolAdapter):
    """Adapter for Aider AI pair programming tool.

    Aider launches interactively and waits for a prompt. The task prompt
    is sent as input after the tool starts. Supports --yes flag for
    auto-accepting changes.
    """

    name = "aider"
    display_name = "Aider"

    _STATE_PATTERNS = {
        AgentState.PLANNING: re.compile(
            r"(Thinking|Analyzing|Looking at|Searching|Reviewing)",
            re.IGNORECASE,
        ),
        AgentState.EDITING: re.compile(
            r"(Applied edit|Wrote|Created|Updated|Modified|SEARCH/REPLACE)",
            re.IGNORECASE,
        ),
        AgentState.TESTING: re.compile(
            r"(Running|Testing|Linting|pytest|npm test|make test)",
            re.IGNORECASE,
        ),
        AgentState.COMMITTING: re.compile(
            r"(Commit|commit [a-f0-9]|git add|Added .* to the chat)",
            re.IGNORECASE,
        ),
    }

    _AUTO_CONFIRM_PATTERNS = [
        # Aider asks to create files
        (re.compile(r"Create new file.*\?", re.IGNORECASE), "y"),
        # Allow edits
        (re.compile(r"Allow edits.*\?", re.IGNORECASE), "y"),
        # Add files to the chat
        (re.compile(r"Add .* to the chat\?", re.IGNORECASE), "y"),
        # Apply changes
        (re.compile(r"Apply.*\[Y/n\]", re.IGNORECASE), "y"),
        # Commit prompt
        (re.compile(r"Commit.*\[y/n\]", re.IGNORECASE), "y"),
    ]

    _COMPLETION_PATTERN = re.compile(
        r"(Tokens:.*sent,.*received|aider>)",
        re.IGNORECASE,
    )

    _ERROR_PATTERN = re.compile(
        r"(Error:|error:|FAILED|APIError|RateLimitError|Can't initialize)",
        re.IGNORECASE,
    )

    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Launch Aider with --yes for auto-accepting changes.

        The task prompt is sent separately after launch via send_input.
        """
        return [
            "aider",
            "--yes",       # Auto-accept changes
            "--no-git",    # Don't auto-commit (CAM manages lifecycle)
        ]

    def detect_state(self, output: str) -> AgentState | None:
        """Detect agent state from recent output."""
        recent = output[-2000:] if len(output) > 2000 else output
        for state, pattern in self._STATE_PATTERNS.items():
            if pattern.search(recent):
                return state
        return None

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Check for confirmation prompts."""
        recent = output[-500:] if len(output) > 500 else output
        for pattern, response in self._AUTO_CONFIRM_PATTERNS:
            if pattern.search(recent):
                return ConfirmAction(response=response, send_enter=True)
        return None

    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect if Aider has completed.

        Aider shows 'aider>' prompt when idle and waiting for next input.
        After processing a task, it shows token usage then returns to prompt.
        """
        if self._ERROR_PATTERN.search(output):
            return AgentStatus.FAILED

        recent = output[-500:] if len(output) > 500 else output
        # If we see the aider prompt after having substantial output,
        # and token stats, it means the task is done
        if self._COMPLETION_PATTERN.search(recent) and len(output) > 200:
            return AgentStatus.COMPLETED

        return None

    def get_startup_wait(self) -> float:
        """Wait for Aider to fully initialize before sending prompt."""
        return 5.0

    def needs_prompt_after_launch(self) -> bool:
        """Aider is interactive — prompt is sent after launch."""
        return True
