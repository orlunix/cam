#!/usr/bin/env python3
"""CAM Relay — Zero-dependency WebSocket relay proxy.

Forwards WebSocket frames between a CAM server and its clients.
Runs on a public-IP machine with nothing but Python 3.10+ stdlib.

Usage:
    python3 relay.py --port 8443 --token SECRET

Protocol:
    Server connects:  ws://relay:port/server?token=SECRET
    Clients connect:  ws://relay:port/client?token=SECRET

    All frames from server are forwarded to every connected client.
    All frames from any client are forwarded to the server.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import logging
import struct
import sys
from urllib.parse import parse_qs, urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")

# RFC 6455 constants
WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


# ── WebSocket frame helpers (RFC 6455) ──────────────────────────────


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """Read one WebSocket frame. Returns (opcode, payload) or None on EOF."""
    try:
        head = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None

    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F

    if length == 126:
        raw = await reader.readexactly(2)
        length = struct.unpack("!H", raw)[0]
    elif length == 127:
        raw = await reader.readexactly(8)
        length = struct.unpack("!Q", raw)[0]

    mask_key = await reader.readexactly(4) if masked else None

    payload = await reader.readexactly(length)

    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    log.debug("read_frame: opcode=%d masked=%s length=%d", opcode, masked, length)
    return opcode, payload


def make_frame(opcode: int, payload: bytes, mask: bool = False) -> bytes:
    """Build a WebSocket frame. Server→client frames are unmasked."""
    frame = bytearray()
    frame.append(0x80 | opcode)  # FIN + opcode

    length = len(payload)
    if length < 126:
        frame.append((0x80 if mask else 0x00) | length)
    elif length < 65536:
        frame.append((0x80 if mask else 0x00) | 126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append((0x80 if mask else 0x00) | 127)
        frame.extend(struct.pack("!Q", length))

    if mask:
        import os
        mask_key = os.urandom(4)
        frame.extend(mask_key)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    frame.extend(payload)
    return bytes(frame)


# ── HTTP Upgrade handshake ──────────────────────────────────────────


async def do_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> tuple[str, dict[str, list[str]]] | None:
    """Perform WebSocket HTTP upgrade. Returns (path, query_params) or None."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
    except (asyncio.TimeoutError, ConnectionError):
        return None

    if not request_line:
        return None

    parts = request_line.decode(errors="replace").strip().split()
    if len(parts) < 2:
        return None

    parsed = urlparse(parts[1])
    path = parsed.path
    query = parse_qs(parsed.query)

    # Read headers
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        if b":" in line:
            key, _, val = line.decode(errors="replace").partition(":")
            headers[key.strip().lower()] = val.strip()

    ws_key = headers.get("sec-websocket-key", "")
    if not ws_key:
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        return None

    # Compute accept key
    accept = base64.b64encode(
        hashlib.sha1((ws_key + WS_MAGIC.decode()).encode()).digest()
    ).decode()

    # Send 101 response
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(response.encode())
    await writer.drain()

    return path, query


# ── Relay logic ─────────────────────────────────────────────────────


class Relay:
    """Stateless WebSocket relay between one server and many clients."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._server_writer: asyncio.StreamWriter | None = None
        self._server_reader: asyncio.StreamReader | None = None
        self._clients: dict[int, asyncio.StreamWriter] = {}
        self._client_id = 0

    def _check_token(self, query: dict[str, list[str]]) -> bool:
        if not self._token:
            return True
        tokens = query.get("token", [])
        return len(tokens) > 0 and tokens[0] == self._token

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        result = await do_handshake(reader, writer)
        if result is None:
            writer.close()
            return

        path, query = result

        if not self._check_token(query):
            log.warning("Auth failed from %s", peer)
            close_frame = make_frame(OP_CLOSE, b"")
            writer.write(close_frame)
            await writer.drain()
            writer.close()
            return

        if path == "/server":
            await self._handle_server(reader, writer, peer)
        elif path == "/client":
            await self._handle_client(reader, writer, peer)
        else:
            log.warning("Unknown path %s from %s", path, peer)
            writer.close()

    async def _handle_server(self, reader, writer, peer) -> None:
        if self._server_writer is not None:
            log.warning("Server reconnecting, dropping old connection")
            try:
                self._server_writer.close()
            except Exception:
                pass

        self._server_writer = writer
        self._server_reader = reader
        log.info("Server connected from %s", peer)

        try:
            while True:
                result = await read_frame(reader)
                if result is None:
                    log.info("Server read_frame returned None (EOF)")
                    break

                opcode, payload = result

                log.debug("Server frame: opcode=%d len=%d", opcode, len(payload))
                if opcode == OP_CLOSE:
                    log.info("Server sent CLOSE frame")
                    break
                elif opcode == OP_PING:
                    log.debug("Server PING, sending PONG")
                    writer.write(make_frame(OP_PONG, payload))
                    await writer.drain()
                    continue
                elif opcode == OP_PONG:
                    log.debug("Server PONG received")
                    continue

                # Forward to all clients
                dead = []
                for cid, cw in self._clients.items():
                    try:
                        cw.write(make_frame(opcode, payload))
                        await cw.drain()
                    except Exception:
                        dead.append(cid)

                for cid in dead:
                    self._clients.pop(cid, None)
                    log.info("Client %d dropped (write error)", cid)

        except Exception as e:
            log.warning("Server error: %s", e)
        finally:
            # Only clear if we're still the active server (avoid race with reconnect)
            if self._server_writer is writer:
                self._server_writer = None
                self._server_reader = None
            log.info("Server disconnected")
            writer.close()

    async def _handle_client(self, reader, writer, peer) -> None:
        self._client_id += 1
        cid = self._client_id
        self._clients[cid] = writer
        log.info("Client %d connected from %s (%d total)", cid, peer, len(self._clients))

        try:
            while True:
                result = await read_frame(reader)
                if result is None:
                    break

                opcode, payload = result

                if opcode == OP_CLOSE:
                    break
                elif opcode == OP_PING:
                    writer.write(make_frame(OP_PONG, payload))
                    await writer.drain()
                    continue
                elif opcode == OP_PONG:
                    continue

                # Forward to server
                if self._server_writer is not None:
                    try:
                        self._server_writer.write(make_frame(opcode, payload))
                        await self._server_writer.drain()
                    except Exception:
                        log.warning("Failed to forward client %d frame to server", cid)
                else:
                    log.debug("No server connected, dropping client %d frame", cid)

        except Exception as e:
            log.warning("Client %d error: %s", cid, e)
        finally:
            self._clients.pop(cid, None)
            log.info("Client %d disconnected (%d remaining)", cid, len(self._clients))
            writer.close()


# ── Entry point ─────────────────────────────────────────────────────


async def run_relay(host: str, port: int, token: str | None) -> None:
    relay = Relay(token=token)
    server = await asyncio.start_server(relay.handle_connection, host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("CAM Relay listening on %s", addrs)
    if token:
        log.info("Auth token: %s...%s", token[:4], token[-4:])
    else:
        log.warning("No auth token — relay is open!")

    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="CAM Relay — WebSocket proxy")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8443, help="Listen port")
    parser.add_argument("--token", default=None, help="Auth token")
    args = parser.parse_args()

    try:
        asyncio.run(run_relay(args.host, args.port, args.token))
    except KeyboardInterrupt:
        log.info("Relay stopped")


if __name__ == "__main__":
    main()
