"""API request/response schemas for CAM API Server.

Thin wrappers over core models. Internal fields (tmux_session, tmux_socket)
are excluded from API responses to keep the wire protocol clean.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cam.core.models import Agent


class RunAgentRequest(BaseModel):
    """POST /api/agents request body."""

    tool: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context: str | None = None
    name: str | None = None
    timeout: str | None = None
    retry: int = Field(default=0, ge=0)
    env: dict[str, str] = Field(default_factory=dict)
    auto_confirm: bool | None = None


class AgentResponse(BaseModel):
    """Agent detail in API responses."""

    id: str
    task_name: str | None
    tool: str
    prompt: str
    context_name: str
    status: str
    state: str
    started_at: str | None
    completed_at: str | None
    exit_reason: str | None
    retry_count: int
    cost_estimate: float | None
    files_changed: list[str]
    auto_confirm: bool | None = None


class AgentListResponse(BaseModel):
    """GET /api/agents response."""

    agents: list[AgentResponse]
    count: int


class CreateContextRequest(BaseModel):
    """POST /api/contexts request body."""

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    host: str | None = None
    user: str | None = None
    port: int | None = None
    key_file: str | None = None
    env_setup: str | None = None
    tags: list[str] = Field(default_factory=list)


class SendInputRequest(BaseModel):
    """POST /api/agents/{id}/input request body."""

    text: str
    send_enter: bool = True


class SendKeyRequest(BaseModel):
    """POST /api/agents/{id}/key request body."""

    key: str = Field(min_length=1)


class HealthResponse(BaseModel):
    """GET /api/system/health response."""

    status: str = "ok"
    version: str
    uptime_seconds: float
    agents_running: int


def agent_to_response(agent: Agent) -> AgentResponse:
    """Convert internal Agent model to API response."""
    return AgentResponse(
        id=str(agent.id),
        task_name=agent.task.name,
        tool=agent.task.tool,
        prompt=agent.task.prompt,
        context_name=agent.context_name,
        status=agent.status.value,
        state=agent.state.value,
        started_at=str(agent.started_at) if agent.started_at else None,
        completed_at=str(agent.completed_at) if agent.completed_at else None,
        exit_reason=agent.exit_reason,
        retry_count=agent.retry_count,
        cost_estimate=agent.cost_estimate,
        files_changed=agent.files_changed,
        auto_confirm=agent.task.auto_confirm,
    )
