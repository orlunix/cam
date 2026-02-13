"""Context management CLI commands.

Provides the `cam context` sub-command for managing work contexts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import typer

from cam.cli.formatters import (
    print_context_detail,
    print_context_list,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from cam.core.models import Context, MachineConfig, TransportType

app = typer.Typer(help="Manage work contexts", no_args_is_help=True)


@app.command("add")
def context_add(
    name: str = typer.Argument(..., help="Context name (unique)"),
    path: str = typer.Argument(..., help="Working directory path"),
    host: Optional[str] = typer.Option(None, "--host", help="SSH hostname"),
    user: Optional[str] = typer.Option(None, "--user", help="SSH username"),
    port: int = typer.Option(22, "--port", help="SSH port"),
    key: Optional[str] = typer.Option(None, "--key", help="SSH key file path"),
    agent: bool = typer.Option(False, "--agent", help="WebSocket Agent Server mode"),
    agent_port: int = typer.Option(9876, "--agent-port", help="Agent server port"),
    token: Optional[str] = typer.Option(None, "--token", help="Auth token"),
    docker: Optional[str] = typer.Option(None, "--docker", help="Docker image"),
    env_setup: Optional[str] = typer.Option(None, "--env-setup", help="Shell commands to run before agent (e.g. PATH setup)"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", help="Tags (repeatable)"),
) -> None:
    """Add a new work context.

    Examples:
        cam context add myproject /path/to/project
        cam context add remote /remote/path --host server.com --user dev
        cam context add remote /path --host srv --env-setup "source /opt/env.sh"
        cam context add agent-ctx /path --agent --host localhost
        cam context add container /app --docker python:3.11
    """
    from cam.cli.app import state
    from cam.storage.context_store import ContextStoreError

    # Determine transport type from options
    if docker:
        transport_type = TransportType.DOCKER
    elif agent:
        transport_type = TransportType.WEBSOCKET
    elif host:
        transport_type = TransportType.SSH
    else:
        transport_type = TransportType.LOCAL

    # Check uniqueness
    if state.context_store.exists(name):
        print_error(f"Context '{name}' already exists. Use a different name or remove the existing one.")
        raise typer.Exit(1)

    # Validate path is absolute
    if not path.startswith("/"):
        print_error(f"Path must be absolute: {path}")
        raise typer.Exit(1)

    # Build MachineConfig - this will validate required fields
    try:
        machine = MachineConfig(
            type=transport_type,
            host=host,
            user=user,
            port=port if port != 22 else None,  # Only set if non-default
            key_file=key,
            agent_port=agent_port if agent else None,
            auth_token=token,
            image=docker,
            env_setup=env_setup,
        )
    except ValueError as e:
        print_error(f"Invalid machine configuration: {e}")
        raise typer.Exit(1)

    # Create and save context
    try:
        context = Context(
            id=str(uuid4()),
            name=name,
            path=path,
            machine=machine,
            tags=tag or [],
            created_at=datetime.now(timezone.utc),
        )
        state.context_store.add(context)
        print_success(f"Context '{name}' added")
        print_context_detail(context)
    except ContextStoreError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except ValueError as e:
        print_error(f"Invalid context data: {e}")
        raise typer.Exit(1)


@app.command("list")
def context_list(
    tag: Optional[str] = typer.Option(None, "--tag", help="Filter by tag"),
    type: Optional[str] = typer.Option(None, "--type", help="Filter by transport type (LOCAL, SSH, WEBSOCKET, DOCKER)"),
) -> None:
    """List all contexts.

    Examples:
        cam context list
        cam context list --tag python
        cam context list --type SSH
    """
    from cam.cli.app import state

    # Parse transport type
    transport_type = None
    if type:
        try:
            transport_type = TransportType(type.lower())
        except ValueError:
            valid_types = ", ".join(t.value for t in TransportType)
            print_error(f"Invalid transport type: {type}. Valid types: {valid_types}")
            raise typer.Exit(1)

    # Build filters
    tags = [tag] if tag else None

    # Retrieve contexts
    contexts = state.context_store.list(tags=tags, transport_type=transport_type)

    if not contexts:
        if tags or transport_type:
            print_info("No contexts found matching the filters.")
        else:
            print_info("No contexts found. Add one with: cam context add <name> <path>")
        return

    print_context_list(contexts)


@app.command("show")
def context_show(
    name_or_id: str = typer.Argument(..., help="Context name or ID"),
) -> None:
    """Show detailed context information.

    Examples:
        cam context show myproject
        cam context show 12345678
    """
    from cam.cli.app import state

    ctx = state.context_store.get(name_or_id)
    if not ctx:
        print_error(f"Context not found: {name_or_id}")
        raise typer.Exit(1)

    print_context_detail(ctx)


@app.command("test")
def context_test(
    name_or_id: str = typer.Argument(..., help="Context name or ID"),
) -> None:
    """Test connectivity to a context.

    Verifies that the transport can connect to the specified context.

    Examples:
        cam context test myproject
        cam context test remote-server
    """
    from cam.cli.app import state
    from cam.transport.factory import TransportFactory

    ctx = state.context_store.get(name_or_id)
    if not ctx:
        print_error(f"Context not found: {name_or_id}")
        raise typer.Exit(1)

    print_info(f"Testing connection to '{ctx.name}' ({ctx.machine.type.value})...")

    try:
        transport = TransportFactory.create(ctx.machine)
        ok, message = asyncio.run(transport.test_connection())

        if ok:
            print_success(f"Connection OK: {message}")
        else:
            print_error(f"Connection failed: {message}")
            raise typer.Exit(1)
    except ImportError as e:
        print_error(f"Transport not available: {e}")
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"Unexpected error during connection test: {e}")
        raise typer.Exit(1)


@app.command("remove")
def context_remove(
    name_or_id: str = typer.Argument(..., help="Context name or ID"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Remove a context.

    This will delete the context configuration but will not affect the actual
    files in the working directory.

    Examples:
        cam context remove myproject
        cam context remove old-project --force
    """
    from cam.cli.app import state
    from cam.storage.context_store import ContextStoreError

    ctx = state.context_store.get(name_or_id)
    if not ctx:
        print_error(f"Context not found: {name_or_id}")
        raise typer.Exit(1)

    # Check if there are active agents using this context
    # Note: This would need to be implemented in agent_store
    # For now, we'll just warn the user

    if not force:
        print_warning(f"About to remove context: {ctx.name}")
        print_info(f"  Path: {ctx.path}")
        print_info(f"  Type: {ctx.machine.type.value}")
        confirm = typer.confirm("Are you sure you want to remove this context?")
        if not confirm:
            print_info("Cancelled")
            return

    try:
        success = state.context_store.remove(name_or_id)
        if success:
            print_success(f"Context '{ctx.name}' removed")
        else:
            # This shouldn't happen since we already checked existence
            print_error(f"Failed to remove context: {name_or_id}")
            raise typer.Exit(1)
    except ContextStoreError as e:
        print_error(f"Failed to remove context: {e}")
        raise typer.Exit(1)


@app.command("update")
def context_update(
    name_or_id: str = typer.Argument(..., help="Context name or ID"),
    path: Optional[str] = typer.Option(None, "--path", help="Update working directory path"),
    env_setup: Optional[str] = typer.Option(None, "--env-setup", help="Shell commands to run before agent"),
    add_tag: Optional[list[str]] = typer.Option(None, "--add-tag", help="Add tags (repeatable)"),
    remove_tag: Optional[list[str]] = typer.Option(None, "--remove-tag", help="Remove tags (repeatable)"),
) -> None:
    """Update context properties.

    Examples:
        cam context update myproject --path /new/path
        cam context update myproject --env-setup "source /opt/env.sh"
        cam context update myproject --add-tag python --add-tag web
        cam context update myproject --remove-tag old-tag
    """
    from cam.cli.app import state
    from cam.storage.context_store import ContextStoreError

    ctx = state.context_store.get(name_or_id)
    if not ctx:
        print_error(f"Context not found: {name_or_id}")
        raise typer.Exit(1)

    # Track if anything changed
    changed = False

    # Update path
    if path:
        if not path.startswith("/"):
            print_error(f"Path must be absolute: {path}")
            raise typer.Exit(1)
        ctx.path = path
        changed = True
        print_info(f"Updated path to: {path}")

    # Update env_setup
    if env_setup is not None:
        ctx.machine.env_setup = env_setup if env_setup else None
        changed = True
        print_info(f"Updated env_setup to: {env_setup or '(cleared)'}")

    # Add tags
    if add_tag:
        for tag_name in add_tag:
            if tag_name not in ctx.tags:
                ctx.tags.append(tag_name)
                changed = True
                print_info(f"Added tag: {tag_name}")
            else:
                print_warning(f"Tag already exists: {tag_name}")

    # Remove tags
    if remove_tag:
        for tag_name in remove_tag:
            if tag_name in ctx.tags:
                ctx.tags.remove(tag_name)
                changed = True
                print_info(f"Removed tag: {tag_name}")
            else:
                print_warning(f"Tag not found: {tag_name}")

    if not changed:
        print_info("No changes made")
        return

    # Save updated context
    # Note: This requires implementing an update method in ContextStore
    # For now, we'll remove and re-add
    try:
        state.context_store.remove(str(ctx.id))
        state.context_store.add(ctx)
        print_success(f"Context '{ctx.name}' updated")
        print_context_detail(ctx)
    except ContextStoreError as e:
        print_error(f"Failed to update context: {e}")
        raise typer.Exit(1)
