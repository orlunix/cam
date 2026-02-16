"""CAM Relay Connector — outbound WebSocket bridge to relay.

Connects from CAM Server (private IP) to a relay (public IP) and tunnels
HTTP API requests + WebSocket event streams through the relay.

Protocol:
    Client → Relay → Server: JSON request frame
    Server → Relay → Client: JSON response frame

Request frame:
    {"id": "req-123", "method": "GET", "path": "/api/agents", "headers": {...}, "body": "..."}

Response frame:
    {"id": "req-123", "status": 200, "headers": {...}, "body": "..."}

Event stream:
    {"id": "ws-setup", "method": "WS", "path": "/api/ws?agent_id=xxx"}
    → server starts streaming events back with {"id": "ws-setup", "event": {...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
logger = logging.getLogger(__name__)


async def relay_loop(
    relay_url: str,
    relay_token: str | None,
    app,
    reconnect_delay: float = 5.0,
    max_delay: float = 60.0,
) -> None:
    """Connect outbound to relay and process proxied requests.

    Runs forever with auto-reconnect on disconnect.

    Args:
        relay_url: WebSocket URL of the relay (e.g. ws://relay:8443)
        relay_token: Shared secret for relay auth
        app: The FastAPI app instance (for ASGI dispatch)
        reconnect_delay: Initial delay between reconnect attempts
        max_delay: Maximum reconnect delay
    """
    import websockets

    delay = reconnect_delay
    url = f"{relay_url}/server"
    if relay_token:
        url += f"?token={relay_token}"

    while True:
        try:
            logger.info("Connecting to relay: %s", relay_url)
            async with websockets.connect(url, compression=None, proxy=None, ping_interval=20, ping_timeout=20, close_timeout=10, max_size=10_000_000) as ws:
                logger.info("Connected to relay successfully")
                delay = reconnect_delay  # Reset on successful connect

                async for raw_message in ws:
                    try:
                        request = json.loads(raw_message)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from relay: %s", raw_message[:100])
                        continue

                    req_id = request.get("id", "")
                    method = request.get("method", "GET")
                    path = request.get("path", "/")
                    headers = request.get("headers", {})
                    body = request.get("body", "")

                    if method == "WS":
                        # WebSocket event stream — handle in background
                        asyncio.create_task(
                            _handle_ws_stream(ws, req_id, path, app)
                        )
                        continue

                    # HTTP request — dispatch through ASGI
                    response = await _dispatch_http(app, method, path, headers, body)
                    response["id"] = req_id

                    try:
                        await ws.send(json.dumps(response))
                    except Exception:
                        break

        except asyncio.CancelledError:
            logger.info("Relay connector cancelled")
            return
        except Exception as e:
            logger.warning("Relay connection error: %s (reconnecting in %.0fs)", e, delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


async def _dispatch_http(app, method: str, path: str, headers: dict, body: str) -> dict:
    """Dispatch an HTTP request through the FastAPI ASGI app and return the response."""
    from starlette.testclient import TestClient
    import io

    # Use ASGI transport for in-process dispatch
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": path.split("?")[0],
        "query_string": (path.split("?", 1)[1] if "?" in path else "").encode(),
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in headers.items()
        ],
        "root_path": "",
    }

    body_bytes = body.encode() if isinstance(body, str) else body
    response_started = False
    status_code = 500
    response_headers = {}
    response_body = bytearray()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        nonlocal response_started, status_code, response_headers, response_body
        if message["type"] == "http.response.start":
            response_started = True
            status_code = message["status"]
            response_headers = {
                k.decode(): v.decode()
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    try:
        await app(scope, receive, send)
    except Exception as e:
        return {"status": 500, "headers": {}, "body": json.dumps({"error": str(e)})}

    return {
        "status": status_code,
        "headers": response_headers,
        "body": response_body.decode(errors="replace"),
    }


async def _handle_ws_stream(ws, req_id: str, path: str, app) -> None:
    """Stream WebSocket events back through the relay connection.

    Subscribes to the server's EventBus and forwards events to the relay
    connection as JSON frames tagged with the request ID.
    """
    state = app.state.server

    # Parse agent_id from path query params
    agent_id = None
    if "?" in path:
        from urllib.parse import parse_qs
        params = parse_qs(path.split("?", 1)[1])
        agent_id = params.get("agent_id", [None])[0]

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)

    def event_handler(event):
        if agent_id and event.agent_id != agent_id:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    state.event_bus.subscribe("*", event_handler)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

            payload = {
                "id": req_id,
                "event": {
                    "type": "event",
                    "agent_id": event.agent_id,
                    "event_type": event.event_type,
                    "timestamp": (
                        event.timestamp.isoformat()
                        if hasattr(event.timestamp, "isoformat")
                        else str(event.timestamp)
                    ),
                    "detail": event.detail,
                },
            }
            try:
                await ws.send(json.dumps(payload))
            except Exception:
                break
    finally:
        state.event_bus.unsubscribe("*", event_handler)
