"""Base adapter interface for AI coding tools.

This module defines the ToolAdapter abstract base class, which provides
a pluggable interface for integrating different AI coding tools into CAM.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NamedTuple

from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition


class ConfirmAction(NamedTuple):
    """Describes how to respond to an auto-confirm prompt.

    Attributes:
        response: Text to send (e.g. "1", "y", "").
        send_enter: Whether to send Enter after the response.
    """

    response: str
    send_enter: bool = True


class ToolAdapter(ABC):
    """Base class for coding tool adapters.

    Each adapter implements tool-specific behavior for launching,
    monitoring, and interacting with an AI coding agent.

    Attributes:
        name: Machine-readable identifier (e.g. "claude")
        display_name: Human-readable name (e.g. "Claude Code")
    """

    name: str
    display_name: str

    @abstractmethod
    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Return the command to launch this tool.

        Args:
            task: The task definition containing the prompt and configuration
            context: The context containing working directory and environment

        Returns:
            Command as a list of strings for safe execution (e.g. ["claude", "-p", "task"])
        """
        ...

    @abstractmethod
    def detect_state(self, output: str) -> AgentState | None:
        """Analyze recent TMUX output and detect the current agent state.

        Args:
            output: Recent output from the TMUX session

        Returns:
            New AgentState if state change detected, None otherwise
        """
        ...

    @abstractmethod
    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Check if output contains a prompt that should be auto-confirmed.

        Args:
            output: Recent output from the TMUX session

        Returns:
            ConfirmAction with response text and send_enter flag,
            or None if no auto-confirm needed.
        """
        ...

    @abstractmethod
    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect if the agent has completed its work.

        Args:
            output: Recent output from the TMUX session

        Returns:
            COMPLETED or FAILED status if completion detected, None if still running
        """
        ...

    def estimate_cost(self, output: str) -> float | None:
        """Estimate cost from output (e.g. based on token usage).

        Args:
            output: Output containing potential cost information

        Returns:
            Estimated cost in USD, or None if unable to estimate
        """
        return None

    def parse_files_changed(self, output: str) -> list[str]:
        """Extract modified file paths from output.

        Args:
            output: Output containing file change information

        Returns:
            List of absolute file paths that were modified
        """
        return []

    def get_startup_wait(self) -> float:
        """Get seconds to wait after launch before sending the task prompt.

        Returns:
            Wait time in seconds (default 2.0)
        """
        return 2.0

    def needs_prompt_after_launch(self) -> bool:
        """Check if task prompt needs to be sent as input after tool launches.

        Returns:
            True for interactive tools (prompt sent via stdin),
            False if prompt is included in launch command
        """
        return False
