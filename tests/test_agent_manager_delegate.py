"""Regression tests for AgentManager's camc delegate routing."""

from unittest.mock import MagicMock

from cam.core.agent_manager import AgentManager
from cam.core.models import (
    Agent,
    AgentStatus,
    Context,
    MachineConfig,
    TaskDefinition,
    TransportType,
)


class FakeCamcDelegate:
    def __init__(self, host=None, user=None, port=None):
        self.host = host
        self.user = user
        self.port = port


def _agent(**overrides):
    data = {
        "id": "agent001",
        "task": TaskDefinition(name="agent", tool="claude", prompt=""),
        "context_id": "",
        "context_name": "",
        "context_path": "/tmp/project",
        "transport_type": TransportType.LOCAL,
        "status": AgentStatus.RUNNING,
        "tmux_session": "cam-agent001",
    }
    data.update(overrides)
    return Agent(**data)


def _manager(agent, context=None):
    agent_store = MagicMock()
    agent_store.get.return_value = agent

    context_store = MagicMock()
    context_store.get.return_value = context

    manager = AgentManager(
        config=MagicMock(),
        context_store=context_store,
        agent_store=agent_store,
        event_bus=MagicMock(),
    )
    return manager, context_store


def test_contextless_local_agent_delegates_to_local_camc(monkeypatch):
    import cam.core.camc_delegate as camc_delegate

    monkeypatch.setattr(camc_delegate, "CamcDelegate", FakeCamcDelegate)
    manager, context_store = _manager(_agent())

    agent, camc_id, delegate = manager._resolve_agent_delegate("agent001")

    assert agent.id == "agent001"
    assert camc_id == "cam-agent001"
    assert (delegate.host, delegate.user, delegate.port) == (None, None, None)
    context_store.get.assert_not_called()


def test_localhost_with_port_remains_ssh_delegate(monkeypatch):
    import cam.core.camc_delegate as camc_delegate

    monkeypatch.setattr(camc_delegate, "CamcDelegate", FakeCamcDelegate)
    manager, context_store = _manager(_agent(
        transport_type=TransportType.SSH,
        machine_host="localhost",
        machine_user="hren",
        machine_port=2223,
    ))

    _agent_obj, _camc_id, delegate = manager._resolve_agent_delegate("agent001")

    assert (delegate.host, delegate.user, delegate.port) == ("localhost", "hren", 2223)
    context_store.get.assert_not_called()


def test_legacy_agent_without_machine_host_uses_context(monkeypatch):
    import cam.core.camc_delegate as camc_delegate

    monkeypatch.setattr(camc_delegate, "CamcDelegate", FakeCamcDelegate)
    context = Context(
        id="ctx1",
        name="ctx",
        path="/tmp/project",
        machine=MachineConfig(
            type=TransportType.SSH,
            host="remote.example.com",
            user="hren",
            port=2222,
        ),
    )
    manager, context_store = _manager(_agent(
        context_id="ctx1",
        context_name="ctx",
        transport_type=TransportType.SSH,
    ), context)

    _agent_obj, _camc_id, delegate = manager._resolve_agent_delegate("agent001")
