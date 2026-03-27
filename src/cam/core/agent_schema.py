"""Canonical agent record schema shared between cam and camc.

This module defines the field names and structure that both cam (Pydantic/SQLite)
and camc (stdlib JSON) use for agent records. The goal is a single source of
truth so that `camc --json list` output can be consumed by cam without
field remapping.

Field categories:
  - CORE: identity and task definition
  - CONTEXT: where the agent runs
  - SESSION: tmux session details
  - STATUS: execution lifecycle
  - TIMING: timestamps
  - METRICS: cost, files changed
  - CLUSTER: hostname for NFS-shared environments

camc stores agents as flat JSON dicts with these exact field names.
cam's Agent Pydantic model mirrors these fields (task is a nested TaskDefinition).

Migration note: camc previously used "session" (now "tmux_session"),
"path" (now "context_path"), "monitor_pid" (now "pid"), and flat
tool/prompt/name/auto_exit (now nested under "task").
"""

from __future__ import annotations

# --- Agent record field names (canonical) ---

# Core identity
F_ID = "id"                         # str: 8-char hex (camc) or full UUID (cam)

# Task definition (nested dict in JSON, TaskDefinition in Pydantic)
F_TASK = "task"                     # dict: {name, tool, prompt, auto_confirm, auto_exit}
F_TASK_NAME = "name"                # str: human-readable task name
F_TASK_TOOL = "tool"                # str: claude, codex, cursor, ...
F_TASK_PROMPT = "prompt"            # str: task prompt text
F_TASK_AUTO_CONFIRM = "auto_confirm"  # bool: auto-confirm enabled
F_TASK_AUTO_EXIT = "auto_exit"      # bool: auto-exit on completion

# Context (where the agent runs)
F_CONTEXT_ID = "context_id"         # str: cam context UUID (empty in camc)
F_CONTEXT_NAME = "context_name"     # str: context name
F_CONTEXT_PATH = "context_path"     # str: working directory path
F_TRANSPORT_TYPE = "transport_type" # str: "local", "ssh", etc.

# Session
F_TMUX_SESSION = "tmux_session"     # str: tmux session name (e.g. "cam-abc12345")
F_TMUX_SOCKET = "tmux_socket"       # str: tmux socket path (or empty)
F_PID = "pid"                       # int|None: monitor process ID

# Status & state
F_STATUS = "status"                 # str: pending/starting/running/completed/failed/timeout/killed/retrying
F_STATE = "state"                   # str: initializing/planning/editing/testing/committing/idle

# Timing
F_STARTED_AT = "started_at"        # str (ISO 8601) | None
F_COMPLETED_AT = "completed_at"    # str (ISO 8601) | None

# Execution result
F_EXIT_REASON = "exit_reason"      # str | None

# Metrics (cam-only, camc defaults to 0/None/[])
F_RETRY_COUNT = "retry_count"      # int: default 0
F_COST_ESTIMATE = "cost_estimate"  # float | None
F_FILES_CHANGED = "files_changed"  # list[str]

# Cluster support (camc-only, used for NFS-shared agent stores)
F_HOSTNAME = "hostname"            # str: machine hostname


def make_agent_record(
    agent_id,       # type: str
    tool,           # type: str
    tmux_session,   # type: str
    context_path,   # type: str
    prompt="",      # type: str
    name=None,      # type: str | None
    auto_exit=False,  # type: bool
    context=None,   # type: dict | None
    hostname=None,  # type: str | None
    started_at=None,  # type: str | None
):
    # type: (...) -> dict
    """Create a canonical agent record dict (for camc's agents.json).

    This is the single place where the camc agent record shape is defined.
    All fields match the cam Agent model's JSON serialization.
    """
    ctx = context or {}
    ctx_name = ctx.get("name", "") if isinstance(ctx, dict) else ""
    ctx_host = ctx.get("host") if isinstance(ctx, dict) else None
    transport = "ssh" if ctx_host and ctx_host not in ("localhost", "127.0.0.1") else "local"
    return {
        F_ID: agent_id,
        F_TASK: {
            F_TASK_NAME: name or "",
            F_TASK_TOOL: tool,
            F_TASK_PROMPT: prompt,
            F_TASK_AUTO_CONFIRM: True,
            F_TASK_AUTO_EXIT: auto_exit,
        },
        F_CONTEXT_ID: "",
        F_CONTEXT_NAME: ctx_name,
        F_CONTEXT_PATH: context_path,
        F_TRANSPORT_TYPE: transport,
        F_STATUS: "running",
        F_STATE: "initializing",
        F_TMUX_SESSION: tmux_session,
        F_TMUX_SOCKET: "",
        F_PID: None,
        F_HOSTNAME: hostname or "",
        F_STARTED_AT: started_at,
        F_COMPLETED_AT: None,
        F_EXIT_REASON: None,
        F_RETRY_COUNT: 0,
        F_COST_ESTIMATE: None,
        F_FILES_CHANGED: [],
    }


# --- Backward compatibility helpers for camc ---
# Old field names → new field names (for reading legacy agents.json)
_LEGACY_FIELD_MAP = {
    "session": F_TMUX_SESSION,
    "path": F_CONTEXT_PATH,
    "monitor_pid": F_PID,
}

# Old flat fields that moved into task
_LEGACY_TASK_FIELDS = {"tool", "prompt", "name", "auto_exit"}


def normalize_agent_record(record):
    # type: (dict) -> dict
    """Normalize a possibly-legacy agent record to canonical format.

    Handles old camc records with session/path/monitor_pid and flat task fields.
    Returns a new dict; does not mutate the input.
    """
    r = dict(record)

    # Rename legacy fields
    for old, new in _LEGACY_FIELD_MAP.items():
        if old in r and new not in r:
            r[new] = r.pop(old)
        elif old in r:
            r.pop(old)  # new field already exists, drop legacy

    # Promote flat task fields into nested task dict
    if F_TASK not in r or not isinstance(r.get(F_TASK), dict):
        r[F_TASK] = {
            F_TASK_NAME: r.pop("name", None) or "",
            F_TASK_TOOL: r.pop("tool", "claude"),
            F_TASK_PROMPT: r.pop("prompt", ""),
            F_TASK_AUTO_CONFIRM: r.pop("auto_confirm", True),
            F_TASK_AUTO_EXIT: r.pop("auto_exit", False),
        }
    else:
        # Remove flat duplicates if task dict exists
        for f in _LEGACY_TASK_FIELDS:
            r.pop(f, None)

    return r


def task_field(agent, field, default=""):
    # type: (dict, str, ...) -> ...
    """Get a task sub-field from an agent record, supporting both formats."""
    t = agent.get(F_TASK)
    if isinstance(t, dict):
        return t.get(field, default)
    # Legacy flat format
    return agent.get(field, default)
