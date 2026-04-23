"""Central orchestrator for CAM agent lifecycle.

The AgentManager delegates agent creation, monitoring, and teardown to camc
(the standalone agent manager) on each machine. cam serve acts as the
aggregation layer: it calls camc via SSH (remote) or subprocess (local),
stores results in SQLite, and publishes events to the WebSocket bus.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cam.constants import LOG_DIR
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
from cam.storage.agent_store import AgentStore
from cam.storage.context_store import ContextStore
from cam.transport.base import Transport
from cam.transport.factory import TransportFactory

logger = logging.getLogger(__name__)
_CONFIGS_DIR = Path(__file__).parent.parent / "adapters" / "configs"


class AgentManagerError(Exception):
    """Error raised by AgentManager operations."""


class AgentManager:
    """Central orchestrator for coding agent lifecycle.

    All agent run/stop operations are delegated to camc instances (local or
    remote via SSH). cam's AgentManager handles SQLite persistence, event
    broadcasting, file sync, and agent import/reconciliation.

    Args:
        config: CAM configuration.
        context_store: Persistent storage for contexts.
        agent_store: Persistent storage for agents and events.
        event_bus: Event bus for lifecycle event broadcasting.
        adapter_registry: Registry of available tool adapters (used by CLI
            for tool validation and ``cam version``; not used for agent
            management which is delegated to camc).
        transport_factory_class: Factory class for creating transports
            (defaults to TransportFactory; injectable for testing).
    """

    def __init__(
        self,
        config: CamConfig,
        context_store: ContextStore,
        agent_store: AgentStore,
        event_bus: EventBus,
        adapter_registry=None,
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
        """Create, start, and optionally monitor an agent via camc.

        camc handles tmux session creation, startup auto-confirm, prompt
        delivery, and background monitoring on the target machine. cam only
        records the agent in SQLite and tracks state via CamcPoller.

        Args:
            task: Task definition including prompt, tool name, and timeouts.
            context: Context describing the workspace and machine.
            follow: If True, block until the agent finishes (foreground).
                    If False, return immediately with RUNNING status.

        Returns:
            The Agent model. If ``follow=True`` the agent will be in a
            terminal state. If ``follow=False`` it will be RUNNING.

        Raises:
            AgentManagerError: If camc fails to start the agent.
        """
        from cam.core.camc_delegate import CamcDelegate

        machine = context.machine
        host = getattr(machine, "host", None)
        user = getattr(machine, "user", None)
        port = getattr(machine, "port", None)

        delegate = CamcDelegate(host=host, user=user, port=port)

        # Call camc run (blocking subprocess, run in thread to avoid blocking event loop)
        result = await asyncio.to_thread(
            delegate.run_agent,
            tool=task.tool,
            prompt=task.prompt,
            path=context.path,
            name=task.name,
            auto_exit=task.auto_exit or False,
        )

        if result is None:
            raise AgentManagerError(
                "camc run failed on %s for tool '%s'" % (host or "local", task.tool)
            )

        # Build Agent model from camc response
        from cam.core.camc_poller import _camc_agent_to_model

        agent = _camc_agent_to_model(result, context.name, context_id=context.id,
                                     machine_host=host, machine_user=user,
                                     machine_port=port)
        agent.context_path = context.path
        agent.transport_type = context.machine.type

        # Persist to cam's SQLite
        self._agent_store.save(agent)

        # Update context last-used timestamp
        try:
            self._context_store.update_last_used(str(context.id))
        except Exception:
            pass

        # Publish started event
        started_event = AgentEvent(
            agent_id=agent.id,
            event_type="agent_started",
            detail={"task": task.name, "tool": task.tool, "context": context.name,
                    "via": "camc"},
        )
        try:
            self._agent_store.add_event(started_event)
        except Exception:
            pass
        self._event_bus.publish(started_event)

        logger.info(
            "Agent %s launched via camc on %s (tool=%s)",
            agent.id, context.name, task.tool,
        )

        # In follow mode, poll camc until agent finishes
        if follow:
            await self._follow_camc_agent(delegate, agent)

        return agent

    async def stop_agent(self, agent_id: str, graceful: bool = True) -> None:
        """Stop a running agent via camc.

        Args:
            agent_id: ID of the agent to stop.
            graceful: If True, use camc stop; if False, use camc kill.

        Raises:
            AgentManagerError: If agent is not found or not in a running state.
        """
        agent = self._agent_store.get(agent_id)
        if agent is None:
            raise AgentManagerError(f"Agent '{agent_id}' not found")

        if agent.is_terminal():
            logger.info("Agent %s is already in terminal state %s", agent_id, agent.status.value)
            return

        agent, camc_id, delegate = self._resolve_agent_delegate(agent_id)

        if graceful:
            ok = await asyncio.to_thread(delegate.stop_agent, camc_id)
        else:
            ok = await asyncio.to_thread(delegate.kill_agent, camc_id)

        if not ok:
            logger.warning("camc stop/kill failed for agent %s, updating local state anyway", agent.id)

        # Update local SQLite
        self._agent_store.update_status(
            agent_id,
            AgentStatus.KILLED,
            exit_reason="Stopped by user" if graceful else "Force killed",
        )

        # Publish event
        kill_event = AgentEvent(
            agent_id=agent.id,
            event_type="agent_killed",
            detail={"graceful": graceful, "via": "camc"},
        )
        try:
            self._agent_store.add_event(kill_event)
        except Exception:
            pass
        self._event_bus.publish(kill_event)

        logger.info("Stopped agent %s via camc", agent_id)

    async def update_agent(self, agent_id: str, name: str | None = None,
                           auto_confirm: bool | None = None,
                           tags: list[str] | None = None,
                           untags: list[str] | None = None) -> None:
        """Update agent properties via camc (source of truth).

        Delegates to camc update on the remote machine. The poller will
        sync the change back to local SQLite.

        Raises:
            AgentManagerError: If agent is not found or camc update fails.
        """
        agent, camc_id, delegate = self._resolve_agent_delegate(agent_id)

        ok = await asyncio.to_thread(
            delegate.update_agent, camc_id, name=name, auto_confirm=auto_confirm,
            tags=tags, untags=untags,
        )
        if not ok:
            raise AgentManagerError(f"camc update failed for agent {agent.id}")

        # Also update local SQLite so the change is visible immediately
        # (poller will overwrite with camc data on next cycle anyway).
        changed = False
        if name is not None:
            agent.task.name = name
            changed = True
        if auto_confirm is not None:
            agent.task.auto_confirm = auto_confirm
            changed = True
        if tags or untags:
            current = list(agent.task.tags or [])
            for t in (tags or []):
                if t not in current:
                    current.append(t)
            for t in (untags or []):
                if t in current:
                    current.remove(t)
            agent.task.tags = current
            changed = True
        if changed:
            self._agent_store.save(agent)

        logger.info("Updated agent %s via camc", agent_id)

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Get an agent by ID."""
        return self._agent_store.get(agent_id)

    async def list_agents(self, **filters) -> list[Agent]:
        """List agents with optional filters.

        Supported filter keys match AgentStore.list() parameters:
        - status: AgentStatus
        - context_id: str
        - tool: str
        - limit: int
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
                context = Context(
                    id=str(agent.context_id),
                    name=agent.context_name or "cwd",
                    path=agent.context_path or "",
                    machine=MachineConfig(type=agent.transport_type or TransportType.LOCAL),
                    created_at=datetime.now(timezone.utc),
                )
                if not context.path:
                    orphaned.append(agent)
                    continue

            try:
                transport = self._create_transport(context)
                alive = await transport.session_exists(agent.tmux_session)
            except Exception as exc:
                logger.warning(
                    "Failed to check session for agent %s: %s", agent.id, exc
                )
                continue

            if not alive:
                has_worked = agent.state not in (AgentState.INITIALIZING, None)
                final_status = AgentStatus.COMPLETED if has_worked else AgentStatus.FAILED
                exit_reason = "Session ended cleanly" if has_worked else "TMUX session disappeared"
                self._agent_store.update_status(
                    str(agent.id),
                    final_status,
                    exit_reason=exit_reason,
                )
                orphaned.append(agent)

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
    # Sync and import
    # ------------------------------------------------------------------

    async def sync_to_target(self, context: Context) -> dict[str, str]:
        """Sync cam-client.py, camc, context.json, and TOML configs to a remote context.

        Public API for ``cam sync <context>``.

        Returns:
            Dict of {filename: status} with entries for each synced file.
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
        camc_path = Path(__file__).parent.parent.parent / "camc"
        if not camc_path.exists():
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
                    await transport._run_ssh(
                        "bash -c 'chmod +x ~/.cam/camc && mkdir -p ~/.local/bin && ln -sf ~/.cam/camc ~/.local/bin/camc'",
                        check=False,
                    )
                results["camc"] = (
                    ("updated" if camc_remote_hash else "deployed") if deployed else "failed"
                )
            else:
                results["camc"] = "unchanged"

            # Try to install heal cron (best-effort)
            if hasattr(transport, '_run_ssh'):
                await transport._run_ssh(
                    "bash -c '(crontab -l 2>/dev/null | grep -v camc.heal; echo \"0,30 * * * * python3 ~/.cam/camc heal >> /tmp/camc-heal.log 2>&1\") | crontab - 2>/dev/null'",
                    check=False,
                )

        # 3. Sync context.json
        ctx_data = {
            "name": context.name,
            "host": context.machine.host,
            "port": context.machine.port,
            "env_setup": context.machine.env_setup,
        }
        import json as _json
        ctx_bytes = (_json.dumps(ctx_data, indent=2) + "\n").encode()
        ctx_local_hash = hashlib.md5(ctx_bytes).hexdigest()[:12]
        ctx_remote_hash = ""
        if hasattr(transport, '_run_ssh'):
            ok, out = await transport._run_ssh(
                "bash -c 'md5sum ~/.cam/context.json 2>/dev/null | cut -c1-12'",
                check=False,
            )
            ctx_remote_hash = out.strip() if ok else ""
        if ctx_remote_hash != ctx_local_hash:
            deployed = await transport.write_file("~/.cam/context.json", ctx_bytes)
            results["context.json"] = (
                ("updated" if ctx_remote_hash else "deployed") if deployed else "failed"
            )
        else:
            results["context.json"] = "unchanged"

        # 4. TOML configs — no longer synced.
        # camc embeds adapter configs; external files in ~/.cam/configs/
        # are optional user overrides only.

        return results

    async def import_remote_agents(
        self, context: Context
    ) -> tuple[int, int, int]:
        """Import agents from remote camc into the server DB.

        Reads the remote agents via camc (preferred) or cam-client.py fallback,
        then creates or updates Agent records in the server store linked
        to *context*.

        Returns:
            Tuple of (imported, skipped, updated) counts.
        """
        transport = self._create_transport(context)

        # Get remote agents via camc or cam-client.py
        remote_agents: list[dict] = []

        if hasattr(transport, 'get_agent_status'):
            # ClientTransport — use the status subcommand
            changed, data = await transport.get_agent_status()
            remote_agents = data.get("agents", [])
        elif hasattr(transport, '_run_ssh'):
            # Try camc first, fall back to cam-client.py
            ok, output = await transport._run_ssh(
                "python3 ~/.cam/camc --json list",
                check=False,
            )
            if ok and output.strip():
                try:
                    remote_agents = json.loads(output)
                    if not isinstance(remote_agents, list):
                        remote_agents = []
                except (json.JSONDecodeError, ValueError):
                    pass

            if not remote_agents:
                # Fallback to cam-client.py
                ok, output = await transport._run_ssh(
                    "python3 ~/.cam/cam-client.py status",
                    check=False,
                )
                if ok and output.strip():
                    try:
                        data = json.loads(output)
                        remote_agents = data.get("agents", [])
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("Failed to parse remote agents: %s", output[:200])
        else:
            raise AgentManagerError(
                f"Transport {context.machine.type.value} does not support agent import"
            )

        imported = skipped = updated = 0

        _ctx_cache: dict[str, Context] = {}

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
                    started_at = datetime.now(timezone.utc)

            completed_at = None
            if ra.get("completed_at"):
                try:
                    completed_at = datetime.fromisoformat(
                        ra["completed_at"].replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

            if existing:
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
                tool = ra.get("tool", "claude")
                prompt = ra.get("prompt", "")
                workdir = ra.get("path", context.path)
                session = ra.get("session", "cam-%s" % remote_id)

                # Resolve context
                remote_ctx = ra.get("context") or {}
                if isinstance(remote_ctx, str):
                    remote_ctx = {"name": remote_ctx}
                ctx_name = remote_ctx.get("name")
                ctx_host = remote_ctx.get("host")
                ctx_port = remote_ctx.get("port")

                target_ctx = context
                if ctx_name:
                    if ctx_name in _ctx_cache:
                        target_ctx = _ctx_cache[ctx_name]
                    else:
                        existing_ctx = self._context_store.get(ctx_name)
                        if existing_ctx:
                            target_ctx = existing_ctx
                        else:
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

                task_name = ra.get("name") or "%s-%s" % (tool, remote_id[:6])
                agent = Agent(
                    id=remote_id,
                    task=TaskDefinition(
                        name=task_name,
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _follow_camc_agent(self, delegate, agent: Agent) -> None:
        """Poll camc until agent reaches a terminal state (follow mode)."""
        poll_interval = 3.0
        while True:
            await asyncio.sleep(poll_interval)
            data = await asyncio.to_thread(delegate.get_agent, agent.id)
            if data is None:
                self._agent_store.update_status(
                    agent.id, AgentStatus.COMPLETED,
                    exit_reason="Agent no longer tracked by camc",
                )
                break

            status = data.get("status", "running")
            if status in ("completed", "failed", "stopped"):
                from cam.core.camc_poller import _STATUS_MAP
                final_status = _STATUS_MAP.get(status, AgentStatus.COMPLETED)
                exit_reason = data.get("exit_reason", "")
                self._agent_store.update_status(
                    agent.id, final_status, exit_reason=exit_reason,
                )
                done_event = AgentEvent(
                    agent_id=agent.id,
                    event_type="agent_completed",
                    detail={"status": status, "exit_reason": exit_reason},
                )
                try:
                    self._agent_store.add_event(done_event)
                except Exception:
                    pass
                self._event_bus.publish(done_event)
                break

            # Update state if changed
            state_str = data.get("state", "")
            if state_str:
                from cam.core.camc_poller import _STATE_MAP
                new_state = _STATE_MAP.get(state_str)
                if new_state and new_state != agent.state:
                    agent.state = new_state

    # ------------------------------------------------------------------
    # Transport operations (shared by CLI and API)
    # ------------------------------------------------------------------

    def _create_transport(self, context: Context) -> Transport:
        """Create a transport for the given context."""
        return self._transport_factory_class.create(context.machine)

    def _resolve_agent_delegate(self, agent_id: str):
        """Look up agent, its context, and create a CamcDelegate.

        Returns (agent, camc_agent_id, delegate).
        Raises AgentManagerError on any lookup failure.
        """
        from cam.core.camc_delegate import CamcDelegate

        agent = self._agent_store.get(agent_id)
        if agent is None:
            raise AgentManagerError(f"Agent not found: {agent_id}")

        # Use agent's machine fields first (set by poller on import),
        # fall back to context lookup for legacy agents.
        host, user, port = agent.machine_host, agent.machine_user, agent.machine_port
        if not host:
            context = self._context_store.get(str(agent.context_id))
            if context is None:
                raise AgentManagerError("Agent's context not found")
            machine = context.machine
            host = getattr(machine, "host", None)
            user = getattr(machine, "user", None)
            port = getattr(machine, "port", None)
        delegate = CamcDelegate(host=host, user=user, port=port)
        # camc uses its own short IDs; pass the tmux session name which
        # camc can match, or fall back to the full agent ID.
        camc_id = agent.tmux_session or str(agent.id)
        return agent, camc_id, delegate

    async def capture_output(
        self, agent_id: str, *, lines: int = 100
    ) -> tuple[str, str]:
        """Capture agent screen output via camc.

        Returns (output_text, md5_hash_prefix).
        """
        _agent, camc_id, delegate = self._resolve_agent_delegate(agent_id)
        output = await asyncio.to_thread(delegate.capture, camc_id, lines)
        output_hash = hashlib.md5(output.encode()).hexdigest()[:8]
        return output, output_hash

    async def send_input(
        self, agent_id: str, text: str, *, send_enter: bool = True
    ) -> bool:
        """Send text input to an agent via camc."""
        agent, camc_id, delegate = self._resolve_agent_delegate(agent_id)
        submit_delay = self._tool_prompt_submit_delay(agent.task.tool)
        if submit_delay > 0 and send_enter and text:
            ok = await asyncio.to_thread(delegate.send_input, camc_id, text, False)
            if not ok:
                return False
            await asyncio.sleep(submit_delay)
            return await asyncio.to_thread(delegate.send_key, camc_id, "Enter")
        return await asyncio.to_thread(delegate.send_input, camc_id, text, send_enter)

    async def send_key(self, agent_id: str, key: str) -> bool:
        """Send a special key to an agent via camc."""
        _agent, camc_id, delegate = self._resolve_agent_delegate(agent_id)
        return await asyncio.to_thread(delegate.send_key, camc_id, key)

    def _tool_prompt_submit_delay(self, tool: str) -> float:
        if not tool:
            return 0.0
        try:
            with open(_CONFIGS_DIR / f"{tool}.toml", "rb") as f:
                data = tomllib.load(f)
            return float(data.get("launch", {}).get("prompt_submit_delay", 0.0))
        except Exception:
            return 0.0

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

        local_hashes: dict[str, str] = {}
        local_data: dict[str, bytes] = {}
        for f in toml_files:
            data = f.read_bytes()
            local_hashes[f.name] = hashlib.md5(data).hexdigest()[:12]
            local_data[f.name] = data

        remote_hashes: dict[str, str] = {}
        if hasattr(transport, '_run_ssh'):
            remote_paths = " ".join(
                f"~/.cam/configs/{name}" for name in local_hashes
            )
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
