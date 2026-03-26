# CAM Architecture v2: camc as Engine

> Date: 2026-03-25
> Status: Proposed

## 1. Motivation

The current architecture has two parallel implementations of agent management:

- **cam** — full Python package (pip install), manages agents via AgentManager/AgentMonitor/Transport classes, stores state in SQLite
- **camc** — standalone single-file CLI (stdlib-only), manages agents via tmux directly, stores state in JSON

This duplication leads to:
- Two codebases doing the same thing (tmux management, state detection, auto-confirm, monitor loop)
- Divergent behavior and bugs fixed in one but not the other
- Complex SSH transport in cam that remotely drives tmux commands (multiple SSH calls per monitor cycle)

## 2. Proposed Architecture

**camc becomes the universal agent management engine. cam becomes a thin server/UI layer on top.**

```
 ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
 │  cam CLI  │  │  Web UI   │  │Mobile App │  │   Teams   │
 │ (global)  │  │   (PWA)   │  │ (Android) │  │   Bot     │
 └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
       │               │              │               │
       │            REST/WS        REST-over-WS    Webhook/
       │               │           via Relay       REST API
       │               │              │               │
       │         ┌─────┴─────┐  ┌────┴─────┐         │
       │         │  Direct   │  │  Relay   │         │
       │         │  HTTP/WS  │  │ (Public) │         │
       │         └─────┬─────┘  └────┬─────┘         │
       │               │             │                │
       │               └──────┬──────┘                │
       │                      │                       │
 ┌─────┴──────────────────────┴───────────────────────┴─────┐
 │                                                          │
 │                   cam serve                              │
 │              (aggregation layer)                         │
 │                                                          │
 │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐ │
 │  │ REST API │ │WebSocket │ │  Relay   │ │  Teams     │ │
 │  │          │ │  Events  │ │Connector │ │ Integration│ │
 │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └─────┬──────┘ │
 │       └─────────────┴────────────┴─────────────┘        │
 │                         │                                │
 │              ┌──────────┴──────────┐                     │
 │              │       SQLite        │                     │
 │              │  - agents (cache)   │                     │
 │              │  - contexts/machines│                     │
 │              │  - events/history   │                     │
 │              └──────────┬──────────┘                     │
 │                         │                                │
 │   Does NOT manage tmux directly.                         │
 │   Delegates all agent ops to camc.                       │
 │                                                          │
 └──────┬──────────────────┬──────────────────┬─────────────┘
        │                  │                  │
   read JSON          SSH + CLI          SSH + CLI
   directly           (JSON stdout)      (JSON stdout)
        │                  │                  │
 ┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴──────┐
 │    camc     │   │    camc     │   │    camc     │
 │   (local)   │   │  (remote A) │   │  (remote B) │
 │             │   │             │   │             │
 │ ┌─────────┐ │   │ ┌─────────┐ │   │ ┌─────────┐ │
 │ │  tmux   │ │   │ │  tmux   │ │   │ │  tmux   │ │
 │ │ sessions│ │   │ │ sessions│ │   │ │ sessions│ │
 │ └─────────┘ │   │ └─────────┘ │   │ └─────────┘ │
 │ ┌─────────┐ │   │ ┌─────────┐ │   │ ┌─────────┐ │
 │ │ monitor │ │   │ │ monitor │ │   │ │ monitor │ │
 │ │ detect  │ │   │ │ detect  │ │   │ │ detect  │ │
 │ │ confirm │ │   │ │ confirm │ │   │ │ confirm │ │
 │ │ probe   │ │   │ │ probe   │ │   │ │ probe   │ │
 │ └─────────┘ │   │ └─────────┘ │   │ └─────────┘ │
 │ ┌─────────┐ │   │ ┌─────────┐ │   │ ┌─────────┐ │
 │ │  JSON   │ │   │ │  JSON   │ │   │ │  JSON   │ │
 │ │  store  │ │   │ │  store  │ │   │ │  store  │ │
 │ └─────────┘ │   │ └─────────┘ │   │ └─────────┘ │
 └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
        │                 │                  │
   camc CLI          camc CLI           camc CLI
   (local ops)       (SSH into          (SSH into
                      machine)           machine)

 Each camc has its own CLI for direct local operations.
 Users can SSH into any machine and use camc directly:
   $ ssh remote-A
   $ camc list              # agents on this machine only
   $ camc logs abc1 -f      # follow local agent output
   $ camc attach abc1       # attach to local tmux session
```

### Key Principles

1. **camc is a pure local agent manager** — it only knows about its own machine. No awareness of other camc instances.
2. **camc instances do NOT communicate with each other** — there is no peer-to-peer protocol.
3. **cam serve is the only aggregation point** — it polls all camc instances, merges status into SQLite, and pushes to Web UI via WebSocket.
4. **Even the local machine runs camc** — cam serve reads the local camc's `~/.cam/agents.json` directly (no SSH needed).

## 3. Storage Design

### cam serve: SQLite (unchanged)

cam serve keeps its existing SQLite database as the aggregation layer:

- **agents** — cached state from all camc instances (source of truth is each camc's JSON)
- **contexts** — machine configurations (host, port, env_setup)
- **agent_events** — state change history, auto-confirm events, completion events
- **schema_version** — migration tracking

cam serve generates events by diffing: each poll cycle compares previous state with current camc output. State changes (e.g. `editing` → `testing`) are recorded as events in SQLite.

### camc: JSON files (unchanged)

Each camc instance stores data in `~/.cam/`:

```
~/.cam/
  agents.json          # Agent state — the source of truth
  agents.json.lock     # File lock for concurrent access
  events.jsonl         # Append-only event history (NEW)
  context.json         # Machine config (env_setup, etc.)
  configs/
    claude.toml        # Adapter configs
    codex.toml
    cursor.toml
  logs/
    monitor-<id>.log   # Monitor process logs
```

**agents.json** — current agent state (flat list, same as today):
```json
[
  {
    "id": "a889245f",
    "tool": "claude",
    "session": "cam-a889245f",
    "status": "running",
    "state": "editing",
    "prompt": "fix the tests",
    "path": "/home/user/project",
    "name": "fix-tests",
    ...
  }
]
```

**events.jsonl** — append-only event log (NEW):
```
{"agent_id":"a889","ts":"2026-03-25T08:30:00Z","type":"state_change","detail":{"from":"initializing","to":"editing"}}
{"agent_id":"a889","ts":"2026-03-25T08:31:15Z","type":"auto_confirm","detail":{"pattern":"trust dialog"}}
{"agent_id":"a889","ts":"2026-03-25T08:35:00Z","type":"state_change","detail":{"from":"editing","to":"testing"}}
{"agent_id":"a889","ts":"2026-03-25T08:36:00Z","type":"completed","detail":{"reason":"task done"}}
```

- Monitor appends events during operation
- `camc history <id>` reads and filters this file
- cam serve can pull incremental events via `camc --json history --since <timestamp>`
- Periodic rotation (retain last 30 days)

### Why this split

| | cam serve (SQLite) | camc (JSON) |
|---|---|---|
| Role | Aggregation cache | Source of truth |
| Scope | All machines | Single machine |
| Writers | cam serve only | camc monitor only |
| Concurrency | Multiple API readers | File lock (simple) |
| History | Full event history across all machines | Local events.jsonl |
| Survives restart | Yes | Yes |

No data duplication problem: cam serve's SQLite is a **derived cache**, camc's JSON is the **source of truth**. If they diverge, cam serve re-polls and self-corrects.

## 4. camc Scope

camc becomes a full-featured agent manager distributed as a single binary (via PyInstaller):

```
src/camc/
  __main__.py        # CLI entry point
  cli.py             # argparse commands (run, list, stop, kill, logs, attach, status, heal, apply, history)
  transport/
    local.py         # Local tmux operations
    ssh.py           # SSH to other camc instances (paramiko)
  core/
    manager.py       # Agent lifecycle (create, start, stop, kill)
    monitor.py       # Background monitor loop
    detection.py     # State detection, completion detection, auto-confirm
    probe.py         # Idle probe
    scheduler.py     # DAG task graph, topological sort, parallel execution
  adapters/
    loader.py        # TOML adapter config loader
    configs/         # Embedded adapter TOML files (claude, codex, cursor)
  storage.py         # JSON file store (~/.cam/agents.json) + events.jsonl
  models.py          # dataclass models (no pydantic dependency)
```

### camc capabilities:
- **Agent lifecycle**: run, stop, kill, list, status, logs, attach
- **Monitoring**: background monitor with auto-restart, state detection, auto-confirm, probe
- **Event history**: append-only events.jsonl, `camc history` command
- **SSH transport**: manage agents on remote machines by SSH-ing to remote camc
- **DAG scheduling**: `camc apply tasks.yaml` — topological sort, parallel execution, dependency tracking
- **Self-healing**: `camc heal` — check monitors, restart dead ones
- **Zero dependencies at runtime**: PyInstaller single binary, drop on any Linux machine

### Build & Distribution

```bash
# Development
pip install -e ".[dev]"
pytest

# Build single binary
pyinstaller --onefile -n camc src/camc/__main__.py

# Deploy to remote
scp camc remote:~/.local/bin/camc
# or
cam sync  # auto-deploys to all remote contexts
```

## 5. Communication Protocol

### cam serve ↔ remote camc: SSH + CLI (JSON)

```
cam serve ──SSH──> camc --json list                    # List agents
cam serve ──SSH──> camc --json status <id>             # Agent detail
cam serve ──SSH──> camc --json history --since <ts>    # Incremental events
cam serve ──SSH──> camc run -t claude "..."            # Start agent
cam serve ──SSH──> camc stop <id>                      # Stop agent
cam serve ──SSH──> camc heal                           # Restart dead monitors
```

- One SSH call per operation (vs current architecture: 3-5 SSH calls per monitor cycle)
- SSH ControlMaster for connection pooling
- camc returns JSON on stdout, cam serve parses it
- Hash-based conditional responses: `camc --json list --hash <prev>` returns `{"unchanged":true}` if nothing changed

### cam serve ↔ local camc: Direct file read

```
cam serve ──read──> ~/.cam/agents.json       # No subprocess or SSH needed
cam serve ──read──> ~/.cam/events.jsonl      # Incremental event read
cam serve ──exec──> camc run/stop/...        # Subprocess for mutations
```

### camc ↔ camc: No direct communication

camc instances are independent. Multi-machine coordination is done by cam serve polling each instance.

### `cam sync` — Single Source of Deployment & Compatibility

All cam ↔ camc compatibility is managed through `cam sync`. It is the **only** way to deploy and update remote camc instances.

```
cam sync [context]
  1. Build camc single-file from src/camc/ package (if needed)
  2. Check remote camc version (camc version --json)
  3. Push camc binary (hash-based, skip if unchanged)
  4. Push context.json (generated from cam SQLite context config)
  5. Push adapter configs (claude.toml, codex.toml, cursor.toml)
  6. Verify remote camc works (camc version)
```

What `cam sync` guarantees:
- **camc version consistency** — all remotes run the same version as local
- **Context config consistency** — context.json on remote matches cam SQLite
- **Adapter config consistency** — same detection patterns everywhere
- **Zero manual steps** — user never touches remote files directly

If a remote camc is outdated (e.g. missing a feature cam serve needs), `cam sync` updates it automatically. Version mismatch is not the user's problem.

## 6. JSON Output Format (cam-compatible)

camc's `--json` output matches cam's API format for seamless integration:

```json
{
  "id": "a889245f",
  "task": {
    "name": "fix-tests",
    "tool": "claude",
    "prompt": "fix the tests",
    "auto_confirm": true,
    "auto_exit": false
  },
  "context_name": "pdx",
  "context_path": "/home/user/project",
  "transport_type": "local",
  "status": "running",
  "state": "editing",
  "tmux_session": "cam-a889245f",
  "tmux_socket": "/tmp/cam-sockets/cam-a889245f.sock",
  "pid": 12345,
  "started_at": "2026-03-25T08:29:34Z",
  "completed_at": null,
  "exit_reason": null,
  "retry_count": 0,
  "cost_estimate": null,
  "files_changed": []
}
```

This format is used by:
- `camc --json list` — array of agents
- `camc --json status <id>` — single agent detail
- `camc --json history <id>` — array of events
- cam serve API responses — same structure passed through to Web UI, Mobile App, Teams

## 7. Backward Compatibility

**This is a critical requirement.** The migration must be seamless for all existing users and interfaces.

### Running Agents — Zero Downtime Migration

- **tmux sessions are not affected** — they are independent processes, don't care who monitors them
- **camc can adopt existing agents** — `camc add <tmux-session> --tool claude` already works
- **Bulk migration** — `cam export | camc import` moves agent records from SQLite to agents.json
- **Monitor handoff** — stop cam's `monitor_runner.py`, start camc's monitor on the same session. The agent keeps running, only the watcher changes.
- **Rollback** — if anything goes wrong, restart cam's monitor. The tmux session is unaffected.

### CLI Interface — No Breaking Changes

| Command | Before (cam) | After (cam) | Change |
|---------|-------------|-------------|--------|
| `cam run` | Direct tmux management | Delegates to camc | **None** (same flags, same output) |
| `cam list` | Reads SQLite | Reads SQLite (populated from camc) | **None** |
| `cam logs` | Direct tmux capture | Delegates to camc | **None** |
| `cam stop/kill` | Direct tmux kill | Delegates to camc | **None** |
| `cam status` | Reads SQLite | Reads SQLite | **None** |
| `cam history` | Reads SQLite events | Reads SQLite events (sourced from camc) | **None** |
| `cam apply` | Internal scheduler | Delegates to camc apply | **None** |

All `cam` commands keep the same flags, output format, and exit codes.

### API / Web UI / Mobile App — No Breaking Changes

| Interface | Protocol | Change |
|-----------|----------|--------|
| REST API | `GET /api/agents`, `POST /api/agents/{id}/stop`, etc. | **None** — same endpoints, same JSON format |
| WebSocket | `ws://host/ws` event stream | **None** — same event format |
| Relay | REST-over-WS via relay server | **None** — relay is transparent proxy |
| Web UI | Fetches from REST API | **None** — API format unchanged |
| Mobile App | Fetches via Relay | **None** — same API through relay |
| Teams Bot | Webhook + REST API | **None** — same API |

The API contract (endpoints, request/response JSON schemas, WebSocket event format) is the external interface. Internal implementation changes are invisible to all clients.

### camc CLI — Backward Compatible

| Before | After | Notes |
|--------|-------|-------|
| `camc run claude "task"` | Still works | Positional tool kept as fallback |
| `camc run -t claude "task"` | Preferred | New flag-based syntax |
| `camc list` | Same table output | No change |
| `camc --json list` | cam-compatible JSON | Format aligned (already done) |
| `camc status --id X --hash Y` | Still works | Old machine-readable mode preserved |
| `camc status <id>` | New detail view | New feature, non-breaking |

### Data Migration

```bash
# Phase 3 migration script (one-time):
cam migrate              # Does all of the below automatically:
  1. Export agents from cam SQLite to camc agents.json
  2. Export events to camc events.jsonl
  3. Stop cam monitor_runner processes
  4. Start camc monitors for all running agents
  5. Verify all tmux sessions still alive
  6. Switch cam serve to read from camc
```

Reversible: `cam migrate --rollback` restores cam's direct management mode.

### Compatibility Test Checklist

Before each migration phase ships:

- [ ] All existing `cam` CLI commands produce identical output
- [ ] REST API responses match previous schema (JSON diff test)
- [ ] WebSocket events have same format
- [ ] Mobile App works without update
- [ ] Web UI works without changes
- [ ] Running agents continue uninterrupted
- [ ] `cam list` shows same agents as before migration
- [ ] `cam history` shows same events as before migration
- [ ] `camc --json list` output maps correctly to cam API format

## 8. Future: Agent Memory

After the core architecture stabilizes, camc can support agent memory:

```
~/.cam/
  memory/
    <context>/
      project.md       # Persistent project knowledge (human-editable)
      feedback.md      # User preferences and corrections
      patterns.md      # Code patterns and conventions
  events.jsonl         # Source for auto-extracting learnings
```

- **File-based persistent memory** — human-readable, git-friendly, agents read at startup
- **Auto-extracted learnings** — after agent completes, extract key findings from events.jsonl into memory files
- **Per-context scoping** — each project/context has its own memory
- **Injected via prompt** — agent startup prompt includes relevant memory files

## 9. Migration Path

### Phase 1: camc gets SSH transport + history
- Add SSH transport to camc (paramiko for PyInstaller build, or subprocess ssh for single-file)
- Add events.jsonl and `camc history` command
- `camc run -t claude "task" --remote user@host` manages agents on remote machines
- cam still works as-is — **no breaking changes**

### Phase 2: camc gets DAG scheduler
- Port `TaskGraph` and `Scheduler` from cam to camc
- `camc apply tasks.yaml` runs DAG locally or across SSH remotes
- YAML format stays the same — **no breaking changes**

### Phase 3: cam delegates to camc
- `cam migrate` one-time script transfers running agents
- cam serve stops using AgentManager/AgentMonitor/Transport directly
- cam serve calls camc for all agent operations
- cam serve polls camc, diffs state, writes events to SQLite
- **All external interfaces unchanged** (CLI, API, Web, Mobile, Teams)

### Phase 4: cam slims down
- Remove duplicated agent management code from cam
- cam = API server + Relay + Web UI + SQLite aggregation
- All agent logic lives in camc
- **All external interfaces unchanged**

Each phase is independently deployable and rollback-safe.

## 10. Trade-offs

### Benefits
- **Single codebase** for agent management (no more cam/camc divergence)
- **Better remote reliability** — monitor runs locally on remote machine, not driven over SSH
- **Simpler deployment** — one binary per machine
- **camc is independently useful** — works without cam serve
- **No SQLite locking issues** — camc uses JSON with file locks, cam serve is the only SQLite writer

### Costs
- **Local Web UI latency** — cam serve polls local camc instead of direct SQLite writes (+1-2s for state updates, within existing 2s poll interval)
- **camc binary size** — PyInstaller binary ~15MB (vs current 58KB script)
- **Migration effort** — phased approach minimizes risk but takes time

### Mitigations
- Local camc: cam serve reads `agents.json` directly (same filesystem), near-zero latency
- Binary size: acceptable for a self-contained tool with zero runtime dependencies
- Migration: each phase is independently useful, deployable, and rollback-safe
