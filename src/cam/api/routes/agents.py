"""Agent REST endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from cam.api.schemas import (
    AgentListResponse,
    AgentResponse,
    UpdateAgentRequest,
    RunAgentRequest,
    SendInputRequest,
    SendKeyRequest,
    UploadFileRequest,
    agent_to_response,
)

router = APIRouter(tags=["agents"])

# Output cache: {(agent_id, lines): (response_dict, timestamp)}
_output_cache: dict[tuple[str, int], tuple[dict, float]] = {}
_OUTPUT_CACHE_TTL = 2.0  # seconds
# Lock per cache key to prevent thundering herd on cache miss —
# only one SSH capture_output in flight per agent at a time.
_output_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _cleanup_output_caches(agent_id: str) -> None:
    """Remove output cache and lock entries for a given agent ID."""
    keys_to_remove = [k for k in _output_cache if k[0] == agent_id]
    for k in keys_to_remove:
        _output_cache.pop(k, None)
        _output_locks.pop(k, None)


def cleanup_agent_caches(agent_id: str) -> None:
    """Remove all cached entries for a given agent ID.

    Use this when an agent is deleted or stopped — not for routine
    terminal checks, which should use _cleanup_output_caches() instead
    to avoid clearing the WS status cache prematurely.
    """
    from cam.api.ws import _agent_status_cache

    _cleanup_output_caches(agent_id)
    _agent_status_cache.pop(agent_id, None)


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


@router.patch("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, body: UpdateAgentRequest, request: Request):
    """Update agent settings (name, auto_confirm, etc.)."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if body.name is not None:
        agent.task.name = body.name
    if body.auto_confirm is not None:
        agent.task.auto_confirm = body.auto_confirm
    state.agent_store.save(agent)
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
        cleanup_agent_caches(str(agent.id))
        return {"ok": True, "agent_id": str(agent.id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/{agent_id}/restart", response_model=AgentResponse)
async def restart_agent(agent_id: str, request: Request):
    """Restart a terminal agent with the same task/context configuration."""
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if not agent.is_terminal():
        raise HTTPException(
            status_code=400,
            detail=f"Agent is still {agent.status.value}. Stop it first.",
        )

    # Resolve context
    context = (
        state.context_store.get(agent.context_name)
        or state.context_store.get(str(agent.context_id))
    )
    if not context:
        raise HTTPException(
            status_code=404,
            detail=f"Original context not found: {agent.context_name}",
        )

    try:
        new_agent = await state.agent_manager.run_agent(
            agent.task, context, follow=False
        )
        return agent_to_response(new_agent)
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
    cleanup_agent_caches(str(agent.id))
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
async def get_output(
    agent_id: str, request: Request, lines: int = 50, hash: str | None = None
):
    """Capture live TMUX output for an agent.

    If `hash` is provided and matches the current output, returns
    `unchanged: true` with no output body (~50 bytes vs ~7KB).
    """
    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(
            status_code=404, detail=f"Agent not found: {agent_id}"
        )

    if not agent.tmux_session or agent.is_terminal():
        _cleanup_output_caches(str(agent.id))
        return {"agent_id": str(agent.id), "output": "", "active": False}

    # Check cache — always stores full output + hash
    cache_key = (str(agent.id), lines)
    cached = _output_cache.get(cache_key)
    if cached:
        cached_resp, cached_ts = cached
        if time.monotonic() - cached_ts < _OUTPUT_CACHE_TTL:
            if hash and cached_resp.get("hash") == hash:
                return {"agent_id": str(agent.id), "unchanged": True, "active": True, "hash": hash}
            return cached_resp

    # Acquire per-key lock to prevent thundering herd: only one SSH
    # capture in flight per agent.  Other requests wait then hit cache.
    lock = _output_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        # Re-check cache — another coroutine may have refreshed it while we waited
        cached = _output_cache.get(cache_key)
        if cached:
            cached_resp, cached_ts = cached
            if time.monotonic() - cached_ts < _OUTPUT_CACHE_TTL:
                if hash and cached_resp.get("hash") == hash:
                    return {"agent_id": str(agent.id), "unchanged": True, "active": True, "hash": hash}
                return cached_resp

        context = state.context_store.get(str(agent.context_id))
        if not context:
            return {"agent_id": str(agent.id), "output": "", "active": False}

        from cam.transport.factory import TransportFactory

        transport = TransportFactory.create(context.machine)
        try:
            output = await transport.capture_output(agent.tmux_session, lines=lines)
            output_hash = hashlib.md5(output.encode()).hexdigest()[:8]
            resp = {"agent_id": str(agent.id), "output": output, "active": True, "hash": output_hash}
            _output_cache[cache_key] = (resp, time.monotonic())
            if hash and output_hash == hash:
                return {"agent_id": str(agent.id), "unchanged": True, "active": True, "hash": output_hash}
            return resp
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
    from cam.utils.terminal import render_raw_data, render_raw_log

    log_path = LOG_DIR / "output" / f"{agent.id}.log"

    # Try local log file first (works for local transport)
    # Always render from the start — pyte needs the full terminal byte
    # stream to correctly reconstruct screen state (cursor, clears, etc.).
    # The offset is only used to detect whether the file has grown.
    if log_path.exists():
        try:
            file_size = log_path.stat().st_size
            if file_size > offset:
                output = render_raw_log(log_path)
                return {
                    "agent_id": str(agent.id),
                    "output": output,
                    "next_offset": file_size,
                    "active": not agent.is_terminal(),
                }
        except Exception:
            pass

    # Fallback: read from transport (e.g. SSH remote log)
    context = state.context_store.get(str(agent.context_id))
    if context and agent.tmux_session:
        from cam.transport.factory import TransportFactory

        transport = TransportFactory.create(context.machine)
        if hasattr(transport, 'read_output_log'):
            try:
                raw_data, next_offset = await transport.read_output_log(
                    agent.tmux_session, 0, max_bytes=2_000_000
                )
                if raw_data and next_offset > offset:
                    output = render_raw_data(raw_data)
                    return {
                        "agent_id": str(agent.id),
                        "output": output,
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


@router.post("/agents/{agent_id}/upload")
async def upload_file(agent_id: str, body: UploadFileRequest, request: Request):
    """Upload a file to the agent's context path.

    Accepts base64-encoded file data, writes it to .cam-images/ under the
    context path, and returns the absolute path for the agent to read.
    """
    import base64
    import re
    from datetime import datetime

    state = await _require_auth(request)

    agent = await state.agent_manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    if not agent.tmux_session or agent.is_terminal():
        raise HTTPException(status_code=400, detail="Agent is not running")

    context = state.context_store.get(str(agent.context_id))
    if not context:
        raise HTTPException(status_code=400, detail="Agent context not found")

    # Decode base64 data
    try:
        file_data = base64.b64decode(body.data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 data")

    # Sanitize filename: keep alphanumeric, dots, hyphens, underscores
    safe_name = re.sub(r"[^\w.\-]", "_", body.filename)
    if not safe_name:
        safe_name = "image.png"

    # Build path: {context_path}/.cam-images/{timestamp}-{filename}
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_path = f"{context.path}/.cam-images/{timestamp}-{safe_name}"

    from cam.transport.factory import TransportFactory

    transport = TransportFactory.create(context.machine)
    ok = await transport.write_file(dest_path, file_data)

    if not ok:
        raise HTTPException(status_code=500, detail="Failed to write file")

    return {"ok": True, "path": dest_path, "size": len(file_data)}
