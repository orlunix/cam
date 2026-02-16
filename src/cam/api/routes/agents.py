"""Agent REST endpoints."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from cam.api.schemas import (
    AgentListResponse,
    AgentResponse,
    RunAgentRequest,
    SendInputRequest,
    SendKeyRequest,
    agent_to_response,
)

router = APIRouter(tags=["agents"])


async def _require_auth(request: Request):
    """Validate auth token from request headers."""
    state = request.app.state.server
    await state.token_auth(authorization=request.headers.get("authorization"))
    return state


@router.post("/agents", response_model=AgentResponse)
async def run_agent(body: RunAgentRequest, request: Request):
    """Start a new coding agent."""
    state = await _require_auth(request)

    from cam.core.config import parse_duration
    from cam.core.models import RetryPolicy, TaskDefinition

    # Resolve context
    if not body.context:
        raise HTTPException(status_code=400, detail="Context name is required")

    context = state.context_store.get(body.context)
    if not context:
        raise HTTPException(
            status_code=404, detail=f"Context not found: {body.context}"
        )

    timeout_seconds = parse_duration(body.timeout) if body.timeout else None
    task_name = body.name or f"{body.tool}-{uuid4().hex[:6]}"

    task = TaskDefinition(
        name=task_name,
        tool=body.tool,
        prompt=body.prompt,
        context=body.context,
        timeout=timeout_seconds,
        retry=RetryPolicy(max_retries=body.retry),
        env=body.env,
        auto_confirm=body.auto_confirm,
    )

    try:
        agent = await state.agent_manager.run_agent(task, context, follow=False)
        return agent_to_response(agent)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    status: str | None = None,
    tool: str | None = None,
    context: str | None = None,
    limit: int = 20,
):
    """List agents with optional filters."""
    state = await _require_auth(request)

    from cam.core.models import AgentStatus

    filters: dict = {}
    if status:
        try:
            filters["status"] = AgentStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid status: {status}"
            )
    if tool:
        filters["tool"] = tool
    if context:
        ctx = state.context_store.get(context)
        if ctx:
            filters["context_id"] = str(ctx.id)
    if limit:
        filters["limit"] = limit

    agents = await state.agent_manager.list_agents(**filters)
    return AgentListResponse(
        agents=[agent_to_response(a) for a in agents],
        count=len(agents),
    )


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, request: Request):
    """Get agent detail."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )
    return agent_to_response(agent)


@router.delete("/agents/{agent_id}")
async def stop_agent(agent_id: str, request: Request, force: bool = False):
    """Stop/kill an agent."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    try:
        await state.agent_manager.stop_agent(str(agent.id), graceful=not force)
        return {"ok": True, "agent_id": str(agent.id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agents/{agent_id}/history")
async def delete_agent_history(agent_id: str, request: Request):
    """Delete an agent record from history (only terminal agents)."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if not agent.is_terminal():
        raise HTTPException(
            status_code=400, detail="Cannot delete a running agent. Stop it first."
        )

    deleted = state.agent_store.delete(str(agent.id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"ok": True, "agent_id": str(agent.id)}


@router.get("/agents/{agent_id}/logs")
async def get_logs(agent_id: str, request: Request, tail: int = 50):
    """Read JSONL logs for an agent."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    from cam.utils.logging import AgentLogger

    agent_logger = AgentLogger(str(agent.id))
    entries = agent_logger.read_lines(tail=tail)
    return {"agent_id": str(agent.id), "entries": entries}


@router.get("/agents/{agent_id}/output")
async def get_output(agent_id: str, request: Request, lines: int = 50):
    """Capture live TMUX output for an agent."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if not agent.tmux_session or agent.is_terminal():
        return {"agent_id": str(agent.id), "output": "", "active": False}

    context = state.context_store.get(str(agent.context_id))
    if not context:
        return {"agent_id": str(agent.id), "output": "", "active": False}

    from cam.transport.factory import TransportFactory

    transport = TransportFactory.create(context.machine)
    try:
        output = await transport.capture_output(agent.tmux_session, lines=lines)
        return {"agent_id": str(agent.id), "output": output, "active": True}
    except Exception:
        return {"agent_id": str(agent.id), "output": "", "active": False}


@router.get("/agents/{agent_id}/fulloutput")
async def get_full_output(
    agent_id: str, request: Request, offset: int = 0
):
    """Read full output log for an agent, supports incremental fetching.

    Returns output from byte offset onwards. Client should track the
    returned next_offset and pass it on subsequent calls to get only new data.
    """
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    from cam.constants import LOG_DIR

    log_path = LOG_DIR / "output" / f"{agent.id}.log"

    # Try local log file first (works for local transport)
    if log_path.exists():
        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(offset)
                data = f.read(256_000)  # max 256KB per request
                next_offset = f.tell()
            return {
                "agent_id": str(agent.id),
                "output": data,
                "next_offset": next_offset,
                "active": not agent.is_terminal(),
            }
        except Exception:
            pass

    # Fallback: read from transport (e.g. SSH remote log)
    context = state.context_store.get(str(agent.context_id))
    if context and agent.tmux_session:
        from cam.transport.factory import TransportFactory

        transport = TransportFactory.create(context.machine)
        try:
            data, next_offset = await transport.read_output_log(
                agent.tmux_session, offset
            )
            if data:
                return {
                    "agent_id": str(agent.id),
                    "output": data,
                    "next_offset": next_offset,
                    "active": not agent.is_terminal(),
                }
        except Exception:
            pass

    return {
        "agent_id": str(agent.id),
        "output": "",
        "next_offset": offset,
        "active": not agent.is_terminal(),
    }


@router.post("/agents/{agent_id}/input")
async def send_input(agent_id: str, body: SendInputRequest, request: Request):
    """Send input to a running agent."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if not agent.tmux_session or agent.is_terminal():
        raise HTTPException(status_code=400, detail="Agent is not running")

    context = state.context_store.get(str(agent.context_id))
    if not context:
        raise HTTPException(status_code=400, detail="Agent context not found")

    from cam.transport.factory import TransportFactory

    transport = TransportFactory.create(context.machine)
    ok = await transport.send_input(
        agent.tmux_session, body.text, send_enter=body.send_enter
    )
    return {"ok": ok}


@router.post("/agents/{agent_id}/key")
async def send_key(agent_id: str, body: SendKeyRequest, request: Request):
    """Send a TMUX special key to a running agent."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if not agent.tmux_session or agent.is_terminal():
        raise HTTPException(status_code=400, detail="Agent is not running")

    context = state.context_store.get(str(agent.context_id))
    if not context:
        raise HTTPException(status_code=400, detail="Agent context not found")

    from cam.transport.factory import TransportFactory

    transport = TransportFactory.create(context.machine)
    ok = await transport.send_key(agent.tmux_session, body.key)
    return {"ok": ok}
