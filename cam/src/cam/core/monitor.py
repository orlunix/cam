"""Agent monitoring loop for CAM.

The AgentMonitor is the heart of the system. It polls a running TMUX session
at regular intervals, detects state changes, handles auto-confirmation prompts,
checks for completion or timeout, and maintains structured JSONL logs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.config import CamConfig
from cam.core.events import EventBus
from cam.core.models import Agent, AgentEvent, AgentState, AgentStatus
from cam.storage.agent_store import AgentStore
from cam.transport.base import Transport
from cam.utils.logging import AgentLogger

logger = logging.getLogger(__name__)


class AgentMonitor:
    """Monitoring loop for a single agent TMUX session.

    On each poll cycle the monitor:
    1. Captures TMUX output via the transport layer.
    2. Compares with the previous capture to detect changes.
    3. Checks auto-confirm patterns and sends a response if matched.
    4. Detects agent state transitions and publishes events.
    5. Detects completion (success or failure) and finalizes the agent.
    6. Enforces total and idle timeouts.
    7. Performs periodic health checks (TMUX session still alive).
    8. Writes structured JSONL log entries for every significant event.

    Args:
        agent: The Agent model instance being monitored.
        transport: Transport for communicating with the TMUX session.
        adapter: ToolAdapter for tool-specific output analysis.
        agent_store: Persistent storage for agent state.
        event_bus: Event bus for publishing lifecycle events.
        agent_logger: Structured JSONL logger for this agent.
        config: CAM configuration (contains poll_interval, timeouts, etc.).
    """

    def __init__(
        self,
        agent: Agent,
        transport: Transport,
        adapter: ToolAdapter,
        agent_store: AgentStore,
        event_bus: EventBus,
        agent_logger: AgentLogger,
        config: CamConfig,
    ) -> None:
        self._agent = agent
        self._transport = transport
        self._adapter = adapter
        self._agent_store = agent_store
        self._event_bus = event_bus
        self._logger = agent_logger
        self._config = config

        # Monitoring state
        self._previous_output: str = ""
        self._last_change_time: float = datetime.utcnow().timestamp()
        self._last_health_check: float = 0.0
        self._last_confirm_time: float = 0.0  # Cooldown to avoid re-sending
        self._poll_count: int = 0
        self._has_worked: bool = False  # True once agent enters a working state
        self._prompt_disappeared: bool = False  # True once input prompt is gone

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> AgentStatus:
        """Run the monitoring loop until the agent reaches a terminal state.

        Returns:
            The final AgentStatus after monitoring ends.
        """
        session_id = self._agent.tmux_session
        if not session_id:
            return self._finalize(AgentStatus.FAILED, "No TMUX session ID set")

        poll_interval: float = self._config.monitor.poll_interval
        idle_timeout: int = self._config.monitor.idle_timeout
        health_check_interval: int = self._config.monitor.health_check_interval

        # Resolve total timeout: task-level overrides the global default
        total_timeout: int | None = self._agent.task.timeout

        self._logger.write("monitor_start", data={
            "session_id": session_id,
            "poll_interval": poll_interval,
            "idle_timeout": idle_timeout,
            "total_timeout": total_timeout,
        })
        self._publish_event("monitor_start")

        try:
            while True:
                self._poll_count += 1
                now = datetime.utcnow().timestamp()

                # --------------------------------------------------
                # 1. Total timeout check
                # --------------------------------------------------
                if total_timeout and self._agent.started_at:
                    elapsed = (datetime.utcnow() - self._agent.started_at).total_seconds()
                    if elapsed >= total_timeout:
                        self._logger.write("timeout", data={"elapsed": elapsed, "limit": total_timeout})
                        await self._transport.kill_session(session_id)
                        return self._finalize(AgentStatus.TIMEOUT, f"Total timeout after {elapsed:.0f}s")

                # --------------------------------------------------
                # 2. Idle timeout check
                # --------------------------------------------------
                if idle_timeout > 0:
                    idle_seconds = now - self._last_change_time
                    if idle_seconds >= idle_timeout:
                        self._logger.write("idle_timeout", data={"idle_seconds": idle_seconds, "limit": idle_timeout})
                        await self._transport.kill_session(session_id)
                        return self._finalize(AgentStatus.TIMEOUT, f"Idle timeout after {idle_seconds:.0f}s with no output change")

                # --------------------------------------------------
                # 3. Health check (periodic, not every poll)
                # --------------------------------------------------
                if now - self._last_health_check >= health_check_interval:
                    self._last_health_check = now
                    alive = await self._transport.session_exists(session_id)
                    if not alive:
                        self._logger.write("session_gone", data={"session_id": session_id})
                        # Session disappeared -- check last output for completion
                        # signals before declaring failure
                        if self._previous_output:
                            completion = self._adapter.detect_completion(self._previous_output)
                            if completion == AgentStatus.COMPLETED:
                                return self._finalize(AgentStatus.COMPLETED, "Session ended cleanly")
                        return self._finalize(AgentStatus.COMPLETED, "TMUX session exited")

                # --------------------------------------------------
                # 4. Capture output
                # --------------------------------------------------
                output = await self._transport.capture_output(session_id)

                # --------------------------------------------------
                # 5. Compare with previous output
                # --------------------------------------------------
                output_changed = output != self._previous_output
                if output_changed:
                    self._last_change_time = now
                    self._logger.write("output", output=output)

                self._previous_output = output

                if not output.strip():
                    await asyncio.sleep(poll_interval)
                    continue

                # --------------------------------------------------
                # 6. Auto-confirm check (only when output changed, with cooldown)
                # --------------------------------------------------
                if output_changed and self._config.general.auto_confirm:
                    # 5-second cooldown prevents re-sending when same prompt persists
                    now_confirm = datetime.utcnow().timestamp()
                    if now_confirm - self._last_confirm_time >= 5.0:
                        confirm_action = self._adapter.should_auto_confirm(output)
                        if confirm_action is not None:
                            self._last_confirm_time = now_confirm
                            self._logger.write("auto_confirm", data={
                                "response": confirm_action.response,
                                "send_enter": confirm_action.send_enter,
                            })
                            self._publish_event("auto_confirm", {
                                "response": confirm_action.response,
                                "send_enter": confirm_action.send_enter,
                            })
                            await self._transport.send_input(
                                session_id,
                                confirm_action.response,
                                send_enter=confirm_action.send_enter,
                            )
                            # Give the tool a moment to process the confirmation
                            await asyncio.sleep(0.5)
                            continue

                # --------------------------------------------------
                # 7. Detect state changes
                # --------------------------------------------------
                new_state = self._adapter.detect_state(output)
                if new_state is not None and new_state != self._agent.state:
                    if new_state != AgentState.INITIALIZING:
                        self._has_worked = True
                    old_state = self._agent.state
                    self._agent.state = new_state
                    self._agent_store.update_status(
                        str(self._agent.id),
                        self._agent.status,
                        state=new_state,
                    )
                    self._logger.write("state_change", data={
                        "from": old_state.value,
                        "to": new_state.value,
                    })
                    self._publish_event("state_change", {
                        "from": old_state.value,
                        "to": new_state.value,
                    })

                # --------------------------------------------------
                # 8. Detect completion (only when output has stabilized)
                # --------------------------------------------------
                idle_for = now - self._last_change_time
                completion_status = (
                    self._adapter.detect_completion(output)
                    if not output_changed and idle_for >= 3.0
                    else None
                )
                if completion_status is not None:
                    # Extract cost and file info from final output
                    cost = self._adapter.estimate_cost(output)
                    if cost is not None:
                        self._agent.cost_estimate = cost

                    files = self._adapter.parse_files_changed(output)
                    if files:
                        self._agent.files_changed = files

                    reason = "completed" if completion_status == AgentStatus.COMPLETED else "failed"
                    return self._finalize(completion_status, reason)

                # --------------------------------------------------
                # 8b. Detect completion via input prompt return
                # --------------------------------------------------
                # For interactive tools (e.g. Claude), completion is
                # detected when the tool returns to its input prompt
                # after having done work. We require the prompt to have
                # disappeared first (Claude was actively working) to
                # avoid false positives from the startup screen.
                if self._adapter.needs_prompt_after_launch():
                    prompt_visible = self._adapter.is_ready_for_input(output)
                    if not prompt_visible and self._has_worked:
                        self._prompt_disappeared = True
                    if (
                        prompt_visible
                        and self._has_worked
                        and self._prompt_disappeared
                    ):
                        self._logger.write("prompt_return_completion", data={
                            "state": self._agent.state.value,
                        })
                        return self._finalize(AgentStatus.COMPLETED, "Tool returned to input prompt")

                # --------------------------------------------------
                # 9. Sleep until next poll
                # --------------------------------------------------
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            self._logger.write("cancelled")
            return self._finalize(AgentStatus.KILLED, "Monitor cancelled")
        except Exception as exc:
            logger.exception("Unexpected error in monitor loop for agent %s", self._agent.id)
            self._logger.write("error", data={"error": str(exc)})
            return self._finalize(AgentStatus.FAILED, f"Monitor error: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish_event(self, event_type: str, detail: dict | None = None) -> None:
        """Create and publish an AgentEvent on the event bus.

        Also appends the event to the agent's in-memory event list and
        persists it via the agent store.

        Args:
            event_type: Short identifier for the event (e.g. 'state_change').
            detail: Optional dict of structured data about the event.
        """
        event = AgentEvent(
            agent_id=self._agent.id,
            event_type=event_type,
            detail=detail or {},
        )
        # In-memory record on the Agent model
        self._agent.events.append(event.model_dump(mode="json"))
        # Persist the event
        try:
            self._agent_store.add_event(event)
        except Exception:
            logger.warning("Failed to persist event %s for agent %s", event_type, self._agent.id)
        # Broadcast
        self._event_bus.publish(event)

    def _finalize(self, status: AgentStatus, reason: str | None = None) -> AgentStatus:
        """Finalize the agent with the given terminal status.

        Updates the agent model, persists the status change, publishes a
        lifecycle event, and writes a log entry.

        Args:
            status: Terminal status to assign (COMPLETED, FAILED, TIMEOUT, KILLED).
            reason: Human-readable explanation for the final status.

        Returns:
            The status that was set (same as the input).
        """
        self._agent.status = status
        self._agent.completed_at = datetime.utcnow()
        self._agent.exit_reason = reason

        # Persist
        try:
            self._agent_store.update_status(
                str(self._agent.id),
                status,
                exit_reason=reason,
            )
        except Exception:
            logger.warning("Failed to persist final status for agent %s", self._agent.id)

        # Log
        duration = self._agent.duration_seconds()
        self._logger.write("finalize", data={
            "status": status.value,
            "reason": reason,
            "duration_seconds": duration,
            "poll_count": self._poll_count,
            "cost_estimate": self._agent.cost_estimate,
            "files_changed": self._agent.files_changed,
        })

        # Publish lifecycle event
        self._publish_event("agent_finished", {
            "status": status.value,
            "reason": reason,
            "duration_seconds": duration,
        })

        logger.info(
            "Agent %s finalized: status=%s reason=%s duration=%.1fs",
            self._agent.id,
            status.value,
            reason,
            duration or 0.0,
        )

        return status
