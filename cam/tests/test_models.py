"""Tests for core data models."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cam.core.models import (
    Agent,
    AgentEvent,
    AgentState,
    AgentStatus,
    Context,
    MachineConfig,
    RetryPolicy,
    TaskDefinition,
    TransportType,
)


class TestTransportType:
    def test_values_are_lowercase(self):
        assert TransportType.LOCAL.value == "local"
        assert TransportType.SSH.value == "ssh"
        assert TransportType.DOCKER.value == "docker"

    def test_from_string(self):
        assert TransportType("local") == TransportType.LOCAL
        assert TransportType("ssh") == TransportType.SSH


class TestMachineConfig:
    def test_defaults(self):
        config = MachineConfig()
        assert config.type == TransportType.LOCAL
        assert config.host is None

    def test_ssh_config(self):
        config = MachineConfig(type=TransportType.SSH, host="example.com", user="dev")
        assert config.host == "example.com"
        assert config.user == "dev"


class TestContext:
    def test_create_local(self):
        ctx = Context(
            id=str(uuid4()),
            name="test",
            path="/tmp/test",
            created_at=datetime.now(timezone.utc),
        )
        assert ctx.name == "test"
        assert ctx.machine.type == TransportType.LOCAL

    def test_id_must_be_string(self):
        with pytest.raises(ValidationError):
            Context(
                id=123,  # type: ignore
                name="test",
                path="/tmp",
                created_at=datetime.now(timezone.utc),
            )

    def test_tags_default_empty(self):
        ctx = Context(
            id=str(uuid4()),
            name="test",
            path="/tmp",
            created_at=datetime.now(timezone.utc),
        )
        assert ctx.tags == []


class TestTaskDefinition:
    def test_minimal(self):
        task = TaskDefinition(tool="claude", prompt="Do something")
        assert task.tool == "claude"
        assert task.name is None
        assert task.retry.max_retries == 0

    def test_with_retry(self):
        task = TaskDefinition(
            tool="claude",
            prompt="test",
            retry=RetryPolicy(max_retries=3),
        )
        assert task.retry.max_retries == 3

    def test_serialization(self):
        task = TaskDefinition(
            name="my-task",
            tool="claude",
            prompt="Do work",
            timeout=600,
        )
        d = task.model_dump(mode="json")
        assert d["name"] == "my-task"
        assert d["timeout"] == 600
        assert d["retry"]["max_retries"] == 0


class TestAgent:
    def test_create_minimal(self):
        agent = Agent(
            task=TaskDefinition(tool="claude", prompt="test"),
            context_id=str(uuid4()),
            context_name="ctx",
            context_path="/tmp",
            transport_type=TransportType.LOCAL,
        )
        assert agent.status == AgentStatus.PENDING
        assert agent.state == AgentState.INITIALIZING
        assert agent.events == []

    def test_is_terminal(self):
        agent = Agent(
            task=TaskDefinition(tool="claude", prompt="test"),
            context_id=str(uuid4()),
            context_name="ctx",
            context_path="/tmp",
            transport_type=TransportType.LOCAL,
            status=AgentStatus.COMPLETED,
        )
        assert agent.is_terminal()

    def test_not_terminal(self):
        agent = Agent(
            task=TaskDefinition(tool="claude", prompt="test"),
            context_id=str(uuid4()),
            context_name="ctx",
            context_path="/tmp",
            transport_type=TransportType.LOCAL,
            status=AgentStatus.RUNNING,
        )
        assert not agent.is_terminal()

    def test_pid_must_be_positive(self):
        with pytest.raises(ValidationError):
            Agent(
                task=TaskDefinition(tool="claude", prompt="test"),
                context_id=str(uuid4()),
                context_name="ctx",
                context_path="/tmp",
                transport_type=TransportType.LOCAL,
                pid=-1,
            )


class TestAgentEvent:
    def test_create(self):
        event = AgentEvent(
            agent_id=str(uuid4()),
            event_type="state_change",
            detail={"from": "planning", "to": "editing"},
        )
        assert event.event_type == "state_change"
        assert event.detail["from"] == "planning"

    def test_timestamp_default(self):
        event = AgentEvent(
            agent_id=str(uuid4()),
            event_type="test",
        )
        assert event.timestamp is not None
