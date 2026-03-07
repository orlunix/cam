"""Client transport implementation for CAM.

Communicates with a remote ``cam-client.py`` script over SSH. Each Transport
method maps to a single ``cam-client.py <subcommand>`` invocation tunneled
through an SSH ControlMaster connection pool for low latency.

Unlike SSHTransport (which sends raw tmux commands), ClientTransport delegates
all OS-specific details to cam-client.py running on the target machine.
cam-client.py handles tmux session management, auto-confirm, state detection,
and completion detection locally.
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


class ClientTransport(Transport):
    """Transport that delegates to a remote cam-client.py script via SSH.

    The remote machine must have ``~/.cam/cam-client.py`` deployed.
    SSH ControlMaster connection pooling is used for reduced latency.

    Args:
        host: Remote hostname or IP address.
        user: SSH username (defaults to current user).
        port: SSH port (defaults to 22).
        key_file: Path to SSH private key file (optional).
        client_script: Remote cam-client.py path (default: ~/.cam/cam-client.py).
        env_setup: Shell commands to run before agent on the target.
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        port: int | None = None,
        key_file: str | None = None,
        client_script: str = "~/.cam/cam-client.py",
        env_setup: str | None = None,
    ) -> None:
        if not host:
            raise ValueError("Client transport requires a host")
        self._host = host
        self._user = user
        self._port = port or 22
        self._key_file = key_file
        self._client_script = client_script
        self._env_setup = env_setup

        # ControlMaster socket — same short-path strategy as SSHTransport
        conn_key = f"{self._user or 'default'}@{self._host}:{self._port}"
        conn_hash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
        self._control_path = Path(f"/tmp/cam-ssh-{conn_hash}")

        # Hash-gated capture cache
        self._capture_hashes: dict[str, str] = {}
        self._capture_cache: dict[str, str] = {}

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

    async def _run_client(
        self,
        client_args: list[str],
        *,
        check: bool = True,
        timeout: float = 30,
        stdin_data: bytes | None = None,
    ) -> tuple[bool, str]:
        """Execute ``python3 cam-client.py <args>`` on the remote host via SSH.

        Returns:
            Tuple of (success, stdout_text).
        """
        remote_cmd = "python3 " + " ".join(
            [shlex.quote(self._client_script)] + [shlex.quote(a) for a in client_args]
        )
        ssh_args = self._ssh_base_args() + ["--", remote_cmd]
        logger.debug("CLIENT SSH: %s", " ".join(shlex.quote(a) for a in ssh_args))

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
                    "cam-client command failed (exit %d): %s",
                    proc.returncode,
                    error.strip(),
                )
                return False, error

            return success, output

        except asyncio.TimeoutError:
            logger.error("cam-client command timed out: %s", client_args[:3])
            return False, "cam-client command timed out"
        except Exception as e:
            logger.error("cam-client execution failed: %s", e)
            return False, str(e)

    async def _run_client_json(
        self, client_args: list[str], **kwargs: object
    ) -> tuple[bool, dict]:
        """Execute cam-client.py and parse JSON response.

        Returns:
            Tuple of (success, parsed_dict).
        """
        ok, output = await self._run_client(client_args, **kwargs)
        if not ok:
            return False, {}
        try:
            return True, json.loads(output)
        except json.JSONDecodeError:
            logger.warning("cam-client returned non-JSON: %s", output[:200])
            return False, {}

    # Also expose _run_ssh for agent_manager._start_cam_client compatibility
    async def _run_ssh(self, cmd: str, *, check: bool = True, timeout: float = 30) -> tuple[bool, str]:
        """Run a raw SSH command (used by agent_manager for deployment)."""
        ssh_args = self._ssh_base_args() + ["--", cmd]
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            success = proc.returncode == 0
            output = stdout.decode("utf-8", errors="replace")
            if not success and check:
                return False, stderr.decode("utf-8", errors="replace")
            return success, output
        except (asyncio.TimeoutError, Exception) as e:
            return False, str(e)

    # -----------------------------------------------------------------
    # Session operations
    # -----------------------------------------------------------------

    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        args = ["session", "create", "--id", session_id, "--workdir", workdir]
        if self._env_setup:
            args.extend(["--env-setup", self._env_setup])
        args.append("--")
        args.extend(command)
        ok, data = await self._run_client_json(args, timeout=60)
        if not ok:
            raise RuntimeError(
                f"cam-client session create failed on {self._host}: {data}"
            )
        return True

    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        args = ["session", "send", "--id", session_id, "--text", text]
        if not send_enter:
            args.append("--no-enter")
        ok, _ = await self._run_client_json(args)
        return ok

    async def send_key(self, session_id: str, key: str) -> bool:
        args = ["session", "key", "--id", session_id, "--key", key]
        ok, _ = await self._run_client_json(args)
        return ok

    async def capture_output(self, session_id: str, lines: int = 100) -> str:
        """Capture output with hash-gated optimization."""
        args = ["session", "capture", "--id", session_id, "--lines", str(lines)]
        prev_hash = self._capture_hashes.get(session_id)
        if prev_hash:
            args.extend(["--hash", prev_hash])

        ok, output = await self._run_client(args, check=False)
        if not ok:
            logger.debug("cam-client capture failed for session %s", session_id)
            return self._capture_cache.get(session_id, "")

        # Parse hash header: first line is "hash:XXXX"
        lines_split = output.split("\n", 1)
        if lines_split[0].startswith("hash:"):
            new_hash = lines_split[0][5:].strip()
            content = lines_split[1] if len(lines_split) > 1 else ""

            if prev_hash and new_hash == prev_hash and not content:
                # Unchanged — return cached
                return self._capture_cache.get(session_id, "")

            self._capture_hashes[session_id] = new_hash
            self._capture_cache[session_id] = content
            return content
        else:
            # Fallback: no hash header
            return output.rstrip()

    async def session_exists(self, session_id: str) -> bool:
        """Check if session is alive — uses exit code (0=alive, 1=dead)."""
        args = ["session", "exists", "--id", session_id]
        ok, _ = await self._run_client(args, check=False)
        return ok

    async def kill_session(self, session_id: str) -> bool:
        args = ["session", "kill", "--id", session_id]
        ok, _ = await self._run_client_json(args, check=False)
        if ok:
            logger.info("Killed remote session %s on %s", session_id, self._host)
            # Clear cache
            self._capture_hashes.pop(session_id, None)
            self._capture_cache.pop(session_id, None)
        return ok

    # -----------------------------------------------------------------
    # Agent status (hash-gated)
    # -----------------------------------------------------------------

    async def get_agent_status(self, prev_hash: str | None = None) -> tuple[bool, dict]:
        """Get agent status from remote cam-client.py.

        Returns:
            Tuple of (changed, data). If unchanged, data has {"unchanged": True, "hash": "..."}.
            If changed, data has {"agents": [...], "hash": "..."}.
        """
        args = ["status"]
        if prev_hash:
            args.extend(["--hash", prev_hash])
        ok, data = await self._run_client_json(args)
        if not ok:
            return False, {}
        return not data.get("unchanged", False), data

    # -----------------------------------------------------------------
    # File operations
    # -----------------------------------------------------------------

    async def list_files(self, path: str) -> list[dict]:
        args = ["file", "list", "--path", path]
        ok, data = await self._run_client_json(args, check=False)
        if not ok:
            return []
        return data.get("entries", [])

    async def read_file(self, path: str, max_bytes: int = 512_000) -> bytes | None:
        """Read file — cam-client returns raw bytes to stdout."""
        args = ["file", "read", "--path", path, "--max-bytes", str(max_bytes)]
        remote_cmd = "python3 " + " ".join(
            [shlex.quote(self._client_script)] + [shlex.quote(a) for a in args]
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
        """Write file — pipe data to cam-client's stdin."""
        args = ["file", "write", "--path", remote_path]
        ok, _ = await self._run_client_json(args, stdin_data=data, timeout=60)
        if ok:
            logger.info("Wrote %d bytes to %s on %s", len(data), remote_path, self._host)
        return ok

    # -----------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------

    async def test_connection(self) -> tuple[bool, str]:
        ok, data = await self._run_client_json(["ping"])
        if not ok:
            return False, f"Cannot reach cam-client on {self._user or ''}@{self._host}:{self._port}"
        version = data.get("version", "?")
        platform_name = data.get("platform", "?")
        return True, f"cam-client {version} on {self._host} ({platform_name})"

    async def get_latency(self) -> float:
        start = time.monotonic()
        await self._run_client(["ping"], check=False)
        elapsed = (time.monotonic() - start) * 1000
        return round(elapsed, 1)

    def get_attach_command(self, session_id: str) -> str:
        """Return SSH command to attach to the remote tmux session."""
        socket = f"/tmp/cam-sockets/{session_id}.sock"

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
