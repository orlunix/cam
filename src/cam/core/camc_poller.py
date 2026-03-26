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


def _camc_agent_to_model(data: dict, context_name: str | None = None,
                         context_id: str | None = None) -> Agent:
    """Convert a camc JSON agent dict to a cam Agent model.

    The camc JSON format (from _agent_to_cam_json) already matches cam's
    API format, so this is mostly field extraction.
    """
    task_data = data.get("task", {})
    status_str = data.get("status", "running")
    state_str = data.get("state", "")

    # Parse timestamps
    started_at = None
    if data.get("started_at"):
        try:
            s = data["started_at"].replace("Z", "+00:00")
            started_at = datetime.fromisoformat(s)
        except (ValueError, AttributeError):
            started_at = datetime.now(timezone.utc)

    completed_at = None
    if data.get("completed_at"):
        try:
            s = data["completed_at"].replace("Z", "+00:00")
            completed_at = datetime.fromisoformat(s)
        except (ValueError, AttributeError):
            pass

    task = TaskDefinition(
        name=task_data.get("name", ""),
        tool=task_data.get("tool", "claude"),
        prompt=task_data.get("prompt", ""),
        auto_confirm=task_data.get("auto_confirm", True),
        auto_exit=task_data.get("auto_exit", False),
    )

    return Agent(
        id=data.get("id", ""),
        task=task,
        context_id=context_id or data.get("context_id", ""),
        context_name=context_name or data.get("context_name", ""),
        context_path=data.get("context_path", ""),
        transport_type=TransportType.LOCAL if data.get("transport_type") == "local" else TransportType.SSH,
        status=_STATUS_MAP.get(status_str, AgentStatus.RUNNING),
        state=_STATE_MAP.get(state_str, AgentState.INITIALIZING),
        tmux_session=data.get("tmux_session", ""),
        tmux_socket=data.get("tmux_socket", ""),
        started_at=started_at,
        completed_at=completed_at,
        exit_reason=data.get("exit_reason"),
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
        self._delegates: dict[str, CamcDelegate] = {}  # context_name -> delegate
        self._last_event_ts: dict[str, str] = {}  # context_name -> last event ts
        self._prev_states: dict[str, str] = {}  # agent_id -> previous status

    def _get_delegate(self, context_name: str, host: str | None,
                      user: str | None, port: int | None) -> CamcDelegate:
        """Get or create a CamcDelegate for a context."""
        if context_name not in self._delegates:
            self._delegates[context_name] = CamcDelegate(host=host, user=user, port=port)
        return self._delegates[context_name]

    async def poll_once(self) -> int:
        """Poll all contexts once. Returns number of agents synced."""
        contexts = self._context_store.list()
        total = 0

        # Deduplicate by host (multiple contexts may share a machine)
        seen_hosts = set()

        for ctx in contexts:
            machine = ctx.machine
            host = machine.host if hasattr(machine, "host") else None
            user = machine.user if hasattr(machine, "user") else None
            port = machine.port if hasattr(machine, "port") else None

            # Deduplicate remote hosts
            host_key = "%s@%s:%s" % (user or "", host or "local", port or "")
            if host_key in seen_hosts:
                continue
            seen_hosts.add(host_key)

            delegate = self._get_delegate(ctx.name, host, user, port)

            # Poll agents
            try:
                if delegate._is_local:
                    # Fast path: read JSON directly
                    camc_agents = await asyncio.to_thread(delegate.read_agents_json)
                else:
                    camc_agents = await asyncio.to_thread(delegate.list_agents)
            except Exception as e:
                logger.warning("Failed to poll %s: %s", ctx.name, e)
                continue

            for agent_data in camc_agents:
                agent_id = agent_data.get("id", "")
                if not agent_id:
                    continue

                # Check if agent already exists in our store
                existing = self._agent_store.get(agent_id)
                status = agent_data.get("status", "running")

                if existing is None:
                    # New agent discovered from camc — import it
                    try:
                        agent = _camc_agent_to_model(agent_data, ctx.name, context_id=ctx.id)
                        self._agent_store.save(agent)
                        logger.info("Imported agent %s from %s", agent_id, ctx.name)
                    except Exception as e:
                        logger.warning("Failed to import agent %s: %s", agent_id, e)
                else:
                    # Update status if changed
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
                since = self._last_event_ts.get(ctx.name)
                if delegate._is_local:
                    new_events = await asyncio.to_thread(delegate.read_events_since, since)
                else:
                    new_events = await asyncio.to_thread(delegate.get_history, since=since)

                for ev in new_events:
                    ts = ev.get("ts", "")
                    if ts > (self._last_event_ts.get(ctx.name) or ""):
                        self._last_event_ts[ctx.name] = ts
                    # Convert to AgentEvent and publish
                    agent_event = AgentEvent(
                        agent_id=ev.get("agent_id", ""),
                        event_type=ev.get("type", "unknown"),
                        detail=ev.get("detail", {}),
                    )
                    self._event_bus.publish(agent_event)
            except Exception as e:
                logger.debug("Failed to poll events from %s: %s", ctx.name, e)

        return total

    async def run(self, interval: float = 5.0) -> None:
        """Run the polling loop. Call as asyncio.create_task(poller.run())."""
        logger.info("CamcPoller started (interval=%.1fs)", interval)
        while True:
            try:
                count = await self.poll_once()
                logger.debug("Polled %d agents", count)
            except asyncio.CancelledError:
                logger.info("CamcPoller stopped")
                return
            except Exception as e:
                logger.error("CamcPoller error: %s", e)
            await asyncio.sleep(interval)
