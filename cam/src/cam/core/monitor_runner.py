"""Standalone monitor runner for detached agents.

This module provides a subprocess entry point that runs the monitoring loop
for a single agent independently of the CLI process. It is spawned by
AgentManager when an agent is launched in detach mode (--detach).

Usage:
    python -m cam.core.monitor_runner <agent_id>

The subprocess:
1. Reads the agent from the database by ID
2. Loads config, creates transport/adapter/logger
3. Runs AgentMonitor.run() with retry support
4. Writes a PID file for management (stop/kill)
5. Cleans up PID file on exit
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from cam.adapters.registry import AdapterRegistry
from cam.constants import PID_DIR
from cam.core.config import load_config
from cam.core.events import EventBus
from cam.core.models import AgentEvent, AgentState, AgentStatus
from cam.core.monitor import AgentMonitor
from cam.storage.agent_store import AgentStore
from cam.storage.context_store import ContextStore
from cam.storage.database import Database
from cam.transport.factory import TransportFactory
from cam.utils.logging import AgentLogger

logger = logging.getLogger(__name__)


def _write_pid(agent_id: str) -> Path:
    """Write current process PID to a file for agent management.

    Args:
        agent_id: Agent identifier used as PID filename.

    Returns:
        Path to the created PID file.
    """
    PID_DIR.mkdir(parents=True, exist_ok=True)
    pid_path = PID_DIR / f"{agent_id}.pid"
    pid_path.write_text(str(os.getpid()))
    return pid_path


def _remove_pid(agent_id: str) -> None:
    """Remove PID file for an agent.

    Args:
        agent_id: Agent identifier used as PID filename.
    """
    pid_path = PID_DIR / f"{agent_id}.pid"
    pid_path.unlink(missing_ok=True)


async def run_monitor(agent_id: str) -> None:
    """Monitor a single detached agent until completion.

    Loads all required dependencies from the database and config,
    then runs the monitor loop with retry support. Same logic as
    AgentManager._run_monitor_loop() but self-contained.

    Args:
        agent_id: ID of the agent to monitor.
    """
    config = load_config()

    # Configure logging level from config
    log_level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    db = Database()
    agent_store = AgentStore(db)
    context_store = ContextStore(db)
    event_bus = EventBus()
    registry = AdapterRegistry()

    # Load agent from DB
    agent = agent_store.get(agent_id)
    if agent is None:
        logger.error("Agent %s not found in database", agent_id)
        return

    # Get adapter for this tool
    adapter = registry.get(agent.task.tool)
    if adapter is None:
        logger.error("Unknown tool adapter: %s", agent.task.tool)
        agent.status = AgentStatus.FAILED
        agent.completed_at = datetime.now(timezone.utc)
        agent.exit_reason = f"Unknown tool adapter: {agent.task.tool}"
        agent_store.save(agent)
        return

    # Load context to get machine config for transport
    context = context_store.get(str(agent.context_id))
    if context is None:
        logger.error("Context %s not found for agent %s", agent.context_id, agent_id)
        agent.status = AgentStatus.FAILED
        agent.completed_at = datetime.now(timezone.utc)
        agent.exit_reason = f"Context not found: {agent.context_id}"
        agent_store.save(agent)
        return

    # Create transport from context machine config
    transport = TransportFactory.create(context.machine)

    # Write PID file
    _write_pid(agent_id)

    try:
        # Run monitor with retry logic (mirrors AgentManager._run_monitor_loop)
        max_retries = agent.task.retry.max_retries

        while True:
            agent_logger = AgentLogger(agent_id)
            agent_logger.open()

            try:
                monitor = AgentMonitor(
                    agent=agent,
                    transport=transport,
                    adapter=adapter,
                    agent_store=agent_store,
                    event_bus=event_bus,
                    agent_logger=agent_logger,
                    config=config,
                )
                final_status = await monitor.run()
            finally:
                agent_logger.close()

            # Check if we should retry
            if final_status == AgentStatus.FAILED and agent.retry_count < max_retries:
                agent.retry_count += 1
                agent.status = AgentStatus.RETRYING
                agent_store.save(agent)

                # Calculate backoff delay
                backoff = min(
                    agent.task.retry.backoff_base ** agent.retry_count,
                    agent.task.retry.backoff_max,
                )
                logger.info(
                    "Agent %s failed, retrying (%d/%d) after %.1fs backoff",
                    agent_id, agent.retry_count, max_retries, backoff,
                )

                retry_event = AgentEvent(
                    agent_id=agent_id,
                    event_type="agent_retry",
                    detail={
                        "attempt": agent.retry_count,
                        "max_retries": max_retries,
                        "backoff_seconds": backoff,
                    },
                )
                try:
                    agent_store.add_event(retry_event)
                except Exception:
                    pass

                await asyncio.sleep(backoff)

                # Recreate TMUX session
                session_name = agent.tmux_session
                if session_name:
                    # Kill old session
                    await transport.kill_session(session_name)

                    # Recreate
                    cmd = adapter.get_launch_command(agent.task, context)
                    session_created = await transport.create_session(
                        session_name, cmd, context.path
                    )
                    if not session_created:
                        agent.status = AgentStatus.FAILED
                        agent.completed_at = datetime.now(timezone.utc)
                        agent.exit_reason = (
                            f"Failed to recreate TMUX session on retry {agent.retry_count}"
                        )
                        agent_store.save(agent)
                        return

                    # Wait for readiness and send prompt
                    if adapter.needs_prompt_after_launch():
                        max_wait = adapter.get_startup_wait()
                        elapsed = 0.0
                        ready = False
                        while elapsed < max_wait:
                            await asyncio.sleep(1.0)
                            elapsed += 1.0
                            output = await transport.capture_output(session_name)
                            if not output.strip():
                                continue
                            confirm_action = adapter.should_auto_confirm(output)
                            if confirm_action is not None:
                                await transport.send_input(
                                    session_name,
                                    confirm_action.response,
                                    send_enter=confirm_action.send_enter,
                                )
                                await asyncio.sleep(3.0)
                                elapsed += 3.0
                                continue
                            if adapter.is_ready_for_input(output):
                                ready = True
                                break
                        if not ready:
                            logger.warning(
                                "Readiness not detected for %s after %.1fs",
                                session_name, elapsed,
                            )
                        await transport.send_input(
                            session_name, agent.task.prompt, send_enter=True
                        )

                    agent.status = AgentStatus.RUNNING
                    agent.state = AgentState.INITIALIZING
                    agent.completed_at = None
                    agent.exit_reason = None
                    agent_store.save(agent)
                else:
                    # Cannot retry without a session name
                    return

                continue

            # No retry needed or no retries left
            break

    except Exception:
        logger.exception("Monitor runner crashed for agent %s", agent_id)
        agent.status = AgentStatus.FAILED
        agent.completed_at = datetime.now(timezone.utc)
        agent.exit_reason = "Monitor subprocess crashed"
        agent_store.save(agent)
    finally:
        _remove_pid(agent_id)


def main() -> None:
    """Entry point for the monitor runner subprocess."""
    if len(sys.argv) != 2:
        print(f"Usage: python -m cam.core.monitor_runner <agent_id>", file=sys.stderr)
        sys.exit(1)

    agent_id = sys.argv[1]

    # Handle SIGTERM gracefully
    def sigterm_handler(signum, frame):
        _remove_pid(agent_id)
        sys.exit(0)

    signal.signal(signal.SIGTERM, sigterm_handler)

    asyncio.run(run_monitor(agent_id))


if __name__ == "__main__":
    main()
