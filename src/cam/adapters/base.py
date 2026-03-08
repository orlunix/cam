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


class ProbeAction(NamedTuple):
    """Describes what to send when probing a session.

    Attributes:
        char: Character to send (empty string = Enter-only mode).
        send_enter: Whether to send Enter with the probe.
        is_confirm: True if this probe doubles as a confirmation attempt.
    """

    char: str
    send_enter: bool = False
    is_confirm: bool = False


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

    def is_ready_for_input(self, output: str) -> bool:
        """Check if the tool's TUI is ready to accept the task prompt.

        Used during startup to avoid sending the prompt before the tool's
        input field is active. Override in subclasses to check for tool-specific
        readiness indicators (e.g. a prompt character like '❯').

        Args:
            output: Recent TMUX output

        Returns:
            True if the tool is ready for input, False otherwise.
            Default returns True (assume ready).
        """
        return True

    def get_startup_wait(self) -> float:
        """Maximum seconds to wait for tool readiness before sending the prompt.

        Returns:
            Max wait time in seconds (default 2.0)
        """
        return 2.0

    def needs_prompt_after_launch(self) -> bool:
        """Check if task prompt needs to be sent as input after tool launches.

        Returns:
            True for interactive tools (prompt sent via stdin),
            False if prompt is included in launch command
        """
        return False

    def get_probe_action(self, auto_confirm: bool) -> ProbeAction:
        """Return the probe action based on auto-confirm state.

        When auto-confirm is on, the probe can double as a fallback
        confirmation (e.g. sending Enter). When off, sends a neutral
        character that won't affect the agent.
        """
        return ProbeAction(char="Z", send_enter=False, is_confirm=False)

    def get_confirm_cooldown(self) -> float:
        """Seconds between auto-confirm attempts."""
        return 5.0

    def get_confirm_sleep(self) -> float:
        """Seconds to sleep after sending a confirmation."""
        return 0.5

    def get_completion_stable(self) -> float:
        """Seconds of idle output before checking completion."""
        return 3.0

    def get_probe_wait(self) -> float:
        """Seconds to wait after probe before recapturing."""
        return 0.3

    def get_probe_idle_threshold(self) -> int:
        """Consecutive COMPLETED probes needed to confirm idle."""
        return 2

    def get_auto_exit(self) -> bool:
        """Whether to auto-exit when agent completes and is confirmed idle."""
        return False

    def get_exit_action(self) -> str:
        """How to exit: 'kill_session', 'send_exit', or 'mark_only'."""
        return "kill_session"

    def get_exit_command(self) -> str:
        """Command to send if exit_action is 'send_exit'."""
        return "/exit"

    def to_dict(self) -> dict:
        """Return adapter config as a dict for serialization to cam-client."""
        return {}
