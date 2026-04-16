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

## MANDATORY Smoke Tests Before Every Commit

Unit tests do NOT catch CLI type annotation bugs or missing method calls.
After ANY code change, run ALL of these before committing:

```bash
# 1. List works
cam list --last 3

# 2. Attach works (Ctrl+B D to detach)
cam attach <any-running-agent-id>

# 3. Status works
cam status <any-agent-id>

# 4. Update works
cam update <any-agent-id> --name smoke-test
cam update <any-agent-id> --name <original-name>

# 5. If tag feature changed:
cam update <any-agent-id> --tag SMOKE
cam list --tag SMOKE
cam update <any-agent-id> --untag SMOKE

# 6. camc rebuild (if camc_pkg changed):
python3 build_camc.py
camc list
```

If ANY command crashes, fix it before committing.

## Known Typer Pitfalls

- **Never use `Optional[list[str]]`** with typer — crashes at import time.
  Use `Optional[str]` and split commas in the function body.
- **Never use `ContextStore.get_by_name()`** — does not exist. Use `get()`.
- Test the actual CLI binary, not just Python imports — typer evaluates
  type annotations at app startup, not at call time.

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
- `[monitor]` — cooldowns, auto_exit, exit_action, busy_pattern, done_pattern

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

## Monitor Loop (`camc_pkg/monitor.py`)

Pure screen-based, tool-agnostic design. No characters sent for idle detection — only for auto-confirm and stuck fallback. The monitor polls every ~1 second:

### Step 1: Health Check (every 15s)

- `tmux_session_exists(session)` — is the tmux session alive?
- Gone → `completed` if `has_worked`, else `failed` → exit monitor

### Step 2: Capture + Hash

- `capture_tmux(session)` → raw terminal text
- **Strip last line before hashing** — Claude's status bar (`? for shortcuts` ↔ `esc to interrupt`) alternates every cycle, causing false hash changes. Stripping it ensures idle detection works.
- MD5 hash → `changed = (hash != prev_hash)`

### Step 2b: Auxiliary Screen Signals (busy/done)

Optional patterns from TOML `[monitor]` config, checked on last 5 non-empty lines:

- **`busy_pattern`** (Claude: `ing[.…]{1,3}`, e.g. "Creating…", "Smooshing…"): Agent is definitely working → skip auto-confirm, reset idle timer, set `has_worked=True`.
- **`done_pattern`** (Claude: `ed\s+for\s+\d+[smh]`, e.g. "Crunched for 36s"): Task just completed → fast-track idle to 5s (instead of 60s) when bare prompt also visible.

Each tool defines its own patterns in TOML. If not configured, falls back to standard 60s idle.

### Step 3: Auto-Confirm (cooldown 5s)

Detects permission dialogs and sends response. All rules defined in TOML `[[confirm]]`.

**Skip conditions** (either skips auto-confirm):
- **Busy signal**: `busy_pattern` matched → agent is working, not at a dialog
- **Bare prompt**: a line in the last 5 that is JUST `❯`/`>`/`›` → agent at input prompt, confirm text is stale history

Detection checks the **last 8 non-empty lines** only. Real dialogs appear at screen bottom; checking full output causes false positives on agent prose.

**Claude Confirm Rules** (defined in TOML):

| Pattern | Response | Scenario |
|---|---|---|
| `Do you want to proceed` | `1` (no Enter) | Numbered permission menu (`1. Yes / 2. No`) |
| `1. Yes` / `1. Allow` | `1` (no Enter) | Numbered permission menu |
| `Allow once/always` | `1` (no Enter) | Claude 4.x+ Ink select menu |
| `(y/n)` / `[Y/n]` | `y` + Enter | y/n confirmation prompt |

### Step 4: State Detection

Regex on recent 2000 chars → `planning`/`editing`/`testing`/`committing`. State change sets `has_worked=True`.

### Step 5: Output Change

Hash changed → reset idle timer (`last_change = now`), clear `idle_confirmed`.

### Step 6: Idle Detection

**Standard**: `has_worked` + screen hash stable 60s + prompt visible → idle confirmed.
**Fast-track**: `done_pattern` matched + bare prompt + 5s stable → idle confirmed.

### Step 6b: Stuck Fallback

`has_worked` + screen frozen 120s + prompt NOT visible → send `1` to try to unblock. The character is configurable via `probe_char` in TOML.

### Step 7: Auto-Exit

`idle_confirmed` + user not attached + `auto_exit` enabled → kill session → mark completed.

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

## TeaSpirit — Teams ↔ CAM Bridge

### Overview

TeaSpirit (`teaspirit/`) connects Microsoft Teams to CAM/CAMC, allowing phone-based agent management. Two versions exist: TypeScript (`workspace/cam/teaspirit/`) and Python (`~/test/aicli/teams-cam-bridge/`).

### Modes

- **Channel mode**: Monitor a Teams channel, relay agent output
- **Chat mode**: Personal 1-on-1 with agent
- **Group mode** (`MODE=group`): Multiple users in a Teams group chat, each can independently attach to agents
- **Attach mode**: Permanently attached to one agent (interactive chatbot)
- **Assistant mode**: Extracts complete answers from tmux capture (not real-time streaming), no duplicates or missing paragraphs

### Group Chat Protocol

In group chat, messages arrive as `[Name] text`. The agent must:
1. Identify the sender by name prefix
2. Respond to that person's question specifically
3. Maintain per-sender attach state (`Map<senderId, attachState>`)

### Group Chat Auth Constraints

All CLI tools (calendar-cli, outlook-cli, etc.) authenticate as the TeaSpirit host user. When other users ask personal questions in group chat:

| Data Type | Accessible? | Reason |
|-----------|-------------|--------|
| Other user's public calendar | Yes | `calendar-cli find --user <email>` if shared |
| Other user's email | No | No cross-user permission |
| NVBugs | Yes | Public query API |
| Helios directory | Yes | Organization-wide |
| Confluence / Glean | Yes | Organization-wide content |
| Other user's private calendar | No | Requires their auth token |

**Strategy**: For queries the host token can't serve, respond honestly ("I don't have permission to access your calendar"). For organization-wide data (bugs, docs, directory), answer normally.

### TeaSpirit Commands (Python version `dispatch_direct`)

Users type `//command` in Teams:

| Command | Action |
|---------|--------|
| `//screen` | Capture current screen |
| `//esc` | Send Escape key |
| `//key` | Enter key submenu |
| `//help` | Show help card |
| `//tab`, `//c-c`, `//y`, `//n` | Send shortcut keys |

### Multi-Machine Management (Planned)

TeaSpirit will manage all machines' agents via SSH → camc (no cam server dependency):

```
Teams → TeaSpirit → SSH → Machine A: camc --json list/capture/send
                  → SSH → Machine B: camc --json list/capture/send
                  → local: camc --json list/capture/send
```

Config: `~/.cam/teaspirit/machines.json`. Agent list grouped by machine. Phase 1: multi-machine list. Phase 2: cross-machine attach. Phase 3: run/stop/heal from Teams.

### Multi-Instance

- Default config: `~/.cam/teaspirit/.env`
- Named instances: `~/.cam/teaspirit/<name>/.env`
- `teaspirit start <name>` / `teaspirit stop <name>`
