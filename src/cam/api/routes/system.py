"""System REST endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from cam import __version__
from cam.api.schemas import HealthResponse
from cam.core.models import AgentStatus

router = APIRouter(tags=["system"])


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
