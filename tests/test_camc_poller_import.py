"""Tests for CamcPoller's agent-import policy.

Covers the 3-behavior sync contract:

  1. New agent in camc list with RUNNING status → import into cam DB.
  2. New agent in camc list with TERMINAL status → SKIP (don't pollute
     `cam list` with dead agents that just finished).
  3. Agent in cam DB but missing from every camc list → mark COMPLETED
     with `exit_reason='Session gone (not in camc list)'`. Do NOT
     delete from cam DB — only `cam rm` can remove.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def poller_setup(monkeypatch):
    """Build a CamcPoller wired to in-memory fakes for agent/context stores."""
    from cam.core.camc_poller import CamcPoller
    from cam.core.models import AgentStatus

    saved: dict[str, dict] = {}
    statuses: dict[str, AgentStatus] = {}
    exit_reasons: dict[str, str | None] = {}

    class FakeStore:
        def list(self, status=None):
            return [
                _dict_to_agent_like(aid, d, statuses[aid], exit_reasons.get(aid))
                for aid, d in saved.items()
            ]
        def get(self, aid):
            d = saved.get(aid)
            if d is None:
                return None
            return _dict_to_agent_like(aid, d, statuses[aid], exit_reasons.get(aid))
        def save(self, agent):
            aid = str(agent.id)
            saved[aid] = {"tmux_session": agent.tmux_session, "context_id": agent.context_id}
            statuses[aid] = agent.status
            exit_reasons[aid] = agent.exit_reason
        def update_status(self, aid, status, exit_reason=None):
            statuses[aid] = status
            if exit_reason is not None:
                exit_reasons[aid] = exit_reason
        def add_event(self, ev):
            pass

    fake_store = FakeStore()
    fake_ctx_store = MagicMock()
    fake_ctx_store.list.return_value = []

    fake_bus = MagicMock()

    poller = CamcPoller(
        agent_store=fake_store,
        context_store=fake_ctx_store,
        event_bus=fake_bus,
    )

    # Stub out machines + delegate so the poll runs without real SSH.
    monkeypatch.setattr(poller, "_load_machines",
                        lambda: [{"name": "m1", "host": None, "user": "u", "port": None}])

    return poller, fake_store, saved, statuses, exit_reasons


def _dict_to_agent_like(aid, d, status, exit_reason):
    """Minimal duck-typed Agent for the poller's internal calls."""
    return types.SimpleNamespace(
        id=aid,
        tmux_session=d.get("tmux_session", ""),
        context_id=d.get("context_id", ""),
        status=status,
        exit_reason=exit_reason,
    )


def _mk_camc_row(aid, status, tmux_session=None, name="t", tool="claude", hostname=""):
    return {
        "id": aid,
        "task": {"name": name, "tool": tool, "prompt": "", "auto_confirm": True,
                 "auto_exit": False},
        "context_path": "/tmp",
        "transport_type": "local",
        "status": status,
        "state": "idle",
        "tmux_session": tmux_session or "cam-%s" % aid,
        "started_at": "2026-04-19T00:00:00+00:00",
        "hostname": hostname,
    }


@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_terminal_new_agents_are_not_imported(monkeypatch, poller_setup, status):
    """Behavior 2: terminal-status agent never seen before → skip."""
    poller, _store, saved, statuses, _ = poller_setup

    # Fake delegate returns one terminal-status agent.
    monkeypatch.setattr(
        poller, "_get_delegate",
        lambda *a, **kw: _stub_delegate([_mk_camc_row("ter00001", status)]),
    )

    asyncio.run(poller.poll_once())

    assert "ter00001" not in saved, "terminal agent should not be imported"


def test_running_new_agent_is_imported(monkeypatch, poller_setup):
    """Behavior 1: new running agent → imported."""
    from cam.core.models import AgentStatus
    poller, _store, saved, statuses, _ = poller_setup

    monkeypatch.setattr(
        poller, "_get_delegate",
        lambda *a, **kw: _stub_delegate([_mk_camc_row("run00001", "running")]),
    )

    asyncio.run(poller.poll_once())

    assert "run00001" in saved
    assert statuses["run00001"] == AgentStatus.RUNNING


def test_existing_agent_missing_from_camc_is_marked_session_gone(monkeypatch, poller_setup):
    """Behavior 3: in cam DB but not in any camc list → mark completed with
    'Session gone ...'. Not deleted."""
    from cam.core.models import AgentStatus
    from cam.core.models import Agent, TaskDefinition, TransportType
    poller, store, saved, statuses, exit_reasons = poller_setup

    # Pre-populate cam DB with a running agent.
    a = Agent(
        id="stale001",
        task=TaskDefinition(name="gone", tool="claude", prompt=""),
        context_id="",
        context_name="",
        context_path="/tmp",
        transport_type=TransportType.LOCAL,
        status=AgentStatus.RUNNING,
        tmux_session="cam-stale001",
    )
    store.save(a)
    assert statuses["stale001"] == AgentStatus.RUNNING

    # camc reports nothing.
    monkeypatch.setattr(
        poller, "_get_delegate",
        lambda *a, **kw: _stub_delegate([]),
    )

    asyncio.run(poller.poll_once())

    assert "stale001" in saved, "stale agent must NOT be deleted from cam DB"
    assert statuses["stale001"] == AgentStatus.COMPLETED
    assert "not in camc list" in (exit_reasons["stale001"] or "").lower()


def _stub_delegate(camc_rows):
    d = MagicMock()
    d.list_agents.return_value = camc_rows
    d.get_history.return_value = []
    return d
