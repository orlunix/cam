"""Central orchestrator for CAM agent lifecycle.

The AgentManager coordinates agent creation, launch, monitoring, and teardown.
It ties together transports, adapters, storage, and the event bus to provide
a single high-level API for running and managing coding agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

from cam.adapters.base import ToolAdapter
from cam.adapters.registry import AdapterRegistry
from cam.constants import PID_DIR, SOCKET_DIR
from cam.core.config import CamConfig
from cam.core.events import EventBus
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
from cam.core.monitor import AgentMonitor
from cam.storage.agent_store import AgentStore
from cam.storage.context_store import ContextStore
from cam.transport.base import Transport
from cam.transport.factory import TransportFactory
from cam.utils.logging import AgentLogger

logger = logging.getLogger(__name__)


class AgentManagerError(Exception):
    """Error raised by AgentManager operations."""


class AgentManager:
    """Central orchestrator for coding agent lifecycle.

    Coordinates the full lifecycle of agents: creation, launch into TMUX
    sessions, monitoring, retry logic, and graceful or forced shutdown.

    Args:
        config: CAM configuration.
        context_store: Persistent storage for contexts.
        agent_store: Persistent storage for agents and events.
        event_bus: Event bus for lifecycle event broadcasting.
        adapter_registry: Registry of available tool adapters.
        transport_factory_class: Factory class for creating transports
            (defaults to TransportFactory; injectable for testing).
    """

    def __init__(
        self,
        config: CamConfig,
        context_store: ContextStore,
        agent_store: AgentStore,
        event_bus: EventBus,
        adapter_registry: AdapterRegistry,
        transport_factory_class: type = TransportFactory,
    ) -> None:
        self._config = config
        self._context_store = context_store
        self._agent_store = agent_store
        self._event_bus = event_bus
        self._adapter_registry = adapter_registry
        self._transport_factory_class = transport_factory_class

        # Track background monitor tasks so they can be awaited/cancelled
        self._monitor_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_agent(
        self,
        task: TaskDefinition,
        context: Context,
        follow: bool = True,
    ) -> Agent:
        """Create, start, and optionally monitor an agent.

        This is the primary entry point for launching work. It:
        1. Resolves the tool adapter from the registry.
        2. Creates a transport for the target machine.
        3. Builds and persists a new Agent model.
        4. Creates a TMUX session and sends the launch command.
        5. Starts the monitoring loop (foreground or background).
        6. Handles retry logic when the monitor reports failure.

        Args:
            task: Task definition including prompt, tool name, and timeouts.
            context: Context describing the workspace and machine.
            follow: If True, block until the agent finishes (foreground).
                    If False, launch monitoring in the background and return
                    immediately.

        Returns:
            The Agent model. If ``follow=True`` the agent will be in a
            terminal state. If ``follow=False`` it will be RUNNING.

        Raises:
            AgentManagerError: If the adapter is not found, session creation
                fails, or other setup errors occur.
        """
        # 1. Get adapter from registry
        adapter = self._adapter_registry.get(task.tool)
        if adapter is None:
            available = ", ".join(self._adapter_registry.names()) or "(none)"
            raise AgentManagerError(
                f"No adapter registered for tool '{task.tool}'. "
                f"Available adapters: {available}"
            )

        # 2. Create transport from context machine config
        transport = self._create_transport(context)

        # 3. Generate agent ID and session name
        agent_id = str(uuid4())
        session_name = f"cam-{agent_id.replace('-', '')[:12]}"
        socket_path = str(SOCKET_DIR / f"{session_name}.sock")

        # 4. Build Agent model
        now = datetime.utcnow()
        agent = Agent(
            id=agent_id,
            task=task,
            context_id=context.id,
            context_name=context.name,
            context_path=context.path,
            transport_type=context.machine.type,
            status=AgentStatus.STARTING,
            state=AgentState.INITIALIZING,
            tmux_session=session_name,
            tmux_socket=socket_path,
            started_at=now,
        )

        # 5. Save agent to store
        self._agent_store.save(agent)
        logger.info("Created agent %s for task '%s' on context '%s'", agent_id, task.name, context.name)

        # 6. Ensure socket directory exists
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)

        # 7. Get launch command from adapter
        launch_command = adapter.get_launch_command(task, context)

        # 8. Create TMUX session via transport
        try:
            session_created = await transport.create_session(
                session_name, launch_command, context.path
            )
        except Exception as transport_err:
            session_created = False
            transport_detail = str(transport_err)
        else:
            transport_detail = ""
        if not session_created:
            reason = transport_detail or "Failed to create TMUX session"
            agent.status = AgentStatus.FAILED
            agent.completed_at = datetime.utcnow()
            agent.exit_reason = reason
            self._agent_store.save(agent)
            raise AgentManagerError(reason)

        # 9. If adapter needs prompt after launch, handle startup prompts then send task
        if adapter.needs_prompt_after_launch():
            await self._wait_and_send_prompt(
                transport, adapter, session_name, task.prompt
            )

        # 10. Update status to RUNNING
        agent.status = AgentStatus.RUNNING
        self._agent_store.update_status(str(agent_id), AgentStatus.RUNNING)

        # Update context last-used timestamp
        try:
            self._context_store.update_last_used(str(context.id))
        except Exception:
            logger.debug("Failed to update last_used_at for context %s", context.id)

        # Publish started event
        started_event = AgentEvent(
            agent_id=agent_id,
            event_type="agent_started",
            detail={"task": task.name, "tool": task.tool, "context": context.name},
        )
        agent.events.append(started_event.model_dump(mode="json"))
        try:
            self._agent_store.add_event(started_event)
        except Exception:
            pass
        self._event_bus.publish(started_event)

        # 11. Start monitoring
        if follow:
            await self._run_monitor_with_retries(
                agent, transport, adapter, follow=True
            )
        else:
            # Spawn a background monitor subprocess that survives CLI exit.
            # This enables auto-confirm, state tracking, and timeouts in detach mode.
            self._spawn_background_monitor(agent)

        return agent

    async def stop_agent(self, agent_id: str, graceful: bool = True) -> None:
        """Stop a running agent.

        Kills the TMUX session and updates the agent status to KILLED.

        Args:
            agent_id: ID of the agent to stop.
            graceful: Reserved for future use (graceful shutdown signals).
                      Currently all stops kill the TMUX session immediately.

        Raises:
            AgentManagerError: If agent is not found or not in a running state.
        """
        agent = self._agent_store.get(agent_id)
        if agent is None:
            raise AgentManagerError(f"Agent '{agent_id}' not found")

        if agent.is_terminal():
            logger.info("Agent %s is already in terminal state %s", agent_id, agent.status.value)
            return

        # Cancel in-process background monitor task if one exists
        monitor_task = self._monitor_tasks.pop(agent_id, None)
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        # Kill background monitor subprocess if running (detach mode)
        pid_path = PID_DIR / f"{agent_id}.pid"
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                logger.info("Killed background monitor pid=%d for agent %s", pid, agent_id)
            except (ProcessLookupError, ValueError, OSError):
                pass
            pid_path.unlink(missing_ok=True)

        # Kill TMUX session
        if agent.tmux_session:
            context = self._context_store.get(str(agent.context_id))
            if context:
                try:
                    transport = self._create_transport(context)
                    await transport.kill_session(agent.tmux_session)
                except Exception as exc:
                    logger.warning("Failed to kill TMUX session %s: %s", agent.tmux_session, exc)

        # Update status
        self._agent_store.update_status(
            agent_id,
            AgentStatus.KILLED,
            exit_reason="Stopped by user" if graceful else "Force killed",
        )

        # Publish event
        kill_event = AgentEvent(
            agent_id=agent.id,
            event_type="agent_killed",
            detail={"graceful": graceful},
        )
        try:
            self._agent_store.add_event(kill_event)
        except Exception:
            pass
        self._event_bus.publish(kill_event)

        logger.info("Stopped agent %s", agent_id)

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Get an agent by ID.

        Args:
            agent_id: The agent's unique identifier.

        Returns:
            Agent model if found, None otherwise.
        """
        return self._agent_store.get(agent_id)

    async def list_agents(self, **filters) -> list[Agent]:
        """List agents with optional filters.

        Supported filter keys match AgentStore.list() parameters:
        - status: AgentStatus
        - context_id: str
        - tool: str
        - limit: int

        Args:
            **filters: Keyword arguments passed to AgentStore.list().

        Returns:
            List of Agent models matching the filters.
        """
        return self._agent_store.list(**filters)

    async def reconcile(self) -> list[Agent]:
        """Check all running agents against actual TMUX sessions.

        For each agent with status RUNNING, verifies that the corresponding
        TMUX session still exists. Agents whose sessions have disappeared
        are marked as FAILED with an appropriate exit reason.

        Returns:
            List of agents that were marked as orphaned (FAILED).
        """
        orphaned: list[Agent] = []
        running_agents = self._agent_store.list(status=AgentStatus.RUNNING)

        for agent in running_agents:
            if not agent.tmux_session:
                # No session ID -- definitely orphaned
                self._agent_store.update_status(
                    str(agent.id),
                    AgentStatus.FAILED,
                    exit_reason="No TMUX session ID recorded",
                )
                orphaned.append(agent)
                continue

            # Build transport for this agent's context
            context = self._context_store.get(str(agent.context_id))
            if context is None:
                # Context was deleted -- cannot verify, mark orphaned
                self._agent_store.update_status(
                    str(agent.id),
                    AgentStatus.FAILED,
                    exit_reason="Context no longer exists",
                )
                orphaned.append(agent)
                continue

            try:
                transport = self._create_transport(context)
                alive = await transport.session_exists(agent.tmux_session)
            except Exception as exc:
                logger.warning(
                    "Failed to check session for agent %s: %s", agent.id, exc
                )
                # Cannot verify -- leave as-is for now
                continue

            if not alive:
                self._agent_store.update_status(
                    str(agent.id),
                    AgentStatus.FAILED,
                    exit_reason="TMUX session disappeared",
                )
                orphaned.append(agent)

                # Publish event
                orphan_event = AgentEvent(
                    agent_id=agent.id,
                    event_type="agent_orphaned",
                    detail={"session": agent.tmux_session},
                )
                try:
                    self._agent_store.add_event(orphan_event)
                except Exception:
                    pass
                self._event_bus.publish(orphan_event)

                logger.warning("Agent %s orphaned: TMUX session %s disappeared", agent.id, agent.tmux_session)

        if orphaned:
            logger.info("Reconciliation found %d orphaned agent(s)", len(orphaned))

        return orphaned

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _wait_and_send_prompt(
        self,
        transport: Transport,
        adapter: ToolAdapter,
        session_name: str,
        prompt: str,
    ) -> None:
        """Wait for interactive tool readiness, handle pre-prompt confirmations, then send the task prompt.

        Polls the TMUX output until the adapter reports readiness (e.g. Claude
        shows its input prompt). During the wait, auto-confirms any
        trust/permission prompts. Falls back to sending the prompt after
        max_wait seconds even if readiness is not detected.

        Args:
            transport: Transport for TMUX communication.
            adapter: Tool adapter (used for is_ready_for_input, should_auto_confirm).
            session_name: TMUX session identifier.
            prompt: Task prompt to send.
        """
        max_wait = adapter.get_startup_wait()
        poll_interval = 1.0
        elapsed = 0.0
        ready = False

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            output = await transport.capture_output(session_name)
            if not output.strip():
                continue

            # Handle trust/permission prompts that appear before readiness
            confirm_action = adapter.should_auto_confirm(output)
            if confirm_action is not None:
                logger.info("Pre-prompt auto-confirm in %s: sending '%s'",
                            session_name, confirm_action.response)
                await transport.send_input(
                    session_name,
                    confirm_action.response,
                    send_enter=confirm_action.send_enter,
                )
                await asyncio.sleep(3.0)
                elapsed += 3.0
                continue

            # Check if tool is ready for input
            if adapter.is_ready_for_input(output):
                logger.info("Tool ready for input in %s after %.1fs", session_name, elapsed)
                ready = True
                break

        if not ready:
            logger.warning(
                "Tool readiness not detected in %s after %.1fs, sending prompt anyway",
                session_name, elapsed,
            )

        await transport.send_input(session_name, prompt, send_enter=True)

    def _spawn_background_monitor(self, agent: Agent) -> None:
        """Spawn a detached subprocess to monitor the agent.

        The subprocess runs cam.core.monitor_runner which handles auto-confirm,
        state detection, timeouts, and completion tracking independently of the
        CLI process. Uses start_new_session=True to survive parent exit.

        Args:
            agent: The agent to monitor.
        """
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "cam.core.monitor_runner", str(agent.id)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info(
                "Spawned background monitor for agent %s (pid=%d)",
                agent.id, proc.pid,
            )
        except Exception:
            logger.exception(
                "Failed to spawn background monitor for agent %s", agent.id
            )

    def _create_transport(self, context: Context) -> Transport:
        """Create a transport for the given context.

        Args:
            context: Context with machine configuration.

        Returns:
            Configured Transport instance.
        """
        return self._transport_factory_class.create(context.machine)

    async def _run_monitor_with_retries(
        self,
        agent: Agent,
        transport: Transport,
        adapter: ToolAdapter,
        follow: bool,
    ) -> AgentStatus:
        """Run the monitor loop with retry logic.

        If the monitor returns FAILED and the agent has retries remaining
        (from task.retry.max_retries), the agent is restarted with exponential
        backoff.

        Args:
            agent: The agent to monitor.
            transport: Transport for TMUX communication.
            adapter: Tool adapter for output analysis.
            follow: If True, run in foreground; if False, run as background task.

        Returns:
            The final AgentStatus.
        """
        if follow:
            return await self._run_monitor_loop(agent, transport, adapter)
        else:
            # Launch as background task
            task = asyncio.create_task(
                self._run_monitor_loop(agent, transport, adapter),
                name=f"monitor-{agent.id}",
            )
            self._monitor_tasks[str(agent.id)] = task
            return agent.status  # Still RUNNING at this point

    async def _run_monitor_loop(
        self,
        agent: Agent,
        transport: Transport,
        adapter: ToolAdapter,
    ) -> AgentStatus:
        """Execute the monitor with retry handling.

        Args:
            agent: The agent to monitor.
            transport: Transport for TMUX communication.
            adapter: Tool adapter for output analysis.

        Returns:
            The final AgentStatus after all retries are exhausted or success.
        """
        max_retries = agent.task.retry.max_retries

        while True:
            agent_logger = AgentLogger(str(agent.id))
            agent_logger.open()

            try:
                monitor = AgentMonitor(
                    agent=agent,
                    transport=transport,
                    adapter=adapter,
                    agent_store=self._agent_store,
                    event_bus=self._event_bus,
                    agent_logger=agent_logger,
                    config=self._config,
                )
                final_status = await monitor.run()
            finally:
                agent_logger.close()

            # Check if we should retry
            if final_status == AgentStatus.FAILED and agent.retry_count < max_retries:
                agent.retry_count += 1
                agent.status = AgentStatus.RETRYING
                self._agent_store.save(agent)

                # Calculate backoff delay
                backoff = min(
                    agent.task.retry.backoff_base ** agent.retry_count,
                    agent.task.retry.backoff_max,
                )
                logger.info(
                    "Agent %s failed, retrying (%d/%d) after %.1fs backoff",
                    agent.id, agent.retry_count, max_retries, backoff,
                )

                retry_event = AgentEvent(
                    agent_id=agent.id,
                    event_type="agent_retry",
                    detail={
                        "attempt": agent.retry_count,
                        "max_retries": max_retries,
                        "backoff_seconds": backoff,
                    },
                )
                try:
                    self._agent_store.add_event(retry_event)
                except Exception:
                    pass
                self._event_bus.publish(retry_event)

                await asyncio.sleep(backoff)

                # Re-create the TMUX session for the retry
                session_name = agent.tmux_session
                if session_name:
                    # Kill old session if it still exists
                    try:
                        await transport.kill_session(session_name)
                    except Exception:
                        pass

                    launch_command = adapter.get_launch_command(agent.task, Context(
                        id=agent.context_id,
                        name=agent.context_name,
                        path=agent.context_path,
                        machine=MachineConfig(type=agent.transport_type),
                    ))
                    session_created = await transport.create_session(
                        session_name, launch_command, agent.context_path
                    )
                    if not session_created:
                        agent.status = AgentStatus.FAILED
                        agent.completed_at = datetime.utcnow()
                        agent.exit_reason = f"Failed to recreate TMUX session on retry {agent.retry_count}"
                        self._agent_store.save(agent)
                        return AgentStatus.FAILED

                    # Send prompt if needed
                    if adapter.needs_prompt_after_launch():
                        await self._wait_and_send_prompt(
                            transport, adapter, session_name, agent.task.prompt
                        )

                    agent.status = AgentStatus.RUNNING
                    agent.state = AgentState.INITIALIZING
                    agent.completed_at = None
                    agent.exit_reason = None
                    self._agent_store.save(agent)
                else:
                    # Cannot retry without a session name
                    return final_status

                # Loop back to run monitor again
                continue

            # No retry needed or no retries left
            return final_status
