"""Configuration CLI commands.

Provides the `cam config` sub-commands for viewing and modifying
the CAM configuration.
"""

from __future__ import annotations

from typing import Optional

import typer

from cam.cli.formatters import console, is_json_mode, print_error, print_info, print_json, print_success

app = typer.Typer(help="Manage CAM configuration", no_args_is_help=True)


@app.command("show")
def config_show(
    section: Optional[str] = typer.Argument(None, help="Config section (e.g. 'general', 'monitor')"),
) -> None:
    """Show effective configuration (merged from all sources).

    Examples:
        cam config show
        cam config show monitor
        cam config show general
    """
    from cam.cli.app import state

    config = state.config
    config_dict = config.model_dump(mode="json")

    if section:
        if section in config_dict:
            data = {section: config_dict[section]}
        else:
            print_error(f"Unknown config section: {section}")
            print_info(f"Available sections: {', '.join(config_dict.keys())}")
            raise typer.Exit(1)
    else:
        data = config_dict

    if is_json_mode():
        print_json(data)
        return

    from rich.panel import Panel

    lines = []
    _format_dict(data, lines, indent=0)

    panel = Panel(
        "\n".join(lines),
        title="CAM Configuration",
        title_align="left",
        border_style="blue",
    )
    console.print(panel)


@app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (dot-notation, e.g. 'monitor.poll_interval')"),
    value: str = typer.Argument(..., help="New value"),
) -> None:
    """Set a global configuration value.

    Values are written to ~/.config/cam/config.toml.
    Use dot notation for nested keys.

    Examples:
        cam config set general.default_tool codex
        cam config set monitor.poll_interval 5
        cam config set general.auto_confirm false
    """
    from pathlib import Path

    from cam.constants import CONFIG_DIR

    config_file = CONFIG_DIR / "config.toml"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Parse existing config
    existing: dict = {}
    if config_file.exists():
        try:
            import tomllib
            with open(config_file, "rb") as f:
                existing = tomllib.load(f)
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
                with open(config_file, "rb") as f:
                    existing = tomllib.load(f)
            except ImportError:
                print_error("Cannot read TOML config. Install Python 3.11+ or 'pip install tomli'")
                raise typer.Exit(1)

    # Parse key path
    parts = key.split(".")
    if len(parts) < 2:
        print_error("Key must use dot notation (e.g. 'monitor.poll_interval')")
        raise typer.Exit(1)

    # Auto-convert value types
    parsed_value: str | int | float | bool = value
    if value.lower() in ("true", "yes"):
        parsed_value = True
    elif value.lower() in ("false", "no"):
        parsed_value = False
    else:
        try:
            parsed_value = int(value)
        except ValueError:
            try:
                parsed_value = float(value)
            except ValueError:
                pass  # Keep as string

    # Set nested value
    target = existing
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        target = target[part]
    target[parts[-1]] = parsed_value

    # Write back as TOML
    try:
        import tomli_w
        with open(config_file, "wb") as f:
            tomli_w.dump(existing, f)
    except ImportError:
        # Fallback: write manually
        _write_toml_simple(config_file, existing)

    print_success(f"Set {key} = {parsed_value}")
    print_info(f"Config file: {config_file}")


@app.command("reset")
def config_reset(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Reset configuration to defaults.

    Deletes the global config file at ~/.config/cam/config.toml.
    """
    from pathlib import Path

    from cam.constants import CONFIG_DIR

    config_file = CONFIG_DIR / "config.toml"

    if not config_file.exists():
        print_info("No config file to reset (using defaults).")
        return

    if not force:
        from cam.cli.formatters import print_warning
        print_warning(f"This will delete: {config_file}")
        confirm = typer.confirm("Are you sure?")
        if not confirm:
            print_info("Cancelled")
            return

    config_file.unlink()
    print_success("Configuration reset to defaults")


def _format_dict(data: dict, lines: list[str], indent: int = 0) -> None:
    """Recursively format a dict for display."""
    prefix = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}[bold]{key}:[/bold]")
            _format_dict(value, lines, indent + 1)
        elif isinstance(value, list):
            if value:
                lines.append(f"{prefix}[bold]{key}:[/bold] {', '.join(str(v) for v in value)}")
            else:
                lines.append(f"{prefix}[bold]{key}:[/bold] []")
        else:
            lines.append(f"{prefix}[bold]{key}:[/bold] {value}")


def _write_toml_simple(path, data: dict) -> None:
    """Simple TOML writer for nested dicts (no tomli_w dependency)."""
    lines: list[str] = []
    _write_toml_section(data, lines, [])
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _write_toml_section(data: dict, lines: list[str], path: list[str]) -> None:
    """Write a TOML section recursively."""
    # First write scalar values at this level
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")

    # Then write nested sections
    for key, value in data.items():
        if isinstance(value, dict):
            section_path = path + [key]
            lines.append("")
            lines.append(f"[{'.'.join(section_path)}]")
            _write_toml_section(value, lines, section_path)


def _toml_value(value) -> str:
    """Convert a Python value to TOML representation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, str):
        return f'"{value}"'
    elif isinstance(value, list):
        items = ", ".join(_toml_value(v) for v in value)
        return f"[{items}]"
    else:
        return f'"{value}"'
