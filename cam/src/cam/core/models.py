"""
CAM System - Core Data Models

Pydantic v2 models for the Coding Agent Manager system.
All models support JSON serialization and include proper validation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentStatus(str, Enum):
    """Agent execution status."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    KILLED = "killed"
    RETRYING = "retrying"


class AgentState(str, Enum):
    """Agent internal state during execution."""

    INITIALIZING = "initializing"
    PLANNING = "planning"
    EDITING = "editing"
    TESTING = "testing"
    COMMITTING = "committing"
    IDLE = "idle"


class TransportType(str, Enum):
    """Transport mechanism for agent communication."""

    LOCAL = "local"
    SSH = "ssh"
    WEBSOCKET = "websocket"
    DOCKER = "docker"
    OPENCLAW = "openclaw"


class MachineConfig(BaseModel):
    """Configuration for remote machine connections."""

    type: TransportType = TransportType.LOCAL
    host: Optional[str] = None
    user: Optional[str] = None
    port: Optional[int] = None
    key_file: Optional[str] = None
    agent_port: Optional[int] = None
    auth_token: Optional[str] = None
    image: Optional[str] = None
    volumes: Optional[dict[str, str]] = None
    env_setup: Optional[str] = None  # Shell commands to run before agent (e.g. PATH setup)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "type": "LOCAL"
                },
                {
                    "type": "SSH",
                    "host": "remote.example.com",
                    "user": "developer",
                    "port": 22,
                    "key_file": "~/.ssh/id_rsa",
                    "env_setup": "source /opt/tools/env.sh"
                },
                {
                    "type": "DOCKER",
                    "image": "python:3.11",
                    "volumes": {"/local/path": "/container/path"}
                }
            ]
        }
    }

    @model_validator(mode="after")
    def validate_transport_config(self) -> MachineConfig:
        """Validate required fields based on transport type."""
        if self.type == TransportType.SSH:
            if not self.host:
                raise ValueError("SSH transport requires 'host'")
            if not self.user:
                raise ValueError("SSH transport requires 'user'")
        elif self.type == TransportType.DOCKER:
            if not self.image:
                raise ValueError("DOCKER transport requires 'image'")
        elif self.type == TransportType.WEBSOCKET:
            if not self.host:
                raise ValueError("WEBSOCKET transport requires 'host'")
            if not self.agent_port:
                raise ValueError("WEBSOCKET transport requires 'agent_port'")
        elif self.type == TransportType.OPENCLAW:
            if not self.host:
                raise ValueError("OPENCLAW transport requires 'host'")

        return self


class Context(BaseModel):
    """Development context (workspace) definition."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    machine: MachineConfig = Field(default_factory=MachineConfig)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "my-project",
                    "path": "/home/user/projects/my-project",
                    "machine": {"type": "LOCAL"},
                    "tags": ["python", "web"]
                }
            ]
        }
    }

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate context name format."""
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Context name must contain only alphanumeric characters, hyphens, and underscores")
        return v

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Validate path is absolute."""
        if not v.startswith("/"):
            raise ValueError("Context path must be absolute")
        return v


class RetryPolicy(BaseModel):
    """Retry policy configuration for task execution."""

    max_retries: int = Field(default=0, ge=0)
    backoff_base: float = Field(default=2.0, gt=1.0)
    backoff_max: float = Field(default=300.0, gt=0.0)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"max_retries": 3, "backoff_base": 2.0, "backoff_max": 300.0}
            ]
        }
    }

    @model_validator(mode="after")
    def validate_backoff(self) -> RetryPolicy:
        """Ensure backoff_max is reasonable."""
        if self.backoff_max < self.backoff_base:
            raise ValueError("backoff_max must be greater than or equal to backoff_base")
        return self


class TaskDefinition(BaseModel):
    """Task definition for agent execution."""

    name: Optional[str] = None
    tool: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context: Optional[str] = None
    timeout: Optional[int] = Field(default=None, gt=0)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    env: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    on_complete: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "implement-feature",
                    "tool": "claude",
                    "prompt": "Implement user authentication",
                    "context": "my-project",
                    "timeout": 3600,
                    "env": {"DEBUG": "1"}
                }
            ]
        }
    }

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: Optional[int]) -> Optional[int]:
        """Validate timeout is reasonable."""
        if v is not None and v > 86400:  # 24 hours
            raise ValueError("Timeout cannot exceed 24 hours (86400 seconds)")
        return v


class AgentEvent(BaseModel):
    """Event logged during agent execution."""

    agent_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_type: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "agent_id": "123e4567-e89b-12d3-a456-426614174000",
                    "event_type": "state_change",
                    "detail": {"from": "PLANNING", "to": "EDITING"}
                }
            ]
        }
    }


class Agent(BaseModel):
    """Agent instance tracking execution state."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task: TaskDefinition
    context_id: str
    context_name: str
    context_path: str
    transport_type: TransportType
    status: AgentStatus = Field(default=AgentStatus.PENDING)
    state: AgentState = Field(default=AgentState.INITIALIZING)
    tmux_session: Optional[str] = None
    tmux_socket: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    exit_reason: Optional[str] = None
    retry_count: int = Field(default=0, ge=0)
    events: list[dict] = Field(default_factory=list)
    cost_estimate: Optional[float] = Field(default=None, ge=0.0)
    files_changed: list[str] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "task": {
                        "name": "test-task",
                        "tool": "claude",
                        "prompt": "Write tests",
                        "context": "my-project"
                    },
                    "context_id": "123e4567-e89b-12d3-a456-426614174000",
                    "context_name": "my-project",
                    "context_path": "/home/user/projects/my-project",
                    "transport_type": "LOCAL",
                    "status": "RUNNING",
                    "state": "EDITING"
                }
            ]
        }
    }

    @field_validator("pid")
    @classmethod
    def validate_pid(cls, v: Optional[int]) -> Optional[int]:
        """Validate PID is positive."""
        if v is not None and v <= 0:
            raise ValueError("PID must be positive")
        return v

    @field_validator("cost_estimate")
    @classmethod
    def validate_cost(cls, v: Optional[float]) -> Optional[float]:
        """Validate cost estimate is reasonable."""
        if v is not None and v > 1000.0:
            raise ValueError("Cost estimate seems unreasonably high (>$1000)")
        return v

    def add_event(self, event_type: str, detail: dict[str, Any] | None = None) -> None:
        """Add an event to the agent's event log."""
        self.events.append({
            "agent_id": self.id,
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "detail": detail or {},
        })

    def duration_seconds(self) -> Optional[float]:
        """Calculate execution duration in seconds."""
        if not self.started_at:
            return None

        end_time = self.completed_at or datetime.utcnow()
        return (end_time - self.started_at).total_seconds()

    def is_terminal(self) -> bool:
        """Check if agent is in a terminal state."""
        return self.status in {
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.TIMEOUT,
            AgentStatus.KILLED
        }

    def is_active(self) -> bool:
        """Check if agent is actively running."""
        return self.status in {
            AgentStatus.STARTING,
            AgentStatus.RUNNING,
            AgentStatus.RETRYING
        }
