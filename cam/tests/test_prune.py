"""Tests for prune command: AgentStore batch methods and file cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from cam.core.models import (
    Agent,
    AgentState,
    AgentStatus,
    TaskDefinition,
    TransportType,
)


def _make_agent(
    status: str = "completed",
    started_at: datetime | None = None,
    context_id: str = "ctx-1",
    tmux_session: str | None = None,
) -> Agent:
    """Create a minimal Agent for testing."""
    now = started_at or datetime.now(timezone.utc)
    return Agent(
        id=str(uuid4()),
        task=TaskDefinition(name="t", tool="claude", prompt="hello"),
        context_id=context_id,
        context_name="test",
        context_path="/tmp/test",
        transport_type=TransportType.LOCAL,
        status=AgentStatus(status),
        state=AgentState.IDLE,
        tmux_session=tmux_session,
        started_at=now,
        completed_at=now + timedelta(seconds=10),
    )


class TestListIdsByFilter:
    def test_filter_by_status(self, agent_store):
        a1 = _make_agent(status="killed")
        a2 = _make_agent(status="completed")
        a3 = _make_agent(status="killed")
        for a in [a1, a2, a3]:
            agent_store.save(a)

        results = agent_store.list_ids_by_filter(statuses=["killed"])
        ids = [r[0] for r in results]
        assert a1.id in ids
        assert a3.id in ids
        assert a2.id not in ids

    def test_filter_by_before(self, agent_store):
        old = _make_agent(started_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = _make_agent(started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        agent_store.save(old)
        agent_store.save(new)

        cutoff = datetime(2025, 6, 1, tzinfo=timezone.utc).isoformat()
        results = agent_store.list_ids_by_filter(before=cutoff)
        ids = [r[0] for r in results]
        assert old.id in ids
        assert new.id not in ids

    def test_filter_by_context(self, agent_store):
        a1 = _make_agent(context_id="ctx-A")
        a2 = _make_agent(context_id="ctx-B")
        agent_store.save(a1)
        agent_store.save(a2)

        results = agent_store.list_ids_by_filter(context_id="ctx-A")
        ids = [r[0] for r in results]
        assert a1.id in ids
        assert a2.id not in ids

    def test_combined_filters(self, agent_store):
        match = _make_agent(
            status="killed",
            started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            context_id="ctx-X",
        )
        no_match = _make_agent(
            status="completed",
            started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            context_id="ctx-X",
        )
        agent_store.save(match)
        agent_store.save(no_match)

        cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()
        results = agent_store.list_ids_by_filter(
            statuses=["killed"], before=cutoff, context_id="ctx-X",
        )
        ids = [r[0] for r in results]
        assert match.id in ids
        assert no_match.id not in ids

    def test_returns_tmux_session(self, agent_store):
        a = _make_agent(tmux_session="cam-abc123")
        agent_store.save(a)

        results = agent_store.list_ids_by_filter(statuses=["completed"])
        assert results[0] == (a.id, "cam-abc123")


class TestDeleteBatch:
    def test_deletes_agents_and_events(self, agent_store):
        a1 = _make_agent()
        a2 = _make_agent()
        agent_store.save(a1)
        agent_store.save(a2)

        from cam.core.models import AgentEvent

        event = AgentEvent(agent_id=a1.id, event_type="test")
        agent_store.add_event(event)

        deleted = agent_store.delete_batch([a1.id, a2.id])
        assert deleted == 2
        assert agent_store.get(a1.id) is None
        assert agent_store.get(a2.id) is None
        assert agent_store.get_events(a1.id) == []

    def test_empty_list_noop(self, agent_store):
        assert agent_store.delete_batch([]) == 0

    def test_partial_ids(self, agent_store):
        a = _make_agent()
        agent_store.save(a)
        deleted = agent_store.delete_batch([a.id, str(uuid4())])
        assert deleted == 1


class TestAllIds:
    def test_returns_all_ids(self, agent_store):
        a1 = _make_agent()
        a2 = _make_agent()
        agent_store.save(a1)
        agent_store.save(a2)

        ids = agent_store.all_ids()
        assert a1.id in ids
        assert a2.id in ids

    def test_empty_db(self, agent_store):
        assert agent_store.all_ids() == set()


class TestCleanAgentFiles:
    def test_removes_log_and_pid_files(self, tmp_path):
        from cam.cli.agent_cmd import _clean_agent_files
        import cam.constants as c

        # Temporarily redirect dirs
        orig_log = c.LOG_DIR
        orig_pid = c.PID_DIR
        orig_sock = c.SOCKET_DIR
        c.LOG_DIR = tmp_path / "logs"
        c.PID_DIR = tmp_path / "pids"
        c.SOCKET_DIR = tmp_path / "sockets"
        c.LOG_DIR.mkdir()
        c.PID_DIR.mkdir()
        c.SOCKET_DIR.mkdir()

        try:
            agent_id = str(uuid4())
            session = "cam-abc123"

            (c.LOG_DIR / f"{agent_id}.jsonl").write_text("log")
            (c.PID_DIR / f"{agent_id}.pid").write_text("123")
            (c.SOCKET_DIR / f"{session}.sock").write_text("")

            counts = _clean_agent_files([agent_id], [session])
            assert counts["logs"] == 1
            assert counts["pids"] == 1
            assert counts["sockets"] == 1
            assert not (c.LOG_DIR / f"{agent_id}.jsonl").exists()
            assert not (c.PID_DIR / f"{agent_id}.pid").exists()
            assert not (c.SOCKET_DIR / f"{session}.sock").exists()
        finally:
            c.LOG_DIR = orig_log
            c.PID_DIR = orig_pid
            c.SOCKET_DIR = orig_sock


class TestFindOrphanFiles:
    def test_finds_orphan_logs(self, tmp_path, agent_store):
        from cam.cli.agent_cmd import _find_orphan_files
        import cam.constants as c

        orig_log = c.LOG_DIR
        orig_pid = c.PID_DIR
        orig_sock = c.SOCKET_DIR
        c.LOG_DIR = tmp_path / "logs"
        c.PID_DIR = tmp_path / "pids"
        c.SOCKET_DIR = tmp_path / "sockets"
        c.LOG_DIR.mkdir()
        c.PID_DIR.mkdir()
        c.SOCKET_DIR.mkdir()

        try:
            # Save one agent
            a = _make_agent()
            agent_store.save(a)

            # Create logs for known + unknown agent
            (c.LOG_DIR / f"{a.id}.jsonl").write_text("known")
            (c.LOG_DIR / f"{uuid4()}.jsonl").write_text("orphan")

            orphans = _find_orphan_files(agent_store)
            assert len(orphans) == 1
            assert "orphan" in Path(orphans[0]).read_text()
        finally:
            c.LOG_DIR = orig_log
            c.PID_DIR = orig_pid
            c.SOCKET_DIR = orig_sock


class TestParseBefore:
    def test_relative_days(self):
        from cam.cli.agent_cmd import _parse_before

        result = _parse_before("7d")
        dt = datetime.fromisoformat(result)
        assert (datetime.now(timezone.utc) - dt).total_seconds() < 7 * 86400 + 10

    def test_iso_date(self):
        from cam.cli.agent_cmd import _parse_before

        result = _parse_before("2025-01-15")
        assert "2025-01-15" in result

    def test_bad_value(self):
        from cam.cli.agent_cmd import _parse_before

        with pytest.raises(Exception):
            _parse_before("not-a-date")
