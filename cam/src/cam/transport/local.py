"""Local transport implementation for CAM.

Executes TMUX sessions on the local machine with per-session Unix sockets.
"""

from __future__ import annotations
import asyncio
import logging
import shlex
import shutil
from pathlib import Path

from cam.constants import SOCKET_DIR
from cam.transport.base import Transport
from cam.utils.ansi import strip_ansi

logger = logging.getLogger(__name__)


class LocalTransport(Transport):
    """Local TMUX-based transport with isolated socket per session."""

    def __init__(self) -> None:
        """Initialize local transport."""
        self._ensure_socket_dir()

    def _ensure_socket_dir(self) -> None:
        """Create socket directory if it doesn't exist."""
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)

    def _get_socket_path(self, session_id: str) -> Path:
        """Get the socket path for a session.

        Args:
            session_id: Session identifier

        Returns:
            Path to the TMUX socket file
        """
        return SOCKET_DIR / f"{session_id}.sock"

    async def _run_tmux(self, args: list[str], socket: Path, check: bool = True) -> tuple[bool, str]:
        """Run a tmux command with the given socket.

        Args:
            args: Command arguments (not including 'tmux')
            socket: Path to TMUX socket
            check: Whether to treat non-zero exit as failure

        Returns:
            Tuple of (success: bool, output: str)
        """
        cmd = ["tmux", "-S", str(socket)] + args
        logger.debug(f"Running: {' '.join(shlex.quote(arg) for arg in cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            success = proc.returncode == 0
            output = stdout.decode("utf-8", errors="replace")

            if not success and check:
                error = stderr.decode("utf-8", errors="replace")
                logger.debug(f"Command failed (exit {proc.returncode}): {error}")
                return False, error

            return success, output

        except Exception as e:
            logger.error(f"Failed to execute tmux command: {e}")
            return False, str(e)

    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        """Create a new TMUX session running the command directly.

        The command is passed as the initial program for the TMUX session
        (via the shell command at the end of ``new-session``). When the
        command exits, the TMUX session terminates automatically — this
        lets the monitor detect completion via ``session_exists()``.

        Args:
            session_id: Unique identifier for the TMUX session
            command: Command to execute as a list of arguments
            workdir: Working directory for the command

        Returns:
            True if session was created successfully
        """
        socket = self._get_socket_path(session_id)

        # Build a safe shell command string for TMUX to execute.
        # When this command exits, the TMUX session exits too.
        command_str = " ".join(shlex.quote(arg) for arg in command)

        # Set remain-on-exit OFF so session dies when process exits
        create_args = [
            "new-session",
            "-d",              # detached
            "-s", session_id,  # session name
            "-c", workdir,     # working directory
            command_str,       # shell command to run (positional arg)
        ]

        success, _ = await self._run_tmux(create_args, socket)
        if not success:
            logger.error(f"Failed to create session {session_id}")
            return False

        logger.info(f"Created session {session_id} in {workdir}: {command_str}")
        return True

    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        """Send text input to a running TMUX session using literal mode.

        Args:
            session_id: Target TMUX session identifier
            text: Text to send (sent literally, not interpreted)
            send_enter: Whether to send Enter key after the text

        Returns:
            True if input was sent successfully
        """
        socket = self._get_socket_path(session_id)
        target = f"{session_id}:0.0"

        # Send text literally with -l flag
        send_args = [
            "send-keys",
            "-t", target,
            "-l",  # literal mode - no special key interpretation
            "--",  # end of options
            text,
        ]

        success, _ = await self._run_tmux(send_args, socket)
        if not success:
            return False

        # Send Enter key separately if requested
        if send_enter:
            enter_args = ["send-keys", "-t", target, "Enter"]
            success, _ = await self._run_tmux(enter_args, socket)

        return success

    async def send_key(self, session_id: str, key: str) -> bool:
        """Send a tmux special key to a TMUX session (without literal mode).

        Args:
            session_id: Target TMUX session identifier
            key: Tmux key name (e.g. 'BSpace', 'Enter', 'Escape')

        Returns:
            True if key was sent successfully, False otherwise
        """
        socket = self._get_socket_path(session_id)
        target = f"{session_id}:0.0"
        success, _ = await self._run_tmux(["send-keys", "-t", target, key], socket)
        return success

    async def capture_output(self, session_id: str, lines: int = 100) -> str:
        """Capture the last N lines of TMUX pane output.

        Strips ANSI escape sequences from the captured output. If the primary
        capture returns mostly empty content (e.g. alternate screen buffer),
        falls back to capturing with -a flag for alternate screen.

        Args:
            session_id: Target TMUX session identifier
            lines: Number of lines to capture from the end

        Returns:
            Captured output with ANSI codes stripped
        """
        socket = self._get_socket_path(session_id)
        target = f"{session_id}:0.0"

        capture_args = [
            "capture-pane",
            "-p",  # print to stdout
            "-J",  # join wrapped lines
            "-t", target,
            "-S", f"-{lines}",  # start from N lines back
        ]

        success, output = await self._run_tmux(capture_args, socket, check=False)
        if not success:
            # Expected when session has exited — use debug to avoid console spam
            logger.debug(f"Failed to capture output from {session_id}")
            return ""

        # If primary capture is near-empty, try alternate screen
        if len(output.strip()) < 20:
            alt_args = [
                "capture-pane",
                "-p",  # print to stdout
                "-J",  # join wrapped lines
                "-a",  # capture alternate screen
                "-t", target,
                "-S", f"-{lines}",
            ]
            alt_success, alt_output = await self._run_tmux(alt_args, socket, check=False)
            if alt_success and len(alt_output.strip()) > len(output.strip()):
                output = alt_output

        return strip_ansi(output)

    async def session_exists(self, session_id: str) -> bool:
        """Check if a TMUX session is still alive.

        Args:
            session_id: TMUX session identifier to check

        Returns:
            True if session exists and is active
        """
        socket = self._get_socket_path(session_id)

        # has-session returns 0 if session exists, non-zero otherwise
        has_args = ["has-session", "-t", session_id]
        success, _ = await self._run_tmux(has_args, socket, check=False)

        return success

    async def kill_session(self, session_id: str) -> bool:
        """Terminate a TMUX session.

        Args:
            session_id: TMUX session identifier to kill

        Returns:
            True if session was terminated successfully
        """
        socket = self._get_socket_path(session_id)

        kill_args = ["kill-session", "-t", session_id]
        success, _ = await self._run_tmux(kill_args, socket, check=False)

        # Clean up socket file
        try:
            socket.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to remove socket {socket}: {e}")

        if success:
            logger.info(f"Killed session {session_id}")

        return success

    async def test_connection(self) -> tuple[bool, str]:
        """Test that tmux binary is available.

        Returns:
            Tuple of (success, message)
        """
        tmux_path = shutil.which("tmux")

        if tmux_path is None:
            return False, "tmux binary not found in PATH"

        # Try to get tmux version
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "-V",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            version = stdout.decode("utf-8", errors="replace").strip()
            return True, f"Local transport ready: {version} at {tmux_path}"
        except Exception as e:
            return False, f"Failed to execute tmux: {e}"

    async def get_latency(self) -> float:
        """Measure round-trip latency. Local is always 0ms.

        Returns:
            Latency in milliseconds (always 0.0 for local)
        """
        return 0.0

    def get_attach_command(self, session_id: str) -> str:
        """Return command to attach to a session interactively.

        Args:
            session_id: TMUX session identifier to attach to

        Returns:
            Shell command string for attaching
        """
        socket = self._get_socket_path(session_id)
        return f"tmux -S {shlex.quote(str(socket))} attach -t {shlex.quote(session_id)}"
