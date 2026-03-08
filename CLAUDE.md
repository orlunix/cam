# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CAM (Coding Agent Manager) is "PM2 for AI coding agents." It manages Claude Code, Codex, Cursor, and other AI CLI tools via TMUX sessions, with auto-confirm, completion detection, background monitoring, and remote execution.

## Build & Test Commands

```bash
pip install -e ".[all,dev]"    # Install with all optional deps
pytest                          # Run full test suite (~450 tests)
pytest tests/test_probe.py      # Run a single test file
pytest -k "test_probe_echo"     # Run a specific test by name
pytest -x                       # Stop on first failure
```

The CLI entry point is `cam` (defined in `pyproject.toml` `[project.scripts]`).

```bash
cam doctor          # Check environment (tmux, python, etc.)
cam serve           # Start API server (FastAPI + uvicorn)
```

## Architecture

Five layers, top to bottom:

1. **CLI** (`src/cam/cli/`) — Typer commands. Shared state via `AppState` singleton in `app.py` with lazy-initialized properties (DB, stores, manager).
2. **Core** (`src/cam/core/`) — `AgentManager` orchestrates lifecycle. `AgentMonitor` polls TMUX output for state changes, auto-confirm, and completion. `Probe` sends invisible characters to detect true idle. `Scheduler` runs DAG workflows.
3. **Transport** (`src/cam/transport/`) — Abstraction over session backends. `LocalTransport` wraps tmux directly. `SSHTransport` runs tmux on remote machines via ControlMaster-pooled SSH. `AgentTransport` uses the `cam-agent` Go binary. `TransportFactory` creates the right one from context config.
4. **Adapters** (`src/cam/adapters/`) — Tool-specific behavior defined in TOML files (`configs/*.toml`), loaded by `ConfigurableAdapter`. No Python code needed to add a new tool — just create a TOML file.
5. **TMUX** — Each agent runs in its own tmux session. The session runs the command directly (not via shell), so it dies when the process exits.

### Key subsystems

- **API Server** (`src/cam/api/`) — FastAPI with token auth, WebSocket events, relay connector for NAT traversal.
- **Relay** (`relay/relay.py`) — Standalone zero-dep WebSocket relay (stdlib-only RFC 6455). Bridges REST-over-WS between mobile clients and CAM server.
- **Storage** (`src/cam/storage/`) — SQLite via raw `sqlite3`. Schema created in `database.py`. Agent/context/history stores with short-ID prefix matching.
- **cam-agent** (`cam-agent/`) — Go binary providing standardized remote protocol over SSH. Wraps tmux on Linux.
- **Web/Mobile** (`web/`) — PWA frontend. Android WebView wrapper in `android/`.

## Critical Pydantic v2 Patterns

The codebase uses `from __future__ import annotations` in every module. This creates specific requirements:

- **UUID fields**: Always `id: str = Field(default_factory=lambda: str(uuid4()))`. Never use `UUID` type — it breaks JSON serialization with deferred annotations.
- **Constructing models**: Pass `str(uuid4())` explicitly, not `uuid4()`.
- **Enum values**: Always lowercase strings (`"local"`, `"pending"`, not `"LOCAL"`).
- **Serialization**: Use `model_dump(mode="json")` for nested models with enums. Plain `model_dump()` keeps enum objects.
- **Agent.events**: Type is `list[dict]`, not `list[AgentEvent]`. Append with `event.model_dump(mode="json")`.
- **DateTime to SQLite**: Always `str(dt)` before inserting.

## DB Schema Notes

- `contexts.last_used_at` allows NULL (new contexts haven't been used yet).
- `agents.created_at` uses a DB default — the Agent Pydantic model has no `created_at` field; use `started_at` for the DB column.
- `AgentStore.get()` supports short ID prefix matching (e.g., pass `"86a9d46b"` instead of the full UUID).

## TOML Adapter Config System

Adapters are declared in `src/cam/adapters/configs/*.toml`. The `ConfigurableAdapter` class loads these at runtime. Key sections:

- `[launch]` — command, prompt_after_launch, startup_wait, ready_pattern
- `[state]` — strategy + `[[state.patterns]]` mapping regex → agent state
- `[completion]` — strategy (prompt_count), prompt_pattern, threshold
- `[[confirm]]` — auto-confirm rules: pattern, response, send_enter
- `[probe]` — idle detection char, wait time, threshold
- `[monitor]` — cooldowns, auto_exit, exit_action

Detection logic lives in `src/cam/client.py` (shared between adapter and standalone client binary `camc.py`).

## Claude Adapter Specifics

- Runs in interactive mode (`claude --allowed-tools ...`), NOT headless `-p` mode.
- Prompt sent via stdin after launch (`prompt_after_launch = true`, `startup_wait = 30s` max).
- Ready detection: polls for `❯` or `>` at start of line (Windows ConPTY renders `❯` as `>`).
- Completion: counts prompt lines — 2+ `❯`/`>` lines = task done (prompt echo + return to input).
- Fallback: single prompt + task summary line (`✻ verb for time`) also signals completion.
- Auto-confirm patterns: trust dialog (Enter), permission menu (Enter on selected option), y/n prompts.
- Pre-prompt auto-confirm: `agent_manager` polls during startup for trust/permission prompts before sending the task prompt.
- `--allowed-tools` pre-authorizes Write/Read/Glob/Grep but Bash still needs per-command confirmation.

## Monitor Loop (`core/monitor.py`)

The monitor polls every ~2 seconds:
1. Check tmux session alive
2. Capture terminal output
3. Run state detection (pattern matching on recent output)
4. Check auto-confirm patterns and send responses (with cooldown)
5. Check completion detection (only after output stable for 3s)
6. Run probe if completion detected (confirm idle before finalizing)
7. Handle auto-exit if configured

Background mode (`--detach`) spawns `monitor_runner.py` as a subprocess with PID file at `~/.local/share/cam/pids/<agent_id>.pid`. Uses `start_new_session=True` so the monitor survives CLI exit. `cam stop/kill` sends SIGTERM to the monitor subprocess and kills the TMUX session.

## TMUX Session Design

- Sessions run the command directly (not via shell), so the session dies when the process exits.
- Monitor detects completion via `session_exists()` returning False OR `detect_completion()`.
- `capture-pane` returns empty during Claude's alternate screen buffer — fallback to `-a` flag.
- Suppress capture-pane failures to debug level (expected when session exits).
- `create_session` wraps command with `env -u CLAUDECODE` to prevent nested-session detection.
- LocalTransport accepts `env_setup` param; TransportFactory passes `config.env_setup`.

## Transport Notes

- `LocalTransport`: Direct tmux commands. `create_session` uses `subprocess.DEVNULL` (not PIPE) because tmux forks a server that inherits pipes.
- `SSHTransport`: ControlMaster socket at `/tmp/cam-ssh-<sha256-hash>` (short path avoids 108-char Unix socket limit). Non-ASCII input base64-encoded to handle POSIX locale remotes.
- `SSHTransport` Windows support: `--shell powershell` on context, `_is_windows` flag, `_cmd_quote()` for cmd.exe double-quoting. Skip `bash -l -c` wrapping on Windows.
- ANSI stripping happens at capture level (`transport/local.py`) and again before pattern matching.

## Test Conventions

- Fixtures in `tests/conftest.py`: `tmp_db` (temp SQLite), `sample_context`, `sample_adapter`
- UUIDs as `str(uuid4())`
- UTC datetimes with `datetime.now(timezone.utc)`
- Test files mirror source: `test_probe.py`, `test_configurable_adapter.py`, `test_api.py`, etc.

## Web Frontend

- PWA with service worker cache (`web/sw.js`). Version query strings (`?v=XX`) on all imports for cache busting — keep them in sync across `index.html`, `app.js`, and `sw.js`.
- `web/js/api.js` — API client supporting direct HTTP and relay (REST-over-WS) modes.
- Views are vanilla JS modules in `web/js/views/`.

## DAG Scheduler (`cam apply`)

- YAML task files with `version`, `defaults` (tool, timeout, retry), and `tasks` with `depends_on`.
- `TaskGraph` validates: no cycles, no missing deps, topological sort.
- `Scheduler.execute()` runs level-by-level with `asyncio.gather()` for parallel tasks.
- `--detach` does NOT work with DAGs — agents return RUNNING immediately, breaking dependency checks. Must use follow mode.

## API Output Optimization

- Hash-based conditional responses: server computes MD5 of output, client sends hash back. Returns `{"unchanged": true}` (~50 bytes) when nothing changed.
- TTL cache (2s) on `capture_output()` to avoid repeated TMUX calls.
- Relay uses `asyncio.create_task()` for concurrent dispatch instead of serial `await`.
- WebSocket `onclose` rejects all pending `_requestMap` requests to prevent 15s hangs.

## cam-agent Go Binary

- Located at `cam-agent/`. Single binary, cross-compiled for linux/windows/darwin.
- CLI subcommands over SSH: `ping`, `session create/exists/kill/capture/send/key`, `file list/read/write`, `session log-start/log-read`.
- Linux backend: thin wrapper around real tmux (sockets at `/tmp/cam-agent-sockets/`).
- ANSI stripping done in Go (agent-side) — capture returns plain text.
- Python side: `AgentTransport` in `src/cam/transport/agent.py`. Reuses SSH ControlMaster pooling.
- Build: `cd cam-agent && make build`.
