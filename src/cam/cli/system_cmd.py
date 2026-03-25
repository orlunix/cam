"""CAM CLI — System commands: version, doctor, init.

These commands are registered directly on the root Typer app (not as a
sub-group) so they appear as ``cam version``, ``cam doctor``, ``cam init``.
"""

from __future__ import annotations

import typer

from cam import __version__
from cam.cli.formatters import (
    console,
    is_json_mode,
    print_doctor_results,
    print_error,
    print_info,
    print_json,
    print_success,
)


# ---------------------------------------------------------------------------
# cam version
# ---------------------------------------------------------------------------


def version() -> None:
    """Show CAM version and installed adapters."""
    if is_json_mode():
        # Lazy import to avoid startup cost when not needed
        from cam.cli.app import state

        adapter_names = state.adapter_registry.names()
        print_json(
            {
                "version": __version__,
                "adapters": adapter_names,
            }
        )
        return

    console.print(f"[bold]CAM[/bold] v{__version__}")

    # List available adapters
    try:
        from cam.cli.app import state

        adapters = state.adapter_registry.list()
        if adapters:
            console.print()
            console.print("[bold]Installed adapters:[/bold]")
            for adapter in adapters:
                console.print(f"  [cyan]{adapter.name}[/cyan]  {adapter.display_name}")
        else:
            console.print()
            console.print("[dim]No adapters installed.[/dim]")
    except Exception as exc:
        console.print()
        console.print(f"[dim]Could not load adapters: {exc}[/dim]")


# ---------------------------------------------------------------------------
# cam doctor
# ---------------------------------------------------------------------------


def doctor() -> None:
    """Check system dependencies and environment."""
    from cam.utils.doctor import check_all

    try:
        checks = check_all()
    except Exception as exc:
        print_error(f"Doctor check failed: {exc}")
        raise typer.Exit(code=1)

    # Convert DoctorCheck objects to the dict format expected by formatters
    results = [
        {
            "name": check.name,
            "passed": check.status,
            "details": check.message,
            "required": check.required,
        }
        for check in checks
    ]

    print_doctor_results(results)

    # Exit with non-zero status if any *required* check failed
    required_failures = [
        r for r in results if r["required"] and not r["passed"]
    ]
    if required_failures:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# cam init
# ---------------------------------------------------------------------------


def init() -> None:
    """Interactive setup wizard for CAM.

    Walks through initial configuration: checks dependencies,
    creates the config directory, and optionally adds a first context.
    """
    from pathlib import Path

    from cam.constants import CONFIG_DIR, DATA_DIR

    console.print()
    console.print("[bold cyan]CAM — Coding Agent Manager[/bold cyan]")
    console.print(f"Version {__version__}")
    console.print()

    # Step 1: Check dependencies
    console.print("[bold]Step 1: Checking dependencies...[/bold]")
    from cam.utils.doctor import check_all

    checks = check_all()
    passed = sum(1 for c in checks if c.status)
    total = len(checks)
    console.print(f"  {passed}/{total} checks passed")

    required_failed = [c for c in checks if c.required and not c.status]
    if required_failed:
        for c in required_failed:
            console.print(f"  [red]✗ {c.name}: {c.message}[/red]")
        console.print()
        console.print("[red]Required dependencies missing. Please install them and try again.[/red]")
        raise typer.Exit(1)

    console.print("  [green]All required dependencies available[/green]")
    console.print()

    # Step 2: Create directories
    console.print("[bold]Step 2: Setting up directories...[/bold]")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sockets").mkdir(parents=True, exist_ok=True)

    console.print(f"  Config: {CONFIG_DIR}")
    console.print(f"  Data:   {DATA_DIR}")
    console.print()

    # Step 3: Initialize database
    console.print("[bold]Step 3: Initializing database...[/bold]")
    from cam.cli.app import state
    _ = state.db  # Triggers DB creation and migration
    console.print(f"  Database: {DATA_DIR / 'cam.db'}")
    console.print()

    # Step 4: Available tools
    console.print("[bold]Step 4: Detecting coding tools...[/bold]")
    import shutil
    tools = {
        "claude": shutil.which("claude"),
        "codex": shutil.which("codex"),
    }
    for tool_name, path in tools.items():
        if path:
            console.print(f"  [green]✓[/green] {tool_name}: {path}")
        else:
            console.print(f"  [dim]✗ {tool_name}: not found[/dim]")

    console.print()

    # Step 5: Offer to add a context
    console.print("[bold]Step 5: Add a context?[/bold]")
    add_ctx = typer.confirm("Would you like to add a local context now?", default=True)

    if add_ctx:
        import os
        default_path = os.getcwd()
        ctx_name = typer.prompt("Context name", default="my-project")
        ctx_path = typer.prompt("Working directory", default=default_path)

        from cam.core.models import Context, MachineConfig
        from datetime import datetime, timezone
        from uuid import uuid4

        context = Context(
            id=str(uuid4()),
            name=ctx_name,
            path=ctx_path,
            machine=MachineConfig(),
            created_at=datetime.now(timezone.utc),
        )
        state.context_store.add(context)
        print_success(f"Context '{ctx_name}' added at {ctx_path}")

    console.print()
    print_success("CAM setup complete!")
    console.print()
    console.print("[bold]Quick start:[/bold]")
    console.print("  cam context list          # List your contexts")
    console.print("  cam run claude \"task\"      # Run an agent")
    console.print("  cam list                  # Check agent status")
    console.print("  cam --help                # Full command reference")


# ---------------------------------------------------------------------------
# cam sync
# ---------------------------------------------------------------------------


def sync(
    ctx_name: str = typer.Argument(None, help="Context name to sync to (omit for all remote contexts)"),
) -> None:
    """Sync cam-client and adapter configs to a remote context.

    If no context is specified, syncs to all remote (SSH/Agent) contexts.
    """
    import asyncio

    from cam.cli.app import state
    from cam.core.models import TransportType

    # Build list of contexts to sync
    if ctx_name:
        context = state.context_store.get(ctx_name)
        if not context:
            print_error(f"Context not found: {ctx_name}")
            raise typer.Exit(1)
        if context.machine.type == TransportType.LOCAL:
            print_error("Sync is only for remote contexts (SSH/Agent)")
            raise typer.Exit(1)
        contexts = [context]
    else:
        all_contexts = state.context_store.list()
        contexts = [c for c in all_contexts if c.machine.type != TransportType.LOCAL]
        if not contexts:
            print_info("No remote contexts found.")
            return
        # Deduplicate by host to avoid syncing the same machine twice
        seen_hosts = set()
        unique = []
        for c in contexts:
            key = (c.machine.host, c.machine.port, c.machine.user)
            if key not in seen_hosts:
                seen_hosts.add(key)
                unique.append(c)
        contexts = unique
        print_info(f"Syncing to {len(contexts)} remote context(s)...")

    total_synced = 0
    total_unchanged = 0
    total_failed = 0

    for context in contexts:
        print_info(f"Syncing to context '{context.name}' ({context.machine.host})...")

        try:
            results = asyncio.run(state.agent_manager.sync_to_target(context))
        except Exception as e:
            print_error(f"  Sync failed: {e}")
            total_failed += 1
            continue

        if not results:
            print_info("  Nothing to sync.")
            continue

        for name, status in results.items():
            if status == "unchanged":
                console.print(f"  [dim]{name}: unchanged[/dim]")
            elif status == "failed":
                console.print(f"  [red]{name}: FAILED[/red]")
            else:
                console.print(f"  [green]{name}: {status}[/green]")

        failed = sum(1 for s in results.values() if s == "failed")
        synced = sum(1 for s in results.values() if s in ("deployed", "updated"))
        unchanged = sum(1 for s in results.values() if s == "unchanged")
        total_synced += synced
        total_unchanged += unchanged
        total_failed += failed

        if failed:
            print_error(f"  {failed} file(s) failed to sync")

    print_success(f"Sync complete: {total_synced} deployed/updated, {total_unchanged} unchanged"
                  + (f", {total_failed} failed" if total_failed else ""))


# ---------------------------------------------------------------------------
# cam heal
# ---------------------------------------------------------------------------


def heal() -> None:
    """Check running agents and restart dead monitor daemons.

    For local agents, restarts the monitor_runner subprocess.
    For remote agents, runs 'camc heal' on the remote machine via SSH.
    Intended to be run periodically via cron.
    """
    import asyncio
    import os
    import signal
    import subprocess
    import sys

    from cam.cli.app import state
    from cam.constants import PID_DIR
    from cam.core.models import AgentStatus, TransportType

    agents = state.agent_store.list(status=AgentStatus.RUNNING)
    if not agents:
        print_info("No running agents.")
        return

    healed = 0
    ok = 0
    failed = 0
    healed_remotes: set[str] = set()  # track hosts we already healed

    for agent in agents:
        agent_id = str(agent.id)
        short_id = agent_id[:8]
        name = agent.task.name or short_id

        context = state.context_store.get(str(agent.context_id))
        transport_type = context.machine.type if context else TransportType.LOCAL

        if transport_type == TransportType.LOCAL:
            # Check if monitor process is alive
            pid_path = PID_DIR / f"{agent_id}.pid"
            monitor_alive = False
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text().strip())
                    os.kill(pid, 0)
                    monitor_alive = True
                except (ValueError, ProcessLookupError, PermissionError):
                    pid_path.unlink(missing_ok=True)

            if monitor_alive:
                console.print(f"  [dim]{name} ({short_id}): monitor alive[/dim]")
                ok += 1
                continue

            # Restart local monitor
            console.print(f"  [yellow]{name} ({short_id}): monitor dead, restarting...[/yellow]")
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "cam.core.monitor_runner", agent_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                console.print(f"  [green]{name} ({short_id}): restarted (PID {proc.pid})[/green]")
                healed += 1
            except Exception as e:
                console.print(f"  [red]{name} ({short_id}): restart failed: {e}[/red]")
                failed += 1
        else:
            # Remote agent — run camc heal once per host
            host_key = (context.machine.host, context.machine.port)
            if host_key in healed_remotes:
                console.print(f"  [dim]{name} ({short_id}): remote heal already ran[/dim]")
                ok += 1
                continue
            healed_remotes.add(host_key)

            try:
                transport = state.agent_manager._create_transport(context)
                success, output = asyncio.run(
                    transport._run_ssh("bash -c 'python3 ~/.cam/camc heal 2>&1'", check=False)
                )
                output = output.strip()
                if success:
                    for line in output.splitlines():
                        console.print(f"    {line}")
                    healed += 1
                else:
                    console.print(f"  [red]{name} ({short_id}): remote heal failed: {output[:200]}[/red]")
                    failed += 1
            except Exception as e:
                console.print(f"  [red]{name} ({short_id}): remote heal failed: {e}[/red]")
                failed += 1

    print_success(f"Heal complete: {ok} healthy, {healed} restarted, {failed} failed")
