"""WebSocket Agent Server for CAM.

A persistent daemon that runs on remote machines and accepts WebSocket
connections from CAM clients. Manages TMUX sessions locally on the
server side, proxying commands and output over WebSocket.

Usage:
    cam-agent-server --port 9876 --token SECRET

Protocol:
    JSON messages with {"action": "...", "session_id": "...", ...}
    Actions: create_session, send_input, capture_output, session_exists,
             kill_session, ping
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Any

logger = logging.getLogger(__name__)

# Server-side socket directory
SOCKET_DIR = "/tmp/cam-agent-sockets"


class AgentServer:
    """WebSocket server managing TMUX sessions for remote CAM clients.

    Listens for incoming WebSocket connections and dispatches commands
    to local TMUX sessions. Supports authentication via bearer token.

    Args:
        host: Bind address (default "0.0.0.0").
        port: Listen port (default 9876).
        auth_token: Required authentication token (None = no auth).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9876,
        auth_token: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._auth_token = auth_token

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            raise RuntimeError(
                "websockets package required. Install with: pip install cam[remote]"
            )

        # Ensure socket directory
        import os
        os.makedirs(SOCKET_DIR, exist_ok=True)

        logger.info("Starting CAM Agent Server on %s:%d", self._host, self._port)

        async with websockets.serve(
            self._handler,
            self._host,
            self._port,
        ):
            await asyncio.Future()  # Run forever

    async def _handler(self, websocket: Any) -> None:
        """Handle a single WebSocket connection."""
        remote = websocket.remote_address
        logger.info("Client connected from %s", remote)

        try:
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({"error": "Invalid JSON"}))
                    continue

                # Auth check
                if self._auth_token:
                    token = message.get("token")
                    if token != self._auth_token:
                        await websocket.send(json.dumps({"error": "Unauthorized"}))
                        continue

                response = await self._dispatch(message)
                await websocket.send(json.dumps(response))

        except Exception as e:
            logger.warning("Client %s disconnected: %s", remote, e)

    async def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        """Route a message to the appropriate handler."""
        action = message.get("action", "")

        handlers = {
            "ping": self._handle_ping,
            "create_session": self._handle_create_session,
            "send_input": self._handle_send_input,
            "capture_output": self._handle_capture_output,
            "session_exists": self._handle_session_exists,
            "kill_session": self._handle_kill_session,
        }

        handler = handlers.get(action)
        if not handler:
            return {"error": f"Unknown action: {action}"}

        try:
            return await handler(message)
        except Exception as e:
            logger.error("Error handling action '%s': %s", action, e)
            return {"error": str(e)}

    async def _run_tmux(self, session_id: str, args: list[str]) -> tuple[bool, str]:
        """Execute a tmux command with a per-session socket."""
        socket = f"{SOCKET_DIR}/{session_id}.sock"
        cmd = ["tmux", "-S", socket] + args

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        success = proc.returncode == 0
        output = stdout.decode("utf-8", errors="replace")
        return success, output

    async def _handle_ping(self, message: dict) -> dict:
        return {"ok": True, "pong": True}

    async def _handle_create_session(self, message: dict) -> dict:
        session_id = message.get("session_id", "")
        command = message.get("command", [])
        workdir = message.get("workdir", "/tmp")

        if not session_id or not command:
            return {"error": "Missing session_id or command"}

        # Create detached session
        ok, _ = await self._run_tmux(session_id, [
            "new-session", "-d", "-s", session_id, "-c", workdir,
        ])
        if not ok:
            return {"ok": False, "error": "Failed to create TMUX session"}

        # Send command
        command_str = " ".join(shlex.quote(arg) for arg in command)
        target = f"{session_id}:0.0"
        await self._run_tmux(session_id, [
            "send-keys", "-t", target, "-l", "--", command_str,
        ])
        await self._run_tmux(session_id, [
            "send-keys", "-t", target, "Enter",
        ])

        return {"ok": True}

    async def _handle_send_input(self, message: dict) -> dict:
        session_id = message.get("session_id", "")
        text = message.get("text", "")
        send_enter = message.get("send_enter", True)

        target = f"{session_id}:0.0"
        ok, _ = await self._run_tmux(session_id, [
            "send-keys", "-t", target, "-l", "--", text,
        ])
        if send_enter:
            await self._run_tmux(session_id, [
                "send-keys", "-t", target, "Enter",
            ])

        return {"ok": ok}

    async def _handle_capture_output(self, message: dict) -> dict:
        session_id = message.get("session_id", "")
        lines = message.get("lines", 50)

        target = f"{session_id}:0.0"
        ok, output = await self._run_tmux(session_id, [
            "capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}",
        ])

        return {"ok": ok, "output": output}

    async def _handle_session_exists(self, message: dict) -> dict:
        session_id = message.get("session_id", "")
        ok, _ = await self._run_tmux(session_id, [
            "has-session", "-t", session_id,
        ])
        return {"ok": True, "exists": ok}

    async def _handle_kill_session(self, message: dict) -> dict:
        session_id = message.get("session_id", "")
        ok, _ = await self._run_tmux(session_id, [
            "kill-session", "-t", session_id,
        ])

        # Clean up socket
        import os
        socket = f"{SOCKET_DIR}/{session_id}.sock"
        try:
            os.unlink(socket)
        except OSError:
            pass

        return {"ok": ok}


def run_server(
    host: str = "0.0.0.0",
    port: int = 9876,
    token: str | None = None,
) -> None:
    """Convenience entry point for running the agent server."""
    server = AgentServer(host=host, port=port, auth_token=token)
    asyncio.run(server.start())
