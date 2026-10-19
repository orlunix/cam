"""Reverse-tunnel registry REST endpoints.

Hosts running `camb reverse up` register their allocated server-side
port here; the tunnel hub uses the registry to dispatch camc operations
onto hosts without per-request SSH.

Auth: the master token always works; a host may register/unregister
itself with its derived token sha256(master + "/" + key).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["tunnels"])


def _state(request: Request):
    return request.app.state.server


def _authorized(request: Request, master: str, key: str | None) -> bool:
    """Master Bearer token, or the derived per-host token for `key`."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    if master and hmac.compare_digest(token, master):
        return True
    if key:
        derived = hashlib.sha256(
            (master + "/" + key).encode()).hexdigest()
        if master and hmac.compare_digest(token, derived):
            return True
    return False


def _deny():
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


@router.post("/tunnels/register")
async def register_tunnel(request: Request):
    state = _state(request)
    body = await request.json()
    key = str(body.get("key") or body.get("name") or "")
    if not key:
        return JSONResponse({"detail": "missing key"}, status_code=400)
    if not _authorized(request, state.auth_token, key):
        return _deny()
    hub = getattr(state, "tunnel_hub", None)
    if hub is None:
        return JSONResponse({"detail": "tunnel hub disabled"},
                            status_code=503)
    try:
        entry = hub.registry.register({
            "key": key,
            "name": body.get("name") or key,
            "port": int(body.get("port")),
            "token": str(body.get("token") or ""),
            "ts": float(body.get("ts") or time.time()),
        })
    except (ValueError, TypeError) as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True, "tunnel": {k: v for k, v in entry.items()
                                   if k != "token"}}


@router.get("/tunnels")
async def list_tunnels(request: Request):
    state = _state(request)
    if not _authorized(request, state.auth_token, None):
        return _deny()
    hub = getattr(state, "tunnel_hub", None)
    if hub is None:
        return JSONResponse({"detail": "tunnel hub disabled"},
                            status_code=503)
    out = []
    for e in hub.registry.list():
        row = {k: v for k, v in e.items() if k != "token"}
        row["alive"] = hub.registry.port_open(e["port"])
        out.append(row)
    return {"tunnels": out}


@router.delete("/tunnels/{key}")
async def delete_tunnel(request: Request, key: str):
    state = _state(request)
    if not _authorized(request, state.auth_token, key):
        return _deny()
    hub = getattr(state, "tunnel_hub", None)
    if hub is None:
        return JSONResponse({"detail": "tunnel hub disabled"},
                            status_code=503)
    removed = hub.registry.unregister(key)
    return {"ok": removed}
