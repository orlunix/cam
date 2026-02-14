"""WebSocket event stream for CAM API Server.

Bridges the synchronous EventBus to async WebSocket connections.
Also polls AgentStore for state changes from background monitor subprocesses
that have their own EventBus instances.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws")
async def ws_event_stream(
    websocket: WebSocket,
    token: str | None = Query(None),
    agent_id: str | None = Query(None),
):
    """Stream real-time events over WebSocket.

    Query params:
        token: Auth token (required).
        agent_id: Optional filter — only receive events for this agent.
    """
    state = websocket.app.state.server

    # Auth
    if not state.ws_token_auth.validate(token):
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()

    # Bridge: sync EventBus → asyncio.Queue → WebSocket
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)

    def event_handler(event):
        """Sync callback from EventBus.publish(). Enqueues for WS send."""
        if agent_id and event.agent_id != agent_id:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("WebSocket event queue full, dropping event")

    state.event_bus.subscribe("*", event_handler)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                # No event from EventBus — poll store for background monitor changes
                await _poll_status_changes(websocket, state, agent_id)
                continue

            payload = {
                "type": "event",
                "agent_id": event.agent_id,
                "event_type": event.event_type,
                "timestamp": (
                    event.timestamp.isoformat()
                    if hasattr(event.timestamp, "isoformat")
                    else str(event.timestamp)
                ),
                "detail": event.detail,
            }
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
    finally:
        state.event_bus.unsubscribe("*", event_handler)


# Track last-seen status per agent for polling-based change detection
_agent_status_cache: dict[str, str] = {}


async def _poll_status_changes(
    websocket: WebSocket,
    state,
    agent_id_filter: str | None,
):
    """Poll AgentStore for status changes from background monitors.

    Background monitor subprocesses have their own EventBus, so their events
    don't flow through the server's EventBus. This polls the DB to detect
    changes and emit synthetic status_update events.
    """
    from cam.core.models import AgentStatus

    try:
        agents = state.agent_store.list(limit=50)
    except Exception:
        return

    for agent in agents:
        aid = str(agent.id)
        if agent_id_filter and aid != agent_id_filter:
            continue

        current = agent.status.value
        previous = _agent_status_cache.get(aid)

        if previous is not None and current != previous:
            await websocket.send_json({
                "type": "status_update",
                "agent_id": aid,
                "status": current,
                "state": agent.state.value,
                "exit_reason": agent.exit_reason,
            })

        _agent_status_cache[aid] = current
