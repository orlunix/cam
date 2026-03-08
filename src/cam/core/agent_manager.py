"""Central orchestrator for CAM agent lifecycle.

The AgentManager coordinates agent creation, launch, monitoring, and teardown.
It ties together transports, adapters, storage, and the event bus to provide
a single high-level API for running and managing coding agents.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cam.adapters.base import ToolAdapter
from cam.adapters.registry import AdapterRegistry
from cam.constants import LOG_DIR, PID_DIR, SOCKET_DIR
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

        # 8b. Start pipe-pane to capture full output to log file
        output_log_dir = LOG_DIR / "output"
        output_log_dir.mkdir(parents=True, exist_ok=True)
        output_log_path = str(output_log_dir / f"{agent_id}.log")
        if hasattr(transport, 'start_logging'):
            await transport.start_logging(session_name, output_log_path)

        # 9. If adapter needs prompt after launch, handle startup prompts then send task
        prompt_failed = False
        if adapter.needs_prompt_after_launch():
            try:
                await self._wait_and_send_prompt(
                    transport, adapter, session_name, task.prompt, task.auto_confirm
                )
            except Exception as e:
                logger.error(
                    "Failed to send prompt during startup for agent %s: %s",
                    agent_id, e,
                )
                prompt_failed = True
                # In follow mode, propagate the error immediately
                if follow:
                    agent.status = AgentStatus.FAILED
                    agent.completed_at = datetime.now(timezone.utc)
                    agent.exit_reason = f"Startup prompt failed: {e}"
                    self._agent_store.save(agent)
                    raise AgentManagerError(f"Failed to start agent: {e}") from e
                # In detach mode, continue so the monitor still gets spawned —
                # the monitor can detect completion/failure from the tmux session.

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

        # 10b. Deploy cam-client on SSH/Agent transports for local monitoring
        cam_client_active = False
        if context.machine.type == TransportType.CLIENT:
            # ClientTransport: session create already spawns a local monitor
            cam_client_active = True
        elif context.machine.type in (TransportType.SSH, TransportType.AGENT):
            cam_client_active = await self._start_cam_client(
                agent, transport, adapter, context
            )

        # 11. Start monitoring
        if follow:
            await self._run_monitor_with_retries(
                agent, transport, adapter, follow=True,
                client_active=cam_client_active,
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
                # If the agent progressed past initializing, it likely
                # completed normally; otherwise it's a startup failure.
                has_worked = agent.state not in (AgentState.INITIALIZING, None)
                final_status = AgentStatus.COMPLETED if has_worked else AgentStatus.FAILED
                exit_reason = "Session ended cleanly" if has_worked else "TMUX session disappeared"
                self._agent_store.update_status(
                    str(agent.id),
                    final_status,
                    exit_reason=exit_reason,
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
        auto_confirm: bool | None = None,
    ) -> None:
        """Wait for interactive tool readiness, then send the task prompt."""
        max_wait = adapter.get_startup_wait()
        poll_interval = 1.0
        elapsed = 0.0
        ready = False

        confirmed_once = False

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            output = await transport.capture_output(session_name)
            if not output.strip():
                continue

            # After a dialog has been confirmed, check readiness first.
            # Old confirm patterns can linger in tmux scrollback; once
            # the tool shows its ready prompt, we're done.
            if confirmed_once and adapter.is_ready_for_input(output):
                logger.info("Tool ready for input in %s after %.1fs", session_name, elapsed)
                ready = True
                break

            # Handle trust/permission prompts that appear before readiness
            ac = auto_confirm if auto_confirm is not None else True
            confirm_action = adapter.should_auto_confirm(output) if ac else None
            if confirm_action is not None:
                logger.info("Pre-prompt auto-confirm in %s: sending '%s' (enter=%s)",
                            session_name, confirm_action.response, confirm_action.send_enter)
                await transport.send_input(
                    session_name,
                    confirm_action.response,
                    send_enter=confirm_action.send_enter,
                )
                confirmed_once = True
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

        if prompt.strip():
            await transport.send_input(session_name, prompt, send_enter=True)

    def _spawn_background_monitor(self, agent: Agent) -> None:
        """Spawn a detached subprocess to monitor the agent.

        The subprocess runs cam.core.monitor_runner which handles auto-confirm,
        state detection, timeouts, and completion tracking independently of the
        CLI process. Uses start_new_session=True to survive parent exit.

        Args:
            agent: The agent to monitor.
        """
        stderr_path = str(LOG_DIR / f"monitor-{agent.id}.stderr")
        try:
            stderr_fh = open(stderr_path, "w")
            proc = subprocess.Popen(
                [sys.executable, "-m", "cam.core.monitor_runner", str(agent.id)],
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
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

    async def _start_cam_client(
        self,
        agent: Agent,
        transport: Transport,
        adapter: ToolAdapter,
        context: Context,
    ) -> bool:
        """Deploy and start cam-client on the target machine.

        Returns True if cam-client was successfully deployed and launched.
        Falls back silently to SSH-polling if anything fails.
        """
        if not hasattr(transport, '_run_ssh') or not hasattr(transport, 'write_file'):
            return False

        try:
            # 1. Read local client.py and compute hash
            client_path = Path(__file__).parent.parent / "client.py"
            if not client_path.exists():
                logger.warning("cam-client.py not found at %s", client_path)
                return False
            client_bytes = client_path.read_bytes()
            import hashlib
            local_hash = hashlib.md5(client_bytes).hexdigest()[:12]

            # 2. Check remote hash — deploy if missing or outdated
            # Wrap in bash -c for csh-default remotes (2>/dev/null is bash syntax)
            ok, remote_hash = await transport._run_ssh(
                "bash -c 'md5sum ~/.cam/cam-client.py 2>/dev/null | cut -c1-12'",
                check=False,
            )
            remote_hash = remote_hash.strip() if ok else ""

            if remote_hash != local_hash:
                deployed = await transport.write_file(
                    "~/.cam/cam-client.py", client_bytes
                )
                if not deployed:
                    logger.warning("Failed to deploy cam-client to %s", transport._host)
                    return False
                action = "Updated" if remote_hash else "Deployed"
                logger.info("%s cam-client.py on %s (%s)", action, transport._host, local_hash)

            # 2b. Sync TOML adapter configs to ~/.cam/configs/
            toml_results = await self._sync_toml_configs(transport)
            for name, status in toml_results.items():
                if status == "failed":
                    logger.warning("Failed to sync %s to %s", name, transport._host)

            # 3. Determine adapter config mode (--tool or --adapter-config)
            tool_name = adapter.name if hasattr(adapter, 'name') else None
            toml_synced = (
                tool_name
                and f"{tool_name}.toml" in toml_results
                and toml_results[f"{tool_name}.toml"] != "failed"
            )

            # 4. Resolve server URL reachable from target
            server_host = self._config.server.host
            server_port = self._config.server.port
            if server_host in ("127.0.0.1", "0.0.0.0", "localhost"):
                # For local server binding, cam-client can't reach it from remote.
                # Skip cam-client for now — user needs to configure a reachable URL.
                logger.info(
                    "Skipping cam-client: server binds to %s (not reachable from remote)",
                    server_host,
                )
                return False
            server_url = f"http://{server_host}:{server_port}"

            # 5. Auth token
            auth_token = self._config.server.auth_token or ""
            if not auth_token:
                logger.info("Skipping cam-client: no auth token configured")
                return False

            # 6. Auto-confirm setting
            ac = agent.task.auto_confirm
            ac_flag = "True" if (ac is True or (ac is None and self._config.general.auto_confirm)) else "False"

            # 7. Launch cam-client as background process on remote
            # Use nohup + disown pattern via shell
            import shlex
            if toml_synced:
                config_flag = f" --tool {shlex.quote(tool_name)}"
            else:
                adapter_config = json.dumps(adapter.to_dict())
                config_flag = f" --adapter-config {shlex.quote(adapter_config)}"
            # Wrap in bash -c for csh-default remotes (2>&1 & is bash syntax)
            prompt_flag = ""
            if agent.task.prompt and agent.task.prompt.strip():
                prompt_flag = f" --prompt {shlex.quote(agent.task.prompt)}"
            inner_cmd = (
                f"nohup python3 ~/.cam/cam-client.py"
                f" --agent-id {shlex.quote(str(agent.id))}"
                f" --session {shlex.quote(agent.tmux_session)}"
                f" --server {shlex.quote(server_url)}"
                f" --token {shlex.quote(auth_token)}"
                f" --auto-confirm {ac_flag}"
                f"{prompt_flag}"
                f"{config_flag}"
                f" > /tmp/cam-client-{agent.id}.log 2>&1 &"
            )
            launch_cmd = f"bash -c {shlex.quote(inner_cmd)}"
            ok, output = await transport._run_ssh(launch_cmd, check=False)
            if not ok:
                logger.warning("Failed to launch cam-client on %s: %s", transport._host, output)
                return False

            logger.info("Started cam-client on %s for agent %s", transport._host, agent.id)
            return True

        except Exception as e:
            logger.warning("cam-client deployment failed: %s (falling back to SSH polling)", e)
            return False

    async def _sync_toml_configs(
        self,
        transport: Transport,
    ) -> dict[str, str]:
        """Sync TOML adapter configs to ~/.cam/configs/ on target.

        Hash-checks each file and only deploys changed ones.

        Returns:
            Dict of {filename: status} where status is
            "deployed", "updated", "unchanged", or "failed".
        """
        configs_dir = Path(__file__).parent.parent / "adapters" / "configs"
        results: dict[str, str] = {}

        toml_files = sorted(configs_dir.glob("*.toml"))
        if not toml_files:
            return results

        # Compute local hashes
        local_hashes: dict[str, str] = {}
        local_data: dict[str, bytes] = {}
        for f in toml_files:
            data = f.read_bytes()
            local_hashes[f.name] = hashlib.md5(data).hexdigest()[:12]
            local_data[f.name] = data

        # Batch-check remote hashes in one SSH call (if transport supports it)
        remote_hashes: dict[str, str] = {}
        if hasattr(transport, '_run_ssh'):
            remote_paths = " ".join(
                f"~/.cam/configs/{name}" for name in local_hashes
            )
            # Wrap in bash -c for csh-default remotes
            ok, output = await transport._run_ssh(
                f"bash -c 'md5sum {remote_paths} 2>/dev/null'", check=False
            )
            if ok and output.strip():
                for line in output.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        rhash = parts[0][:12]
                        rname = parts[-1].rsplit("/", 1)[-1]
                        remote_hashes[rname] = rhash

        # Deploy changed files
        for name, data in local_data.items():
            if local_hashes[name] == remote_hashes.get(name):
                results[name] = "unchanged"
                continue
            deployed = await transport.write_file(
                f"~/.cam/configs/{name}", data
            )
            if deployed:
                action = "updated" if name in remote_hashes else "deployed"
                results[name] = action
                logger.info(
                    "%s %s on %s",
                    action.capitalize(),
                    name,
                    getattr(transport, '_host', 'target'),
                )
            else:
                results[name] = "failed"

        return results

    async def sync_to_target(self, context: Context) -> dict[str, str]:
        """Sync cam-client.py and TOML adapter configs to a context's target.

        Public API for ``cam sync <context>``.

        Returns:
            Dict of {filename: status} with entries for cam-client.py
            and each TOML config file.
        """
        transport = self._create_transport(context)
        if not hasattr(transport, 'write_file'):
            raise AgentManagerError(
                f"Transport {context.machine.type.value} does not support file deployment"
            )

        results: dict[str, str] = {}

        # 1. Sync cam-client.py
        client_path = Path(__file__).parent.parent / "client.py"
        if client_path.exists():
            client_bytes = client_path.read_bytes()
            local_hash = hashlib.md5(client_bytes).hexdigest()[:12]

            remote_hash = ""
            if hasattr(transport, '_run_ssh'):
                ok, out = await transport._run_ssh(
                    "bash -c 'md5sum ~/.cam/cam-client.py 2>/dev/null | cut -c1-12'",
                    check=False,
                )
                remote_hash = out.strip() if ok else ""

            if remote_hash != local_hash:
                deployed = await transport.write_file(
                    "~/.cam/cam-client.py", client_bytes
                )
                results["cam-client.py"] = (
                    ("updated" if remote_hash else "deployed") if deployed else "failed"
                )
            else:
                results["cam-client.py"] = "unchanged"

        # 2. Sync camc (standalone CLI)
        camc_path = Path(__file__).parent.parent / "camc.py"
        if camc_path.exists():
            camc_bytes = camc_path.read_bytes()
            camc_local_hash = hashlib.md5(camc_bytes).hexdigest()[:12]

            camc_remote_hash = ""
            if hasattr(transport, '_run_ssh'):
                ok, out = await transport._run_ssh(
                    "bash -c 'md5sum ~/.cam/camc 2>/dev/null | cut -c1-12'",
                    check=False,
                )
                camc_remote_hash = out.strip() if ok else ""

            if camc_remote_hash != camc_local_hash:
                deployed = await transport.write_file(
                    "~/.cam/camc", camc_bytes
                )
                if deployed:
                    # Make executable + symlink into PATH
                    await transport._run_ssh(
                        "bash -c 'chmod +x ~/.cam/camc && mkdir -p ~/.local/bin && ln -sf ~/.cam/camc ~/.local/bin/camc'",
                        check=False,
                    )
                results["camc"] = (
                    ("updated" if camc_remote_hash else "deployed") if deployed else "failed"
                )
            else:
                results["camc"] = "unchanged"

        # 3. Sync TOML configs
        toml_results = await self._sync_toml_configs(transport)
        results.update(toml_results)

        return results

    async def import_remote_agents(
        self, context: Context
    ) -> tuple[int, int, int]:
        """Import agents from remote ~/.cam/agents.json into the server DB.

        Reads the remote agents.json via SSH (``cam-client.py status``),
        then creates or updates Agent records in the server store linked
        to *context*.

        Returns:
            Tuple of (imported, skipped, updated) counts.
        """
        transport = self._create_transport(context)

        # Get remote agents via cam-client.py status or direct SSH
        remote_agents: list[dict] = []

        if hasattr(transport, 'get_agent_status'):
            # ClientTransport — use the status subcommand
            changed, data = await transport.get_agent_status()
            remote_agents = data.get("agents", [])
        elif hasattr(transport, '_run_ssh'):
            # SSHTransport — call cam-client.py directly
            ok, output = await transport._run_ssh(
                "python3 ~/.cam/cam-client.py status",
                check=False,
            )
            if ok and output.strip():
                try:
                    data = json.loads(output)
                    remote_agents = data.get("agents", [])
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Failed to parse remote agents.json: %s", output[:200])
        else:
            raise AgentManagerError(
                f"Transport {context.machine.type.value} does not support agent import"
            )

        imported = skipped = updated = 0

        # Cache of auto-created contexts: remote_ctx_name -> Context
        _ctx_cache: dict[str, Context] = {}

        # Status/state mappings
        _status_map = {
            "running": AgentStatus.RUNNING,
            "completed": AgentStatus.COMPLETED,
            "failed": AgentStatus.FAILED,
            "stopped": AgentStatus.KILLED,
            "killed": AgentStatus.KILLED,
        }
        _state_map = {
            "initializing": AgentState.INITIALIZING,
            "planning": AgentState.PLANNING,
            "editing": AgentState.EDITING,
            "testing": AgentState.TESTING,
            "committing": AgentState.COMMITTING,
            "idle": AgentState.IDLE,
        }

        for ra in remote_agents:
            remote_id = ra.get("id", "")
            if not remote_id:
                continue

            # Check if already exists in server DB
            existing = self._agent_store.get(remote_id)

            agent_status = _status_map.get(ra.get("status", ""), AgentStatus.RUNNING)
            agent_state = _state_map.get(ra.get("state", ""), AgentState.INITIALIZING)

            # Parse timestamps
            started_at = None
            if ra.get("started_at"):
                try:
                    started_at = datetime.fromisoformat(
                        ra["started_at"].replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    started_at = datetime.utcnow()

            completed_at = None
            if ra.get("completed_at"):
                try:
                    completed_at = datetime.fromisoformat(
                        ra["completed_at"].replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

            if existing:
                # Update if state changed
                old_status = existing.status
                old_state = existing.state
                if old_status == agent_status and old_state == agent_state:
                    skipped += 1
                    continue
                self._agent_store.update_status(
                    str(existing.id), agent_status,
                    state=agent_state if agent_state != old_state else None,
                    exit_reason=ra.get("exit_reason"),
                )
                updated += 1
            else:
                # Create new Agent record
                tool = ra.get("tool", "claude")
                prompt = ra.get("prompt", "")
                workdir = ra.get("path", context.path)
                session = ra.get("session", "cam-%s" % remote_id)

                # Resolve context: use remote agent's context info or fall back
                remote_ctx = ra.get("context") or {}
                if isinstance(remote_ctx, str):
                    remote_ctx = {"name": remote_ctx}
                ctx_name = remote_ctx.get("name")
                ctx_host = remote_ctx.get("host")
                ctx_port = remote_ctx.get("port")

                # Determine which server Context to link to
                target_ctx = context  # default: the importing context
                if ctx_name:
                    if ctx_name in _ctx_cache:
                        target_ctx = _ctx_cache[ctx_name]
                    else:
                        # Look up existing server context by name
                        existing_ctx = self._context_store.get(ctx_name)
                        if existing_ctx:
                            target_ctx = existing_ctx
                        else:
                            # Auto-create from remote info
                            host = ctx_host or context.machine.host
                            user = context.machine.user
                            port = ctx_port or context.machine.port
                            new_ctx = Context(
                                name=ctx_name,
                                path=workdir,
                                machine=MachineConfig(
                                    type=context.machine.type,
                                    host=host,
                                    user=user,
                                    port=port,
                                    key_file=context.machine.key_file,
                                    env_setup=context.machine.env_setup,
                                ),
                            )
                            self._context_store.add(new_ctx)
                            target_ctx = new_ctx
                            logger.info(
                                "Auto-created context '%s' (%s@%s:%s)",
                                ctx_name, user, host, port,
                            )
                        _ctx_cache[ctx_name] = target_ctx

                agent = Agent(
                    id=remote_id,
                    task=TaskDefinition(
                        name="%s-%s" % (tool, remote_id[:6]),
                        tool=tool,
                        prompt=prompt,
                        context=target_ctx.name,
                    ),
                    context_id=target_ctx.id,
                    context_name=target_ctx.name,
                    context_path=workdir,
                    transport_type=target_ctx.machine.type,
                    status=agent_status,
                    state=agent_state,
                    tmux_session=session,
                    started_at=started_at,
                    completed_at=completed_at,
                    exit_reason=ra.get("exit_reason"),
                )
                self._agent_store.save(agent)
                imported += 1
                logger.info(
                    "Imported remote agent %s (%s, %s) from %s",
                    remote_id, tool, agent_status.value, context.name,
                )

        return imported, skipped, updated

    async def _run_monitor_with_retries(
        self,
        agent: Agent,
        transport: Transport,
        adapter: ToolAdapter,
        follow: bool,
        client_active: bool = False,
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
            return await self._run_monitor_loop(agent, transport, adapter, client_active=client_active)
        else:
            # Launch as background task
            task = asyncio.create_task(
                self._run_monitor_loop(agent, transport, adapter, client_active=client_active),
                name=f"monitor-{agent.id}",
            )
            self._monitor_tasks[str(agent.id)] = task
            return agent.status  # Still RUNNING at this point

    async def _run_monitor_loop(
        self,
        agent: Agent,
        transport: Transport,
        adapter: ToolAdapter,
        client_active: bool = False,
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
                    client_active=client_active,
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
                            transport, adapter, session_name, agent.task.prompt,
                            agent.task.auto_confirm,
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
