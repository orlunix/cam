"""WebSocket client transport for CAM.

Connects to a remote CAM Agent Server over WebSocket and proxies all
TMUX operations. Supports authentication and automatic reconnection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from cam.transport.base import Transport

logger = logging.getLogger(__name__)


class WebSocketClient(Transport):
    """WebSocket-based transport connecting to a remote Agent Server.

    Args:
        host: Remote hostname or IP.
        user: Username (informational, not used for auth).
        port: WebSocket port (default 9876).
        auth_token: Authentication token.
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        port: int | None = None,
        auth_token: str | None = None,
    ) -> None:
        if not host:
            raise ValueError("WebSocket transport requires a host")
        self._host = host
        self._user = user
        self._port = port or 9876
        self._auth_token = auth_token
        self._ws: Any = None
        self._uri = f"ws://{self._host}:{self._port}"

    async def _connect(self) -> Any:
        """Get or create a WebSocket connection."""
        if self._ws is not None:
            try:
                await self._ws.ping()
                return self._ws
            except Exception:
                self._ws = None

        try:
            import websockets
        except ImportError:
            raise ImportError(
                "websockets package required. Install with: pip install cam[remote]"
            )

        self._ws = await websockets.connect(self._uri)
        logger.info("Connected to Agent Server at %s", self._uri)
        return self._ws

    async def _send(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a message and receive the response."""
        if self._auth_token:
            message["token"] = self._auth_token

        ws = await self._connect()
        await ws.send(json.dumps(message))
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        return json.loads(raw)

    async def create_session(self, session_id: str, command: list[str], workdir: str) -> bool:
        """Create a TMUX session on the remote agent server."""
        resp = await self._send({
            "action": "create_session",
            "session_id": session_id,
            "command": command,
            "workdir": workdir,
        })
        if not resp.get("ok"):
            logger.error("Failed to create remote session: %s", resp.get("error", "unknown"))
            return False
        return True

    async def send_input(self, session_id: str, text: str, send_enter: bool = True) -> bool:
        """Send input to a remote TMUX session."""
        resp = await self._send({
            "action": "send_input",
            "session_id": session_id,
            "text": text,
            "send_enter": send_enter,
        })
        return resp.get("ok", False)

    async def capture_output(self, session_id: str, lines: int = 50) -> str:
        """Capture output from a remote TMUX session."""
        resp = await self._send({
            "action": "capture_output",
            "session_id": session_id,
            "lines": lines,
        })
        return resp.get("output", "")

    async def session_exists(self, session_id: str) -> bool:
        """Check if a remote TMUX session exists."""
        resp = await self._send({
            "action": "session_exists",
            "session_id": session_id,
        })
        return resp.get("exists", False)

    async def kill_session(self, session_id: str) -> bool:
        """Kill a remote TMUX session."""
        resp = await self._send({
            "action": "kill_session",
            "session_id": session_id,
        })
        return resp.get("ok", False)

    async def test_connection(self) -> tuple[bool, str]:
        """Test WebSocket connectivity with a ping."""
        try:
            resp = await self._send({"action": "ping"})
            if resp.get("pong"):
                return True, f"Agent Server connected at {self._uri}"
            return False, f"Unexpected response from {self._uri}"
        except Exception as e:
            return False, f"Cannot connect to Agent Server at {self._uri}: {e}"

    async def get_latency(self) -> float:
        """Measure WebSocket round-trip latency."""
        start = time.monotonic()
        await self._send({"action": "ping"})
        return round((time.monotonic() - start) * 1000, 1)

    def get_attach_command(self, session_id: str) -> str:
        """WebSocket sessions cannot be attached directly.

        Returns a message explaining how to use SSH instead.
        """
        return f"echo 'WebSocket sessions cannot be attached directly. Use SSH to connect to {self._host}'"

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
