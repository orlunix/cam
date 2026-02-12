"""OpenAI Codex CLI adapter implementation.

Provides the ToolAdapter for the Codex CLI, which runs in full-auto mode
with pattern-based detection for states, confirmations, and completion.
"""

from __future__ import annotations

import re

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition


class CodexAdapter(ToolAdapter):
    """Adapter for OpenAI Codex CLI tool.

    Supports headless execution using the --full-auto flag. Detects states,
    auto-confirms prompts, and identifies completion from output patterns.
    """

    name = "codex"
    display_name = "OpenAI Codex"

    _STATE_PATTERNS = {
        AgentState.PLANNING: re.compile(
            r"(Thinking|Planning|Analyzing|Reading|Searching|Reviewing)",
            re.IGNORECASE,
        ),
        AgentState.EDITING: re.compile(
            r"(Editing|Writing|Creating|Modifying|Applying|Patching)",
            re.IGNORECASE,
        ),
        AgentState.TESTING: re.compile(
            r"(Running|Testing|Executing|Verifying|npm test|pytest|cargo test)",
            re.IGNORECASE,
        ),
        AgentState.COMMITTING: re.compile(
            r"(Committing|Pushing|git commit|git push|Creating PR)",
            re.IGNORECASE,
        ),
    }

    _AUTO_CONFIRM_PATTERNS = [
        (re.compile(r"(Apply|Accept|Approve|Continue|Proceed).*\[Y/n\]", re.IGNORECASE), "y"),
        (re.compile(r"(Apply|Accept|Approve|Continue|Proceed).*\[y/N\]", re.IGNORECASE), "y"),
        (re.compile(r"Press Enter", re.IGNORECASE), ""),
    ]

    _COMPLETION_PATTERN = re.compile(
        r"(Done|Completed|Finished|All changes applied)",
        re.IGNORECASE,
    )

    _ERROR_PATTERN = re.compile(
        r"(Error:|error:|FAILED|fatal:|Exception|command not found)",
        re.IGNORECASE,
    )

    _SHELL_PROMPT_PATTERN = re.compile(
        r"(\$|\#|>)\s*$",
        re.MULTILINE,
    )

    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Launch Codex in full-auto mode with the task prompt."""
        return [
            "codex",
            "--full-auto",
            task.prompt,
        ]

    def detect_state(self, output: str) -> AgentState | None:
        """Detect agent state from recent output."""
        recent = output[-2000:] if len(output) > 2000 else output
        for state, pattern in self._STATE_PATTERNS.items():
            if pattern.search(recent):
                return state
        return None

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Check for confirmation prompts in output."""
        recent = output[-500:] if len(output) > 500 else output
        for pattern, response in self._AUTO_CONFIRM_PATTERNS:
            if pattern.search(recent):
                return ConfirmAction(response=response, send_enter=True)
        return None

    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect if Codex has finished."""
        if self._ERROR_PATTERN.search(output):
            return AgentStatus.FAILED

        recent = output[-500:] if len(output) > 500 else output
        if self._COMPLETION_PATTERN.search(recent):
            return AgentStatus.COMPLETED

        if self._SHELL_PROMPT_PATTERN.search(recent) and len(output) > 100:
            return AgentStatus.COMPLETED

        return None

    def get_startup_wait(self) -> float:
        return 0.0

    def needs_prompt_after_launch(self) -> bool:
        return False
