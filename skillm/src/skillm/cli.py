"""CLI interface for skillm."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import core

console = Console()


def find_project_root() -> Path:
    """Find project root by looking for skills.json or .git."""
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / core.SKILLS_JSON).exists():
            return p
        if (p / ".git").exists():
            return p
        # Don't go above home
        if p == Path.home():
            break
    return cwd


@click.group()
@click.version_option(package_name="skillm")
def main():
    """skillm — Per-project AI Agent Skill Manager."""
    pass


@main.command()
def init():
    """Initialize skills.json and .skills/ in current directory."""
    root = Path.cwd()
    skills_json = root / core.SKILLS_JSON
    skills_dir = root / core.SKILLS_DIR

    if skills_json.exists():
        console.print("[yellow]skills.json already exists[/yellow]")
    else:
        config = core.SkillsConfig()
        config.save(root)
        console.print("[green]✓[/green] Created skills.json")

    skills_dir.mkdir(exist_ok=True)
    console.print("[green]✓[/green] Created .skills/")

    # Add .skills to .gitignore if it exists
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".skills" not in content:
            with open(gitignore, "a") as f:
                f.write("\n.skills/\n")
            console.print("[green]✓[/green] Added .skills/ to .gitignore")
    else:
        gitignore.write_text(".skills/\n")
        console.print("[green]✓[/green] Created .gitignore with .skills/")


@main.command()
@click.argument("source")
@click.option("--name", "-n", help="Override skill name")
def add(source: str, name: str | None):
    """Add and install a skill from GitHub (owner/repo[/path])."""
    root = find_project_root()
    console.print(f"[dim]Fetching {source}...[/dim]")

    try:
        skill_name = core.install_skill(root, source, name)
        core.update_claude_md(root)
        console.print(f"[green]✓[/green] Installed [bold]{skill_name}[/bold]")
        console.print(f"  → .skills/{skill_name}/")
    except Exception as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)


@main.command()
@click.argument("name")
def remove(name: str):
    """Remove a skill completely."""
    root = find_project_root()
    core.remove_skill(root, name)
    core.update_claude_md(root)
    console.print(f"[green]✓[/green] Removed [bold]{name}[/bold]")


@main.command()
@click.argument("name")
def disable(name: str):
    """Disable a skill (keep files, hide from agent)."""
    root = find_project_root()
    try:
        core.toggle_skill(root, name, enabled=False)
        core.update_claude_md(root)
        console.print(f"[yellow]⊘[/yellow] Disabled [bold]{name}[/bold]")
    except KeyError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)


@main.command()
@click.argument("name")
def enable(name: str):
    """Re-enable a disabled skill."""
    root = find_project_root()
    try:
        core.toggle_skill(root, name, enabled=True)
        core.update_claude_md(root)
        console.print(f"[green]✓[/green] Enabled [bold]{name}[/bold]")
    except KeyError as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)


@main.command("list")
def list_cmd():
    """List all skills and their status."""
    root = find_project_root()
    skills = core.list_skills(root)

    if not skills:
        console.print("[dim]No skills configured. Run 'skillm init' then 'skillm add'.[/dim]")
        return

    table = Table(title="Project Skills", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Source", style="dim")
    table.add_column("Status")
    table.add_column("Installed")

    for name, entry, installed in skills:
        if not entry.enabled:
            status = "[yellow]disabled[/yellow]"
        else:
            status = "[green]enabled[/green]"

        inst = "[green]✓[/green]" if installed else "[red]✗[/red]"
        table.add_row(name, entry.source, status, inst)

    console.print(table)


@main.command()
def sync():
    """Install all skills from skills.json."""
    root = find_project_root()
    console.print("[dim]Syncing skills...[/dim]")

    installed = core.sync_skills(root)
    core.update_claude_md(root)

    if installed:
        for name in installed:
            console.print(f"  [green]✓[/green] {name}")
        console.print(f"\n[green]Synced {len(installed)} skill(s)[/green]")
    else:
        console.print("[dim]All skills already installed.[/dim]")


@main.command()
@click.argument("name", required=False)
def update(name: str | None):
    """Update skill(s) to latest version."""
    root = find_project_root()
    config = core.SkillsConfig.load(root)

    targets = [name] if name else list(config.skills.keys())

    for skill_name in targets:
        if skill_name not in config.skills:
            console.print(f"[red]✗[/red] {skill_name} not found")
            continue
        entry = config.skills[skill_name]
        console.print(f"[dim]Updating {skill_name}...[/dim]")
        try:
            core.install_skill(root, entry.source, skill_name)
            console.print(f"  [green]✓[/green] {skill_name}")
        except Exception as e:
            console.print(f"  [red]✗[/red] {skill_name}: {e}")

    core.update_claude_md(root)


@main.command()
def inject():
    """Regenerate the skills section in CLAUDE.md."""
    root = find_project_root()
    core.update_claude_md(root)
    console.print("[green]✓[/green] Updated CLAUDE.md skills section")


@main.command()
@click.argument("query")
def search(query: str):
    """Search for skills on GitHub."""
    console.print(f"[dim]Searching GitHub for '{query}' skills...[/dim]")

    client = httpx.Client(timeout=15)
    # Search GitHub repos with "SKILL.md" + query
    resp = client.get(
        f"{core.GH_API}/search/repositories",
        params={
            "q": f"{query} SKILL.md in:readme",
            "sort": "stars",
            "per_page": 15,
        },
    )
    client.close()

    if resp.status_code != 200:
        console.print(f"[red]Search failed: {resp.status_code}[/red]")
        return

    items = resp.json().get("items", [])
    if not items:
        console.print("[dim]No results found.[/dim]")
        return

    table = Table(title=f"Search: {query}", show_lines=False)
    table.add_column("Source", style="bold")
    table.add_column("★", justify="right")
    table.add_column("Description", max_width=50)

    for item in items:
        table.add_row(
            item["full_name"],
            str(item.get("stargazers_count", 0)),
            (item.get("description") or "")[:50],
        )

    console.print(table)
    console.print("\n[dim]Install with: skillm add owner/repo[/dim]")


# Need httpx import at module level for search command
import httpx
