"""Abstract base class for CAM transport layer.

All transports execute commands through TMUX sessions for consistency and observability.
Security-first: commands are always list[str], never raw shell strings.
"""

from __future__ import annotations
from abc import ABC, abstractmethod


class Transport(ABC):
    """Abstract base for execution backends. All session operations go through TMUX."""

    @abstractmethod
    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        """Create a new TMUX session and run the command inside it.

        Args:
            session_id: Unique identifier for the TMUX session
            command: Command to execute as a list of arguments (shell-injection safe)
            workdir: Working directory for the command

        Returns:
            True if session was created successfully, False otherwise
        """
        ...

    @abstractmethod
    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        """Send text input to a running TMUX session. Uses literal send-keys.

        Args:
            session_id: Target TMUX session identifier
            text: Text to send (will be sent literally, not interpreted as shell)
            send_enter: Whether to send Enter key after the text

        Returns:
            True if input was sent successfully, False otherwise
        """
        ...

    @abstractmethod
    async def send_key(self, session_id: str, key: str) -> bool:
        """Send a tmux special key to a TMUX session (without literal mode).

        Unlike send_input which uses the -l flag for literal text, this sends
        tmux key names like 'BSpace', 'Enter', 'Escape', etc.

        Args:
            session_id: Target TMUX session identifier
            key: Tmux key name (e.g. 'BSpace', 'Enter', 'Escape')

        Returns:
            True if key was sent successfully, False otherwise
        """
        ...

    @abstractmethod
    async def capture_output(self, session_id: str, lines: int = 50) -> str:
        """Capture the last N lines of TMUX pane output.

        Args:
            session_id: Target TMUX session identifier
            lines: Number of lines to capture from the end

        Returns:
            Captured output as a string
        """
        ...

    @abstractmethod
    async def session_exists(self, session_id: str) -> bool:
        """Check if a TMUX session is still alive.

        Args:
            session_id: TMUX session identifier to check

        Returns:
            True if session exists and is active, False otherwise
        """
        ...

    @abstractmethod
    async def kill_session(self, session_id: str) -> bool:
        """Terminate a TMUX session.

        Args:
            session_id: TMUX session identifier to kill

        Returns:
            True if session was terminated successfully, False otherwise
        """
        ...

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Test that this transport can connect.

        Returns:
            Tuple of (success: bool, message: str) describing connection status
        """
        ...

    @abstractmethod
    async def get_latency(self) -> float:
        """Measure round-trip latency in milliseconds.

        Returns:
            Latency in milliseconds
        """
        ...

    async def read_output_log(self, session_id: str, offset: int = 0, max_bytes: int = 256_000) -> tuple[str, int]:
        """Read the pipe-pane output log for incremental fetching.

        Args:
            session_id: TMUX session identifier
            offset: Byte offset to start reading from
            max_bytes: Maximum bytes to read per call

        Returns:
            Tuple of (output_data, next_offset). Default returns empty.
        """
        return "", offset

    @abstractmethod
    def get_attach_command(self, session_id: str) -> str:
        """Return the shell command for a user to attach interactively.

        Args:
            session_id: TMUX session identifier to attach to

        Returns:
            Shell command string that user can run to attach to session
        """
        ...
