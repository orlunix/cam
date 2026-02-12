"""Safe shell execution utilities.

SECURITY-CRITICAL MODULE.

Rules:
- NEVER use shell=True
- All commands as list[str]
- Use shlex.quote() when constructing strings that will be interpreted by a shell
"""

import asyncio
import shlex
import shutil
import subprocess
from pathlib import Path


async def run_async(
    cmd: list[str],
    timeout: float | None = None,
    cwd: str | None = None,
    capture: bool = True,
) -> tuple[int, str, str]:
    """Run a command asynchronously.

    Args:
        cmd: Command as list of strings (NEVER a single string)
        timeout: Optional timeout in seconds
        cwd: Working directory
        capture: If True, capture stdout/stderr; if False, inherit from parent

    Returns:
        Tuple of (returncode, stdout, stderr)

    Raises:
        TypeError: If cmd is not a list
        asyncio.TimeoutError: If timeout is exceeded
    """
    if not isinstance(cmd, list):
        raise TypeError(f"cmd must be a list[str], got {type(cmd).__name__}")

    if not cmd:
        raise ValueError("cmd cannot be empty")

    stdout_mode = asyncio.subprocess.PIPE if capture else None
    stderr_mode = asyncio.subprocess.PIPE if capture else None

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=stdout_mode,
        stderr=stderr_mode,
        cwd=cwd,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        return proc.returncode or 0, stdout, stderr
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise


def run_sync(
    cmd: list[str],
    timeout: float | None = None,
    cwd: str | None = None,
    capture: bool = True,
) -> tuple[int, str, str]:
    """Run a command synchronously.

    Args:
        cmd: Command as list of strings (NEVER a single string)
        timeout: Optional timeout in seconds
        cwd: Working directory
        capture: If True, capture stdout/stderr; if False, inherit from parent

    Returns:
        Tuple of (returncode, stdout, stderr)

    Raises:
        TypeError: If cmd is not a list
        subprocess.TimeoutExpired: If timeout is exceeded
    """
    if not isinstance(cmd, list):
        raise TypeError(f"cmd must be a list[str], got {type(cmd).__name__}")

    if not cmd:
        raise ValueError("cmd cannot be empty")

    stdout_mode = subprocess.PIPE if capture else None
    stderr_mode = subprocess.PIPE if capture else None

    result = subprocess.run(
        cmd,
        stdout=stdout_mode,
        stderr=stderr_mode,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )

    stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""

    return result.returncode, stdout, stderr


def tmux_new_session(socket: str, session: str, workdir: str) -> list[str]:
    """Build command to create a new TMUX session.

    Args:
        socket: Path to TMUX socket
        session: Session name
        workdir: Working directory for the session

    Returns:
        Command as list[str]
    """
    return ["tmux", "-S", socket, "new-session", "-d", "-s", session, "-c", workdir]


def tmux_send_literal(socket: str, session: str, text: str) -> list[str]:
    """Build command to send literal text to TMUX (no special key interpretation).

    Args:
        socket: Path to TMUX socket
        session: Session name
        text: Text to send (will not be interpreted as keys)

    Returns:
        Command as list[str]
    """
    return ["tmux", "-S", socket, "send-keys", "-t", f"{session}:0.0", "-l", "--", text]


def tmux_send_enter(socket: str, session: str) -> list[str]:
    """Build command to send Enter key.

    Args:
        socket: Path to TMUX socket
        session: Session name

    Returns:
        Command as list[str]
    """
    return ["tmux", "-S", socket, "send-keys", "-t", f"{session}:0.0", "Enter"]


def tmux_capture_pane(socket: str, session: str, lines: int = 50) -> list[str]:
    """Build command to capture TMUX pane content.

    Args:
        socket: Path to TMUX socket
        session: Session name
        lines: Number of lines to capture from history

    Returns:
        Command as list[str]
    """
    return [
        "tmux",
        "-S",
        socket,
        "capture-pane",
        "-p",
        "-J",
        "-t",
        f"{session}:0.0",
        "-S",
        f"-{lines}",
    ]


def tmux_has_session(socket: str, session: str) -> list[str]:
    """Build command to check if TMUX session exists.

    Args:
        socket: Path to TMUX socket
        session: Session name

    Returns:
        Command as list[str] (exits 0 if exists, non-zero otherwise)
    """
    return ["tmux", "-S", socket, "has-session", "-t", session]


def tmux_kill_session(socket: str, session: str) -> list[str]:
    """Build command to kill a TMUX session.

    Args:
        socket: Path to TMUX socket
        session: Session name

    Returns:
        Command as list[str]
    """
    return ["tmux", "-S", socket, "kill-session", "-t", session]


def which(binary: str) -> str | None:
    """Find binary in PATH.

    Args:
        binary: Name of binary to find

    Returns:
        Full path to binary, or None if not found
    """
    return shutil.which(binary)
