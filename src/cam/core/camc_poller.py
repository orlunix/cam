"""Poller that syncs camc agent state into cam serve's SQLite.

CamcPoller runs as an asyncio task inside cam serve. It periodically
polls each camc instance (local + remote contexts) and merges agent
state into the server's AgentStore (SQLite). Events from camc are
converted into AgentEvents for the EventBus.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from cam.core.camc_delegate import CamcDelegate
from cam.core.events import EventBus
from cam.core.models import (
    Agent,
    AgentEvent,
    AgentStatus,
    AgentState,
    TaskDefinition,
    TransportType,
)
from cam.storage.agent_store import AgentStore
from cam.storage.context_store import ContextStore

logger = logging.getLogger(__name__)

# Map camc status strings to AgentStatus enum
_STATUS_MAP = {
    "running": AgentStatus.RUNNING,
    "completed": AgentStatus.COMPLETED,
    "failed": AgentStatus.FAILED,
    "stopped": AgentStatus.KILLED,
}

# Map camc state strings to AgentState enum
_STATE_MAP = {
    "initializing": AgentState.INITIALIZING,
    "planning": AgentState.PLANNING,
    "editing": AgentState.EDITING,
    "testing": AgentState.TESTING,
    "committing": AgentState.COMMITTING,
    "idle": AgentState.IDLE,
}

# Terminal statuses — never promote back to RUNNING from these
_TERMINAL_STATUSES = {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.KILLED}


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string to datetime."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _camc_agent_to_model(data: dict, context_name: str | None = None,
                         context_id: str | None = None,
                         machine_host: str | None = None,
                         machine_user: str | None = None,
                         machine_port: int | None = None) -> Agent:
    """Convert a camc JSON agent dict to a cam Agent model.

    Supports both the unified schema (new format with nested task, tmux_session,
    context_path, pid) and legacy format (flat tool/prompt/name, session, path,
    monitor_pid). The unified format is a near pass-through.
    """
    # Task: new format has nested dict, legacy has flat fields
    task_data = data.get("task", {})
    if isinstance(task_data, dict) and task_data:
        task = TaskDefinition(
            name=task_data.get("name", ""),
            tool=task_data.get("tool", "claude"),
            prompt=task_data.get("prompt", ""),
            auto_confirm=task_data.get("auto_confirm", True),
            auto_exit=task_data.get("auto_exit", False),
        )
    else:
        # Legacy flat format
        task = TaskDefinition(
            name=data.get("name", ""),
            tool=data.get("tool", "claude"),
            prompt=data.get("prompt", ""),
            auto_confirm=True,
            auto_exit=data.get("auto_exit", False),
        )

    status_str = data.get("status", "running")
    state_str = data.get("state", "")

    # Context name: unified has context_name, legacy derives from context dict
    ctx_name = context_name or data.get("context_name")
    if not ctx_name:
        ctx = data.get("context", {}) or {}
        ctx_name = ctx.get("name", "") if isinstance(ctx, dict) else ""

    return Agent(
        id=data.get("id", ""),
        task=task,
        context_id=context_id or data.get("context_id", ""),
        context_name=ctx_name,
        context_path=data.get("context_path") or data.get("path", ""),
        transport_type=TransportType.LOCAL if data.get("transport_type") == "local" else TransportType.SSH,
        status=_STATUS_MAP.get(status_str, AgentStatus.RUNNING),
        state=_STATE_MAP.get(state_str, AgentState.INITIALIZING),
        tmux_session=data.get("tmux_session") or data.get("session", ""),
        tmux_socket=data.get("tmux_socket") or data.get("socket", ""),
        started_at=_parse_ts(data.get("started_at")) or datetime.now(timezone.utc),
        completed_at=_parse_ts(data.get("completed_at")),
        exit_reason=data.get("exit_reason"),
        machine_host=machine_host,
        machine_user=machine_user,
        machine_port=machine_port,
    )


class CamcPoller:
    """Polls camc instances and syncs state to cam serve's SQLite.

    Usage:
        poller = CamcPoller(agent_store, context_store, event_bus)
        # Start as asyncio task
        task = asyncio.create_task(poller.run(interval=5))
    """

    def __init__(
        self,
        agent_store: AgentStore,
        context_store: ContextStore,
        event_bus: EventBus,
    ) -> None:
        self._agent_store = agent_store
        self._context_store = context_store
        self._event_bus = event_bus
        self._delegates: dict[str, CamcDelegate] = {}  # machine_name -> delegate
        self._last_event_ts: dict[str, str] = {}  # machine_name -> last event ts
        self._prev_states: dict[str, str] = {}  # agent_id -> previous status

    @staticmethod
    def _load_machines() -> list[dict]:
        """Load machines from ~/.cam/machines.json."""
        import json
        from pathlib import Path
        machines_path = Path.home() / ".cam" / "machines.json"
        if not machines_path.exists():
            return []
        try:
            with open(machines_path) as f:
                return json.load(f)
        except (ValueError, OSError):
            return []

    def _get_delegate(self, machine_name: str, host: str | None,
                      user: str | None, port: int | None) -> CamcDelegate:
        """Get or create a CamcDelegate for a machine."""
        if machine_name not in self._delegates:
            self._delegates[machine_name] = CamcDelegate(host=host, user=user, port=port)
        return self._delegates[machine_name]

    def _machines_from_contexts(self) -> list[dict]:
        """Derive unique machines from contexts (fallback when no machines.json)."""
        contexts = self._context_store.list()
        seen: dict[str, dict] = {}
        for ctx in contexts:
            m = ctx.machine
            host = m.host if hasattr(m, "host") else None
            user = m.user if hasattr(m, "user") else None
            port = m.port if hasattr(m, "port") else None
            key = "%s@%s:%s" % (user or "", host or "local", port or "")
            if key not in seen:
                seen[key] = {"name": ctx.name, "host": host, "user": user, "port": port}
        return list(seen.values())

    async def poll_once(self) -> int:
        """Poll all machines once. Returns number of agents synced."""
        machines = self._load_machines()
        if not machines:
            machines = self._machines_from_contexts()
        total = 0

        for machine in machines:
            name = machine.get("name", "")
            host = machine.get("host")
            user = machine.get("user")
            port = machine.get("port")

            delegate = self._get_delegate(name, host, user, port)

            # Poll agents from this machine
            try:
                if delegate._is_local:
                    camc_agents = await asyncio.to_thread(delegate.read_agents_json)
                else:
                    camc_agents = await asyncio.to_thread(delegate.list_agents)
            except Exception as e:
                logger.warning("Failed to poll machine %s: %s", name, e)
                continue

            # Build tmux_session index from existing DB agents to avoid
            # importing camc shadow agents that duplicate real cam agents.
            all_db_agents = self._agent_store.list()
            db_sessions = {a.tmux_session for a in all_db_agents if a.tmux_session}

            for agent_data in camc_agents:
                agent_id = agent_data.get("id", "")
                if not agent_id:
                    continue

                # Check if agent already exists in our store (by ID)
                existing = self._agent_store.get(agent_id)
                status = agent_data.get("status", "running")

                # Resolve tmux session name (unified: tmux_session, legacy: session)
                tmux_sess = agent_data.get("tmux_session") or agent_data.get("session", "")

                if existing is None:
                    # Skip if another agent with same tmux_session already exists
                    # (this is a camc shadow of a cam-managed agent)
                    if tmux_sess and tmux_sess in db_sessions:
                        # Update the real agent's status, but never resurrect
                        # a terminal state (completed/failed/killed) back to running
                        for dba in all_db_agents:
                            if dba.tmux_session == tmux_sess:
                                real_id = str(dba.id)
                                prev = self._prev_states.get(real_id)
                                if prev != status:
                                    new_status = _STATUS_MAP.get(status)
                                    if new_status and new_status != dba.status:
                                        self._agent_store.update_status(
                                            real_id, new_status,
                                            exit_reason=agent_data.get("exit_reason"),
                                        )
                                self._prev_states[real_id] = status
                                break
                        total += 1
                        continue

                    # New agent discovered from camc — import it.
                    # Store source machine info so cam attach/capture can
                    # connect to the right host directly.
                    try:
                        agent = _camc_agent_to_model(
                            agent_data,
                            machine_host=host,
                            machine_user=user,
                            machine_port=port,
                        )
                        self._agent_store.save(agent)
                        if tmux_sess:
                            db_sessions.add(tmux_sess)
                        logger.info("Imported agent %s from machine %s", agent_id, name)
                    except Exception as e:
                        logger.warning("Failed to import agent %s: %s", agent_id, e)
                else:
                    # Backfill machine fields for agents imported before v2
                    if host and not existing.machine_host:
                        try:
                            self._agent_store.db.execute(
                                "UPDATE agents SET machine_host=?, machine_user=?, machine_port=? WHERE id=?",
                                (host, user, port, agent_id),
                            )
                        except Exception:
                            pass

                    # Update status if changed — trust agents.json as source of truth
                    prev_status = self._prev_states.get(agent_id)
                    if prev_status != status:
                        new_status = _STATUS_MAP.get(status)
                        if new_status and new_status != existing.status:
                            exit_reason = agent_data.get("exit_reason")
                            self._agent_store.update_status(
                                agent_id, new_status, exit_reason=exit_reason
                            )
                            # Emit event
                            event = AgentEvent(
                                agent_id=agent_id,
                                event_type="status_change",
                                detail={"from": prev_status or existing.status.value, "to": status},
                            )
                            try:
                                self._agent_store.add_event(event)
                            except Exception:
                                pass
                            self._event_bus.publish(event)

                self._prev_states[agent_id] = status
                total += 1

            # Poll events (incremental)
            try:
                since = self._last_event_ts.get(name)
                if delegate._is_local:
                    new_events = await asyncio.to_thread(delegate.read_events_since, since)
                else:
                    new_events = await asyncio.to_thread(delegate.get_history, since=since)

                for ev in new_events:
                    ts = ev.get("ts", "")
                    if ts > (self._last_event_ts.get(name) or ""):
                        self._last_event_ts[name] = ts
                    # Convert to AgentEvent and publish
                    agent_event = AgentEvent(
                        agent_id=ev.get("agent_id", ""),
                        event_type=ev.get("type", "unknown"),
                        detail=ev.get("detail", {}),
                    )
                    self._event_bus.publish(agent_event)
            except Exception as e:
                logger.debug("Failed to poll events from %s: %s", name, e)

        return total

    async def run(self, interval: float = 5.0) -> None:
        """Run the polling loop. Call as asyncio.create_task(poller.run())."""
        logger.info("CamcPoller started (interval=%.1fs)", interval)
        while True:
            try:
                count = await self.poll_once()
                logger.info("Polled %d agents", count)
            except asyncio.CancelledError:
                logger.info("CamcPoller stopped")
                return
            except Exception as e:
                logger.error("CamcPoller error: %s", e)
            await asyncio.sleep(interval)
