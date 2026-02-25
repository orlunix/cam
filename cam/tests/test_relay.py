"""Tests for the CAM Relay and relay connector.

Tests the zero-dep relay (relay/relay.py) by starting it as a local
asyncio server and connecting via the websockets library.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Add relay directory to path so we can import it
RELAY_DIR = Path(__file__).parent.parent / "relay"
sys.path.insert(0, str(RELAY_DIR))

import websockets
from relay import Relay, do_ws_upgrade, make_frame, read_frame, OP_TEXT, OP_CLOSE, OP_PING


# ── Helpers ─────────────────────────────────────────────────────────


async def _start_relay(token: str | None = "test-token") -> tuple:
    """Start a relay on a random port, return (relay, server, port)."""
    relay = Relay(token=token)
    server = await asyncio.start_server(relay.handle_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return relay, server, port


# ── Frame helpers ───────────────────────────────────────────────────


class TestMakeFrame:
    def test_short_text_frame(self):
        frame = make_frame(OP_TEXT, b"hello")
        assert frame[0] == 0x81  # FIN + TEXT
        assert frame[1] == 5  # length
        assert frame[2:] == b"hello"

    def test_medium_frame(self):
        payload = b"x" * 200
        frame = make_frame(OP_TEXT, payload)
        assert frame[0] == 0x81
        assert frame[1] == 126  # extended length marker
        # 2 bytes for length
        assert len(frame) == 2 + 2 + 200

    def test_close_frame(self):
        frame = make_frame(OP_CLOSE, b"")
        assert frame[0] == 0x88  # FIN + CLOSE
        assert frame[1] == 0

    def test_ping_frame(self):
        frame = make_frame(OP_PING, b"ping")
        assert frame[0] == 0x89  # FIN + PING


class TestReadFrame:
    async def test_read_text_frame(self):
        frame = make_frame(OP_TEXT, b"hello")
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        result = await read_frame(reader)
        assert result is not None
        opcode, payload = result
        assert opcode == OP_TEXT
        assert payload == b"hello"

    async def test_read_eof_returns_none(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        result = await read_frame(reader)
        assert result is None


# ── Relay integration ───────────────────────────────────────────────


class TestRelayIntegration:
    async def test_server_connects(self):
        """Server can connect to /server path."""
        relay, server, port = await _start_relay()
        async with server:
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )
            # Connection succeeded — send/recv works
            await ws.close()
        server.close()

    async def test_client_connects(self):
        """Client can connect to /client path."""
        relay, server, port = await _start_relay()
        async with server:
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )
            await ws.close()
        server.close()

    async def test_bad_token_rejected(self):
        """Wrong token causes immediate close after connect."""
        relay, server, port = await _start_relay()
        async with server:
            try:
                ws = await websockets.connect(
                    f"ws://127.0.0.1:{port}/server?token=wrong"
                )
                # If connect succeeds, recv should fail
                try:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
                    assert False, "Should have received close"
                except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
                    pass
                await ws.close()
            except (websockets.exceptions.InvalidMessage, websockets.exceptions.InvalidStatus):
                pass  # Relay closed before handshake completed
        server.close()

    async def test_no_token_rejected(self):
        """Missing token causes immediate close after connect."""
        relay, server, port = await _start_relay()
        async with server:
            try:
                ws = await websockets.connect(
                    f"ws://127.0.0.1:{port}/server"
                )
                try:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
                    assert False, "Should have received close"
                except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
                    pass
                await ws.close()
            except (websockets.exceptions.InvalidMessage, websockets.exceptions.InvalidStatus):
                pass  # Relay closed before handshake completed
        server.close()

    async def test_server_to_client_forwarding(self):
        """Frames from server are forwarded to client."""
        relay, server, port = await _start_relay()
        async with server:
            # Connect server
            server_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )
            # Connect client
            client_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )

            # Server sends a message
            await server_ws.send("hello from server")

            # Client should receive it
            msg = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            assert msg == "hello from server"

            await server_ws.close()
            await client_ws.close()
        server.close()

    async def test_client_to_server_forwarding(self):
        """Frames from client are forwarded to server."""
        relay, server, port = await _start_relay()
        async with server:
            server_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )
            client_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )

            # Client sends a message
            await client_ws.send("hello from client")

            # Server should receive it
            msg = await asyncio.wait_for(server_ws.recv(), timeout=2.0)
            assert msg == "hello from client"

            await server_ws.close()
            await client_ws.close()
        server.close()

    async def test_multiple_clients(self):
        """Server message is broadcast to all clients."""
        relay, server, port = await _start_relay()
        async with server:
            server_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )
            client1 = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )
            client2 = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )

            await server_ws.send("broadcast")

            msg1 = await asyncio.wait_for(client1.recv(), timeout=2.0)
            msg2 = await asyncio.wait_for(client2.recv(), timeout=2.0)
            assert msg1 == "broadcast"
            assert msg2 == "broadcast"

            await server_ws.close()
            await client1.close()
            await client2.close()
        server.close()

    async def test_json_request_response(self):
        """Simulates the REST-over-WS protocol used by relay_connector."""
        relay, server, port = await _start_relay()
        async with server:
            server_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )
            client_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )

            # Client sends a JSON request
            request = {
                "id": "req-1",
                "method": "GET",
                "path": "/api/system/health",
                "headers": {},
                "body": "",
            }
            await client_ws.send(json.dumps(request))

            # Server receives it
            raw = await asyncio.wait_for(server_ws.recv(), timeout=2.0)
            received = json.loads(raw)
            assert received["id"] == "req-1"
            assert received["method"] == "GET"

            # Server sends response back
            response = {
                "id": "req-1",
                "status": 200,
                "body": json.dumps({"status": "ok"}),
            }
            await server_ws.send(json.dumps(response))

            # Client receives it
            raw = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            received = json.loads(raw)
            assert received["id"] == "req-1"
            assert received["status"] == 200

            await server_ws.close()
            await client_ws.close()
        server.close()

    async def test_no_auth_relay(self):
        """Relay with no token allows all connections."""
        relay, server, port = await _start_relay(token=None)
        async with server:
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/server"
            )
            await ws.close()
        server.close()

    async def test_server_reconnect(self):
        """New server connection replaces old one."""
        relay, server, port = await _start_relay()
        async with server:
            # First server connection
            server_ws1 = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )
            # Second server connection replaces first
            server_ws2 = await websockets.connect(
                f"ws://127.0.0.1:{port}/server?token=test-token"
            )

            client_ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/client?token=test-token"
            )

            # Message from new server should reach client
            await server_ws2.send("from new server")
            msg = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            assert msg == "from new server"

            await server_ws1.close()
            await server_ws2.close()
            await client_ws.close()
        server.close()
