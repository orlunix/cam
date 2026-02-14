"""Context REST endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from cam.api.schemas import CreateContextRequest

router = APIRouter(tags=["contexts"])


async def _require_auth(request: Request):
    """Validate auth token from request headers."""
    state = request.app.state.server
    await state.token_auth(authorization=request.headers.get("authorization"))
    return state


@router.get("/contexts")
async def list_contexts(request: Request):
    """List all contexts."""
    state = await _require_auth(request)

    contexts = state.context_store.list()
    return {
        "contexts": [c.model_dump(mode="json") for c in contexts],
        "count": len(contexts),
    }


@router.post("/contexts", status_code=201)
async def create_context(body: CreateContextRequest, request: Request):
    """Create a new context."""
    state = await _require_auth(request)

    from cam.core.models import Context, MachineConfig, TransportType

    transport_type = TransportType.SSH if body.host else TransportType.LOCAL

    try:
        machine = MachineConfig(
            type=transport_type,
            host=body.host,
            user=body.user,
            port=body.port,
            key_file=body.key_file,
            env_setup=body.env_setup,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        context = Context(
            id=str(uuid4()),
            name=body.name,
            path=body.path,
            machine=machine,
            tags=body.tags,
            created_at=datetime.now(timezone.utc),
        )
        state.context_store.add(context)
        return context.model_dump(mode="json")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/contexts/{name_or_id}")
async def get_context(name_or_id: str, request: Request):
    """Get context detail."""
    state = await _require_auth(request)

    context = state.context_store.get(name_or_id)
    if not context:
        raise HTTPException(
            status_code=404, detail=f"Context not found: {name_or_id}"
        )
    return context.model_dump(mode="json")


@router.put("/contexts/{name_or_id}")
async def update_context(name_or_id: str, body: CreateContextRequest, request: Request):
    """Update an existing context."""
    state = await _require_auth(request)

    existing = state.context_store.get(name_or_id)
    if not existing:
        raise HTTPException(
            status_code=404, detail=f"Context not found: {name_or_id}"
        )

    from cam.core.models import MachineConfig, TransportType

    transport_type = TransportType.SSH if body.host else TransportType.LOCAL
    try:
        machine = MachineConfig(
            type=transport_type,
            host=body.host,
            user=body.user,
            port=body.port,
            key_file=body.key_file,
            env_setup=body.env_setup,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing.name = body.name
    existing.path = body.path
    existing.machine = machine
    existing.tags = body.tags

    try:
        state.context_store.update(existing)
        return existing.model_dump(mode="json")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/contexts/{name_or_id}")
async def delete_context(name_or_id: str, request: Request):
    """Remove a context."""
    state = await _require_auth(request)

    removed = state.context_store.remove(name_or_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"Context not found: {name_or_id}"
        )
    return {"ok": True}
