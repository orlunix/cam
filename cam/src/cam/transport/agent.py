"""Agent transport implementation for CAM.

Communicates with a remote `cam-agent` binary over SSH. Each Transport method
maps to a single `cam-agent <subcommand>` invocation tunneled through an SSH
ControlMaster connection pool for low latency.

This replaces the ad-hoc tmux/bash command assembly in SSHTransport with a
standardized protocol — cam-agent handles platform differences internally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import time
from pathlib import Path

from cam.transport.base import Transport

logger = logging.getLogger(__name__)


class AgentTransport(Transport):
    """Transport that delegates to a remote cam-agent binary via SSH.

    The remote machine must have ``cam-agent`` on its PATH.
    SSH ControlMaster connection pooling is used for reduced latency.

    Args:
        host: Remote hostname or IP address.
        user: SSH username (defaults to current user).
        port: SSH port (defaults to 22).
        key_file: Path to SSH private key file (optional).
        agent_bin: Remote cam-agent binary path (default: "cam-agent").
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        port: int | None = None,
        key_file: str | None = None,
        agent_bin: str = "cam-agent",
        env_setup: str | None = None,
    ) -> None:
        if not host:
            raise ValueError("Agent transport requires a host")
        self._host = host
        self._user = user
        self._port = port or 22
        self._key_file = key_file
        self._agent_bin = agent_bin
        self._env_setup = env_setup

        # ControlMaster socket — same short-path strategy as SSHTransport
        conn_key = f"{self._user or 'default'}@{self._host}:{self._port}"
        conn_hash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
        self._control_path = Path(f"/tmp/cam-ssh-{conn_hash}")

    # -----------------------------------------------------------------
    # SSH execution
    # -----------------------------------------------------------------

    def _ssh_base_args(self) -> list[str]:
        """Build base SSH command with ControlMaster options."""
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

    async def _run_agent(
        self,
        agent_args: list[str],
        *,
        check: bool = True,
        timeout: float = 30,
        stdin_data: bytes | None = None,
    ) -> tuple[bool, str]:
        """Execute ``cam-agent <args>`` on the remote host via SSH.

        Returns:
            Tuple of (success, stdout_text).
        """
        # Build the remote command string
        remote_cmd = " ".join(
            [shlex.quote(self._agent_bin)] + [shlex.quote(a) for a in agent_args]
        )
        ssh_args = self._ssh_base_args() + ["--", remote_cmd]
        logger.debug("AGENT SSH: %s", " ".join(shlex.quote(a) for a in ssh_args))

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=timeout
            )

            success = proc.returncode == 0
            output = stdout.decode("utf-8", errors="replace")

            if not success and check:
                error = stderr.decode("utf-8", errors="replace")
                logger.warning(
                    "cam-agent command failed (exit %d): %s",
                    proc.returncode,
                    error.strip(),
                )
                return False, error

            return success, output

        except asyncio.TimeoutError:
            logger.error("cam-agent command timed out: %s", agent_args[:3])
            return False, "cam-agent command timed out"
        except Exception as e:
            logger.error("cam-agent execution failed: %s", e)
            return False, str(e)

    async def _run_agent_json(
        self, agent_args: list[str], **kwargs: object
    ) -> tuple[bool, dict]:
        """Execute cam-agent and parse JSON response.

        Returns:
            Tuple of (success, parsed_dict).
        """
        ok, output = await self._run_agent(agent_args, **kwargs)
        if not ok:
            return False, {}
        try:
            return True, json.loads(output)
        except json.JSONDecodeError:
            logger.warning("cam-agent returned non-JSON: %s", output[:200])
            return False, {}

    # -----------------------------------------------------------------
    # Session operations
    # -----------------------------------------------------------------

    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        args = ["session", "create", "--id", session_id, "--workdir", workdir]
        if self._env_setup:
            args.extend(["--env-setup", self._env_setup])
        args.append("--")
        args.extend(command)
        ok, data = await self._run_agent_json(args, timeout=60)
        if not ok:
            raise RuntimeError(
                f"cam-agent session create failed on {self._host}: {data}"
            )
        return True

    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        args = ["session", "send", "--id", session_id, "--text", text]
        if not send_enter:
            args.append("--no-enter")
        ok, _ = await self._run_agent_json(args)
        return ok

    async def send_key(self, session_id: str, key: str) -> bool:
        args = ["session", "key", "--id", session_id, "--key", key]
        ok, _ = await self._run_agent_json(args)
        return ok

    async def capture_output(self, session_id: str, lines: int = 100) -> str:
        """Capture output — returns plain text (no JSON parsing on hot path)."""
        args = ["session", "capture", "--id", session_id, "--lines", str(lines)]
        ok, output = await self._run_agent(args, check=False)
        if not ok:
            logger.debug("cam-agent capture failed for session %s", session_id)
            return ""
        return output.rstrip()

    async def session_exists(self, session_id: str) -> bool:
        """Check if session is alive — uses exit code (0=alive, 1=dead)."""
        args = ["session", "exists", "--id", session_id]
        ok, _ = await self._run_agent(args, check=False)
        return ok

    async def kill_session(self, session_id: str) -> bool:
        args = ["session", "kill", "--id", session_id]
        ok, _ = await self._run_agent_json(args, check=False)
        if ok:
            logger.info("Killed remote session %s on %s", session_id, self._host)
        return ok

    # -----------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------

    async def start_logging(self, session_id: str, log_path: str = "") -> bool:
        """Start logging session output on the remote host."""
        args = ["session", "log-start", "--id", session_id]
        ok, data = await self._run_agent_json(args)
        if ok:
            remote_path = data.get("path", "")
            logger.info("Logging started for %s: %s", session_id, remote_path)
        return ok

    async def read_output_log(
        self, session_id: str, offset: int = 0, max_bytes: int = 256_000
    ) -> tuple[str, int]:
        args = [
            "session", "log-read",
            "--id", session_id,
            "--offset", str(offset),
            "--max-bytes", str(max_bytes),
        ]
        ok, output = await self._run_agent(args, check=False)
        if not ok or not output:
            return "", offset

        # First line is "offset:<N>", rest is content
        lines = output.split("\n", 1)
        if lines[0].startswith("offset:"):
            try:
                new_offset = int(lines[0].split(":", 1)[1])
            except ValueError:
                new_offset = offset
            content = lines[1] if len(lines) > 1 else ""
        else:
            content = output
            new_offset = offset + len(content.encode("utf-8", errors="replace"))

        return content, new_offset

    # -----------------------------------------------------------------
    # File operations
    # -----------------------------------------------------------------

    async def list_files(self, path: str) -> list[dict]:
        args = ["file", "list", "--path", path]
        ok, data = await self._run_agent_json(args, check=False)
        if not ok:
            return []
        return data.get("entries", [])

    async def read_file(self, path: str, max_bytes: int = 512_000) -> bytes | None:
        """Read file — cam-agent returns raw bytes to stdout."""
        args = ["file", "read", "--path", path, "--max-bytes", str(max_bytes)]
        # Use raw SSH for binary data
        remote_cmd = " ".join(
            [shlex.quote(self._agent_bin)] + [shlex.quote(a) for a in args]
        )
        ssh_args = self._ssh_base_args() + ["--", remote_cmd]

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                return None
            return stdout
        except (asyncio.TimeoutError, Exception):
            return None

    async def write_file(self, remote_path: str, data: bytes) -> bool:
        """Write file — pipe data to cam-agent's stdin."""
        args = ["file", "write", "--path", remote_path]
        ok, _ = await self._run_agent_json(args, stdin_data=data, timeout=60)
        if ok:
            logger.info("Wrote %d bytes to %s on %s", len(data), remote_path, self._host)
        return ok

    # -----------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------

    async def test_connection(self) -> tuple[bool, str]:
        ok, data = await self._run_agent_json(["ping"])
        if not ok:
            return False, f"Cannot reach cam-agent on {self._user or ''}@{self._host}:{self._port}"
        version = data.get("version", "?")
        platform = data.get("platform", "?")
        return True, f"cam-agent {version} on {self._host} ({platform})"

    async def get_latency(self) -> float:
        start = time.monotonic()
        await self._run_agent(["ping"], check=False)
        elapsed = (time.monotonic() - start) * 1000
        return round(elapsed, 1)

    def get_attach_command(self, session_id: str) -> str:
        """Return SSH command to attach to the remote session.

        cam-agent doesn't expose tmux attach directly, so we build the
        equivalent tmux attach command for the user.
        """
        socket = f"/tmp/cam-agent-sockets/{session_id}.sock"

        ssh_parts = ["ssh"]
        if self._port != 22:
            ssh_parts.extend(["-p", str(self._port)])
        if self._key_file:
            ssh_parts.extend(["-i", self._key_file])
        ssh_parts.append("-t")
        if self._user:
            ssh_parts.append(f"{self._user}@{self._host}")
        else:
            ssh_parts.append(self._host)

        ssh_parts.append(
            f"tmux -u -S {shlex.quote(socket)} attach -t {shlex.quote(session_id)}"
        )
        return " ".join(
            shlex.quote(p) if " " in p else p for p in ssh_parts
        )
