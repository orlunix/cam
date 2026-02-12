"""Agent operation CLI commands.

Defines the top-level agent commands (run, list, status, logs, attach, stop,
kill) as plain functions with Typer annotations. These are imported by
``cam.cli.app`` and registered on the root Typer app via
``app.command(name=...)(agent_cmd.<func>)``.
"""

from __future__ import annotations

from typing import Optional

import typer


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run(
    tool: str = typer.Argument(..., help="Tool name (claude, codex, aider, ...)"),
    prompt: str = typer.Argument(..., help="Task prompt"),
    ctx: Optional[str] = typer.Option(None, "--ctx", help="Context name (default: current directory)"),
    timeout: Optional[str] = typer.Option(None, "--timeout", help="Timeout (e.g. '30m', '2h')"),
    retry: int = typer.Option(0, "--retry", help="Retry count on failure"),
    name: Optional[str] = typer.Option(None, "--name", help="Human-readable name"),
    detach: bool = typer.Option(False, "--detach", help="Don't follow output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show plan without executing"),
) -> None:
    """Start a coding agent on a task."""
    import asyncio
    import os
    from datetime import datetime, timezone
    from uuid import uuid4

    from cam.cli.app import state
    from cam.cli.formatters import print_agent_detail, print_error, print_info, print_success
    from cam.core.config import parse_duration
    from cam.core.models import (
        Context,
        MachineConfig,
        RetryPolicy,
        TaskDefinition,
        TransportType,
    )

    # Resolve context
    if ctx:
        context = state.context_store.get(ctx)
        if not context:
            print_error(f"Context not found: {ctx}")
            raise typer.Exit(1)
    else:
        # Use current directory as a temporary local context
        context = Context(
            id=str(uuid4()),
            name="cwd",
            path=os.getcwd(),
            machine=MachineConfig(type=TransportType.LOCAL),
            created_at=datetime.now(timezone.utc),
        )

    # Parse timeout
    timeout_seconds = parse_duration(timeout) if timeout else None

    # Build a human-readable name if not provided
    task_name = name or f"{tool}-{uuid4().hex[:6]}"

    # Build task definition
    task = TaskDefinition(
        name=task_name,
        tool=tool,
        prompt=prompt,
        context=ctx or context.name,
        timeout=timeout_seconds,
        retry=RetryPolicy(max_retries=retry),
    )

    # Validate tool exists
    adapter = state.adapter_registry.get(tool)
    if not adapter:
        available = ", ".join(state.adapter_registry.names())
        print_error(f"Unknown tool: {tool}. Available: {available}")
        raise typer.Exit(1)

    if dry_run:
        print_info("Dry run — would execute:")
        cmd = adapter.get_launch_command(task, context)
        print_info(f"  Tool: {adapter.display_name}")
        print_info(f"  Context: {context.name} ({context.path})")
        print_info(f"  Command: {' '.join(cmd)}")
        if timeout:
            print_info(f"  Timeout: {timeout}")
        if retry > 0:
            print_info(f"  Retries: {retry}")
        return

    # Run the agent
    try:
        agent = asyncio.run(
            state.agent_manager.run_agent(task, context, follow=not detach)
        )
        if detach:
            print_success(f"Agent started: {str(agent.id)[:8]}")
            print_agent_detail(agent)
        else:
            # After follow completes, re-read from store for latest state
            agent = state.agent_store.get(str(agent.id))
            if agent:
                print_agent_detail(agent)
    except Exception as e:
        print_error(f"Failed to start agent: {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def list(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
    tool: Optional[str] = typer.Option(None, "--tool", help="Filter by tool"),
    ctx_name: Optional[str] = typer.Option(None, "--ctx", help="Filter by context"),
    last: int = typer.Option(20, "--last", "-n", help="Show last N agents"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Auto-refresh every 2s"),
) -> None:
    """List agents."""
    import time

    from cam.cli.app import state
    from cam.cli.formatters import console, print_agent_list, print_info
    from cam.core.models import AgentStatus

    agent_status = AgentStatus(status) if status else None

    # Resolve context ID if name given
    context_id: str | None = None
    if ctx_name:
        ctx_obj = state.context_store.get(ctx_name)
        if ctx_obj:
            context_id = str(ctx_obj.id)

    def fetch_and_print() -> None:
        agents = state.agent_store.list(
            status=agent_status,
            context_id=context_id,
            tool=tool,
            limit=last,
        )
        if not agents:
            print_info("No agents found.")
            return
        print_agent_list(agents)

    if watch:
        try:
            while True:
                console.clear()
                fetch_and_print()
                time.sleep(2)
        except KeyboardInterrupt:
            pass
    else:
        fetch_and_print()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(
    agent_id: str = typer.Argument(..., help="Agent ID (full or short)"),
) -> None:
    """Show detailed agent status."""
    from cam.cli.app import state
    from cam.cli.formatters import print_agent_detail, print_error

    agent = state.agent_store.get(agent_id)
    if not agent:
        print_error(f"Agent not found: {agent_id}")
        raise typer.Exit(1)
    print_agent_detail(agent)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def logs(
    agent_id: str = typer.Argument(..., help="Agent ID"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow output"),
    tail: int = typer.Option(50, "--tail", "-n", help="Last N lines"),
) -> None:
    """View agent output logs."""
    from cam.cli.app import state
    from cam.cli.formatters import print_error, print_info
    from cam.utils.logging import AgentLogger

    agent = state.agent_store.get(agent_id)
    if not agent:
        print_error(f"Agent not found: {agent_id}")
        raise typer.Exit(1)

    logger = AgentLogger(str(agent.id))
    if not logger.log_path.exists():
        print_info(f"No log file yet: {logger.log_path}")
        return

    # Print existing lines
    entries = logger.read_lines(tail=tail)
    for entry in entries:
        _print_log_entry(entry)

    # Follow mode
    if follow:
        try:
            for entry in logger.follow():
                _print_log_entry(entry)
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


def attach(
    agent_id: str = typer.Argument(..., help="Agent ID"),
) -> None:
    """Attach to an agent's TMUX session (interactive)."""
    import os

    from cam.cli.app import state
    from cam.cli.formatters import print_error, print_info
    from cam.transport.factory import TransportFactory

    agent = state.agent_store.get(agent_id)
    if not agent:
        print_error(f"Agent not found: {agent_id}")
        raise typer.Exit(1)

    if not agent.tmux_session:
        print_error("Agent has no TMUX session")
        raise typer.Exit(1)

    ctx = state.context_store.get(str(agent.context_id))
    if not ctx:
        print_error("Agent's context not found")
        raise typer.Exit(1)

    transport = TransportFactory.create(ctx.machine)
    attach_cmd = transport.get_attach_command(agent.tmux_session)
    print_info(f"Attaching to TMUX session: {agent.tmux_session}")
    print_info("Press Ctrl+B, D to detach")
    os.system(attach_cmd)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def stop(
    agent_id: str = typer.Argument(..., help="Agent ID"),
) -> None:
    """Gracefully stop a running agent."""
    import asyncio

    from cam.cli.app import state
    from cam.cli.formatters import print_error, print_success

    agent = state.agent_store.get(agent_id)
    if not agent:
        print_error(f"Agent not found: {agent_id}")
        raise typer.Exit(1)

    try:
        asyncio.run(state.agent_manager.stop_agent(str(agent.id)))
        print_success(f"Agent {str(agent.id)[:8]} stopped")
    except Exception as e:
        print_error(f"Failed to stop agent: {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------


def kill(
    agent_id: str = typer.Argument(..., help="Agent ID"),
) -> None:
    """Force kill a running agent."""
    import asyncio

    from cam.cli.app import state
    from cam.cli.formatters import print_error, print_success

    agent = state.agent_store.get(agent_id)
    if not agent:
        print_error(f"Agent not found: {agent_id}")
        raise typer.Exit(1)

    try:
        asyncio.run(state.agent_manager.stop_agent(str(agent.id), graceful=False))
        print_success(f"Agent {str(agent.id)[:8]} killed")
    except Exception as e:
        print_error(f"Failed to kill agent: {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


def retry(
    agent_id: str = typer.Argument(..., help="Agent ID of a failed/killed agent"),
    detach: bool = typer.Option(False, "--detach", help="Don't follow output"),
) -> None:
    """Re-run a failed or killed agent with the same configuration."""
    import asyncio

    from cam.cli.app import state
    from cam.cli.formatters import print_agent_detail, print_error, print_info, print_success
    from cam.core.models import AgentStatus

    agent = state.agent_store.get(agent_id)
    if not agent:
        print_error(f"Agent not found: {agent_id}")
        raise typer.Exit(1)

    if not agent.is_terminal():
        print_error(f"Agent {str(agent.id)[:8]} is still {agent.status.value}. Stop it first.")
        raise typer.Exit(1)

    # Resolve context
    ctx = state.context_store.get(agent.context_name) or state.context_store.get(str(agent.context_id))
    if not ctx:
        print_error(f"Original context not found: {agent.context_name}")
        raise typer.Exit(1)

    print_info(f"Retrying agent {str(agent.id)[:8]} ({agent.task.tool}: {agent.task.prompt[:50]}...)")

    try:
        new_agent = asyncio.run(
            state.agent_manager.run_agent(agent.task, ctx, follow=not detach)
        )
        if detach:
            print_success(f"Agent restarted: {str(new_agent.id)[:8]}")
            print_agent_detail(new_agent)
        else:
            new_agent = state.agent_store.get(str(new_agent.id))
            if new_agent:
                print_agent_detail(new_agent)
    except Exception as e:
        print_error(f"Failed to retry agent: {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _print_log_entry(entry: dict) -> None:
    """Format and print a single JSONL log entry."""
    from rich.console import Console

    console = Console()
    ts = entry.get("ts", "")[:19]
    event_type = entry.get("type", "")
    output = entry.get("output", "")
    data = entry.get("data", {})

    if event_type == "output" and output:
        console.print(f"[dim]{ts}[/dim] {output}")
    elif event_type == "state_change":
        state_val = data.get("state", "")
        console.print(f"[dim]{ts}[/dim] [bold cyan]State -> {state_val}[/bold cyan]")
    elif event_type == "auto_confirm":
        console.print(f"[dim]{ts}[/dim] [yellow]Auto-confirmed[/yellow]")
    elif event_type == "agent_finished":
        status_val = data.get("status", "")
        console.print(f"[dim]{ts}[/dim] [bold]Finished: {status_val}[/bold]")
    else:
        console.print(f"[dim]{ts}[/dim] [{event_type}] {data or output}")
