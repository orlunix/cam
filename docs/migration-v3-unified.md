# CAM v3: Unified Architecture Migration Plan

> Date: 2026-03-26
> Status: Planned
> Supersedes: docs/architecture-v2-camc-engine.md

## 1. Goal

Unify cam and camc into a single zero-dependency codebase with optional rich UI
and optional server layer. Eliminate SQLite, Pydantic, typer as hard dependencies.
After migration, the system has two modes:

```
┌──────────────────────────────────────────────────────┐
│  cam serve (optional, pip install cam[server])       │
│  FastAPI + WebSocket + Relay                         │
│  For web/mobile clients only                         │
├──────────────────────────────────────────────────────┤
│  camc (zero-dependency, single file, Python 3.6+)    │
│  Full CLI: machines, contexts, agents, SSH, monitor  │
│  Optional: pip install rich → pretty tables/panels   │
├──────────────────────────────────────────────────────┤
│  ~/.cam/ (unified data directory)                    │
│  JSON files, shared by both camc and cam serve       │
└──────────────────────────────────────────────────────┘
```

## 2. Current Problems

### 2.1 Split storage
- cam: SQLite at `~/.local/share/cam/cam.db` (142MB, 800K events)
- camc: JSON at `~/.cam/agents.json` (312KB)
- CamcPoller syncs camc→SQLite every 5s, adding latency and complexity

### 2.2 No Machine layer
- Machine config (host/user/port) is embedded in each Context
- `cam sync`, `CamcPoller`, `cam heal` all manually deduplicate by host
- Changing a port means editing every context on that machine

### 2.3 Scattered files
```
~/.local/share/cam/          cam data (SQLite 142MB, logs 2.7GB)
~/.cam/                      camc data (agents.json, configs, logs)
/tmp/camc-<id>.{log,pid}     camc monitor stderr and PID files
/tmp/cam-ssh-<hash>          SSH ControlMaster sockets
/tmp/cam-sockets/            tmux sockets
```

### 2.4 Hard dependencies for CLI
- typer (CLI framework) — only used for argument parsing
- rich (output formatting) — only used for tables/panels/colors
- pydantic (data models) — only used for validation/serialization
- All replaceable with stdlib + optional rich

### 2.5 Duplicate codebases
- cam and camc both implement agent management, monitoring, state detection
- Bugs fixed in one may not be fixed in the other
- Two different storage formats requiring conversion (CamcPoller)

## 3. Target Architecture

### 3.1 Directory layout

```
~/.cam/                              # Single data root
├── machines.json                    # Machine definitions
├── contexts.json                    # Context definitions (reference machine by name)
├── agents.json                      # Agent records (unified schema)
├── agents.json.lock                 # fcntl write lock
├── events.jsonl                     # Event log (auto-rotate 30 days)
├── camc                             # Single-file CLI binary
├── configs/                         # Adapter TOML configs
│   ├── claude.toml
│   ├── codex.toml
│   └── cursor.toml
├── logs/                            # All logs (unified)
│   ├── monitor-<id>.log             # Monitor runtime log
│   ├── output-<id>.log              # Agent terminal output snapshot (optional)
│   └── serve.log                    # cam serve log (optional)
└── pids/                            # Monitor PID files (unified)
    └── <id>.pid

/tmp/cam-ssh-<hash>                  # SSH ControlMaster sockets (must be /tmp)
/tmp/cam-sockets/                    # tmux sockets (must be /tmp, per-machine local)
```

**Why /tmp for sockets:** Unix socket paths have a 108-char limit. Home directory
paths (especially on NFS clusters like `/home/scratch.hren_gpu/...`) exceed this.
tmux sockets must also be local per-machine (not shared via NFS).

### 3.2 machines.json

```json
[
  {
    "name": "local",
    "type": "local"
  },
  {
    "name": "pdx-110",
    "type": "ssh",
    "host": "pdx-110.nvidia.com",
    "user": "hren",
    "port": 3859,
    "env_setup": "source /opt/tools/env.sh",
    "key_file": null
  },
  {
    "name": "bpmpfw",
    "type": "ssh",
    "host": "bpmpfw.nvidia.com",
    "user": "hren",
    "port": 22
  }
]
```

Operations that target machines (sync, heal, poll) iterate this list directly.
No deduplication needed.

### 3.3 contexts.json

```json
[
  {
    "name": "falcon-rtl",
    "machine": "pdx-110",
    "path": "/home/scratch.hren_gpu/p5d1_sc0/hw/nvip/ip/peregrine"
  },
  {
    "name": "bpmpfw-fw",
    "machine": "bpmpfw",
    "path": "/home/hren/bpmpfw-project"
  },
  {
    "name": "local-cam",
    "machine": "local",
    "path": "/home/hren/.openclaw/workspace/cam"
  }
]
```

Context references machine by name. Changing a machine's port updates all
contexts on that machine automatically.

### 3.4 agents.json (unified schema, already done)

See `src/cam/core/agent_schema.py` for the canonical field definitions.
Both cam and camc already use this format as of Phase 0.

### 3.5 Rich as optional dependency

```python
# In formatters or output code:
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
```

| rich feature        | ANSI fallback                             |
|---------------------|-------------------------------------------|
| `Table`             | `%-10s %-16s ...` + ANSI color codes      |
| `Panel`             | `─` border chars + indented lines         |
| `Console.print()`   | `\033[32m✓\033[0m` style ANSI escapes     |
| `Text(style=...)`   | ANSI color wrapper function               |
| `Progress`          | Simple `... processing` print             |

User's local machine: `pip install rich` for full UI.
Remote machines / zero-dep: automatic ANSI fallback, still looks good.

Same code, same commands, same output structure. Just simpler rendering.

## 4. Migration Phases

### Phase 0: Schema Unification (DONE ✓)

- [x] Unified agent record field names (session→tmux_session, path→context_path,
      monitor_pid→pid, flat fields→nested task)
- [x] `_tf()` / `_sf()` helpers for backward-compatible field access
- [x] `_agent_to_cam_json()` handles both old and new format
- [x] CamcPoller `_camc_agent_to_model()` handles both formats
- [x] Rebuilt single-file camc (`dist/camc`)
- [x] All 416 tests pass

Files changed:
- `src/cam/core/agent_schema.py` (NEW) — canonical schema definition
- `src/camc_pkg/cli.py` — new format in cmd_run/cmd_add, _tf/_sf helpers
- `src/camc_pkg/storage.py` — tmux_session lookup support
- `src/camc_pkg/monitor.py` — both-format field access
- `src/camc_pkg/scheduler.py` — new format in agent records
- `src/camc_pkg/utils.py` — _kill_monitor reads both pid and monitor_pid
- `src/cam/core/camc_poller.py` — simplified conversion, both formats

### Phase 1: Machine Layer + JSON Storage

**Goal:** Replace SQLite with JSON files. Introduce Machine as first-class entity.

#### 1a. Machine and Context JSON stores

New files:
- `src/camc_pkg/machine_store.py` — read/write `~/.cam/machines.json` (fcntl locking)
- `src/camc_pkg/context_store.py` — read/write `~/.cam/contexts.json` (fcntl locking)

Same pattern as existing `AgentStore` (JSON + fcntl + atomic write).

#### 1b. CLI commands for machine management

```bash
camc machine list                              # List all machines
camc machine add pdx-110 --host pdx-110.nvidia.com --user hren --port 3859
camc machine rm pdx-110
camc machine edit pdx-110 --port 3860          # Update in-place

camc context list                              # List all contexts
camc context add falcon-rtl --machine pdx-110 --path /home/...
camc context rm falcon-rtl
```

#### 1c. Sync by machine (not context)

```bash
camc sync                     # Sync camc + configs to all SSH machines
camc sync pdx-110             # Sync to specific machine
```

Iterates `machines.json` directly. No deduplication needed.

#### 1d. Heal by machine

```bash
camc heal                     # Heal local + SSH to all remote machines
```

Local: check local agents. Remote: `ssh machine "camc heal"` for each SSH machine.

#### 1e. cam serve reads JSON directly

- Remove CamcPoller (no more SQLite sync loop)
- cam serve reads `~/.cam/agents.json` directly for local state
- For remote machines, SSH to camc (same as today, but simpler)
- Remove cam's SQLite stores (AgentStore, ContextStore, EventStore)

### Phase 2: Directory Consolidation

**Goal:** All files under `~/.cam/`, eliminate scattered paths.

#### 2a. Log unification

- Monitor logs: `~/.cam/logs/monitor-<id>.log` (camc already does this)
- Monitor stderr: redirect to same log file (eliminate `/tmp/camc-<id>.log`)
- PID files: `~/.cam/pids/<id>.pid` (eliminate `/tmp/camc-<id>.pid`)

Code changes:
- `camc_pkg/cli.py`: `stderr=open(...)` path → `~/.cam/logs/`
- `camc_pkg/cli.py`: PID path → `~/.cam/pids/`
- `camc_pkg/monitor.py`: already uses `~/.cam/logs/`

#### 2b. Events auto-rotate

- On startup: `EventStore.rotate(max_age_days=30)`
- On heal: same rotation
- Optional: split by month `events-2026-03.jsonl` for easy cleanup

#### 2c. Remove old cam paths

After migration, these are no longer used:
- `~/.local/share/cam/cam.db` (SQLite)
- `~/.local/share/cam/cam.db-shm`, `cam.db-wal`
- `~/.local/share/cam/logs/` (old cam logs)
- `~/.local/share/cam/pids/` (old cam PIDs)
- `~/.local/share/cam/sockets/` (unused)
- `~/.local/share/cam/camc_managed.json`

### Phase 3: Migration Tool

```bash
camc migrate
```

Automated steps:

1. **SQLite → machines.json**: Read contexts from `cam.db`, extract unique
   (host, user, port, env_setup) → write `machines.json`
2. **SQLite → contexts.json**: Read contexts, replace inline machine config
   with machine name reference → write `contexts.json`
3. **Agent merge**: Read active agents from `cam.db`, merge into
   `agents.json` (skip duplicates by ID)
4. **Log migration**: Move `~/.local/share/cam/logs/monitor-*.stderr` →
   `~/.cam/logs/` (only for active agents)
5. **PID migration**: Move `~/.local/share/cam/pids/*.pid` → `~/.cam/pids/`
6. **Cleanup /tmp**: Remove `/tmp/camc-*.{log,pid}` empty files
7. **Verify**: Check all running agents' monitors and tmux sessions are alive
8. **Report**: Print summary, suggest `rm -rf ~/.local/share/cam/` if clean

**Events are NOT migrated** — 800K rows with no practical value. Fresh start
with `events.jsonl` + auto-rotate.

### Phase 4: CLI Unification (DONE ✓)

**Goal:** camc becomes the full CLI, cam CLI wrapper is optional.

#### 4a. camc entry point in pyproject.toml

- `camc = "camc_pkg.cli:main"` added as entry point
- `camc_pkg` added to build targets
- `cam` CLI still works as before (with full deps)

#### 4b. Rich as optional dependency (DONE)

- `rich` moved from hard dependency to `[ui]` optional
- `pip install cam[ui]` for rich tables/panels
- `pip install cam[all]` for everything

#### 4c. Formatters with fallback (DONE)

`src/camc_pkg/formatters.py` created with:

```python
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    _HAS_RICH = True
    _console = Console()
except ImportError:
    _HAS_RICH = False
    _console = None

def print_table(headers, rows, title=None):
    if _HAS_RICH:
        table = Table(title=title)
        for h in headers:
            table.add_column(h)
        for row in rows:
            table.add_row(*[str(c) for c in row])
        _console.print(table)
    else:
        # ANSI fallback
        widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                  for i, h in enumerate(headers)]
        fmt = "  ".join("%%-%ds" % w for w in widths)
        if title:
            print("\033[1m%s\033[0m" % title)
        print(fmt % tuple(headers))
        print("-" * (sum(widths) + 2 * (len(headers) - 1)))
        for row in rows:
            print(fmt % tuple(str(c) for c in row))

def print_panel(lines, title=None):
    if _HAS_RICH:
        from rich.panel import Panel
        _console.print(Panel("\n".join(lines), title=title, border_style="cyan"))
    else:
        width = max((len(l) for l in lines), default=40) + 4
        border = "\u2500" * width
        if title:
            print("\u2500\u2500 %s %s" % (title, "\u2500" * (width - len(title) - 4)))
        else:
            print(border)
        for l in lines:
            print("  %s" % l)
        print(border)
```

### Phase 5: cam serve Simplification

**Goal:** cam serve becomes a thin HTTP/WS wrapper around camc's JSON files.

- Remove `CamcPoller` — no more SQLite sync loop
- Read `~/.cam/agents.json` directly for local agents
- For remote agents: SSH → `camc --json list/status/capture`
- Read `~/.cam/machines.json` and `contexts.json` for config
- FastAPI routes call camc functions directly (no AgentManager needed)
- WebSocket events: watch `events.jsonl` for new lines (inotify or poll)

## 5. Dependency Matrix (After Migration)

| Mode | Dependencies | Install |
|---|---|---|
| `camc` (CLI only) | None (stdlib) | Single file copy |
| `camc` + pretty UI | `rich` | `pip install rich` |
| `cam serve` (API) | `fastapi`, `uvicorn` | `pip install cam[server]` |
| Full install | `rich`, `fastapi`, `uvicorn` | `pip install cam[all]` |

## 6. Backward Compatibility

### Agent records
- `_tf()` / `_sf()` helpers read both old flat format and new nested format
- `AgentStore.get()` matches by `tmux_session` and legacy `session`
- Old agents.json records work without modification

### Migration is optional
- `camc migrate` does the conversion
- Old cam serve continues to work until migration
- No data loss — SQLite is read-only during migration

### Remote machines
- `camc sync` deploys the new single-file camc
- Old camc on remotes handles both field formats (backward compat in Phase 0)
- No coordinated upgrade needed — sync machines one at a time

## 7. File Changes Summary

### New files
- `src/camc_pkg/machine_store.py` — machines.json read/write
- `src/camc_pkg/context_store.py` — contexts.json read/write
- `src/camc_pkg/formatters.py` — rich/ANSI dual-mode output
- `src/camc_pkg/migrate.py` — SQLite → JSON migration tool

### Modified files
- `src/camc_pkg/cli.py` — add machine/context/sync/migrate commands
- `src/camc_pkg/__init__.py` — add MACHINES_FILE, CONTEXTS_FILE constants
- `src/cam/api/` — simplify to read JSON directly
- `build_camc.py` — include new modules in build order

### Removed files (after migration)
- `src/cam/storage/agent_store.py` — replaced by camc_pkg/storage.py
- `src/cam/storage/context_store.py` — replaced by camc_pkg/context_store.py
- `src/cam/storage/database.py` — SQLite no longer needed
- `src/cam/core/camc_poller.py` — no more polling, read JSON directly
- `src/cam/cli/formatters.py` — replaced by camc_pkg/formatters.py
- `src/cam/core/models.py` — Pydantic models no longer needed (dict-based)

## 8. Risk Assessment

| Risk | Impact | Mitigation |
|---|---|---|
| Concurrent JSON writes (many monitors) | Data corruption | fcntl locking (proven in camc) |
| NFS + fcntl locking | Lock may not work on NFS | hostname filter in agents.json prevents cross-machine writes |
| Large events.jsonl | Slow reads | Auto-rotate 30 days + monthly split |
| Rich import on remote | ImportError | Graceful fallback, already handled |
| Migration interruption | Partial state | Migration is idempotent, re-run safe |
| Running agents during migration | Agent disruption | Migration only reads SQLite, doesn't touch tmux |
