"""History and statistics CLI commands."""

from __future__ import annotations

from typing import Optional

import typer

from cam.cli.formatters import (
    console,
    format_duration,
    format_short_id,
    is_json_mode,
    print_error,
    print_info,
    print_json,
)


def history(
    ctx_name: Optional[str] = typer.Option(None, "--ctx", help="Filter by context name"),
    tool: Optional[str] = typer.Option(None, "--tool", help="Filter by tool"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
    last: str = typer.Option("30d", "--last", help="Time window (e.g. '7d', '30d', '24h')"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
) -> None:
    """View completed agent history.

    Examples:
        cam history
        cam history --ctx my-project --last 7d
        cam history --tool claude --status failed
    """
    from datetime import datetime, timedelta, timezone

    from cam.cli.app import state
    from cam.core.config import parse_duration
    from cam.storage.history_store import HistoryStore

    # Parse time window
    since = None
    if last:
        seconds = parse_duration(last)
        if seconds:
            since = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    history_store = HistoryStore(state.db)
    entries = history_store.list_history(
        context_name=ctx_name,
        tool=tool,
        status=status,
        since=since,
        limit=limit,
    )

    if not entries:
        print_info("No history found matching the filters.")
        return

    if is_json_mode():
        print_json(entries)
        return

    from rich.table import Table

    table = Table(title="Agent History", title_style="bold yellow")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Tool", style="bold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Context", style="cyan")
    table.add_column("Duration", justify="right")
    table.add_column("Task", max_width=40)
    table.add_column("Reason", style="dim", max_width=25)

    for entry in entries:
        status_val = entry["status"]
        if status_val == "completed":
            status_display = "[green]✓ completed[/green]"
        elif status_val == "failed":
            status_display = "[red]✗ failed[/red]"
        elif status_val == "timeout":
            status_display = "[red]⏱ timeout[/red]"
        elif status_val == "killed":
            status_display = "[red]☠ killed[/red]"
        else:
            status_display = status_val

        prompt = entry.get("prompt", "")
        prompt_preview = prompt[:37] + "..." if len(prompt) > 40 else prompt

        table.add_row(
            format_short_id(entry["id"]),
            entry.get("tool", ""),
            status_display,
            entry.get("context", ""),
            format_duration(entry.get("duration")),
            prompt_preview,
            entry.get("exit_reason", "") or "",
        )

    console.print(table)


def stats(
    ctx_name: Optional[str] = typer.Option(None, "--ctx", help="Filter by context"),
    last: str = typer.Option("30d", "--last", help="Time window (e.g. '7d', '30d')"),
) -> None:
    """Show aggregated agent statistics.

    Examples:
        cam stats
        cam stats --ctx my-project --last 7d
    """
    from datetime import datetime, timedelta, timezone

    from rich.panel import Panel

    from cam.cli.app import state
    from cam.core.config import parse_duration
    from cam.storage.history_store import HistoryStore

    # Parse time window
    since = None
    if last:
        seconds = parse_duration(last)
        if seconds:
            since = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    history_store = HistoryStore(state.db)
    data = history_store.get_stats(context_name=ctx_name, since=since)

    if is_json_mode():
        print_json(data)
        return

    if data["total"] == 0:
        print_info("No agents found in the specified time window.")
        return

    lines = []
    lines.append(f"[bold]Total Agents:[/bold] {data['total']}")
    lines.append("")

    # By status
    lines.append("[bold]By Status:[/bold]")
    for s, count in sorted(data["by_status"].items()):
        lines.append(f"  {s}: {count}")

    # By tool
    lines.append("")
    lines.append("[bold]By Tool:[/bold]")
    for t, count in sorted(data["by_tool"].items()):
        lines.append(f"  {t}: {count}")

    # Metrics
    lines.append("")
    if data["success_rate"] is not None:
        lines.append(f"[bold]Success Rate:[/bold] {data['success_rate']}%")
    if data["avg_duration_seconds"] is not None:
        lines.append(f"[bold]Avg Duration:[/bold] {format_duration(data['avg_duration_seconds'])}")
    if data["total_cost"] is not None:
        lines.append(f"[bold]Total Cost:[/bold] ${data['total_cost']:.4f}")

    title = "Agent Statistics"
    if ctx_name:
        title += f" ({ctx_name})"
    if last:
        title += f" — last {last}"

    panel = Panel("\n".join(lines), title=title, title_align="left", border_style="yellow")
    console.print(panel)
