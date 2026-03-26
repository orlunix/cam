"""Migrate contexts to camc-managed mode, one machine at a time.

Migration steps per context:
1. Ensure camc is deployed and up-to-date (cam sync)
2. For each running agent on the context, register in camc via `camc add`
3. Mark context as camc-managed
4. After migration, new agents are created via CamcDelegate
5. CamcPoller syncs state back to SQLite

The migration is reversible: unmark camc_managed to revert to direct management.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cam.core.camc_delegate import CamcDelegate, _run_camc_ssh
from cam.core.models import Agent, AgentStatus, TransportType
from cam.storage.agent_store import AgentStore
from cam.storage.context_store import ContextStore

logger = logging.getLogger(__name__)

# File tracking which contexts are camc-managed
_MANAGED_FILE = Path.home() / ".local" / "share" / "cam" / "camc_managed.json"


def _load_managed() -> set[str]:
    """Load set of camc-managed context names."""
    if not _MANAGED_FILE.exists():
        return set()
    try:
        with open(_MANAGED_FILE, "r") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (ValueError, OSError):
        return set()


def _save_managed(names: set[str]) -> None:
    """Save set of camc-managed context names."""
    _MANAGED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_MANAGED_FILE, "w") as f:
        json.dump(sorted(names), f, indent=2)
        f.write("\n")


def is_camc_managed(context_name: str) -> bool:
    """Check if a context is camc-managed."""
    return context_name in _load_managed()


def mark_managed(context_name: str) -> None:
    """Mark a context as camc-managed."""
    managed = _load_managed()
    managed.add(context_name)
    _save_managed(managed)


def unmark_managed(context_name: str) -> None:
    """Unmark a context (revert to direct management)."""
    managed = _load_managed()
    managed.discard(context_name)
    _save_managed(managed)


def list_managed() -> list[str]:
    """List all camc-managed context names."""
    return sorted(_load_managed())


def migrate_context(
    context_name: str,
    agent_store: AgentStore,
    context_store: ContextStore,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate a single context to camc-managed mode.

    Steps:
    1. Find all running agents on this context
    2. For each, register in remote camc via `camc add <session> --tool <tool>`
    3. Mark context as camc-managed

    Args:
        context_name: Name of the context to migrate.
        agent_store: cam's AgentStore (SQLite).
        context_store: cam's ContextStore.
        dry_run: If True, show plan without executing.

    Returns:
        Dict with migration results: adopted, failed, skipped counts.
    """
    context = context_store.get(context_name)
    if context is None:
        return {"error": "Context '%s' not found" % context_name}

    machine = context.machine
    host = getattr(machine, "host", None)
    user = getattr(machine, "user", None)
    port = getattr(machine, "port", None)
    is_local = machine.type == TransportType.LOCAL

    # Check if already managed
    if is_camc_managed(context_name):
        return {"error": "Context '%s' is already camc-managed" % context_name}

    # Find running agents on this context
    all_running = agent_store.list(status=AgentStatus.RUNNING)
    context_agents = [a for a in all_running if a.context_name == context_name]

    result = {
        "context": context_name,
        "host": host or "local",
        "agents_found": len(context_agents),
        "adopted": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }

    if dry_run:
        result["dry_run"] = True
        for a in context_agents:
            result["details"].append({
                "agent_id": str(a.id)[:8],
                "tool": a.task.tool,
                "session": a.tmux_session,
                "action": "would adopt",
            })
        return result

    # Create delegate for this machine
    delegate = CamcDelegate(host=host, user=user, port=port)

    # Verify camc is available
    ver = delegate.version()
    if not ver:
        return {"error": "camc not reachable on %s. Run 'cam sync %s' first." % (
            host or "local", context_name)}
    logger.info("Migration target: %s (%s)", context_name, ver)

    # Adopt each running agent
    for agent in context_agents:
        aid = str(agent.id)[:8]
        session = agent.tmux_session
        tool = agent.task.tool

        if not session:
            result["skipped"] += 1
            result["details"].append({
                "agent_id": aid, "action": "skipped", "reason": "no session"})
            continue

        # Call camc add on the target machine
        args = ["add", session, "--tool", tool]
        if is_local:
            from cam.core.camc_delegate import _run_camc
            rc, out = _run_camc(args, timeout=15)
        else:
            rc, out = _run_camc_ssh(host, user, port, args, timeout=15)

        if rc == 0:
            result["adopted"] += 1
            result["details"].append({
                "agent_id": aid, "tool": tool, "session": session,
                "action": "adopted", "output": out.strip()})
            logger.info("Adopted %s (%s) on %s", aid, session, context_name)
        else:
            result["failed"] += 1
            result["details"].append({
                "agent_id": aid, "tool": tool, "session": session,
                "action": "failed", "output": out.strip()})
            logger.warning("Failed to adopt %s on %s: %s", aid, context_name, out.strip())

    # Mark as managed (even if some failed — partial migration is OK)
    if result["adopted"] > 0 or result["agents_found"] == 0:
        mark_managed(context_name)
        result["managed"] = True
    else:
        result["managed"] = False

    return result


def rollback_context(
    context_name: str,
    agent_store: AgentStore,
    context_store: ContextStore,
) -> dict[str, Any]:
    """Revert a context from camc-managed to direct management.

    This simply unmarks the context. Running agents continue in their
    tmux sessions — cam's own monitor can pick them up again via reconcile.
    """
    if not is_camc_managed(context_name):
        return {"error": "Context '%s' is not camc-managed" % context_name}

    unmark_managed(context_name)
    return {"context": context_name, "action": "reverted to direct management"}
