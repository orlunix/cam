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

### cam ↔ camc Delegation Model

cam is the server/orchestrator. camc is the standalone agent manager deployed to each machine. All agent operations are delegated from cam to camc:

```
Mobile APP / CLI / Web
        ↓
   cam server (API + Relay)
        ↓
   Core: AgentManager
        ↓
   CamcDelegate ──SSH (ControlMaster)──→ remote camc
        │                                    ↓
   local camc                          camc manages locally:
        │                              - tmux sessions
        │                              - monitor loops
   CamcPoller ←──SSH polling (5s)────  - agents.json
        ↓                              - auto-confirm/completion
   Storage: import to SQLite
```

**CamcDelegate** (`core/camc_delegate.py`): Wraps `camc` CLI calls over SSH. All agent operations (run, stop, kill, capture, send, key) go through camc. Uses ControlMaster to piggy-back on SSHTransport's persistent SSH connections (same socket path: `/tmp/cam-ssh-{sha256(user@host:port)[:12]}`).

**CamcPoller** (`core/camc_poller.py`): Polls `camc --json list` on each remote every 5 seconds. Imports agent state into cam's SQLite for the API/mobile to query. Three-layer machine_host correctness strategy:

1. **Write at creation**: `AgentManager.run_agent()` passes machine connection info (host/user/port) from the context when creating the Agent model, so new agents get correct host from the start.
2. **Hostname guard + self-healing backfill**: Poller skips agents whose `hostname` doesn't match the machine being polled (`_is_same_host()`), preventing cross-contamination on NFS clusters. For agents that pass the guard, poller corrects `machine_host/port` if they don't match — this self-heals any prior misassignment (e.g. from before the hostname guard existed).
3. **Attach context fallback**: `cam attach` falls back to the context's SSH config when `machine_host` is missing or `localhost`, so attach still works even if the DB record is incomplete.

**Key principle**: cam delegates, camc executes. Transport layer provides the SSH tunnel; camc does all tmux operations locally on each machine.

### cam Server Layers

Six layers, top to bottom:

1. **CLI** (`src/cam/cli/`) — Typer commands. Shared state via `AppState` singleton in `app.py` with lazy-initialized properties (DB, stores, manager).
2. **API** (`src/cam/api/`) — FastAPI with token auth, WebSocket events, relay connector for NAT traversal.
3. **Core** (`src/cam/core/`) — `AgentManager` orchestrates lifecycle. `CamcDelegate` delegates operations to camc. `CamcPoller` syncs remote state. `Scheduler` runs DAG workflows.
4. **Transport** (`src/cam/transport/`) — SSH tunnel layer for cam ↔ camc communication. `SSHTransport` provides ControlMaster-pooled SSH connections. Also used for file browsing and `cam sync` deployment. `LocalTransport` wraps local tmux. `TransportFactory` creates the right one from context config.
5. **Storage** (`src/cam/storage/`) — SQLite via raw `sqlite3`. Agent/context/history stores with short-ID prefix matching.
6. **Adapters** (`src/cam/adapters/`) — Tool-specific behavior defined in TOML files (`configs/*.toml`), loaded by `ConfigurableAdapter`. No Python code needed to add a new tool — just create a TOML file.

### Key subsystems

- **Relay** (`relay/relay.py`) — Standalone zero-dep WebSocket relay (stdlib-only RFC 6455). Bridges REST-over-WS between mobile clients and CAM server.
- **cam-agent** (`src/cam-agent/`) — Go binary providing standardized remote protocol over SSH. Wraps tmux on Linux.
- **camc** (`src/camc_pkg/`, built to `dist/camc`) — Standalone single-file CLI (stdlib-only, Python 3.6+). Self-contained agent manager for each machine.
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

### cam vs camc Storage

| | cam server | camc (per machine) |
|---|---|---|
| **Storage** | SQLite (`~/.local/share/cam/cam.db`) | JSON (`~/.cam/agents.json`) |
| **Agent ID** | 8-char (imported from camc) or full UUID (legacy local) | 8-char hex via `uuid5(hostname+time+random)` |
| **Events** | SQLite events table | `~/.cam/events.jsonl` |
| **Sync** | CamcPoller imports from camc every 5s | Source of truth per machine |

cam's SQLite is a cached aggregation of all remote camc instances. camc's `agents.json` is the source of truth for each machine.

### Unified Agent Record Schema

Both cam and camc use the same JSON field names for agent records. The canonical schema is defined in `src/cam/core/agent_schema.py`. Key fields:

```
{
  "id":             "abc12345",                    # 8-char hex (camc) or full UUID (cam)
  "task": {                                        # Nested task definition
    "name":         "my-task",
    "tool":         "claude",
    "prompt":       "fix the bug",
    "auto_confirm": true,
    "auto_exit":    false
  },
  "context_id":     "",                            # cam context UUID (empty in camc)
  "context_name":   "my-project",
  "context_path":   "/home/user/project",
  "transport_type": "ssh",                         # "local" or "ssh"
  "status":         "running",                     # pending/starting/running/completed/failed/timeout/killed
  "state":          "editing",                     # initializing/planning/editing/testing/committing/idle
  "tmux_session":   "cam-abc12345",
  "tmux_socket":    "",
  "pid":            12345,                         # monitor process ID
  "hostname":       "bpmpfw",                      # machine hostname (for NFS clusters)
  "started_at":     "2026-01-01T00:00:00Z",
  "completed_at":   null,
  "exit_reason":    null,
  "retry_count":    0,
  "cost_estimate":  null,
  "files_changed":  []
}
```

**Legacy compatibility**: CamcPoller and camc CLI handle old-format records with `session` (now `tmux_session`), `path` (now `context_path`), `monitor_pid` (now `pid`), and flat `tool/prompt/name/auto_exit` (now nested under `task`). The `_sf()` and `_tf()` helpers in camc provide transparent access across both formats.

## TOML Adapter Config System

Adapters are declared in `src/cam/adapters/configs/*.toml`. The `ConfigurableAdapter` class loads these at runtime. Key sections:

- `[launch]` — command, prompt_after_launch, startup_wait, ready_pattern
- `[state]` — strategy + `[[state.patterns]]` mapping regex → agent state
- `[completion]` — strategy (prompt_count), prompt_pattern, threshold
- `[[confirm]]` — auto-confirm rules: pattern, response, send_enter
- `[probe]` — idle detection char, wait time, threshold
- `[monitor]` — cooldowns, auto_exit, exit_action

Detection logic lives in `src/cam/client.py` (shared between adapter and standalone client binary `camc.py`).

## Sync and Heal (`cam sync`, `cam heal`)

### `cam sync [context]`
Deploys files to remote contexts via SSH:
- `cam-client.py` — shared detection library
- `camc` — standalone CLI (from `src/camc`, with chmod+symlink to `~/.local/bin/camc`)
- `context.json` — context config including `env_setup` from server context
- TOML adapter configs (claude.toml, codex.toml, cursor.toml)
- Installs `camc heal` cron (best-effort, crontab may not be available)
- Hash-based: only transfers files that changed (MD5 comparison)
- Without args, syncs to all remote contexts (deduplicated by host)

### `cam heal`
Checks all running agents and restarts dead monitors:
- **Local agents**: checks PID file at `~/.local/share/cam/pids/<id>.pid`, restarts `monitor_runner.py` if dead
- **Remote agents**: SSHes into each unique host, runs `python3 ~/.cam/camc heal`
- Deduplicates remote hosts (one SSH per host, not per agent)
- Server cron: `0 * * * *` (hourly at :00)

## Claude Adapter Specifics

- Runs in interactive mode (`claude --allowed-tools ...`), NOT headless `-p` mode.
- Prompt sent via stdin after launch (`prompt_after_launch = true`, `startup_wait = 30s` max).
- Ready detection: polls for `❯` or `>` at start of line (Windows ConPTY renders `❯` as `>`).
- Completion: counts prompt lines — 2+ `❯`/`>` lines = task done (prompt echo + return to input).
- Fallback: single prompt + task summary line (`✻ verb for time`) also signals completion.
- Auto-confirm patterns: trust dialog (Enter), permission menu (`1` to select), y/n prompts (`y`+Enter).
- Pre-prompt auto-confirm: `agent_manager` polls during startup for trust/permission prompts before sending the task prompt.
- `--allowed-tools` pre-authorizes Write/Read/Glob/Grep but Bash still needs per-command confirmation.

## Auto-Confirm Strategy

Auto-confirm detects permission dialogs in terminal output and sends the appropriate response. Patterns and responses are defined per-adapter in TOML `[[confirm]]` rules.

### Detection: Last 32 Lines Only

`should_auto_confirm()` only checks the **last 32 non-empty lines** of captured output, not the full screen. This prevents false positives when the agent's response text contains confirm keywords (e.g. a table mentioning "1. Yes"). Real permission dialogs always appear at the bottom of the terminal.

### Claude Confirm Rules

| Pattern | Response | Scenario |
|---|---|---|
| `Do you want to proceed` | `1` (no Enter) | Numbered permission menu (`1. Yes / 2. No`) |
| `1. Yes` / `1. Allow` | `1` (no Enter) | Numbered permission menu |
| `Enter to confirm/select` | Enter | Trust dialog, plan mode interview |
| `Allow once/always` | Enter | Claude 4.x+ Ink select menu |
| `(y/n)` / `[Y/n]` | `y` + Enter | y/n confirmation prompt |

For numbered menus, `1` is sent without Enter — the Ink TUI consumes the keypress to select option 1. For Ink select menus where the cursor is already on the right option, Enter confirms the selection.

### Cooldown

Auto-confirm has a cooldown (`confirm_cooldown`, default 5s) to prevent rapid-fire confirmations. After sending a response, the monitor sleeps `confirm_sleep` (0.5s) then continues to the next poll cycle.

## Smart Probe Strategy

Smart probe detects whether the agent is truly idle after completion is detected. It sends a probe character and observes the terminal's reaction.

### Mechanism

1. **Capture baseline** — snapshot current terminal output
2. **Send probe char** (`1`, no Enter) — the char is chosen to also work as a confirmation response
3. **Wait** (`probe_wait`, 0.3s) — let terminal process the input
4. **Recapture** — snapshot terminal output again
5. **Classify** — compare baseline vs after:
   - **idle**: probe char appeared in a new line (echoed at prompt `❯`) → agent is waiting for input
   - **busy**: output unchanged (agent in raw mode) or output changed but char not echoed (consumed by a dialog) → agent is working
6. **BSpace cleanup** — send Backspace to remove the probe char from the terminal

### Key Design Decisions

- **Scans all lines, not just the last**: Claude Code's TUI has separator and status lines below the prompt `❯`. The probe char may echo on any line, so classification compares all new lines between baseline and after capture.
- **Probe-caused output filter**: If output changes shortly after a probe (`probe_wait + 2s`), it's likely caused by the probe itself (echo or BSpace). The monitor updates the hash but doesn't reset idle state.
- **"confirmed" = busy**: If the probe's `1` is consumed by a permission dialog (output changes but `1` not echoed), the agent was blocked and now resumes work. This is classified as **busy**, not idle.

### Idle Confirmation Flow

```
completion_detected (output stable 3s)
    → probe #1: idle (consecutive=1)
    → probe #2: idle (consecutive=2) → idle_confirmed ✓
    → auto-exit if configured
```

- Probes only start after `completion_detected` AND `has_worked` AND output stable for `probe_stable` (10s).
- `probe_idle_threshold` (default 2) consecutive idle probes needed to confirm.
- `probe_cooldown` (20s) between probes.
- Max probes = `threshold * 3` — gives up after too many attempts.
- If any probe returns busy, `consecutive_idle` and `completion_detected` reset.

### Auto-Confirm vs Probe: Two Paths, Same Char

Both auto-confirm and probe use `1` as the input character, but they are distinct flows:

| | Auto-Confirm | Smart Probe |
|---|---|---|
| **When** | Confirm pattern matched in output | After completion detected, output stable |
| **Send** | `1` (no Enter, no BSpace) | `1` (no Enter) + BSpace cleanup |
| **Why no BSpace** | Dialog consumes the `1` | Char may echo at prompt, needs cleanup |
| **Result** | Agent resumes work | Classify: idle or busy |

## Monitor Loop (`core/monitor.py`)

The monitor polls every ~1 second:
1. Check tmux session alive (health check every 15s)
2. Capture terminal output, compute hash
3. Filter probe-caused output changes (don't reset idle state)
4. Check auto-confirm patterns on last 32 non-empty lines, send response (with cooldown)
5. Run state detection (pattern matching on recent output)
6. Check completion detection (only after output stable for 3s)
7. Run smart probe if completion detected (confirm idle before finalizing)
8. Handle auto-exit on completion + idle confirmed (or long stable fallback)

Background mode (`--detach`) spawns `monitor_runner.py` as a subprocess with PID file at `~/.local/share/cam/pids/<agent_id>.pid`. Uses `start_new_session=True` so the monitor survives CLI exit. `cam stop/kill` sends SIGTERM to the monitor subprocess and kills the TMUX session.

### Monitor Self-Healing

Both server-side `monitor_runner.py` and standalone `camc` monitors auto-restart on crash (up to 5 times with backoff). This handles transient errors like `database is locked` from SQLite contention with many concurrent agents.

Three layers of protection:
1. **Self-healing monitors** — auto-restart with exponential backoff (5s, 10s, 15s...)
2. **`cam heal`** — CLI command that checks all running agents, restarts dead monitors (local + remote via SSH). Server cron runs hourly.
3. **`camc heal`** — standalone equivalent, installed as cron (every 30min) on remote machines by `cam sync`.

`cam heal` deduplicates remote hosts — runs `camc heal` once per unique host, not per agent.

## TMUX Session Design

- Sessions run the command directly (not via shell), so the session dies when the process exits.
- Monitor detects completion via `session_exists()` returning False OR `detect_completion()`.
- `capture-pane` returns empty during Claude's alternate screen buffer — fallback to `-a` flag.
- Suppress capture-pane failures to debug level (expected when session exits).
- `create_session` wraps command with `env -u CLAUDECODE` to prevent nested-session detection.
- `create_session` unsets `TMUX`/`TMUX_PANE` env vars so sessions can be created from inside tmux (nested tmux servers via `-S` socket are independent, but tmux blocks on the env var).
- LocalTransport accepts `env_setup` param; TransportFactory passes `config.env_setup`.

## Transport and SSH Notes

- `LocalTransport`: Direct tmux commands. `create_session` uses `subprocess.DEVNULL` (not PIPE) because tmux forks a server that inherits pipes.
- `SSHTransport`: ControlMaster socket at `/tmp/cam-ssh-{sha256(user@host:port)[:12]}` (short path avoids 108-char Unix socket limit). Non-ASCII input base64-encoded to handle POSIX locale remotes. Creates tmux sessions with `-S /tmp/cam-sockets/{session}.sock`.
- `SSHTransport` Windows support: `--shell powershell` on context, `_is_windows` flag, `_cmd_quote()` for cmd.exe double-quoting. Skip `bash -l -c` wrapping on Windows.
- ANSI stripping happens at capture level (`transport/local.py`) and again before pattern matching.

### CamcDelegate SSH Connection

`CamcDelegate._run_camc_ssh()` reuses the same ControlMaster socket as `SSHTransport` (computed from `user@host:port`). This is critical because:
- Some remote machines (NVIDIA containers) only accept SSH via specific ports (e.g., 3859, 3706, 3422) — not port 22.
- ControlMaster sharing means CamcDelegate piggy-backs on SSHTransport's already-authenticated persistent connection.
- Without ControlMaster, each camc subprocess call would need fresh SSH authentication, which fails on machines that require jump hosts or Kerberos.

**Non-ASCII SSH handling**: Remote shells (csh/tcsh on NVIDIA machines) mangle non-ASCII bytes in command arguments. For non-ASCII args (e.g., Chinese prompts), `_run_camc_ssh` pipes a bash script via stdin (`ssh -T target bash` with `input=script`) to bypass the login shell.

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

## Standalone camc (`src/camc_pkg/`, built to `dist/camc`)

Single-file, stdlib-only CLI (Python 3.6+) for machines without the full cam package. Deployed to remotes by `cam sync`. Source in `src/camc_pkg/`, built to `dist/camc` via `python3 build_camc.py`. Copy `dist/camc` to `src/camc` before `cam sync` (sync source is `src/camc`).

- **Self-contained**: all detection logic, TOML parser, and adapter configs (claude/codex/cursor) embedded inline. No pip install needed.
- **Commands**: `init`, `run`, `list`, `logs`, `attach`, `stop`, `add`, `rm`, `status`, `heal`, `capture`, `send`, `key`, `version`.
- **`camc run`**: creates tmux session, handles startup auto-confirm, spawns background monitor subprocess.
- **PATH passthrough**: if no `env_setup` in `~/.cam/context.json`, injects the caller's `PATH` into the tmux session so tools like `claude` are found.
- **`context.json`**: optional config at `~/.cam/context.json` with `env_setup` (shell commands run before agent launch). Deployed by `cam sync` from the server context's `machine.env_setup`.
- **Agent store**: JSON file at `~/.cam/agents.json` (not SQLite — keeps it stdlib-only).
- **Monitor logs**: `~/.cam/logs/monitor-<id>.log`. Stderr of monitor subprocess goes to `/tmp/camc-<id>.log`.
- **`camc heal`**: checks all running agents, restarts dead monitors. Filters by hostname — only touches agents belonging to the current machine. Can be cron'd.
- **`camc status`**: machine-readable JSON output with hash-based conditional responses (used by cam server pull mode).
- **`camc capture/send/key`**: operates on agents by ID or tmux session name. If agent is not in `agents.json`, falls back to using the ID as a tmux session name directly (supports sessions created by SSHTransport before camc delegation).

### camc Agent ID Generation

Agent IDs are 8 hex characters generated via `uuid5(NAMESPACE_DNS, hostname + timestamp + random)[:8]`. This ensures:
- **Cluster-safe**: hostname component prevents collisions when multiple machines share `~/.cam/agents.json` via NFS.
- **Format-compatible**: 8 chars, same as before. All existing code (tmux session names, prefix matching, display) works unchanged.

### camc Cluster / Shared-Disk Support

In environments where multiple machines share the same home directory (NFS clusters like NVIDIA PDX containers), `~/.cam/agents.json` is shared across all machines but tmux sessions are per-machine (`/tmp/cam-sockets/` is local).

- **`hostname` field**: `camc run` records `socket.gethostname()` in each agent record.
- **`camc list`**: filters to only show agents from the current hostname.
- **`camc heal`**: skips agents from other hostnames (won't mistakenly mark remote-machine agents as dead).
- **Hostname comparison**: uses `_is_same_host()` which compares short hostnames (before first `.`) to handle FQDN vs short name inconsistencies (e.g., `bpmpfw` vs `bpmpfw.nvidia.com`). Used by both camc (local filtering) and CamcPoller (prevents importing agents with wrong machine_host).
- **CamcPoller NFS guard**: when polling machine X, skips agents whose `hostname` ≠ X. For agents that pass, corrects `machine_host/port` if they don't match the polling machine — self-heals prior cross-contamination without manual intervention.
- **`camc capture/send/key`**: tmux sockets in `/tmp/cam-sockets/` provide natural per-machine isolation — can only capture sessions running on the current machine.

### camc Tmux Socket Paths

camc searches multiple socket directories via `_find_tmux_socket()`:
1. `/tmp/cam-sockets/{session}.sock` — primary (matches SSHTransport convention)
2. `/tmp/cam-agent-sockets/{session}.sock` — cam-agent Go binary
3. `~/.local/share/cam/sockets/{session}.sock` — cam server local sockets

This allows camc to operate on sessions created by any transport backend.

## v3 Migration Plan

Full plan: `docs/migration-v3-unified.md`

### Current Status: Phase 0 Complete

Phase 0 (schema unification) is done. camc and cam use the same agent record
field names. Next phases:

1. **Phase 1: Machine Layer + JSON Storage** — introduce `machines.json` as
   first-class entity, replace SQLite with JSON, add machine/context commands
   to camc, sync/heal by machine instead of context.
2. **Phase 2: Directory Consolidation** — all files under `~/.cam/`, eliminate
   `~/.local/share/cam/` and `/tmp/camc-*` scatter.
3. **Phase 3: Migration Tool** — `camc migrate` converts SQLite → JSON.
4. **Phase 4: CLI Unification** — camc becomes the full CLI, rich is optional
   (`pip install rich` for pretty tables, ANSI fallback without).
5. **Phase 5: cam serve Simplification** — thin HTTP/WS wrapper reading JSON
   directly, no more CamcPoller/SQLite.

### Key Design Decisions

- **Zero hard dependencies**: camc is stdlib-only, single file, Python 3.6+.
- **Rich is optional**: `try: import rich` with ANSI fallback. Same code, same
  commands — just simpler rendering without rich.
- **Machine is first-class**: `machines.json` defines hosts. Contexts reference
  machines by name. Sync/heal/poll iterate machines, not contexts.
- **No SQLite**: JSON + fcntl locking. Data volume is tiny (tens of agents,
  a few machines). Events auto-rotate at 30 days.
- **Logs under ~/.cam/**: `~/.cam/logs/` and `~/.cam/pids/` for everything.
  No more `/tmp/camc-*.log` scatter. Sockets stay in `/tmp/` (108-char limit).
- **cam serve is optional**: only needed for web/mobile. `pip install cam[server]`.

## cam-agent Go Binary

- Located at `src/cam-agent/`. Single binary, cross-compiled for linux/windows/darwin.
- CLI subcommands over SSH: `ping`, `session create/exists/kill/capture/send/key`, `file list/read/write`, `session log-start/log-read`.
- Linux backend: thin wrapper around real tmux (sockets at `/tmp/cam-agent-sockets/`).
- ANSI stripping done in Go (agent-side) — capture returns plain text.
- Python side: `AgentTransport` in `src/cam/transport/agent.py`. Reuses SSH ControlMaster pooling.
- Build: `cd cam-agent && make build`.

## AI News Digest (Follow Builders Skill)

Get curated AI industry news from top builders (founders, researchers, engineers) on X/Twitter and YouTube podcasts.

### Quick Use

- **On-demand**: Run the follow-builders skill from `~/skills/follow-builders/`
  ```bash
  cd ~/skills/follow-builders/scripts && node prepare-digest.js 2>/dev/null
  ```
  This outputs JSON with tweets, podcasts, and prompts. The agent then remixes it into a readable digest.

- **Config**: `~/.follow-builders/config.json` — language (`en`/`zh`/`bilingual`), frequency, delivery method.
- **Sources**: Curated centrally, updated automatically. Tracks ~10 AI builders on X and select podcasts.
- **Delivery**: `stdout` (default), Telegram, or email. OpenClaw users get automatic channel delivery via cron.
