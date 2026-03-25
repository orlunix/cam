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
from cam.core.probe import ProbeResult, probe_session
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
        client_active: bool = False,
    ) -> None:
        self._agent = agent
        self._transport = transport
        self._adapter = adapter
        self._agent_store = agent_store
        self._event_bus = event_bus
        self._logger = agent_logger
        self._config = config

        # Pull mode: cam-client monitors locally, server reads processed state
        # via SSH.  Falls back to full SSH polling if pull fails for 30s.
        self._pull_mode: bool = client_active
        self._last_pull_success: float = datetime.utcnow().timestamp()
        self._pull_hash: str = ""

        # Legacy passive mode (HTTP push) — kept for backward compat
        self._client_active: bool = False
        self._last_client_sync: float = datetime.utcnow().timestamp()

        # Monitoring state
        self._previous_output: str = ""
        self._last_change_time: float = datetime.utcnow().timestamp()
        self._last_health_check: float = 0.0
        self._last_confirm_time: float = 0.0  # Cooldown to avoid re-sending
        self._poll_count: int = 0
        # If agent already advanced beyond initializing (e.g. monitor restart),
        # assume it has done work so we don't falsely mark it as failed.
        self._has_worked: bool = agent.state not in (AgentState.INITIALIZING, None)
        # Probe detection state
        self._last_probe_time: float = 0.0
        self._probe_count: int = 0
        self._consecutive_probe_completed: int = 0
        self._idle_confirmed: bool = False  # True once probe confirms idle
        self._completion_detected: AgentStatus | None = None  # Set when completion pattern matched

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

        # All agents are interactive — they never auto-complete.
        # Agents end only when: user stops, session dies, or explicit timeout.

        try:
            while True:
                self._poll_count += 1
                now = datetime.utcnow().timestamp()

                # --------------------------------------------------
                # 1. Total timeout check (only if explicitly set)
                # --------------------------------------------------
                if total_timeout and total_timeout > 0 and self._agent.started_at:
                    elapsed = (datetime.utcnow() - self._agent.started_at).total_seconds()
                    if elapsed >= total_timeout:
                        self._logger.write("timeout", data={"elapsed": elapsed, "limit": total_timeout})
                        await self._transport.kill_session(session_id)
                        return self._finalize(AgentStatus.TIMEOUT, f"Total timeout after {elapsed:.0f}s")

                # --------------------------------------------------
                # 2a. Pull mode: read cam-client's local state via SSH
                # --------------------------------------------------
                if self._pull_mode:
                    pulled = await self._pull_client_status()
                    if pulled is not None:
                        self._last_pull_success = now
                        terminal = self._apply_remote_state(pulled)
                        if terminal is not None:
                            return terminal
                    else:
                        # Pull failed — if no success for 30s, fall back
                        if now - self._last_pull_success >= 30:
                            logger.warning(
                                "Pull mode failed for 30s, falling back to SSH polling for agent %s",
                                self._agent.id,
                            )
                            self._pull_mode = False
                            # Continue to normal SSH polling below
                        else:
                            await asyncio.sleep(poll_interval * 2)
                            continue

                    await asyncio.sleep(poll_interval * 2)
                    continue

                # --------------------------------------------------
                # 2b. cam-client passive mode (legacy HTTP push)
                # --------------------------------------------------
                if self._client_active:
                    fresh = self._agent_store.get(str(self._agent.id))
                    if fresh and fresh.is_terminal():
                        return fresh.status
                    await asyncio.sleep(poll_interval * 2)
                    continue

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
                        # If the agent never entered a working state, this is
                        # a crash (e.g. command not found) — mark as FAILED.
                        if not self._has_worked:
                            return self._finalize(AgentStatus.FAILED, "TMUX session exited before agent started working")
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
                # 6. Auto-confirm check (with cooldown)
                # --------------------------------------------------
                # Re-read auto_confirm from DB so API changes take
                # effect on the running monitor without restart.
                fresh = self._agent_store.get(str(self._agent.id))
                if fresh and fresh.task.auto_confirm != self._agent.task.auto_confirm:
                    self._agent.task.auto_confirm = fresh.task.auto_confirm

                # Check every poll cycle (not just on output_changed)
                # to catch prompts that rendered between polls.
                # 5-second cooldown prevents rapid re-sending.
                ac = self._agent.task.auto_confirm
                if ac is None:
                    ac = self._config.general.auto_confirm
                if ac:
                    now_confirm = datetime.utcnow().timestamp()
                    if now_confirm - self._last_confirm_time >= self._adapter.get_confirm_cooldown():
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
                            await asyncio.sleep(self._adapter.get_confirm_sleep())
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
                # 8. Detect completion
                # --------------------------------------------------
                idle_for = now - self._last_change_time
                if not output_changed and idle_for >= self._adapter.get_completion_stable():
                    completion_status = self._adapter.detect_completion(output)
                    if completion_status is not None:
                        self._has_worked = True  # completion signal = agent did work
                        if not self._completion_detected:
                            self._completion_detected = completion_status
                            cost = self._adapter.estimate_cost(output)
                            if cost is not None:
                                self._agent.cost_estimate = cost
                            files = self._adapter.parse_files_changed(output)
                            if files:
                                self._agent.files_changed = files
                            self._logger.write("completion_detected", data={
                                "status": completion_status.value,
                            })
                            self._publish_event("completion_detected", {
                                "status": completion_status.value,
                            })

                # --------------------------------------------------
                # 8b. Probe-based idle detection
                # --------------------------------------------------
                # Probe to confirm idle state, but stop once confirmed
                # to avoid polluting the terminal with probe characters.
                # Resumes if output changes (agent starts working again).
                # Skip reset if output changed shortly after a probe — that
                # change was likely caused by the probe itself (e.g. Enter
                # triggering a confirmation response).
                probe_caused = (
                    self._last_probe_time > 0
                    and now - self._last_probe_time < self._adapter.get_probe_wait() + poll_interval * 2
                )
                if output_changed and not probe_caused:
                    self._idle_confirmed = False
                    self._consecutive_probe_completed = 0
                    self._completion_detected = None

                max_probes = self._adapter.get_probe_idle_threshold() * 3
                if (
                    self._config.monitor.probe_detection
                    and self._has_worked
                    and not self._idle_confirmed
                    and self._probe_count < max_probes
                ):
                    probe_stable = self._config.monitor.probe_stable_seconds
                    probe_cooldown = self._config.monitor.probe_cooldown
                    if (
                        idle_for >= probe_stable
                        and now - self._last_probe_time >= probe_cooldown
                    ):
                        self._last_probe_time = now
                        self._probe_count += 1
                        probe_action = self._adapter.get_probe_action(
                            auto_confirm=bool(ac),
                        )
                        probe_result = await probe_session(
                            self._transport, session_id,
                            wait=self._adapter.get_probe_wait(),
                            probe_char=probe_action.char,
                            send_enter=probe_action.send_enter,
                        )
                        probe_data = {
                            "result": probe_result.value,
                            "probe_count": self._probe_count,
                            "consecutive_completed": self._consecutive_probe_completed,
                        }
                        self._logger.write("probe", data=probe_data)
                        self._publish_event("probe", probe_data)

                        if probe_result == ProbeResult.CONFIRMED and probe_action.is_confirm:
                            self._logger.write("smart_probe_confirm", data={"char": probe_action.char})
                            self._publish_event("smart_probe_confirm")

                        if probe_result == ProbeResult.COMPLETED:
                            self._consecutive_probe_completed += 1
                        elif probe_result == ProbeResult.CONFIRMED and probe_action.is_confirm:
                            # Smart probe: "1" consumed by TUI = idle at prompt.
                            # Treat same as COMPLETED for idle counting.
                            self._consecutive_probe_completed += 1
                        elif probe_result == ProbeResult.BUSY:
                            self._last_change_time = now
                            self._consecutive_probe_completed = 0
                        elif probe_result == ProbeResult.CONFIRMED:
                            # Non-smart confirm: something changed unexpectedly
                            self._last_change_time = now
                            self._consecutive_probe_completed = 0
                        else:
                            self._consecutive_probe_completed = 0

                        if self._consecutive_probe_completed >= self._adapter.get_probe_idle_threshold():
                            # Confirmed idle — stop probing
                            self._idle_confirmed = True
                            self._logger.write("idle_confirmed", data={
                                "probe_count": self._probe_count,
                            })
                            self._publish_event("idle_confirmed", {
                                "probe_count": self._probe_count,
                            })

                # --------------------------------------------------
                # 8c. Auto-exit on completion + idle confirmed
                # --------------------------------------------------
                # CLI --auto-exit flag overrides adapter config
                auto_exit = self._agent.task.auto_exit
                if auto_exit is None:
                    auto_exit = self._adapter.get_auto_exit()
                # Two paths to auto-exit:
                # 1. Probe-confirmed idle: has_worked + idle_confirmed + completion
                # 2. Completion pattern only: for trivial tasks where state never
                #    changes (e.g. "say hello"), completion pattern (two ❯ prompts)
                #    is strong enough evidence if output has been stable long enough
                completion_stable_enough = (
                    self._completion_detected == AgentStatus.COMPLETED
                    and idle_for >= self._adapter.get_completion_stable() * 3
                )
                if (
                    auto_exit
                    and self._completion_detected == AgentStatus.COMPLETED
                    and (
                        (self._has_worked and self._idle_confirmed)
                        or completion_stable_enough
                    )
                ):
                    exit_action = self._adapter.get_exit_action()
                    self._logger.write("auto_exit", data={
                        "exit_action": exit_action,
                    })
                    if exit_action == "kill_session":
                        try:
                            await self._transport.kill_session(session_id)
                        except Exception:
                            pass
                    elif exit_action == "send_exit":
                        try:
                            await self._transport.send_input(
                                session_id,
                                self._adapter.get_exit_command(),
                                send_enter=True,
                            )
                            # Wait for session to die
                            for _ in range(10):
                                await asyncio.sleep(1)
                                if not await self._transport.session_exists(session_id):
                                    break
                            else:
                                # Force kill if send_exit didn't work
                                try:
                                    await self._transport.kill_session(session_id)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    # mark_only: just finalize without touching the session
                    return self._finalize(AgentStatus.COMPLETED, "Task completed (auto-exit)")

                # --------------------------------------------------
                # 9. Sleep until next poll
                # --------------------------------------------------
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            # Monitor was stopped externally (SIGTERM, restart, etc.)
            # Do NOT change agent status here — if `cam kill` wants to
            # mark KILLED, it does so explicitly via update_status().
            self._logger.write("monitor_stopped")
            return self._agent.status
        except Exception as exc:
            logger.exception("Unexpected error in monitor loop for agent %s", self._agent.id)
            self._logger.write("error", data={"error": str(exc)})
            return self._finalize(AgentStatus.FAILED, f"Monitor error: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _pull_client_status(self) -> dict | None:
        """Read cam-client's processed state for this agent via SSH.

        Runs ``cam-client.py status --id <agent_id> --hash <prev_hash>``
        on the remote.  The remote returns JSON with agent state/status, or
        ``{"unchanged": true}`` if the hash matches (nothing new).

        Returns:
            Parsed dict on success, None on any failure.
        """
        import json
        import shlex

        if not hasattr(self._transport, '_run_ssh'):
            return None

        agent_id = str(self._agent.id)
        cmd = f"python3 ~/.cam/cam-client.py status --id {shlex.quote(agent_id)}"
        if self._pull_hash:
            cmd += f" --hash {shlex.quote(self._pull_hash)}"

        try:
            ok, output = await self._transport._run_ssh(cmd, check=False)
            if not ok or not output.strip():
                return None
            data = json.loads(output.strip())
            return data
        except Exception as exc:
            logger.debug("Pull failed for agent %s: %s", agent_id, exc)
            return None

    def _apply_remote_state(self, data: dict) -> AgentStatus | None:
        """Apply state from cam-client pull response to the local agent.

        Args:
            data: Parsed JSON from cam-client status command.

        Returns:
            Terminal AgentStatus if the agent has finished, else None.
        """
        if data.get("unchanged"):
            return None

        # Update pull hash for next conditional request
        if "hash" in data:
            self._pull_hash = data["hash"]

        # Extract remote agent info (may be a list or single object)
        agents = data.get("agents", [])
        if not agents:
            # Single agent response
            if "status" in data:
                agents = [data]
            else:
                return None

        agent_id = str(self._agent.id)
        remote = None
        for a in agents:
            if a.get("id") == agent_id:
                remote = a
                break
        if not remote:
            return None

        # Apply state change
        remote_state_str = remote.get("state")
        if remote_state_str:
            try:
                new_state = AgentState(remote_state_str)
                if new_state != self._agent.state:
                    if new_state != AgentState.INITIALIZING:
                        self._has_worked = True
                    old_state = self._agent.state
                    self._agent.state = new_state
                    self._agent_store.update_status(
                        agent_id, self._agent.status, state=new_state,
                    )
                    self._logger.write("state_change", data={
                        "from": old_state.value, "to": new_state.value, "source": "pull",
                    })
                    self._publish_event("state_change", {
                        "from": old_state.value, "to": new_state.value,
                    })
            except ValueError:
                pass  # Unknown state string

        # Apply status change (terminal states)
        remote_status_str = remote.get("status")
        if remote_status_str in ("completed", "failed", "stopped"):
            try:
                remote_status = AgentStatus(remote_status_str)
                reason = remote.get("exit_reason", f"Reported by cam-client: {remote_status_str}")
                return self._finalize(remote_status, reason)
            except ValueError:
                pass

        return None

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
        self._agent.state = AgentState.IDLE
        self._agent.completed_at = datetime.utcnow()
        self._agent.exit_reason = reason

        # Persist
        try:
            self._agent_store.update_status(
                str(self._agent.id),
                status,
                state=AgentState.IDLE,
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
