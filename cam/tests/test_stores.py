"""Tests for storage layer (ContextStore, AgentStore)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from cam.core.models import (
    Agent,
    AgentEvent,
    AgentState,
    AgentStatus,
    Context,
    MachineConfig,
    TaskDefinition,
    TransportType,
)


class TestContextStore:
    def test_add_and_get(self, context_store, sample_context):
        context_store.add(sample_context)
        retrieved = context_store.get(sample_context.name)
        assert retrieved is not None
        assert retrieved.name == sample_context.name
        assert retrieved.path == sample_context.path

    def test_add_duplicate_name(self, context_store, sample_context):
        context_store.add(sample_context)
        with pytest.raises(Exception):
            context_store.add(sample_context)

    def test_get_by_id(self, context_store, sample_context):
        context_store.add(sample_context)
        retrieved = context_store.get(sample_context.id)
        assert retrieved is not None
        assert retrieved.id == sample_context.id

    def test_list(self, context_store, sample_context):
        context_store.add(sample_context)
        contexts = context_store.list()
        assert len(contexts) == 1
        assert contexts[0].name == sample_context.name

    def test_list_with_tag_filter(self, context_store, sample_context):
        context_store.add(sample_context)
        # Should match
        contexts = context_store.list(tags=["test"])
        assert len(contexts) == 1
        # Should not match
        contexts = context_store.list(tags=["nonexistent"])
        assert len(contexts) == 0

    def test_remove(self, context_store, sample_context):
        context_store.add(sample_context)
        assert context_store.remove(sample_context.name)
        assert context_store.get(sample_context.name) is None

    def test_exists(self, context_store, sample_context):
        assert not context_store.exists(sample_context.name)
        context_store.add(sample_context)
        assert context_store.exists(sample_context.name)


class TestAgentStore:
    def _make_agent(self):
        return Agent(
            id=str(uuid4()),
            task=TaskDefinition(name="test", tool="claude", prompt="Hello"),
            context_id=str(uuid4()),
            context_name="test-ctx",
            context_path="/tmp/test",
            transport_type=TransportType.LOCAL,
            status=AgentStatus.RUNNING,
            state=AgentState.INITIALIZING,
            started_at=datetime.now(timezone.utc),
        )

    def test_save_and_get(self, agent_store):
        agent = self._make_agent()
        agent_store.save(agent)
        retrieved = agent_store.get(agent.id)
        assert retrieved is not None
        assert retrieved.id == agent.id
        assert retrieved.task.tool == "claude"
        assert retrieved.status == AgentStatus.RUNNING

    def test_list(self, agent_store):
        a1 = self._make_agent()
        a2 = self._make_agent()
        agent_store.save(a1)
        agent_store.save(a2)
        agents = agent_store.list()
        assert len(agents) == 2

    def test_list_with_status_filter(self, agent_store):
        a1 = self._make_agent()
        a2 = self._make_agent()
        a2.status = AgentStatus.COMPLETED
        agent_store.save(a1)
        agent_store.save(a2)

        running = agent_store.list(status=AgentStatus.RUNNING)
        assert len(running) == 1
        assert running[0].id == a1.id

    def test_update_status(self, agent_store):
        agent = self._make_agent()
        agent_store.save(agent)
        agent_store.update_status(agent.id, AgentStatus.COMPLETED, exit_reason="Done")

        retrieved = agent_store.get(agent.id)
        assert retrieved.status == AgentStatus.COMPLETED
        assert retrieved.exit_reason == "Done"

    def test_add_and_get_events(self, agent_store):
        agent = self._make_agent()
        agent_store.save(agent)

        event = AgentEvent(
            agent_id=agent.id,
            event_type="state_change",
            detail={"from": "initializing", "to": "editing"},
        )
        agent_store.add_event(event)

        events = agent_store.get_events(agent.id)
        assert len(events) == 1
        assert events[0].event_type == "state_change"

    def test_delete(self, agent_store):
        agent = self._make_agent()
        agent_store.save(agent)
        assert agent_store.delete(agent.id)
        assert agent_store.get(agent.id) is None
