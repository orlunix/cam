"""Dependency checker for `cam doctor`."""

import subprocess
import sys

from cam.utils.shell import which


class DoctorCheck:
    """Result of a single dependency check.

    Attributes:
        name: Human-readable name of the check
        status: True if check passed, False otherwise
        message: Detailed message about the check result
        required: If True, this dependency is required for CAM to work
    """

    def __init__(self, name: str, status: bool, message: str, required: bool = True):
        self.name = name
        self.status = status
        self.message = message
        self.required = required

    def __repr__(self):
        status_str = "OK" if self.status else "FAIL"
        req_str = " (required)" if self.required else " (optional)"
        return f"<DoctorCheck {self.name}: {status_str}{req_str} - {self.message}>"


def check_all() -> list[DoctorCheck]:
    """Run all dependency checks.

    Returns:
        List of DoctorCheck results
    """
    checks = []

    # Required dependencies
    checks.append(_check_python())
    checks.append(_check_tmux())

    # Optional dependencies
    checks.append(_check_ssh())
    checks.append(_check_docker())

    # Coding tools (all optional)
    checks.append(_check_tool("claude", "Claude Code"))
    checks.append(_check_tool("codex", "OpenAI Codex"))
    checks.append(_check_tool("aider", "Aider"))

    return checks


def _check_python() -> DoctorCheck:
    """Check Python version."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 11)
    message = f"Python {version}"
    if not ok:
        message += " (3.11+ required)"
    return DoctorCheck("Python", ok, message, required=True)


def _check_tmux() -> DoctorCheck:
    """Check tmux availability and version."""
    path = which("tmux")
    if path:
        # Get version
        try:
            result = subprocess.run(
                ["tmux", "-V"], capture_output=True, text=True, timeout=5
            )
            version = result.stdout.strip()
            return DoctorCheck("tmux", True, version, required=True)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return DoctorCheck(
                "tmux", False, "Found but cannot execute", required=True
            )
    return DoctorCheck(
        "tmux", False, "Not found (required for local execution)", required=True
    )


def _check_ssh() -> DoctorCheck:
    """Check SSH client availability."""
    path = which("ssh")
    message = path if path else "Not found"
    return DoctorCheck("SSH", bool(path), message, required=False)


def _check_docker() -> DoctorCheck:
    """Check Docker availability."""
    path = which("docker")
    message = path if path else "Not found"
    return DoctorCheck("Docker", bool(path), message, required=False)


def _check_tool(binary: str, display_name: str) -> DoctorCheck:
    """Check for a coding tool binary.

    Args:
        binary: Binary name to search for
        display_name: Human-readable name for display

    Returns:
        DoctorCheck result
    """
    path = which(binary)
    message = path if path else "Not installed"
    return DoctorCheck(display_name, bool(path), message, required=False)
