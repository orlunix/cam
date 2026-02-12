"""Docker transport implementation for CAM.

Runs coding agents inside Docker containers with TMUX sessions.
The container starts, installs tmux if needed, and the agent tool
runs in a TMUX session inside the container.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time

from cam.transport.base import Transport

logger = logging.getLogger(__name__)


class DockerTransport(Transport):
    """Docker-based transport that runs agents inside containers.

    Creates a long-running container from the specified image, then
    creates TMUX sessions inside it for agent execution.

    Args:
        image: Docker image to use (e.g. "python:3.11").
        volumes: Volume mount specifications (e.g. ["/host/path:/container/path"]).
        container_name_prefix: Prefix for container names.
    """

    def __init__(
        self,
        image: str | None = None,
        volumes: list[str] | None = None,
        container_name_prefix: str = "cam",
    ) -> None:
        if not image:
            raise ValueError("Docker transport requires an image")
        self._image = image
        self._volumes = volumes or []
        self._prefix = container_name_prefix
        self._containers: dict[str, str] = {}  # session_id -> container_id

    def _container_name(self, session_id: str) -> str:
        """Generate a container name from session ID."""
        return f"{self._prefix}-{session_id}"

    async def _run_docker(self, args: list[str], check: bool = True) -> tuple[bool, str]:
        """Execute a docker command.

        Args:
            args: Docker command arguments.
            check: Whether to treat non-zero exit as failure.

        Returns:
            Tuple of (success, output).
        """
        cmd = ["docker"] + args
        logger.debug("Docker: %s", " ".join(shlex.quote(a) for a in cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            success = proc.returncode == 0
            output = stdout.decode("utf-8", errors="replace")

            if not success and check:
                error = stderr.decode("utf-8", errors="replace")
                logger.warning("Docker command failed (exit %d): %s", proc.returncode, error)
                return False, error

            return success, output

        except asyncio.TimeoutError:
            logger.error("Docker command timed out")
            return False, "Docker command timed out"
        except Exception as e:
            logger.error("Docker execution failed: %s", e)
            return False, str(e)

    async def _exec_in_container(
        self, container: str, cmd: str, check: bool = True
    ) -> tuple[bool, str]:
        """Execute a command inside a running container."""
        return await self._run_docker(
            ["exec", container, "bash", "-c", cmd],
            check=check,
        )

    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        """Create a Docker container and TMUX session inside it."""
        container = self._container_name(session_id)

        # Build docker run command
        run_args = [
            "run", "-d",
            "--name", container,
            "-w", workdir,
        ]

        # Add volume mounts
        for vol in self._volumes:
            run_args.extend(["-v", vol])

        # Use the image with a long-running command to keep container alive
        run_args.extend([self._image, "sleep", "infinity"])

        success, output = await self._run_docker(run_args)
        if not success:
            logger.error("Failed to create container %s: %s", container, output)
            return False

        container_id = output.strip()[:12]
        self._containers[session_id] = container
        logger.info("Created container %s (%s) from %s", container, container_id, self._image)

        # Ensure tmux is available in the container
        await self._exec_in_container(
            container,
            "which tmux || (apt-get update -qq && apt-get install -qq -y tmux) 2>/dev/null || "
            "(apk add --no-cache tmux) 2>/dev/null || true",
            check=False,
        )

        # Create TMUX session inside the container
        ok, _ = await self._exec_in_container(
            container,
            f"tmux new-session -d -s {shlex.quote(session_id)} -c {shlex.quote(workdir)}",
        )
        if not ok:
            logger.error("Failed to create TMUX session in container %s", container)
            await self._run_docker(["rm", "-f", container], check=False)
            return False

        # Send the command
        command_str = " ".join(shlex.quote(arg) for arg in command)
        if not await self.send_input(session_id, command_str, send_enter=True):
            logger.error("Failed to send command to container session %s", session_id)
            await self.kill_session(session_id)
            return False

        return True

    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        """Send input to a TMUX session inside the container."""
        container = self._containers.get(session_id, self._container_name(session_id))
        target = f"{session_id}:0.0"

        # Send text literally
        escaped_text = text.replace("'", "'\\''")
        ok, _ = await self._exec_in_container(
            container,
            f"tmux send-keys -t {shlex.quote(target)} -l -- '{escaped_text}'",
        )
        if not ok:
            return False

        if send_enter:
            ok, _ = await self._exec_in_container(
                container,
                f"tmux send-keys -t {shlex.quote(target)} Enter",
            )

        return ok

    async def capture_output(self, session_id: str, lines: int = 50) -> str:
        """Capture output from TMUX session inside the container."""
        container = self._containers.get(session_id, self._container_name(session_id))
        target = f"{session_id}:0.0"

        ok, output = await self._exec_in_container(
            container,
            f"tmux capture-pane -p -J -t {shlex.quote(target)} -S -{lines}",
            check=False,
        )
        return output if ok else ""

    async def session_exists(self, session_id: str) -> bool:
        """Check if the container and TMUX session are alive."""
        container = self._containers.get(session_id, self._container_name(session_id))

        # Check container is running
        ok, output = await self._run_docker(
            ["inspect", "--format", "{{.State.Running}}", container],
            check=False,
        )
        if not ok or output.strip() != "true":
            return False

        # Check TMUX session inside container
        ok, _ = await self._exec_in_container(
            container,
            f"tmux has-session -t {shlex.quote(session_id)}",
            check=False,
        )
        return ok

    async def kill_session(self, session_id: str) -> bool:
        """Kill the TMUX session and remove the container."""
        container = self._containers.get(session_id, self._container_name(session_id))

        # Kill TMUX session
        await self._exec_in_container(
            container,
            f"tmux kill-session -t {shlex.quote(session_id)}",
            check=False,
        )

        # Stop and remove container
        success, _ = await self._run_docker(["rm", "-f", container], check=False)
        self._containers.pop(session_id, None)

        if success:
            logger.info("Killed container %s for session %s", container, session_id)
        return success

    async def test_connection(self) -> tuple[bool, str]:
        """Test Docker availability and image accessibility."""
        ok, output = await self._run_docker(["version", "--format", "{{.Client.Version}}"], check=False)
        if not ok:
            return False, "Docker is not available"

        version = output.strip()

        # Check if image exists locally or can be pulled
        ok, _ = await self._run_docker(
            ["image", "inspect", self._image],
            check=False,
        )
        if ok:
            return True, f"Docker {version}, image '{self._image}' available locally"
        else:
            return True, f"Docker {version}, image '{self._image}' will be pulled on first use"

    async def get_latency(self) -> float:
        """Measure Docker command latency."""
        start = time.monotonic()
        await self._run_docker(["version"], check=False)
        return round((time.monotonic() - start) * 1000, 1)

    def get_attach_command(self, session_id: str) -> str:
        """Return command to attach to the container's TMUX session."""
        container = self._containers.get(session_id, self._container_name(session_id))
        return f"docker exec -it {shlex.quote(container)} tmux attach -t {shlex.quote(session_id)}"
