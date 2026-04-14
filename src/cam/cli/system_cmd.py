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
    """Sync camc and adapter configs to a remote context.

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
    """Check running agents and restart dead monitor daemons via camc.

    For local agents, runs 'camc heal' locally.
    For remote agents, runs 'camc heal' on the remote machine via SSH.
    Deduplicates by host (one heal per unique machine).
    """
    from cam.cli.app import state
    from cam.core.camc_delegate import CamcDelegate
    from cam.core.models import AgentStatus, TransportType

    agents = state.agent_store.list(status=AgentStatus.RUNNING)
    if not agents:
        print_info("No running agents.")
        return

    healed = 0
    failed = 0
    healed_hosts: set[str] = set()  # track hosts we already healed

    for agent in agents:
        # Use agent's own machine fields (set by poller), fall back to context
        host = agent.machine_host
        user = agent.machine_user
        port = agent.machine_port
        if not host and agent.context_id:
            context = state.context_store.get(str(agent.context_id))
            if context is None:
                continue
            machine = context.machine
            host = getattr(machine, "host", None)
            user = getattr(machine, "user", None)
            port = getattr(machine, "port", None)

        # Deduplicate by host
        host_key = "%s@%s:%s" % (user or "", host or "local", port or "")
        if host_key in healed_hosts:
            continue
        healed_hosts.add(host_key)

        host_label = host or "local"
        console.print(f"  Healing {host_label}...")

        try:
            delegate = CamcDelegate(host=host, user=user, port=port)
            ok = delegate.heal()
            if ok:
                console.print(f"  [green]{host_label}: healed[/green]")
                healed += 1
            else:
                console.print(f"  [red]{host_label}: heal failed[/red]")
                failed += 1
        except Exception as e:
            console.print(f"  [red]{host_label}: heal failed: {e}[/red]")
            failed += 1

    print_success(f"Heal complete: {healed} hosts healed, {failed} failed")


# ---------------------------------------------------------------------------
# cam migrate
# ---------------------------------------------------------------------------


def migrate(
    ctx_name: str = typer.Argument(..., help="Context name to migrate (or 'list' to show managed contexts)"),
    rollback: bool = typer.Option(False, "--rollback", help="Revert context to direct management"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show migration plan without executing"),
) -> None:
    """Migrate a context to camc-managed mode (or rollback).

    After migration, agent operations on this context are delegated to camc.
    camc must be deployed first via 'cam sync'.

    Use 'cam migrate list' to see which contexts are camc-managed.
    Use --rollback to revert a context to direct management.
    """
    from cam.cli.app import state
    from cam.core.camc_migration import (
        list_managed,
        migrate_context,
        rollback_context,
    )

    # Special case: list managed contexts
    if ctx_name == "list":
        managed = list_managed()
        if not managed:
            print_info("No contexts are camc-managed.")
        else:
            console.print("[bold]camc-managed contexts:[/bold]")
            for name in managed:
                console.print(f"  [cyan]{name}[/cyan]")
        return

    if rollback:
        result = rollback_context(
            ctx_name,
            agent_store=state.agent_store,
            context_store=state.context_store,
        )
        if "error" in result:
            print_error(result["error"])
            raise typer.Exit(1)
        print_success(f"Context '{ctx_name}' reverted to direct management.")
        return

    # Migrate
    result = migrate_context(
        ctx_name,
        agent_store=state.agent_store,
        context_store=state.context_store,
        dry_run=dry_run,
    )

    if is_json_mode():
        print_json(result)
        return

    if "error" in result:
        print_error(result["error"])
        raise typer.Exit(1)

    if dry_run:
        console.print(f"[bold]Migration plan for '{ctx_name}':[/bold]")
        console.print(f"  Host: {result.get('host', 'local')}")
        console.print(f"  Agents found: {result.get('agents_found', 0)}")
        for d in result.get("details", []):
            console.print(
                f"  {d['action']}: {d.get('tool', '?')} "
                f"(session={d.get('session', '?')}, id={d.get('agent_id', '?')})"
            )
        return

    console.print(f"[bold]Migration results for '{ctx_name}':[/bold]")
    console.print(f"  Adopted: {result.get('adopted', 0)}")
    console.print(f"  Failed:  {result.get('failed', 0)}")
    console.print(f"  Skipped: {result.get('skipped', 0)}")
    if result.get("managed"):
        print_success(f"Context '{ctx_name}' is now camc-managed.")
    else:
        print_error("Migration did not complete — no agents were adopted.")
