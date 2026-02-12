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
        "aider": shutil.which("aider"),
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
