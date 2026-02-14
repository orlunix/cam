"""CAM CLI — Root Typer Application.

This is the entry point for the ``cam`` command. It defines the main Typer app,
global options (--json, --verbose, --config, --data-dir), and lazy-initialised
shared state that subcommands access via ``from cam.cli.app import state``.

Subcommand registration:
- ``cam context ...`` is mounted as a Typer sub-group.
- Agent commands (run, list, status, logs, attach, stop, kill) and system
  commands (version, doctor) are registered directly on the root app so they
  appear as top-level ``cam <command>`` invocations.
"""

from __future__ import annotations

import typer

from cam import __version__
from cam.cli.formatters import set_json_mode

# ---------------------------------------------------------------------------
# Main Typer app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="cam",
    help="CAM \u2014 Coding Agent Manager. PM2 for AI coding agents.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Global state (lazy-initialised, accessible as ``from cam.cli.app import state``)
# ---------------------------------------------------------------------------


class AppState:
    """Shared state object that is populated by the root callback and consumed
    by every subcommand.  Heavy objects (database, stores, manager) are created
    lazily on first access so that lightweight commands like ``cam version`` pay
    no startup cost.
    """

    def __init__(self) -> None:
        self.json_mode: bool = False
        self.verbose: bool = False
        self.config_path: str | None = None
        self.data_dir: str | None = None

        # Private caches for lazy properties
        self._db = None
        self._config = None
        self._context_store = None
        self._agent_store = None
        self._event_bus = None
        self._adapter_registry = None
        self._agent_manager = None

    # -- Lazy properties ----------------------------------------------------

    @property
    def config(self):
        """Load and cache the merged CamConfig."""
        if self._config is None:
            from cam.core.config import load_config

            overrides: dict = {}
            if self.data_dir:
                overrides["paths"] = {"data_dir": self.data_dir}
            self._config = load_config(**overrides)
        return self._config

    @property
    def db(self):
        """Open (or create) the SQLite database."""
        if self._db is None:
            from pathlib import Path

            from cam.storage.database import Database

            data_dir = Path(self.config.paths.data_dir).expanduser()
            self._db = Database(data_dir / "cam.db")
        return self._db

    @property
    def context_store(self):
        """Return the ContextStore backed by :pyattr:`db`."""
        if self._context_store is None:
            from cam.storage.context_store import ContextStore

            self._context_store = ContextStore(self.db)
        return self._context_store

    @property
    def agent_store(self):
        """Return the AgentStore backed by :pyattr:`db`."""
        if self._agent_store is None:
            from cam.storage.agent_store import AgentStore

            self._agent_store = AgentStore(self.db)
        return self._agent_store

    @property
    def event_bus(self):
        """Return the singleton EventBus."""
        if self._event_bus is None:
            from cam.core.events import EventBus

            self._event_bus = EventBus()
        return self._event_bus

    @property
    def adapter_registry(self):
        """Return the AdapterRegistry (built-in adapters auto-registered)."""
        if self._adapter_registry is None:
            from cam.adapters.registry import AdapterRegistry

            self._adapter_registry = AdapterRegistry()
        return self._adapter_registry

    @property
    def agent_manager(self):
        """Return the fully-wired AgentManager."""
        if self._agent_manager is None:
            from cam.core.agent_manager import AgentManager

            self._agent_manager = AgentManager(
                config=self.config,
                context_store=self.context_store,
                agent_store=self.agent_store,
                event_bus=self.event_bus,
                adapter_registry=self.adapter_registry,
            )
        return self._agent_manager


state = AppState()


# ---------------------------------------------------------------------------
# Root callback — processes global options before any subcommand runs
# ---------------------------------------------------------------------------


@app.callback()
def main_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output as JSON instead of Rich tables.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Verbose output (debug logging).",
    ),
    config: str | None = typer.Option(
        None,
        "--config",
        help="Path to a TOML config file (overrides default locations).",
    ),
    data_dir: str | None = typer.Option(
        None,
        "--data-dir",
        help="Data directory path (overrides CAM_DATA_DIR / config).",
    ),
) -> None:
    """CAM \u2014 Coding Agent Manager."""
    state.json_mode = json_output
    state.verbose = verbose
    state.config_path = config
    state.data_dir = data_dir
    set_json_mode(json_output)


# ---------------------------------------------------------------------------
# Register subcommand groups and top-level commands
# ---------------------------------------------------------------------------

# Context commands live under ``cam context <subcommand>``
from cam.cli import context_cmd  # noqa: E402

app.add_typer(context_cmd.app, name="context", help="Manage work contexts.")

# Agent commands are registered directly on the root app so they feel like
# first-class verbs: ``cam run``, ``cam list``, ``cam stop``, etc.
from cam.cli import agent_cmd  # noqa: E402

app.command(name="run")(agent_cmd.run)
app.command(name="list")(agent_cmd.list)
app.command(name="status")(agent_cmd.status)
app.command(name="logs")(agent_cmd.logs)
app.command(name="attach")(agent_cmd.attach)
app.command(name="stop")(agent_cmd.stop)
app.command(name="kill")(agent_cmd.kill)
app.command(name="retry")(agent_cmd.retry)
app.command(name="prune")(agent_cmd.prune)

# Task command (cam apply -f)
from cam.cli import task_cmd  # noqa: E402

app.command(name="apply")(task_cmd.apply)

# History and stats commands
from cam.cli import history_cmd  # noqa: E402

app.command(name="history")(history_cmd.history)
app.command(name="stats")(history_cmd.stats)

# Config commands live under ``cam config <subcommand>``
from cam.cli import config_cmd  # noqa: E402

app.add_typer(config_cmd.app, name="config", help="Manage CAM configuration.")

# System commands are also top-level: ``cam version``, ``cam doctor``
from cam.cli import system_cmd  # noqa: E402

app.command(name="version")(system_cmd.version)
app.command(name="doctor")(system_cmd.doctor)
app.command(name="init")(system_cmd.init)


# Server command — starts the API server
@app.command(name="serve")
def serve(
    host: str | None = typer.Option(None, "--host", help="Bind address"),
    port: int | None = typer.Option(None, "--port", help="Listen port"),
    token: str | None = typer.Option(
        None, "--token", help="Auth token (auto-generated if not set)"
    ),
    relay: str | None = typer.Option(
        None, "--relay", help="Relay URL (e.g. ws://relay:8443)"
    ),
    relay_token: str | None = typer.Option(
        None, "--relay-token", help="Relay auth token"
    ),
) -> None:
    """Start the CAM API server."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        from rich import print as rprint

        rprint("[red]API server requires:[/red] pip install cam[server]")
        raise typer.Exit(1)

    from cam.api.server import create_app
    from cam.core.config import load_config

    overrides: dict = {}
    if host:
        overrides.setdefault("server", {})["host"] = host
    if port:
        overrides.setdefault("server", {})["port"] = port
    if token:
        overrides.setdefault("server", {})["auth_token"] = token
    if relay:
        overrides.setdefault("server", {})["relay_url"] = relay
    if relay_token:
        overrides.setdefault("server", {})["relay_token"] = relay_token

    config = load_config(**overrides)
    app_instance = create_app(overrides=overrides)

    import uvicorn

    uvicorn.run(
        app_instance,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point (referenced by ``[project.scripts]`` in pyproject.toml)."""
    app()
