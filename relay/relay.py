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
import mimetypes
import os
import struct
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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


async def read_http_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> tuple[str, str, dict[str, str], dict[str, list[str]]] | None:
    """Read HTTP request. Returns (method, path, headers, query) or None."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
    except (asyncio.TimeoutError, ConnectionError):
        return None

    if not request_line:
        return None

    parts = request_line.decode(errors="replace").strip().split()
    if len(parts) < 2:
        return None

    method = parts[0].upper()
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

    return method, path, headers, query


async def do_ws_upgrade(
    writer: asyncio.StreamWriter,
    ws_key: str,
) -> None:
    """Complete WebSocket upgrade handshake."""
    accept = base64.b64encode(
        hashlib.sha1((ws_key + WS_MAGIC.decode()).encode()).digest()
    ).decode()

    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(response.encode())
    await writer.drain()


# ── Relay logic ─────────────────────────────────────────────────────


class Relay:
    """Stateless WebSocket relay between one server and many clients."""

    def __init__(self, token: str | None = None, web_root: str | None = None) -> None:
        self._token = token
        self._server_writer: asyncio.StreamWriter | None = None
        self._server_reader: asyncio.StreamReader | None = None
        self._clients: dict[int, asyncio.StreamWriter] = {}
        self._client_id = 0
        self._web_root = Path(web_root) if web_root else None
        self._proxy_counter = 0
        self._proxy_pending: dict[str, asyncio.Future] = {}
        self._api_ws_clients: dict[str, asyncio.StreamWriter] = {}  # ws_id → writer

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
        result = await read_http_request(reader, writer)
        if result is None:
            writer.close()
            return

        method, path, headers, query = result

        # WebSocket upgrade?
        ws_key = headers.get("sec-websocket-key", "")
        if ws_key:
            # /api/ws — proxy WebSocket event stream to server
            if path.startswith("/api/ws"):
                await do_ws_upgrade(writer, ws_key)
                await self._handle_api_ws(reader, writer, peer, path, headers)
                return

            if not self._check_token(query):
                log.warning("Auth failed from %s", peer)
                close_frame = make_frame(OP_CLOSE, b"")
                writer.write(close_frame)
                await writer.drain()
                writer.close()
                return

            await do_ws_upgrade(writer, ws_key)

            if path == "/server":
                await self._handle_server(reader, writer, peer)
            elif path == "/client":
                await self._handle_client(reader, writer, peer)
            else:
                log.warning("Unknown WS path %s from %s", path, peer)
                writer.close()
            return

        # API requests — proxy through connected server
        if path.startswith("/api/"):
            log.info("HTTP-API %s %s from %s", method, path, peer)
            # Read request body if present
            body = ""
            content_length = int(headers.get("content-length", 0))
            if content_length > 0:
                body_bytes = await reader.readexactly(content_length)
                body = body_bytes.decode(errors="replace")
            await self._proxy_api(writer, method, path, headers, body)
            return

        # Regular HTTP — serve static files
        log.info("HTTP %s %s from %s", method, path, peer)
        await self._serve_static(writer, method, path)

    async def _proxy_api(self, writer: asyncio.StreamWriter, method: str, path: str, headers: dict, body: str = "") -> None:
        """Proxy an HTTP API request through the connected server WebSocket."""
        import json as _json

        if self._server_writer is None:
            resp = b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: 36\r\n\r\n{\"detail\":\"No server connected\"}\r\n"
            writer.write(resp)
            await writer.drain()
            writer.close()
            return

        self._proxy_counter += 1
        req_id = f"proxy-{self._proxy_counter}"
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._proxy_pending[req_id] = future

        # Send request frame to server
        frame_data = _json.dumps({
            "id": req_id,
            "method": method,
            "path": path,
            "headers": headers,
            "body": body,
        })
        try:
            self._server_writer.write(make_frame(OP_TEXT, frame_data.encode()))
            await self._server_writer.drain()
        except Exception:
            self._proxy_pending.pop(req_id, None)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Wait for response from server (with timeout)
        timeout = 60 if "/upload" in path else 30
        try:
            resp_data = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._proxy_pending.pop(req_id, None)
            writer.write(b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Build HTTP response
        status = resp_data.get("status", 500)
        resp_body = resp_data.get("body", "").encode()
        resp_headers = resp_data.get("headers", {})
        content_type = resp_headers.get("content-type", "application/json")

        http_resp = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(resp_body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
        )
        writer.write(http_resp.encode() + resp_body)
        await writer.drain()
        writer.close()

    async def _serve_static(self, writer: asyncio.StreamWriter, method: str, path: str) -> None:
        """Serve a static file from web_root."""
        if not self._web_root:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Sanitize path
        clean = unquote(path).lstrip("/")
        if not clean or clean.endswith("/"):
            clean += "index.html"

        file_path = (self._web_root / clean).resolve()

        # Prevent directory traversal
        try:
            file_path.relative_to(self._web_root.resolve())
        except ValueError:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        if not file_path.is_file():
            # SPA fallback: serve index.html for non-file paths
            index = self._web_root / "index.html"
            if index.is_file() and "." not in clean.split("/")[-1]:
                file_path = index
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return

        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"

        try:
            data = file_path.read_bytes()
            header = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"Cache-Control: no-cache\r\n"
                f"\r\n"
            )
            writer.write(header.encode() + data)
            await writer.drain()
        except Exception as e:
            log.warning("Error serving %s: %s", file_path, e)
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
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

                # Check if this is a response to an HTTP proxy or API-WS request
                if opcode == OP_TEXT and (self._proxy_pending or self._api_ws_clients):
                    import json as _json
                    try:
                        msg = _json.loads(payload)
                        req_id = msg.get("id", "")

                        # HTTP proxy response
                        if req_id in self._proxy_pending:
                            future = self._proxy_pending.pop(req_id)
                            if not future.done():
                                future.set_result(msg)
                            continue

                        # API WebSocket event — forward inner event to phone
                        if req_id in self._api_ws_clients:
                            event = msg.get("event")
                            if event:
                                cw = self._api_ws_clients[req_id]
                                try:
                                    cw.write(make_frame(OP_TEXT, _json.dumps(event).encode()))
                                    await cw.drain()
                                except Exception:
                                    self._api_ws_clients.pop(req_id, None)
                            continue
                    except (ValueError, TypeError):
                        pass

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

    async def _handle_api_ws(self, reader, writer, peer, path: str, headers: dict) -> None:
        """Proxy a /api/ws WebSocket connection through to the CAM server.

        The phone opens ws://relay/api/ws?token=... expecting raw event JSON.
        We send a WS request frame to the server via its relay connection,
        and forward events back to the phone.
        """
        import json as _json

        if self._server_writer is None:
            log.warning("API-WS from %s but no server connected", peer)
            writer.write(make_frame(OP_CLOSE, b""))
            await writer.drain()
            writer.close()
            return

        # Register this WS client for event forwarding
        self._proxy_counter += 1
        ws_id = f"api-ws-{self._proxy_counter}"
        self._api_ws_clients[ws_id] = writer
        log.info("API-WS %s connected from %s (path=%s)", ws_id, peer, path)

        # Tell the server to start streaming events for this client
        frame = _json.dumps({
            "id": ws_id,
            "method": "WS",
            "path": path,
            "headers": {k: v for k, v in headers.items() if k.startswith("authorization")},
        })
        try:
            self._server_writer.write(make_frame(OP_TEXT, frame.encode()))
            await self._server_writer.drain()
        except Exception:
            log.warning("API-WS %s: failed to send WS request to server", ws_id)
            self._api_ws_clients.pop(ws_id, None)
            writer.close()
            return

        # Run frame reader and ping sender concurrently
        async def _read_loop():
            while True:
                result = await read_frame(reader)
                if result is None:
                    log.info("API-WS %s: read_frame returned None (EOF)", ws_id)
                    return
                opcode, payload = result
                if opcode == OP_CLOSE:
                    log.info("API-WS %s: client sent CLOSE", ws_id)
                    return
                elif opcode == OP_PING:
                    writer.write(make_frame(OP_PONG, payload))
                    await writer.drain()
                elif opcode == OP_PONG:
                    log.debug("API-WS %s: PONG received", ws_id)

        async def _ping_loop():
            await asyncio.sleep(3)
            while True:
                try:
                    writer.write(make_frame(OP_PING, b""))
                    await writer.drain()
                    log.debug("API-WS %s: PING sent", ws_id)
                except Exception as e:
                    log.info("API-WS %s: ping failed: %s", ws_id, e)
                    return
                await asyncio.sleep(20)

        try:
            read_task = asyncio.create_task(_read_loop())
            ping_task = asyncio.create_task(_ping_loop())
            done, pending = await asyncio.wait(
                [read_task, ping_task], return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        except Exception as e:
            log.info("API-WS %s error: %s", ws_id, e)
        finally:
            self._api_ws_clients.pop(ws_id, None)
            log.info("API-WS %s disconnected", ws_id)
            writer.close()


# ── Entry point ─────────────────────────────────────────────────────


async def run_relay(host: str, port: int, token: str | None, web_root: str | None = None) -> None:
    relay = Relay(token=token, web_root=web_root)
    # family=AF_INET avoids dual-stack issues in some Docker environments
    import socket
    server = await asyncio.start_server(
        relay.handle_connection, host, port,
        family=socket.AF_INET,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("CAM Relay listening on %s", addrs)
    if token:
        log.info("Auth token: %s...%s", token[:4], token[-4:])
    else:
        log.warning("No auth token — relay is open!")
    if web_root:
        log.info("Serving static files from %s", web_root)

    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="CAM Relay — WebSocket proxy")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8443, help="Listen port")
    parser.add_argument("--token", default=None, help="Auth token")
    parser.add_argument("--web-root", default=None, help="Directory to serve static files from")
    args = parser.parse_args()

    try:
        asyncio.run(run_relay(args.host, args.port, args.token, args.web_root))
    except KeyboardInterrupt:
        log.info("Relay stopped")


if __name__ == "__main__":
    main()
