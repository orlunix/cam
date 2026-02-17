"""File browser REST endpoints for context directories."""

from __future__ import annotations

import base64
import os

from fastapi import APIRouter, HTTPException, Request

from cam.transport.factory import TransportFactory

router = APIRouter(tags=["files"])


async def _require_auth(request: Request):
    """Validate auth token from request headers."""
    state = request.app.state.server
    await state.token_auth(authorization=request.headers.get("authorization"))
    return state


def _resolve_path(context_path: str, subpath: str) -> str:
    """Resolve a subpath relative to the context root, preventing traversal."""
    if not subpath or subpath == "/":
        return context_path
    # Normalize and join
    full = os.path.normpath(os.path.join(context_path, subpath.lstrip("/")))
    # Security: must stay within context root
    if not full.startswith(context_path.rstrip("/") + "/") and full != context_path:
        raise HTTPException(status_code=403, detail="Path outside context root")
    return full


@router.get("/contexts/{name_or_id}/files")
async def list_files(name_or_id: str, request: Request, path: str = ""):
    """List files and directories in a context's working directory."""
    state = await _require_auth(request)

    context = state.context_store.get(name_or_id)
    if not context:
        raise HTTPException(status_code=404, detail=f"Context not found: {name_or_id}")

    full_path = _resolve_path(context.path, path)
    transport = TransportFactory.create(context.machine)

    try:
        entries = await transport.list_files(full_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Compute parent path for navigation
    parent = None
    if path and path != "/":
        parent_sub = os.path.dirname(path.rstrip("/"))
        if parent_sub:
            parent = parent_sub

    return {
        "context_id": str(context.id),
        "context_name": context.name,
        "path": path or "",
        "parent": parent,
        "entries": entries,
    }


@router.get("/contexts/{name_or_id}/files/read")
async def read_file(name_or_id: str, request: Request, path: str = ""):
    """Read a file's content from a context's working directory."""
    state = await _require_auth(request)

    context = state.context_store.get(name_or_id)
    if not context:
        raise HTTPException(status_code=404, detail=f"Context not found: {name_or_id}")

    if not path:
        raise HTTPException(status_code=400, detail="path parameter required")

    full_path = _resolve_path(context.path, path)
    transport = TransportFactory.create(context.machine)

    try:
        data = await transport.read_file(full_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not data:
        raise HTTPException(status_code=404, detail="File not found or empty")

    # Detect binary content
    is_binary = b"\x00" in data[:8192]

    if is_binary:
        return {
            "path": path,
            "binary": True,
            "size": len(data),
            "content": base64.b64encode(data).decode("ascii"),
        }

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    return {
        "path": path,
        "binary": False,
        "size": len(data),
        "content": text,
    }
