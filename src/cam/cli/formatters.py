"""
CAM CLI - Rich-based Output Formatting

Provides both Rich (terminal) and JSON output formatting for all CLI commands.
Supports colored tables, panels, and structured data display.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cam.core.models import (
    Agent,
    AgentState,
    AgentStatus,
    Context,
    TransportType,
)

# Console instances for normal and error output
console = Console()
error_console = Console(stderr=True)

# Global state for JSON mode
_json_mode = False


def set_json_mode(enabled: bool) -> None:
    """Enable or disable JSON output mode."""
    global _json_mode
    _json_mode = enabled


def is_json_mode() -> bool:
    """Check if JSON output mode is enabled."""
    return _json_mode


# --- Status and state styling ---

STATUS_STYLES = {
    AgentStatus.PENDING: ("yellow", "⏳"),
    AgentStatus.STARTING: ("yellow", "🚀"),
    AgentStatus.RUNNING: ("green", "▶"),
    AgentStatus.COMPLETED: ("green", "✓"),
    AgentStatus.FAILED: ("red", "✗"),
    AgentStatus.TIMEOUT: ("red", "⏱"),
    AgentStatus.KILLED: ("red", "☠"),
    AgentStatus.RETRYING: ("yellow", "↻"),
}

STATE_STYLES = {
    AgentState.INITIALIZING: ("dim", "..."),
    AgentState.PLANNING: ("cyan", "🧠"),
    AgentState.EDITING: ("blue", "✏"),
    AgentState.TESTING: ("magenta", "🧪"),
    AgentState.COMMITTING: ("green", "📦"),
    AgentState.IDLE: ("dim", "💤"),
}

TRANSPORT_LABELS = {
    TransportType.LOCAL: "local",
    TransportType.SSH: "ssh",
    TransportType.WEBSOCKET: "ws",
    TransportType.DOCKER: "docker",
    TransportType.OPENCLAW: "oc",
}


# --- Basic output functions ---

def print_success(message: str) -> None:
    """Print a success message in green."""
    if _json_mode:
        print_json({"status": "success", "message": message})
    else:
        console.print(f"[green]✓[/green] {message}")


def print_error(message: str) -> None:
    """Print an error message in red to stderr."""
    if _json_mode:
        print_json({"status": "error", "message": message})
    else:
        error_console.print(f"[red]✗[/red] {message}")


def print_warning(message: str) -> None:
    """Print a warning message in yellow."""
    if _json_mode:
        print_json({"status": "warning", "message": message})
    else:
        console.print(f"[yellow]⚠[/yellow] {message}")


def print_info(message: str) -> None:
    """Print an informational message."""
    if _json_mode:
        print_json({"status": "info", "message": message})
    else:
        console.print(f"[cyan]ℹ[/cyan] {message}")


# --- Context formatting ---

def print_context_list(contexts: list[Context]) -> None:
    """Print contexts as a Rich table or JSON array."""
    if _json_mode:
        print(json.dumps([c.model_dump(mode="json") for c in contexts], indent=2, default=str))
        return

    if not contexts:
        console.print("[dim]No contexts found.[/dim]")
        return

    table = Table(title="Contexts", title_style="bold cyan")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Machine")
    table.add_column("Path", style="blue")
    table.add_column("Tags", style="dim")

    for ctx in contexts:
        machine_label = format_machine_label(ctx.machine)
        tags_str = ", ".join(ctx.tags) if ctx.tags else "-"

        table.add_row(
            format_short_id(str(ctx.id)),
            ctx.name,
            TRANSPORT_LABELS.get(ctx.machine.type, "?"),
            machine_label,
            ctx.path,
            tags_str,
        )

    console.print(table)


def print_context_detail(context: Context) -> None:
    """Print detailed context info as a Rich panel or JSON."""
    if _json_mode:
        print(json.dumps(context.model_dump(mode="json"), indent=2, default=str))
        return

    lines = []
    lines.append(f"[bold]Name:[/bold] {context.name}")
    lines.append(f"[bold]ID:[/bold] {context.id}")
    lines.append(f"[bold]Path:[/bold] {context.path}")
    lines.append(f"[bold]Transport:[/bold] {TRANSPORT_LABELS.get(context.machine.type, '?')}")

    # Machine details
    machine_info = format_machine_detail(context.machine)
    if machine_info:
        lines.append(f"[bold]Machine:[/bold] {machine_info}")

    # Env setup
    if context.machine.env_setup:
        lines.append(f"[bold]Env Setup:[/bold] {context.machine.env_setup}")

    # Tags
    if context.tags:
        lines.append(f"[bold]Tags:[/bold] {', '.join(context.tags)}")
    else:
        lines.append("[bold]Tags:[/bold] [dim]none[/dim]")

    # Timestamps
    lines.append(f"[bold]Created:[/bold] {format_timestamp(context.created_at)}")
    if context.last_used_at:
        lines.append(f"[bold]Last Used:[/bold] {format_timestamp(context.last_used_at)}")
    else:
        lines.append("[bold]Last Used:[/bold] [dim]never[/dim]")

    panel = Panel(
        "\n".join(lines),
        title=f"Context: {context.name}",
        title_align="left",
        border_style="cyan",
    )
    console.print(panel)


# --- Agent formatting ---

def print_agent_list(agents: list[Agent]) -> None:
    """Print agents as a Rich table or JSON array."""
    if _json_mode:
        print(json.dumps([a.model_dump(mode="json") for a in agents], indent=2, default=str))
        return

    if not agents:
        console.print("[dim]No agents found.[/dim]")
        return

    table = Table(title="Agents", title_style="bold green")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Tool", style="bold")
    table.add_column("Status", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Context", style="cyan")
    table.add_column("Transport", style="magenta")
    table.add_column("Duration", justify="right")
    table.add_column("Task", max_width=40)

    for agent in agents:
        status_text = format_status(agent.status)
        state_text = format_state(agent.state)
        duration_text = format_duration(agent.duration_seconds())
        transport_label = TRANSPORT_LABELS.get(agent.transport_type, "?")

        # Truncate task prompt
        task_preview = agent.task.prompt[:37] + "..." if len(agent.task.prompt) > 40 else agent.task.prompt

        table.add_row(
            format_short_id(str(agent.id)),
            agent.task.tool,
            status_text,
            state_text,
            agent.context_name,
            transport_label,
            duration_text,
            task_preview,
        )

    console.print(table)


def print_agent_detail(agent: Agent) -> None:
    """Print detailed agent info as a Rich panel or JSON."""
    if _json_mode:
        print(json.dumps(agent.model_dump(mode="json"), indent=2, default=str))
        return

    lines = []

    # Basic info
    lines.append(f"[bold]ID:[/bold] {agent.id}")
    lines.append(f"[bold]Task:[/bold] {agent.task.name}")
    lines.append(f"[bold]Tool:[/bold] {agent.task.tool}")
    lines.append(f"[bold]Status:[/bold] {format_status(agent.status)}")
    lines.append(f"[bold]State:[/bold] {format_state(agent.state)}")

    # Context info
    lines.append("")
    lines.append(f"[bold]Context:[/bold] {agent.context_name}")
    lines.append(f"[bold]Context ID:[/bold] {agent.context_id}")
    lines.append(f"[bold]Context Path:[/bold] {agent.context_path}")
    lines.append(f"[bold]Transport:[/bold] {TRANSPORT_LABELS.get(agent.transport_type, '?')}")

    # Execution info
    lines.append("")
    if agent.started_at:
        lines.append(f"[bold]Started:[/bold] {format_timestamp(agent.started_at)}")
    if agent.completed_at:
        lines.append(f"[bold]Completed:[/bold] {format_timestamp(agent.completed_at)}")

    duration = agent.duration_seconds()
    if duration is not None:
        lines.append(f"[bold]Duration:[/bold] {format_duration(duration)}")

    if agent.retry_count > 0:
        lines.append(f"[bold]Retries:[/bold] {agent.retry_count}")

    # Tmux session info
    if agent.tmux_session:
        lines.append("")
        lines.append(f"[bold]Tmux Session:[/bold] {agent.tmux_session}")
        if agent.tmux_socket:
            lines.append(f"[bold]Tmux Socket:[/bold] {agent.tmux_socket}")

    # Monitor PID (background monitor subprocess)
    from cam.constants import PID_DIR
    monitor_pid_path = PID_DIR / f"{agent.id}.pid"
    if monitor_pid_path.exists():
        try:
            monitor_pid = int(monitor_pid_path.read_text().strip())
            lines.append(f"[bold]Monitor PID:[/bold] {monitor_pid} [dim](background)[/dim]")
        except (ValueError, OSError):
            pass

    # PID
    if agent.pid:
        lines.append(f"[bold]PID:[/bold] {agent.pid}")

    # Exit reason
    if agent.exit_reason:
        lines.append("")
        lines.append(f"[bold]Exit Reason:[/bold] {agent.exit_reason}")

    # Cost estimate
    if agent.cost_estimate is not None:
        lines.append("")
        lines.append(f"[bold]Cost Estimate:[/bold] ${agent.cost_estimate:.4f}")

    # Files changed
    if agent.files_changed:
        lines.append("")
        lines.append(f"[bold]Files Changed:[/bold] {len(agent.files_changed)}")
        for file_path in agent.files_changed[:5]:  # Show first 5
            lines.append(f"  • {file_path}")
        if len(agent.files_changed) > 5:
            lines.append(f"  ... and {len(agent.files_changed) - 5} more")

    # Task prompt
    lines.append("")
    lines.append("[bold]Prompt:[/bold]")
    lines.append(f"[dim]{agent.task.prompt}[/dim]")

    # Task configuration
    if agent.task.timeout:
        lines.append("")
        lines.append(f"[bold]Timeout:[/bold] {agent.task.timeout}s")

    if agent.task.retry.max_retries > 0:
        lines.append(f"[bold]Max Retries:[/bold] {agent.task.retry.max_retries}")

    if agent.task.env:
        lines.append("")
        lines.append("[bold]Environment:[/bold]")
        for key, value in agent.task.env.items():
            lines.append(f"  {key}={value}")

    # Events
    if agent.events:
        lines.append("")
        lines.append(f"[bold]Events:[/bold] {len(agent.events)} total")
        for event in agent.events[-5:]:  # Show last 5 events
            event_time = str(event.get("timestamp", ""))[:19] if isinstance(event, dict) else ""
            event_type = event.get("event_type", "") if isinstance(event, dict) else ""
            lines.append(f"  [dim]{event_time}[/dim] {event_type}")

    panel = Panel(
        "\n".join(lines),
        title=f"Agent: {agent.task.name}",
        title_align="left",
        border_style="green",
    )
    console.print(panel)


# --- Doctor results formatting ---

def print_doctor_results(checks: list[dict[str, Any]]) -> None:
    """Print cam doctor results as a table."""
    if _json_mode:
        print_json(checks)
        return

    if not checks:
        console.print("[dim]No checks performed.[/dim]")
        return

    table = Table(title="System Check Results", title_style="bold yellow")
    table.add_column("Check", style="bold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Details")

    for check in checks:
        name = check.get("name", "Unknown")
        passed = check.get("passed", False)
        details = check.get("details", "")

        if passed:
            status = Text("✓ PASS", style="green")
        else:
            status = Text("✗ FAIL", style="red")

        table.add_row(name, status, details)

    console.print(table)

    # Summary
    total = len(checks)
    passed = sum(1 for c in checks if c.get("passed", False))
    failed = total - passed

    console.print()
    if failed == 0:
        console.print(f"[green]All {total} checks passed![/green]")
    else:
        console.print(f"[yellow]{passed}/{total} checks passed, {failed} failed.[/yellow]")


# --- Helper formatting functions ---

def format_status(status: AgentStatus) -> Text:
    """Format agent status with color and icon."""
    style, icon = STATUS_STYLES.get(status, ("white", "?"))
    return Text(f"{icon} {status.value}", style=style)


def format_state(state: AgentState) -> Text:
    """Format agent state with color and icon."""
    style, icon = STATE_STYLES.get(state, ("white", "?"))
    return Text(f"{icon} {state.value}", style=style)


def format_duration(seconds: float | None) -> str:
    """Format seconds into human-readable duration: '5m 23s', '2h 15m', etc."""
    if seconds is None:
        return "-"

    if seconds < 0:
        return "0s"

    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    seconds = seconds % 60

    if minutes < 60:
        if seconds > 0:
            return f"{minutes}m {seconds}s"
        return f"{minutes}m"

    hours = minutes // 60
    minutes = minutes % 60

    if hours < 24:
        if minutes > 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h"

    days = hours // 24
    hours = hours % 24

    if hours > 0:
        return f"{days}d {hours}h"
    return f"{days}d"


def format_short_id(full_id: str) -> str:
    """Return first 8 chars of a UUID for display."""
    if not full_id:
        return "?"
    return full_id[:8]


def format_timestamp(dt: datetime | None) -> str:
    """Format datetime as a human-readable string."""
    if dt is None:
        return "-"

    # Format as ISO-like but more readable
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_machine_label(machine: Any) -> str:
    """Format machine config as a short label for table display."""
    if machine.type == TransportType.LOCAL:
        return "localhost"
    elif machine.type == TransportType.SSH:
        host = machine.host or "?"
        user = machine.user or "?"
        return f"{user}@{host}"
    elif machine.type == TransportType.DOCKER:
        image = machine.image or "?"
        return f"docker:{image}"
    elif machine.type == TransportType.WEBSOCKET:
        host = machine.host or "?"
        port = machine.agent_port or "?"
        return f"ws://{host}:{port}"
    elif machine.type == TransportType.OPENCLAW:
        host = machine.host or "?"
        return f"oc://{host}"
    else:
        return "unknown"


def format_machine_detail(machine: Any) -> str:
    """Format machine config as detailed string."""
    if machine.type == TransportType.LOCAL:
        return "localhost"
    elif machine.type == TransportType.SSH:
        parts = []
        if machine.user and machine.host:
            parts.append(f"{machine.user}@{machine.host}")
        if machine.port and machine.port != 22:
            parts.append(f"port {machine.port}")
        if machine.key_file:
            parts.append(f"key: {machine.key_file}")
        return ", ".join(parts) if parts else "ssh"
    elif machine.type == TransportType.DOCKER:
        parts = [f"image: {machine.image}"]
        if machine.volumes:
            parts.append(f"volumes: {len(machine.volumes)}")
        return ", ".join(parts)
    elif machine.type == TransportType.WEBSOCKET:
        return f"ws://{machine.host}:{machine.agent_port}"
    elif machine.type == TransportType.OPENCLAW:
        return f"openclaw://{machine.host}"
    else:
        return "unknown transport"


def print_json(data: Any) -> None:
    """Print any data as formatted JSON."""
    print(json.dumps(data, indent=2, default=str))


# --- Progress and streaming helpers ---

def create_progress_bar(description: str = "Processing") -> Any:
    """Create a Rich progress bar context manager."""
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def print_streaming_output(line: str, prefix: str = "") -> None:
    """Print streaming output line (for agent logs)."""
    if prefix:
        console.print(f"[dim]{prefix}[/dim] {line}", markup=False, highlight=False)
    else:
        console.print(line, markup=False, highlight=False)


def print_separator(char: str = "─", style: str = "dim") -> None:
    """Print a horizontal separator line."""
    if not _json_mode:
        width = console.width
        console.print(char * width, style=style)
