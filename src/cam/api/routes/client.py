"""Client sync endpoint for cam-client remote monitors.

cam-client runs on the target machine and pushes output/state/events
to the server via this endpoint, replacing SSH-based polling.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["client"])


class ClientSyncRequest(BaseModel):
    """POST /api/client/{agent_id}/sync request body."""

    output: str | None = None
    output_hash: str | None = None
    state: str | None = None
    status: str | None = None
    exit_reason: str | None = None
    events: list[dict] = Field(default_factory=list)
    cost_estimate: float | None = None
    files_changed: list[str] | None = None


class ClientSyncResponse(BaseModel):
    """Response returned to cam-client."""

    commands: list[dict] = Field(default_factory=list)
    auto_confirm: bool | None = None
    interval: float = 2.0


async def _require_auth(request: Request):
    """Validate auth token from request headers."""
    state = request.app.state.server
    await state.token_auth(authorization=request.headers.get("authorization"))
    return state


@router.post("/client/{agent_id}/sync", response_model=ClientSyncResponse)
async def client_sync(agent_id: str, body: ClientSyncRequest, request: Request):
    """Receive state push from cam-client and return pending commands.

    This is the single endpoint cam-client calls every 1-2 seconds.
    It replaces SSH-based output capture, auto-confirm, and state detection
    for agents that have a cam-client running on the target machine.
    """
    state = await _require_auth(request)

    # Verify agent exists
    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    # Register this agent as having an active cam-client
    state.client_agents.add(agent_id)

    # 1. Update output cache
    if body.output is not None:
        state.client_output[agent_id] = (body.output, body.output_hash or "", time.time())
    elif body.output_hash and agent_id in state.client_output:
        # Output unchanged — just update timestamp
        old_output, _, _ = state.client_output[agent_id]
        state.client_output[agent_id] = (old_output, body.output_hash, time.time())

    # 2. Update agent state if changed
    if body.state:
        from cam.core.models import AgentState

        try:
            new_state = AgentState(body.state)
            if new_state != agent.state:
                state.agent_store.update_status(
                    agent_id, agent.status, state=new_state
                )
        except ValueError:
            pass

    # 3. Update terminal status if reported
    if body.status in ("completed", "failed"):
        from cam.core.models import AgentStatus

        try:
            new_status = AgentStatus(body.status)
            state.agent_store.update_status(
                agent_id, new_status,
                exit_reason=body.exit_reason,
            )
        except ValueError:
            pass

    # 4. Store events
    if body.events:
        from cam.core.models import AgentEvent

        for ev in body.events:
            event = AgentEvent(
                agent_id=agent_id,
                event_type=ev.get("type", "unknown"),
                detail=ev.get("detail", {}),
            )
            try:
                state.agent_store.add_event(event)
            except Exception:
                pass
            state.event_bus.publish(event)

    # 5. Update cost/files if provided
    if body.cost_estimate is not None or body.files_changed is not None:
        fresh = await state.agent_manager.get_agent(agent_id)
        if fresh:
            if body.cost_estimate is not None:
                fresh.cost_estimate = body.cost_estimate
            if body.files_changed is not None:
                fresh.files_changed = body.files_changed
            state.agent_store.save(fresh)

    # 6. Drain pending commands
    commands = state.client_commands.pop(agent_id, [])

    # 7. Read current auto_confirm from DB (so API changes propagate)
    fresh = await state.agent_manager.get_agent(agent_id)
    auto_confirm = fresh.task.auto_confirm if fresh else None

    return ClientSyncResponse(
        commands=commands,
        auto_confirm=auto_confirm,
        interval=2.0,
    )
