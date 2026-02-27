"""SSH tunnel manager for CAM API Server.

Maintains a persistent SSH port-forward tunnel with auto-reconnect.
Used to bridge cam serve (private IP) to a relay (public IP) when
direct connectivity is not available.

Usage:
    tunnel = SSHTunnel("hlren.duckdns.org:8001")
    await tunnel.start()       # returns local port
    ...
    await tunnel.stop()
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_is_open(port: int) -> bool:
    """Check if a TCP port is accepting connections on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _parse_tunnel_spec(spec: str) -> tuple[str, str | None, int]:
    """Parse tunnel spec into (host, user, remote_port).

    Formats:
        host:port
        user@host:port
    """
    user = None
    if "@" in spec:
        user, spec = spec.rsplit("@", 1)

    if ":" not in spec:
        raise ValueError(
            f"Invalid ssh-tunnel format: expected 'host:port' or 'user@host:port', got '{spec}'"
        )

    host, port_str = spec.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f"Invalid port in ssh-tunnel: '{port_str}'")

    return host, user, port


class SSHTunnel:
    """Manages a persistent SSH port-forward tunnel with auto-reconnect."""

    def __init__(
        self,
        tunnel_spec: str,
        reconnect_delay: float = 5.0,
        max_delay: float = 60.0,
    ) -> None:
        self._host, self._user, self._remote_port = _parse_tunnel_spec(tunnel_spec)
        self._reconnect_delay = reconnect_delay
        self._max_delay = max_delay

        self._local_port: int = 0
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    @property
    def local_port(self) -> int:
        return self._local_port

    @property
    def relay_url(self) -> str:
        return f"ws://127.0.0.1:{self._local_port}"

    async def start(self) -> int:
        """Start the tunnel and return the local port.

        Waits up to 10s for the tunnel to become reachable.
        """
        self._local_port = _find_free_port()
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop())

        # Wait for tunnel to actually accept connections
        for _ in range(20):  # 20 * 0.5s = 10s max
            await asyncio.sleep(0.5)
            if _port_is_open(self._local_port):
                logger.info("SSH tunnel ready on localhost:%d", self._local_port)
                return self._local_port

        logger.warning("SSH tunnel not ready after 10s, proceeding anyway")
        return self._local_port

    async def stop(self) -> None:
        """Stop the tunnel and clean up."""
        self._stopped = True

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self._kill_process()

    async def _run_loop(self) -> None:
        """Main loop: start SSH, monitor, reconnect on failure."""
        delay = self._reconnect_delay

        while not self._stopped:
            try:
                await self._kill_process()

                ssh_dest = f"{self._user}@{self._host}" if self._user else self._host
                forward = f"127.0.0.1:{self._local_port}:127.0.0.1:{self._remote_port}"

                cmd = [
                    "ssh",
                    "-L", forward,
                    "-N",
                    "-o", "ServerAliveInterval=30",
                    "-o", "ServerAliveCountMax=3",
                    "-o", "ExitOnForwardFailure=yes",
                    "-o", "ConnectTimeout=10",
                    "-o", "BatchMode=yes",
                    ssh_dest,
                ]

                logger.info(
                    "SSH tunnel connecting: localhost:%d -> %s:%d",
                    self._local_port, self._host, self._remote_port,
                )

                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )

                # Wait a moment then check it's alive
                await asyncio.sleep(2)
                if self._process.returncode is not None:
                    logger.warning(
                        "SSH tunnel failed to start (exit %d)",
                        self._process.returncode,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._max_delay)
                    continue

                logger.info(
                    "SSH tunnel established (PID %d): localhost:%d -> %s:%d",
                    self._process.pid, self._local_port, self._host, self._remote_port,
                )
                delay = self._reconnect_delay  # reset on success

                # Wait for process to exit
                await self._process.wait()

                if self._stopped:
                    break

                logger.warning("SSH tunnel died (exit %d), reconnecting...", self._process.returncode)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("SSH tunnel error: %s", e)

            if not self._stopped:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_delay)

        logger.info("SSH tunnel stopped")

    async def _kill_process(self) -> None:
        """Kill the SSH process if running."""
        if self._process and self._process.returncode is None:
            try:
                # Send to process group since start_new_session=True
                import os
                os.killpg(self._process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(self._process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self._process = None
