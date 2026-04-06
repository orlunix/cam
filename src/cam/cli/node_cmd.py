"""Node management CLI commands.

Provides the `cam node` sub-command for listing and inspecting nodes.
A node is a unique connection endpoint (host:port) where agents run.
"""

from __future__ import annotations

from typing import Optional

import typer

from cam.cli.formatters import print_error, print_info

app = typer.Typer(help="View nodes and their agents", no_args_is_help=True)


@app.command("list")
def node_list(
    status: Optional[str] = typer.Option(None, "--status", help="Only show nodes with agents in this status"),
) -> None:
    """List all nodes with agent summary."""
    from cam.cli.app import state
    from cam.core.models import AgentStatus
    from rich.table import Table
    from cam.cli.formatters import console

    agents = state.agent_store.list(
        status=AgentStatus(status) if status else None,
    )

    # Build node map
    nodes: dict[str, dict] = {}
    for a in agents:
        host = getattr(a, "machine_host", None) or ""
        if not host or host in ("localhost", "127.0.0.1"):
            key = "local"
            host = "local"
        else:
            key = host.split(".")[0]  # short hostname

        if key not in nodes:
            nodes[key] = {
                "host": host,
                "user": getattr(a, "machine_user", None) or "",
                "port": getattr(a, "machine_port", None),
                "running": 0,
                "completed": 0,
                "failed": 0,
                "other": 0,
                "total": 0,
                "contexts": set(),
            }
        n = nodes[key]
        n["total"] += 1
        s = a.status.value if a.status else ""
        if s == "running":
            n["running"] += 1
        elif s == "completed":
            n["completed"] += 1
        elif s in ("failed", "killed", "timeout"):
            n["failed"] += 1
        else:
            n["other"] += 1
        if a.context_name:
            n["contexts"].add(a.context_name)

    if not nodes:
        print_info("No nodes found.")
        return

    table = Table(title="Nodes", show_lines=False)
    table.add_column("#", style="bold", width=3)
    table.add_column("Node", style="bold")
    table.add_column("Host")
    table.add_column("Running", justify="right", style="green")
    table.add_column("Done", justify="right", style="dim")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Total", justify="right")
    table.add_column("Contexts")

    # Sort: local last, then alphabetical
    sorted_keys = sorted(nodes.keys(), key=lambda k: (k == "local", k))
    for i, key in enumerate(sorted_keys, 1):
        n = nodes[key]
        port_str = f":{n['port']}" if n["port"] else ""
        user_str = f"{n['user']}@" if n["user"] else ""
        host_display = f"{user_str}{n['host']}{port_str}" if key != "local" else "local"
        ctx_str = ", ".join(sorted(n["contexts"])[:5])
        if len(n["contexts"]) > 5:
            ctx_str += f" (+{len(n['contexts']) - 5})"

        table.add_row(
            str(i),
            key,
            host_display,
            str(n["running"]) if n["running"] else "-",
            str(n["completed"]) if n["completed"] else "-",
            str(n["failed"]) if n["failed"] else "-",
            str(n["total"]),
            ctx_str,
        )

    console.print(table)


@app.command("status")
def node_status(
    node: str = typer.Argument(..., help="Node name or host (substring match, or 'local')"),
) -> None:
    """Show detailed status for a node."""
    from cam.cli.app import state
    from cam.cli.formatters import console
    from rich.table import Table
    from rich.panel import Panel

    # Find matching agents
    all_agents = state.agent_store.list()

    def matches(a) -> bool:
        host = getattr(a, "machine_host", None) or ""
        if node.lower() == "local":
            return not host or host in ("localhost", "127.0.0.1")
        return node.lower() in host.lower()

    agents = [a for a in all_agents if matches(a)]

    if not agents:
        print_error(f"No agents found for node: {node}")
        raise typer.Exit(1)

    # Derive node info from first agent
    sample = agents[0]
    host = getattr(sample, "machine_host", None) or "local"
    user = getattr(sample, "machine_user", None) or ""
    port = getattr(sample, "machine_port", None)

    short_host = "local" if node.lower() == "local" else host.split(".")[0]
    host_display = f"{user + '@' if user else ''}{host}{':' + str(port) if port else ''}"

    # Summary
    running = [a for a in agents if a.status and a.status.value == "running"]
    completed = [a for a in agents if a.status and a.status.value == "completed"]
    failed = [a for a in agents if a.status and a.status.value in ("failed", "killed", "timeout")]
    contexts = sorted({a.context_name for a in agents if a.context_name})

    console.print(Panel(
        f"[bold]{short_host}[/bold]  {host_display}\n"
        f"Agents: [green]{len(running)} running[/green], "
        f"[dim]{len(completed)} completed[/dim], "
        f"[red]{len(failed)} failed[/red] "
        f"({len(agents)} total)\n"
        f"Contexts: {', '.join(contexts) if contexts else 'none'}",
        title="Node",
    ))

    # Agent table
    table = Table(show_lines=False)
    table.add_column("#", width=3)
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("State")
    table.add_column("Context")

    status_styles = {
        "running": "▶ running",
        "completed": "✓ completed",
        "failed": "✗ failed",
        "killed": "✗ killed",
        "timeout": "✗ timeout",
    }

    # Sort: running first, then by name
    agents.sort(key=lambda a: (0 if a.status and a.status.value == "running" else 1, a.task.name or ""))

    for i, a in enumerate(agents, 1):
        s = a.status.value if a.status else ""
        st = a.state.value if a.state else ""
        table.add_row(
            str(i),
            str(a.id)[:8],
            a.task.name or "",
            a.task.tool or "",
            status_styles.get(s, s),
            st,
            a.context_name or "",
        )

    console.print(table)
