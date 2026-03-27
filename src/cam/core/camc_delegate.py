"""Delegate agent operations to camc (local or remote via SSH).

CamcDelegate wraps the camc CLI to provide agent lifecycle management.
cam serve uses this instead of directly managing tmux sessions, allowing
camc to be the single source of truth for agent state on each machine.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Path to the camc binary/script
_CAMC_LOCAL = os.path.expanduser("~/.cam/camc")
_CAMC_FALLBACK = os.path.expanduser("~/.local/bin/camc")


def _find_camc() -> str:
    """Find the local camc binary."""
    for p in (_CAMC_LOCAL, _CAMC_FALLBACK):
        if os.path.exists(p):
            return p
    # Try PATH
    import shutil
    found = shutil.which("camc")
    if found:
        return found
    raise FileNotFoundError(
        "camc not found. Expected at %s or in PATH. "
        "Run 'cam sync' to deploy." % _CAMC_LOCAL
    )


def _run_camc(args: list[str], timeout: float = 30) -> tuple[int, str]:
    """Run a camc command and return (returncode, stdout)."""
    camc = _find_camc()
    cmd = ["python3", camc] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        output = proc.stdout
        if proc.returncode != 0 and not output.strip():
            output = proc.stderr
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        logger.warning("camc command timed out: %s", " ".join(args))
        return -1, ""
    except Exception as e:
        logger.error("camc command failed: %s: %s", " ".join(args), e)
        return -1, ""


def _run_camc_ssh(host: str, user: str, port: int | None, args: list[str],
                  timeout: float = 30) -> tuple[int, str]:
    """Run a camc command on a remote machine via SSH."""
    import hashlib as _hl
    camc_remote = "~/.cam/camc"
    # Reuse the same ControlMaster socket as SSHTransport so we piggy-back
    # on its already-authenticated persistent connection.
    conn_key = "%s@%s:%s" % (user or "default", host, port or 22)
    conn_hash = _hl.sha256(conn_key.encode()).hexdigest()[:12]
    control_path = "/tmp/cam-ssh-%s" % conn_hash
    ssh_cmd = ["ssh"]
    if port:
        ssh_cmd += ["-p", str(port)]
    ssh_cmd += [
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ControlPath=%s" % control_path,
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
    ]
    target = "%s@%s" % (user, host) if user else host
    import shlex
    # Check if any arg contains non-ASCII (e.g. Chinese prompts).
    # Remote shells (csh/tcsh) mangle non-ASCII in positional args,
    # so we pipe a bash script via stdin to bypass the login shell.
    has_non_ascii = any(not a.isascii() for a in args)
    if has_non_ascii:
        quoted_args = " ".join(shlex.quote(a) for a in args)
        # Pipe a bash script via stdin; SSH -T disables pty allocation
        bash_script = "#!/bin/bash\npython3 %s %s\n" % (camc_remote, quoted_args)
        ssh_cmd += ["-T", target, "bash"]
    else:
        remote_cmd = "python3 %s %s" % (camc_remote, " ".join(shlex.quote(a) for a in args))
        ssh_cmd += [target, remote_cmd]
        bash_script = None
    try:
        proc = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout,
            input=bash_script,
        )
        output = proc.stdout
        if proc.returncode != 0 and not output.strip():
            output = proc.stderr
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        logger.warning("SSH camc timed out: %s %s", target, " ".join(args))
        return -1, ""
    except Exception as e:
        logger.error("SSH camc failed: %s %s: %s", target, " ".join(args), e)
        return -1, ""


class CamcDelegate:
    """Delegate agent operations to a camc instance (local or remote).

    For local operations, calls camc directly via subprocess.
    For remote operations, wraps camc calls in SSH.
    """

    def __init__(self, host: str | None = None, user: str | None = None,
                 port: int | None = None) -> None:
        self._host = host
        self._user = user
        self._port = port
        self._is_local = host is None or host in ("localhost", "127.0.0.1")

    def _run(self, args: list[str], timeout: float = 30) -> tuple[int, str]:
        if self._is_local:
            return _run_camc(args, timeout)
        return _run_camc_ssh(self._host, self._user, self._port, args, timeout)

    def _run_json(self, args: list[str], timeout: float = 30) -> Any:
        """Run camc with --json and parse output."""
        rc, out = self._run(["--json"] + args, timeout)
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def run_agent(self, tool: str, prompt: str, path: str,
                  name: str | None = None, auto_exit: bool = False) -> dict | None:
        """Launch an agent via camc run. Returns agent info dict or None."""
        args = ["run", "--tool", tool, "--path", path]
        if name:
            args += ["--name", name]
        if auto_exit:
            args += ["--auto-exit"]
        if prompt:
            args.append(prompt)
        rc, out = self._run(args, timeout=60)
        if rc != 0:
            logger.error("camc run failed (rc=%d): %s", rc, out[:200])
            return None
        # Parse agent ID from output: "Starting claude agent abc12345..."
        # Then fetch full status
        for line in out.splitlines():
            if "ID:" in line:
                # "  ID: abc12345  Tool: claude  Session: cam-abc12345"
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == "ID:" and i + 1 < len(parts):
                        agent_id = parts[i + 1]
                        return self.get_agent(agent_id)
        return None

    def stop_agent(self, agent_id: str) -> bool:
        """Stop an agent via camc stop."""
        rc, _ = self._run(["stop", agent_id])
        return rc == 0

    def kill_agent(self, agent_id: str) -> bool:
        """Force kill an agent via camc kill."""
        rc, _ = self._run(["kill", agent_id])
        return rc == 0

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_agents(self, status: str | None = None) -> list[dict]:
        """List agents via camc --json list. Returns cam-compatible JSON."""
        args = ["list"]
        if status:
            args += ["--status", status]
        result = self._run_json(args)
        return result if isinstance(result, list) else []

    def get_agent(self, agent_id: str) -> dict | None:
        """Get single agent status via camc --json status."""
        result = self._run_json(["status", agent_id])
        if isinstance(result, dict) and "id" in result:
            return result
        return None

    def get_agent_output(self, agent_id: str, lines: int = 50) -> str:
        """Capture agent output via camc logs."""
        rc, out = self._run(["logs", agent_id, "--tail", str(lines)])
        return out if rc == 0 else ""

    def get_status_hash(self, prev_hash: str | None = None) -> dict | None:
        """Get status with hash-based conditional response."""
        args = ["status"]
        if prev_hash:
            args += ["--hash", prev_hash]
        return self._run_json(args)

    def get_history(self, agent_id: str | None = None,
                    since: str | None = None) -> list[dict]:
        """Get event history via camc --json history."""
        args = ["history"]
        if agent_id:
            args.append(agent_id)
        if since:
            args += ["--since", since]
        result = self._run_json(args)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Agent interaction
    # ------------------------------------------------------------------

    def capture(self, agent_id: str, lines: int = 100) -> str:
        """Capture agent screen output via camc capture."""
        args = ["capture", agent_id, "--lines", str(lines)]
        rc, out = self._run(args)
        return out if rc == 0 else ""

    def send_input(self, agent_id: str, text: str,
                   send_enter: bool = True) -> bool:
        """Send text input to an agent via camc send."""
        args = ["send", agent_id, "--text", text]
        if not send_enter:
            args.append("--no-enter")
        rc, _ = self._run(args)
        return rc == 0

    def send_key(self, agent_id: str, key: str) -> bool:
        """Send a special key to an agent via camc key."""
        rc, _ = self._run(["key", agent_id, "--key", key])
        return rc == 0

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def heal(self) -> bool:
        """Run camc heal to restart dead monitors."""
        rc, out = self._run(["heal"])
        if out.strip():
            logger.info("camc heal: %s", out.strip().splitlines()[-1])
        return rc == 0

    def version(self) -> str | None:
        """Get camc version string."""
        rc, out = self._run(["version"])
        if rc == 0:
            # First line: "camc v1.0.0"
            return out.splitlines()[0].strip() if out.strip() else None
        return None

    # ------------------------------------------------------------------
    # Local-only: direct file read (faster than subprocess)
    # ------------------------------------------------------------------

    def read_agents_json(self) -> list[dict]:
        """Read agents.json directly (local only, for polling)."""
        if not self._is_local:
            return self.list_agents()
        agents_path = os.path.expanduser("~/.cam/agents.json")
        if not os.path.exists(agents_path):
            return []
        try:
            with open(agents_path, "r") as f:
                return json.load(f)
        except (ValueError, OSError):
            return []

    def read_events_since(self, since: str | None = None) -> list[dict]:
        """Read events.jsonl directly (local only, for polling)."""
        if not self._is_local:
            return self.get_history(since=since)
        events_path = os.path.expanduser("~/.cam/events.jsonl")
        if not os.path.exists(events_path):
            return []
        events = []
        try:
            with open(events_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if since and ev.get("ts", "") < since:
                        continue
                    events.append(ev)
        except OSError:
            pass
        return events
