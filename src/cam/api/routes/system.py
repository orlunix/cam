"""System REST endpoints."""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cam import __version__
from cam.api.schemas import HealthResponse
from cam.core.models import AgentStatus

router = APIRouter(tags=["system"])

_WEB_DIR = Path(__file__).parent.parent.parent.parent.parent / "web"


@router.get("/system/health", response_model=HealthResponse)
async def health(request: Request):
    """Health check (no auth required)."""
    state = request.app.state.server
    running = state.agent_store.list(status=AgentStatus.RUNNING)
    return HealthResponse(
        version=__version__,
        uptime_seconds=round(time.time() - state.started_at, 1),
        agents_running=len(running),
        adapters=state.adapter_registry.names(),
    )


@router.get("/system/config")
async def get_config(request: Request):
    """Current server configuration (auth required, sensitive fields removed)."""
    state = request.app.state.server
    await state.token_auth(authorization=request.headers.get("authorization"))

    config_dict = state.config.model_dump(mode="json")
    config_dict.pop("security", None)
    if "server" in config_dict:
        config_dict["server"].pop("auth_token", None)
    return config_dict


def _get_apk_version() -> str | None:
    """Read cam-version from bundled index.html."""
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return None
    text = index.read_text()
    m = re.search(r'cam-version.*?content="([^"]+)"', text)
    return m.group(1) if m else None


@router.get("/system/apk/info")
async def apk_info(request: Request):
    """Return APK version and size."""
    apk_path = _WEB_DIR / "assets" / "cam.apk"
    if not apk_path.exists():
        return JSONResponse({"error": "APK not found"}, status_code=404)
    return {
        "version": _get_apk_version(),
        "size": apk_path.stat().st_size,
    }


@router.get("/system/apk/download")
async def apk_download(request: Request):
    """Return APK as base64 JSON (for relay-only clients)."""
    state = request.app.state.server
    await state.token_auth(authorization=request.headers.get("authorization"))

    apk_path = _WEB_DIR / "assets" / "cam.apk"
    if not apk_path.exists():
        return JSONResponse({"error": "APK not found"}, status_code=404)
    data = apk_path.read_bytes()
    return {
        "version": _get_apk_version(),
        "size": len(data),
        "data": base64.b64encode(data).decode(),
    }
