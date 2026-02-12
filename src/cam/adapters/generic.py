"""Generic adapter for arbitrary CLI tools.

Provides a minimal ToolAdapter that works with any command-line tool.
Uses basic heuristics for state detection and completion — primarily
relies on the TMUX session exiting to signal completion.
"""

from __future__ import annotations

import re

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition


class GenericAdapter(ToolAdapter):
    """Fallback adapter for any CLI tool.

    Uses the tool name as the binary name and passes the prompt as
    arguments. Relies on session exit for completion detection.
    State detection is minimal.

    The launch command is: <tool_name> <prompt>
    Override via config [tools.<name>] for custom arguments.
    """

    name = "generic"
    display_name = "Generic CLI"

    _SHELL_PROMPT_PATTERN = re.compile(
        r"(\$|\#|>)\s*$",
        re.MULTILINE,
    )

    _ERROR_PATTERN = re.compile(
        r"(Error:|error:|FAILED|fatal:|Exception|command not found|No such file)",
        re.IGNORECASE,
    )

    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Launch the tool by name with the prompt as argument.

        For generic tools, the tool field is used as the binary name.
        """
        return [task.tool, task.prompt]

    def detect_state(self, output: str) -> AgentState | None:
        """Generic adapter has no state detection."""
        return None

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Generic adapter does not auto-confirm."""
        return None

    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect completion via shell prompt return or errors.

        Primarily relies on the monitor's session_exists check.
        """
        if self._ERROR_PATTERN.search(output):
            return AgentStatus.FAILED

        recent = output[-500:] if len(output) > 500 else output
        if self._SHELL_PROMPT_PATTERN.search(recent) and len(output) > 50:
            return AgentStatus.COMPLETED

        return None

    def get_startup_wait(self) -> float:
        return 0.0

    def needs_prompt_after_launch(self) -> bool:
        return False
