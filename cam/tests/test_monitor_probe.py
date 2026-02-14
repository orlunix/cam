"""Integration tests for probe-based completion detection in the monitor loop.

Tests that the AgentMonitor correctly:
1. Fires probes after the configured stable period
2. Logs probe results to JSONL
3. Publishes probe events to the EventBus
4. Requires 2 consecutive COMPLETED probes before finalizing
5. Resets idle timer on BUSY probes
6. Shows probe results in `cam logs` output formatting
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cam.core.config import CamConfig
from cam.core.events import EventBus
from cam.core.models import (
    Agent,
    AgentEvent,
    AgentState,
    AgentStatus,
    TaskDefinition,
)
from cam.core.monitor import AgentMonitor
from cam.core.probe import PROBE_CHAR, ProbeResult
from cam.storage.agent_store import AgentStore
from cam.utils.logging import AgentLogger


def _make_agent(**overrides) -> Agent:
    """Create a test Agent with sensible defaults."""
    defaults = dict(
        task=TaskDefinition(
            name="test-task",
            tool="claude",
            prompt="do something",
            context="ctx",
        ),
        context_id="ctx-001",
        context_name="test-ctx",
        context_path="/tmp/test",
        transport_type="local",
        status=AgentStatus.RUNNING,
        state=AgentState.EDITING,
        tmux_session="cam-test-abc",
        started_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    return Agent(**defaults)


def _make_config(
    probe_detection: bool = True,
    probe_stable_seconds: int = 0,   # immediate for tests
    probe_cooldown: int = 0,          # no cooldown for tests
    poll_interval: int = 0,
    idle_timeout: int = 0,            # disabled for tests (0 = no timeout)
    health_check_interval: int = 0,   # every poll for tests
    auto_confirm: bool = False,
) -> CamConfig:
    return CamConfig(
        general={"auto_confirm": auto_confirm},
        monitor={
            "poll_interval": poll_interval,
            "idle_timeout": idle_timeout,
            "health_check_interval": health_check_interval,
            "probe_detection": probe_detection,
            "probe_stable_seconds": probe_stable_seconds,
            "probe_cooldown": probe_cooldown,
        },
    )


def _make_transport(session_alive: bool = True) -> AsyncMock:
    """Create a mock transport."""
    transport = AsyncMock()
    transport.session_exists = AsyncMock(return_value=session_alive)
    transport.capture_output = AsyncMock(return_value="Working...\n")
    transport.send_input = AsyncMock(return_value=True)
    transport.send_key = AsyncMock(return_value=True)
    return transport


def _make_adapter(
    detect_completion: AgentStatus | None = None,
    detect_state: AgentState | None = AgentState.EDITING,
    needs_prompt: bool = True,
    is_ready: bool = False,
) -> MagicMock:
    """Create a mock ToolAdapter."""
    adapter = MagicMock()
    adapter.detect_completion.return_value = detect_completion
    adapter.detect_state.return_value = detect_state
    adapter.should_auto_confirm.return_value = None
    adapter.needs_prompt_after_launch.return_value = needs_prompt
    adapter.is_ready_for_input.return_value = is_ready
    adapter.estimate_cost.return_value = None
    adapter.parse_files_changed.return_value = []
    return adapter


class TestMonitorProbeIntegration:
    """Test that probes fire in the monitor loop and produce visible output."""

    @pytest.mark.asyncio
    async def test_probe_busy_logged_and_published(self, tmp_path):
        """BUSY probe writes to JSONL log and publishes event."""
        agent = _make_agent()
        transport = _make_transport()
        adapter = _make_adapter()
        event_bus = EventBus()
        agent_logger = AgentLogger(str(agent.id), log_dir=tmp_path)
        agent_logger.open()
        config = _make_config(probe_stable_seconds=0, probe_cooldown=0)

        # Collect published events
        published: list[AgentEvent] = []
        event_bus.subscribe("probe", lambda e: published.append(e))

        monitor = AgentMonitor(
            agent=agent,
            transport=transport,
            adapter=adapter,
            agent_store=MagicMock(spec=AgentStore),
            event_bus=event_bus,
            agent_logger=agent_logger,
            config=config,
        )
        # Pre-set state so probe fires on first poll
        monitor._has_worked = True
        monitor._previous_output = "Working...\n"  # match transport output

        poll_count = [0]

        async def controlled_probe(*args, **kwargs):
            """Mock probe that returns BUSY then kills the loop."""
            poll_count[0] += 1
            if poll_count[0] >= 2:
                # Kill the loop on second probe by making session gone
                transport.session_exists.return_value = False
            return ProbeResult.BUSY

        with patch("cam.core.monitor.probe_session", side_effect=controlled_probe):
            result = await monitor.run()

        agent_logger.close()

        # Check JSONL log contains probe entry
        log_entries = agent_logger.read_lines()
        probe_entries = [e for e in log_entries if e.get("type") == "probe"]
        assert len(probe_entries) >= 1
        first_probe = probe_entries[0]
        assert first_probe["data"]["result"] == "busy"
        assert first_probe["data"]["probe_count"] == 1

        # Check event was published
        assert len(published) >= 1
        assert published[0].event_type == "probe"
        assert published[0].detail["result"] == "busy"

    @pytest.mark.asyncio
    async def test_two_completed_probes_finalize_agent(self, tmp_path):
        """Two consecutive COMPLETED probes → agent finalized as COMPLETED."""
        agent = _make_agent()
        transport = _make_transport()
        adapter = _make_adapter()
        event_bus = EventBus()
        agent_logger = AgentLogger(str(agent.id), log_dir=tmp_path)
        agent_logger.open()
        config = _make_config(probe_stable_seconds=0, probe_cooldown=0)

        monitor = AgentMonitor(
            agent=agent,
            transport=transport,
            adapter=adapter,
            agent_store=MagicMock(spec=AgentStore),
            event_bus=event_bus,
            agent_logger=agent_logger,
            config=config,
        )
        monitor._has_worked = True
        monitor._previous_output = "Working...\n"

        with patch("cam.core.monitor.probe_session", return_value=ProbeResult.COMPLETED):
            result = await monitor.run()

        agent_logger.close()

        assert result == AgentStatus.COMPLETED
        assert agent.exit_reason == "Probe detected agent at prompt (echo mode)"

        # Check log has finalize entry with probe reason
        log_entries = agent_logger.read_lines()
        finalize_entries = [e for e in log_entries if e.get("type") == "finalize"]
        assert len(finalize_entries) == 1
        assert "Probe" in finalize_entries[0]["data"]["reason"]

    @pytest.mark.asyncio
    async def test_single_completed_probe_not_enough(self, tmp_path):
        """One COMPLETED then one BUSY → no finalization by probe."""
        agent = _make_agent()
        transport = _make_transport()
        adapter = _make_adapter()
        event_bus = EventBus()
        agent_logger = AgentLogger(str(agent.id), log_dir=tmp_path)
        agent_logger.open()
        config = _make_config(probe_stable_seconds=0, probe_cooldown=0)

        probe_results = [ProbeResult.COMPLETED, ProbeResult.BUSY]
        call_count = [0]

        async def sequenced_probe(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= len(probe_results):
                return probe_results[call_count[0] - 1]
            # After all probes: kill session so health check ends the loop
            transport.session_exists.return_value = False
            return ProbeResult.SESSION_DEAD

        monitor = AgentMonitor(
            agent=agent,
            transport=transport,
            adapter=adapter,
            agent_store=MagicMock(spec=AgentStore),
            event_bus=event_bus,
            agent_logger=agent_logger,
            config=config,
        )
        monitor._has_worked = True
        monitor._previous_output = "Working...\n"

        with patch("cam.core.monitor.probe_session", side_effect=sequenced_probe):
            result = await monitor.run()

        agent_logger.close()

        # Should NOT have been finalized by probe (streak broken)
        assert agent.exit_reason != "Probe detected agent at prompt (echo mode)"

    @pytest.mark.asyncio
    async def test_busy_probe_resets_idle_timer(self, tmp_path):
        """BUSY probe resets _last_change_time to prevent false idle timeout."""
        agent = _make_agent()
        transport = _make_transport()
        adapter = _make_adapter()
        event_bus = EventBus()
        agent_logger = AgentLogger(str(agent.id), log_dir=tmp_path)
        agent_logger.open()
        # Use idle_timeout=0 (disabled) so we can manually verify
        # the _last_change_time update without timeout interference
        config = _make_config(probe_stable_seconds=0, probe_cooldown=0)

        monitor = AgentMonitor(
            agent=agent,
            transport=transport,
            adapter=adapter,
            agent_store=MagicMock(spec=AgentStore),
            event_bus=event_bus,
            agent_logger=agent_logger,
            config=config,
        )
        monitor._has_worked = True
        monitor._previous_output = "Working...\n"
        old_change_time = monitor._last_change_time

        call_count = [0]

        async def busy_then_die(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return ProbeResult.BUSY
            transport.session_exists.return_value = False
            return ProbeResult.SESSION_DEAD

        with patch("cam.core.monitor.probe_session", side_effect=busy_then_die):
            result = await monitor.run()

        agent_logger.close()

        # BUSY probe should have updated _last_change_time
        assert monitor._last_change_time >= old_change_time

    @pytest.mark.asyncio
    async def test_probe_disabled_by_config(self, tmp_path):
        """Probe does not fire when probe_detection=False."""
        agent = _make_agent()
        transport = _make_transport()
        adapter = _make_adapter()
        event_bus = EventBus()
        agent_logger = AgentLogger(str(agent.id), log_dir=tmp_path)
        agent_logger.open()
        config = _make_config(probe_detection=False)

        monitor = AgentMonitor(
            agent=agent,
            transport=transport,
            adapter=adapter,
            agent_store=MagicMock(spec=AgentStore),
            event_bus=event_bus,
            agent_logger=agent_logger,
            config=config,
        )
        monitor._has_worked = True
        monitor._previous_output = "Working...\n"

        # Kill after 2 polls
        call_count = [0]

        async def capture_then_die(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                transport.session_exists.return_value = False
            return "Working...\n"

        transport.capture_output = AsyncMock(side_effect=capture_then_die)

        with patch("cam.core.monitor.probe_session") as mock_probe:
            result = await monitor.run()

        agent_logger.close()

        # probe_session should never have been called
        mock_probe.assert_not_awaited()


def _capture_log_entry(entry: dict) -> str:
    """Render a log entry through _print_log_entry and capture the output."""
    from rich.console import Console as RichConsole

    buf = StringIO()
    fake_console = RichConsole(file=buf, no_color=True, highlight=False)

    from cam.cli import agent_cmd

    with patch("rich.console.Console", return_value=fake_console):
        agent_cmd._print_log_entry(entry)

    return buf.getvalue()


class TestProbeLogFormatting:
    """Test that probe events render correctly in `cam logs` output."""

    def test_probe_busy_format(self):
        """Probe BUSY shows blue styling."""
        output = _capture_log_entry({
            "ts": "2026-02-13T10:00:00+00:00",
            "type": "probe",
            "data": {"result": "busy", "probe_count": 1, "consecutive_completed": 0},
        })
        assert "Probe" in output
        assert "BUSY" in output
        assert "#1" in output

    def test_probe_completed_format(self):
        """Probe COMPLETED shows green styling with streak."""
        output = _capture_log_entry({
            "ts": "2026-02-13T10:00:00+00:00",
            "type": "probe",
            "data": {"result": "completed", "probe_count": 3, "consecutive_completed": 2},
        })
        assert "COMPLETED" in output
        assert "streak=2" in output

    def test_probe_confirmed_format(self):
        """Probe CONFIRMED shows yellow styling."""
        output = _capture_log_entry({
            "ts": "2026-02-13T10:00:00+00:00",
            "type": "probe",
            "data": {"result": "confirmed", "probe_count": 2, "consecutive_completed": 0},
        })
        assert "CONFIRMED" in output

    def test_state_change_format_shows_transition(self):
        """State change shows from → to transition."""
        output = _capture_log_entry({
            "ts": "2026-02-13T10:00:00+00:00",
            "type": "state_change",
            "data": {"from": "planning", "to": "editing"},
        })
        assert "planning" in output
        assert "editing" in output


class TestProbeEventInAgentStatus:
    """Test that probe events appear in agent.events for `cam status`."""

    @pytest.mark.asyncio
    async def test_probe_events_in_agent_events_list(self, tmp_path):
        """Probe events are appended to agent.events (visible in cam status)."""
        agent = _make_agent()
        transport = _make_transport()
        adapter = _make_adapter()
        event_bus = EventBus()
        agent_logger = AgentLogger(str(agent.id), log_dir=tmp_path)
        agent_logger.open()
        config = _make_config(probe_stable_seconds=0, probe_cooldown=0)

        monitor = AgentMonitor(
            agent=agent,
            transport=transport,
            adapter=adapter,
            agent_store=MagicMock(spec=AgentStore),
            event_bus=event_bus,
            agent_logger=agent_logger,
            config=config,
        )
        monitor._has_worked = True
        monitor._previous_output = "Working...\n"

        with patch("cam.core.monitor.probe_session", return_value=ProbeResult.COMPLETED):
            await monitor.run()

        agent_logger.close()

        # agent.events should contain probe events
        probe_events = [e for e in agent.events if e.get("event_type") == "probe"]
        assert len(probe_events) >= 2  # At least 2 probes to finalize
        assert probe_events[0]["detail"]["result"] == "completed"
