"""SSH transport implementation for CAM.

Executes TMUX sessions on remote machines via SSH with ControlMaster
connection pooling for reduced latency on repeated operations.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shlex
import time
from pathlib import Path

from cam.constants import SOCKET_DIR
from cam.transport.base import Transport

logger = logging.getLogger(__name__)


class SSHTransport(Transport):
    """SSH-based transport with ControlMaster connection pooling.

    All TMUX operations are tunneled through SSH. A persistent ControlMaster
    connection is used to avoid re-authenticating on every command.

    Args:
        host: Remote hostname or IP address.
        user: SSH username (defaults to current user).
        port: SSH port (defaults to 22).
        key_file: Path to SSH private key file (optional).
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        port: int | None = None,
        key_file: str | None = None,
        env_setup: str | None = None,
    ) -> None:
        if not host:
            raise ValueError("SSH transport requires a host")
        self._host = host
        self._user = user
        self._port = port or 22
        self._key_file = key_file
        self._env_setup = env_setup

        # ControlMaster socket path — kept short to avoid exceeding the
        # 108-char Unix socket limit (SSH appends a ~25-char random suffix).
        # Use /tmp with a hash of connection details for a stable, short path.
        conn_key = f"{self._user or 'default'}@{self._host}:{self._port}"
        conn_hash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
        self._control_path = Path(f"/tmp/cam-ssh-{conn_hash}")

    def _ssh_base_args(self) -> list[str]:
        """Build base SSH command arguments with ControlMaster options."""
        args = [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=600",
        ]
        if self._port != 22:
            args.extend(["-p", str(self._port)])
        if self._key_file:
            args.extend(["-i", self._key_file])
        if self._user:
            args.append(f"{self._user}@{self._host}")
        else:
            args.append(self._host)
        return args

    async def _run_ssh(self, remote_cmd: str, check: bool = True) -> tuple[bool, str]:
        """Execute a command on the remote host via SSH.

        Args:
            remote_cmd: Command string to execute on the remote host.
            check: Whether to treat non-zero exit as failure.

        Returns:
            Tuple of (success, output).
        """
        ssh_args = self._ssh_base_args() + ["--", remote_cmd]
        logger.debug("SSH: %s", " ".join(shlex.quote(a) for a in ssh_args))

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            success = proc.returncode == 0
            output = stdout.decode("utf-8", errors="replace")

            if not success and check:
                error = stderr.decode("utf-8", errors="replace")
                logger.warning("SSH command failed (exit %d): %s", proc.returncode, error)
                return False, error

            return success, output

        except asyncio.TimeoutError:
            logger.error("SSH command timed out: %s", remote_cmd[:80])
            return False, "SSH command timed out"
        except Exception as e:
            logger.error("SSH execution failed: %s", e)
            return False, str(e)

    def _remote_tmux_cmd(self, session_id: str, tmux_args: list[str]) -> str:
        """Build a remote tmux command string.

        The session socket is stored on the remote host under /tmp/cam-sockets/.

        Args:
            session_id: TMUX session ID.
            tmux_args: Arguments for the tmux command.

        Returns:
            Shell-safe command string for remote execution.
        """
        socket = f"/tmp/cam-sockets/{session_id}.sock"
        parts = ["tmux", "-S", shlex.quote(socket)] + [shlex.quote(a) for a in tmux_args]
        return " ".join(parts)

    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        """Create a TMUX session on the remote host.

        Ensures the remote socket directory exists, then creates a detached
        session with the command as the initial program. When the command
        exits, the TMUX session terminates automatically — matching the
        LocalTransport behavior so the monitor can detect completion via
        session_exists().
        """
        # Ensure remote socket directory exists
        ok, _ = await self._run_ssh("mkdir -p /tmp/cam-sockets", check=False)
        if not ok:
            logger.warning("Could not create remote socket dir (may already exist)")

        # Build shell command string (passed as positional arg to new-session)
        command_str = " ".join(shlex.quote(arg) for arg in command)

        # Wrap with env_setup if configured (e.g. PATH setup for remote tools)
        if self._env_setup:
            command_str = f"bash -c {shlex.quote(self._env_setup + ' && exec ' + command_str)}"

        # Create detached session with command as initial program.
        # Session dies when process exits (same as LocalTransport).
        create_cmd = self._remote_tmux_cmd(session_id, [
            "new-session", "-d", "-s", session_id, "-c", workdir, command_str,
        ])
        success, error = await self._run_ssh(create_cmd)
        if not success:
            logger.error("Failed to create remote session %s: %s", session_id, error)
            raise RuntimeError(f"SSH session creation failed on {self._host}: {error}")

        logger.info("Created remote session %s on %s in %s", session_id, self._host, workdir)
        return True

    async def start_logging(self, session_id: str, log_path: str) -> bool:
        """Pipe all remote TMUX pane output to a log file via pipe-pane."""
        target = f"{session_id}:0.0"
        # Ensure remote log directory exists
        remote_dir = "/tmp/cam-logs"
        await self._run_ssh(f"mkdir -p {remote_dir}", check=False)
        # Use remote path for SSH
        remote_log = f"{remote_dir}/{session_id}.output.log"
        pipe_cmd = self._remote_tmux_cmd(session_id, [
            "pipe-pane", "-t", target,
            f"cat >> {remote_log}",
        ])
        success, _ = await self._run_ssh(pipe_cmd, check=False)
        if success:
            logger.info("Logging remote output to %s for %s", remote_log, session_id)
        else:
            logger.warning("Failed to start pipe-pane for remote session %s", session_id)
        return success

    async def read_output_log(self, session_id: str, offset: int = 0, max_bytes: int = 256_000) -> tuple[str, int]:
        """Read the pipe-pane output log from the remote host."""
        remote_log = f"/tmp/cam-logs/{session_id}.output.log"
        # Use dd to read from offset, limited to max_bytes
        cmd = f"dd if={shlex.quote(remote_log)} bs=1 skip={offset} count={max_bytes} 2>/dev/null"
        success, output = await self._run_ssh(cmd, check=False)
        if not success or not output:
            return "", offset
        next_offset = offset + len(output.encode("utf-8", errors="replace"))
        return output, next_offset

    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        """Send text input to a remote TMUX session."""
        target = f"{session_id}:0.0"

        # Send text literally
        if text:
            # Non-ASCII text (e.g. CJK) gets corrupted by SSH shell
            # interpretation on POSIX-locale remotes. Use base64 encoding
            # to transport the bytes safely and decode on the remote side.
            if text.isascii():
                send_cmd = self._remote_tmux_cmd(session_id, [
                    "send-keys", "-t", target, "-l", "--", text,
                ])
            else:
                import base64
                b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
                socket = f"/tmp/cam-sockets/{session_id}.sock"
                send_cmd = (
                    f"bash -c 'tmux -S {shlex.quote(socket)}"
                    f" send-keys -t {shlex.quote(target)}"
                    f" -l -- \"$(echo {b64} | base64 -d)\"'"
                )
            success, _ = await self._run_ssh(send_cmd)
            if not success:
                return False

        if send_enter:
            enter_cmd = self._remote_tmux_cmd(session_id, [
                "send-keys", "-t", target, "Enter",
            ])
            success, _ = await self._run_ssh(enter_cmd)

        return success

    async def send_key(self, session_id: str, key: str) -> bool:
        """Send a tmux special key to a remote TMUX session (without literal mode)."""
        cmd = self._remote_tmux_cmd(session_id, [
            "send-keys", "-t", f"{session_id}:0.0", key,
        ])
        success, _ = await self._run_ssh(cmd)
        return success

    async def capture_output(self, session_id: str, lines: int = 50) -> str:
        """Capture output from a remote TMUX session.

        If the primary capture returns near-empty content (e.g. during
        Claude's alternate screen buffer), falls back to the -a flag.
        """
        target = f"{session_id}:0.0"
        capture_cmd = self._remote_tmux_cmd(session_id, [
            "capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}",
        ])
        success, output = await self._run_ssh(capture_cmd, check=False)
        if not success:
            logger.debug("Failed to capture output from remote session %s", session_id)
            return ""

        # If primary capture is near-empty, try alternate screen
        if len(output.strip()) < 20:
            alt_cmd = self._remote_tmux_cmd(session_id, [
                "capture-pane", "-p", "-J", "-a", "-t", target, "-S", f"-{lines}",
            ])
            alt_success, alt_output = await self._run_ssh(alt_cmd, check=False)
            if alt_success and len(alt_output.strip()) > len(output.strip()):
                output = alt_output

        return output

    async def session_exists(self, session_id: str) -> bool:
        """Check if a remote TMUX session is alive."""
        has_cmd = self._remote_tmux_cmd(session_id, [
            "has-session", "-t", session_id,
        ])
        success, _ = await self._run_ssh(has_cmd, check=False)
        return success

    async def kill_session(self, session_id: str) -> bool:
        """Kill a remote TMUX session and clean up socket."""
        kill_cmd = self._remote_tmux_cmd(session_id, [
            "kill-session", "-t", session_id,
        ])
        success, _ = await self._run_ssh(kill_cmd, check=False)

        # Clean up remote socket
        socket = f"/tmp/cam-sockets/{session_id}.sock"
        await self._run_ssh(f"rm -f {shlex.quote(socket)}", check=False)

        if success:
            logger.info("Killed remote session %s on %s", session_id, self._host)
        return success

    async def test_connection(self) -> tuple[bool, str]:
        """Test SSH connectivity and verify tmux is available on remote."""
        # Test basic SSH connectivity
        success, output = await self._run_ssh("echo ok && tmux -V", check=False)
        if not success:
            return False, f"Cannot connect to {self._user or ''}@{self._host}:{self._port}"

        lines = output.strip().splitlines()
        if len(lines) >= 2 and lines[0].strip() == "ok":
            tmux_version = lines[1].strip()
            return True, f"SSH connected to {self._host}: {tmux_version}"
        elif lines and lines[0].strip() == "ok":
            return False, f"SSH connected to {self._host} but tmux not found"
        else:
            return False, f"Unexpected response from {self._host}: {output[:100]}"

    async def get_latency(self) -> float:
        """Measure SSH round-trip latency in milliseconds."""
        start = time.monotonic()
        await self._run_ssh("true", check=False)
        elapsed = (time.monotonic() - start) * 1000
        return round(elapsed, 1)

    def get_attach_command(self, session_id: str) -> str:
        """Return command for user to attach to a remote TMUX session."""
        socket = f"/tmp/cam-sockets/{session_id}.sock"

        ssh_parts = ["ssh"]
        if self._port != 22:
            ssh_parts.extend(["-p", str(self._port)])
        if self._key_file:
            ssh_parts.extend(["-i", self._key_file])
        ssh_parts.extend([
            "-t",  # Force pseudo-terminal for interactive tmux
        ])
        if self._user:
            ssh_parts.append(f"{self._user}@{self._host}")
        else:
            ssh_parts.append(self._host)

        ssh_parts.append(f"tmux -S {shlex.quote(socket)} attach -t {shlex.quote(session_id)}")
        return " ".join(shlex.quote(p) if " " in p else p for p in ssh_parts)
