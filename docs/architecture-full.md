# CAM Full Architecture

> Date: 2026-03-27 | Auto-generated from codebase analysis

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        USER INTERFACES                                  │
│                                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ cam CLI   │  │ camc CLI │  │ Web PWA  │  │ Android  │  │ Teams Bot│ │
│  │ (typer)   │  │ (stdlib) │  │ (JS)     │  │ (WebView)│  │(teaspirit│ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘ │
│       │              │             │              │              │       │
└───────┼──────────────┼─────────────┼──────────────┼──────────────┼───────┘
        │              │             │              │              │
        │              │             └──────┬───────┘              │
        ▼              ▼                    ▼                      ▼
┌──────────────┐ ┌──────────────┐  ┌──────────────┐     ┌──────────────┐
│ cam serve    │ │ camc (local) │  │ Relay Server │     │ Relay Server │
│ (FastAPI)    │ │ (standalone) │  │ (WebSocket)  │     │ (WebSocket)  │
│ :8420        │ │              │  │ :8443        │     │ :8443        │
└──────┬───────┘ └──────┬───────┘  └──────┬───────┘     └──────────────┘
       │                │                  │
       │         ┌──────┘                  │
       ▼         ▼                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        EXECUTION LAYER                                  │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    camc (per machine)                            │   │
│  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌─────────────┐ │   │
│  │  │ tmux      │  │ monitor   │  │ detection  │  │ auto-confirm│ │   │
│  │  │ sessions  │  │ subprocess│  │ (state/    │  │ (pattern    │ │   │
│  │  │           │  │ (per agent│  │  completion)│  │  match)     │ │   │
│  │  └───────────┘  └───────────┘  └───────────┘  └─────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐       │
│  │ Claude Code │  │ Codex CLI  │  │ Cursor     │  │ (any tool) │       │
│  │ (in tmux)   │  │ (in tmux)  │  │ (in tmux)  │  │ (in tmux)  │       │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘       │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                        DATA LAYER                                       │
│                                                                         │
│  ~/.cam/ (JSON, source of truth)       ~/.local/share/cam/ (SQLite,    │
│  ├── agents.json                        cached aggregation)             │
│  ├── machines.json                     ├── cam.db                       │
│  ├── contexts.json                     │   ├── agents (21 rows)         │
│  ├── events.jsonl                      │   ├── contexts (25 rows)       │
│  ├── configs/*.toml                    │   ├── agent_events (811K rows) │
│  ├── logs/monitor-*.log                │   └── schema_version           │
│  └── pids/*.pid                        └── (to be removed in Phase 5)  │
│                                                                         │
│  /tmp/cam-sockets/*.sock  (tmux sockets, per-machine local)            │
│  /tmp/cam-ssh-*           (SSH ControlMaster sockets)                   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. User Interfaces

| Interface | Technology | Connection | Source |
|-----------|-----------|------------|--------|
| **cam CLI** | Python + Typer + Rich | Direct to cam serve (localhost) | `src/cam/cli/` |
| **camc CLI** | Python 3.6+ stdlib only | Direct local (no server needed) | `src/camc_pkg/cli.py` |
| **Web PWA** | Vanilla JS + Service Worker | HTTP direct or WebSocket via Relay | `web/` |
| **Android APP** | WebView wrapping PWA | Same as Web (CamBridge JS interface) | `apps/cam-mobile/` |
| **Teams Bot** | teaspirit (external) | HTTP to cam serve API | external |

### 2. Servers

#### cam serve (FastAPI)
- **File**: `src/cam/api/server.py`
- **Port**: 8420 (default)
- **Auth**: Bearer token
- **Background tasks**: CamcPoller (5s), Relay connector
- **Routes**:

| Route | Method | Purpose |
|-------|--------|---------|
| `/agents` | GET/POST | List/create agents |
| `/agents/{id}` | GET/PATCH/DELETE | Get/update/stop agent |
| `/agents/{id}/output` | GET | Capture tmux output (hash-based cache) |
| `/agents/{id}/fulloutput` | GET | Full output with incremental offset |
| `/agents/{id}/input` | POST | Send text input |
| `/agents/{id}/key` | POST | Send special key (C-c, Enter) |
| `/agents/{id}/logs` | GET | Read JSONL monitor logs |
| `/agents/{id}/restart` | POST | Restart terminal agent |
| `/agents/{id}/upload` | POST | Upload file to context path |
| `/client/{id}/sync` | POST | cam-client push mode |
| `/contexts` | GET/POST | List/create contexts |
| `/contexts/{id}/files` | GET | File browser |
| `/api/ws` | WS | Real-time event stream |
| `/system/health` | GET | Health check |
| `/system/apk/info` | GET | APK version |
| `/system/apk/download` | GET | APK base64 download |

#### Relay Server
- **File**: `relay/relay.py`
- **Port**: 8443
- **Zero dependencies** (stdlib-only RFC 6455 WebSocket)
- **Endpoints**:

| Path | Role | Description |
|------|------|-------------|
| `/server?sid=X&token=T` | cam serve connects here | One persistent WS connection |
| `/client?token=T` | Mobile/Web connects here | Multiple client WS connections |
| `/api/ws` | Event stream proxy | WS event forwarding |
| `/_relay/status` | Health | Plain HTTP, no WS |

**Relay data flow**:
```
Client frame  → Relay → forward to Server
Server frame  → Relay → broadcast to all Clients
```

### 3. Core (cam serve internals)

| Component | File | Role |
|-----------|------|------|
| **AgentManager** | `src/cam/core/agent_manager.py` | Orchestrates agent lifecycle |
| **CamcDelegate** | `src/cam/core/camc_delegate.py` | Wraps camc CLI calls (local/SSH) |
| **CamcPoller** | `src/cam/core/camc_poller.py` | Polls camc every 5s, syncs to SQLite |
| **EventBus** | `src/cam/core/events.py` | In-memory pub/sub for real-time events |
| **Scheduler** | `src/cam/core/scheduler.py` | DAG task execution |
| **RelayConnector** | `src/cam/api/relay_connector.py` | Outbound WS to Relay server |

**Agent operation delegation**:
```
AgentManager → CamcDelegate → subprocess (local) or SSH → camc binary
```

### 4. Transport Backends

| Transport | File | Protocol | Use Case |
|-----------|------|----------|----------|
| **LocalTransport** | `transport/local.py` | subprocess → tmux | Local machine agents |
| **SSHTransport** | `transport/ssh.py` | SSH ControlMaster → tmux | Remote machine agents |
| **AgentTransport** | `transport/agent.py` | SSH → cam-agent (Go) | Standardized remote protocol |
| **ClientTransport** | (push mode) | HTTP POST → cam serve | cam-client push mode |
| **DockerTransport** | `transport/docker.py` | docker exec → tmux | Container agents |
| **WebSocketTransport** | `transport/websocket.py` | WebSocket | Future use |

**SSH ControlMaster pooling**:
```
Socket: /tmp/cam-ssh-{sha256(user@host:port)[:12]}
Shared between: SSHTransport, CamcDelegate, cam sync
Persist: 600s after last use
```

### 5. camc (Standalone Agent Manager)

- **Source**: `src/camc_pkg/` (package) → `dist/camc` (single-file build)
- **Deployed to**: each machine via `cam sync` or `camc sync`
- **Zero dependencies**: Python 3.6+ stdlib only
- **Data**: `~/.cam/agents.json`, `~/.cam/events.jsonl`

**Commands**:
```
camc run          Start agent with prompt
camc list         List local agents (filtered by hostname)
camc stop/kill    Stop agent
camc add          Adopt existing tmux session
camc rm           Remove agent record
camc attach       Attach to tmux session
camc status       JSON status with hash-based conditional
camc capture      Capture tmux output
camc send/key     Send input/key to agent
camc logs         View agent output (follow mode)
camc heal         Check agents, restart dead monitors
camc apply        DAG scheduler (YAML tasks)
camc history      Show event log
camc machine      list/add/rm/edit/ping machines
camc context      list/add/rm contexts
camc sync         Deploy camc to remote machines
camc migrate      SQLite → JSON migration
camc version      Show version and supported tools
```

**Monitor subprocess** (per agent):
```
camc run → spawns monitor subprocess → polls every 1s:
  1. tmux_session_exists() → health check
  2. capture_tmux() → get screen content
  3. detect_state() → pattern match → planning/editing/testing/committing
  4. should_auto_confirm() → pattern match → send Enter/response
  5. detect_completion() → prompt count → idle detection
  6. auto_exit → kill session or send /exit
```

### 6. cam-agent (Go Binary)

- **Source**: `src/cam-agent/`
- **Cross-platform**: linux/windows/darwin
- **Protocol**: JSON over SSH stdin/stdout

**Commands**:
```
cam-agent ping
cam-agent session create/exists/kill/capture/send/key/log-start/log-read
cam-agent file list/read/write
```

**Tmux sockets**: `/tmp/cam-agent-sockets/<session>.sock`

## Data Flow Diagrams

### Agent Output (Mobile → Display)

```
Mobile APP
  │ GET /agents/{id}/output?hash=abc
  ▼
Relay (passthrough, no cache)
  │
  ▼
cam serve
  │
  ├─ Tier 1: cam-client cache (memory, 10s TTL)
  │   └─ Hit? → return immediately (~0ms)
  │
  ├─ Tier 2: output cache (memory, 2s TTL)
  │   └─ Hit + hash match? → return {"unchanged": true} (50 bytes)
  │   └─ Hit + hash mismatch? → return cached output
  │
  └─ Tier 3: SSH → camc capture → tmux capture-pane
      └─ Cache result, return output (~500-1400ms for remote)
```

**Latency breakdown**:

| Scenario | Latency | Response Size |
|----------|---------|---------------|
| Hash match (unchanged) | ~100ms | 50 bytes |
| Cache hit (changed) | ~160ms | ~7KB |
| Cache miss, local | ~200ms | ~7KB |
| Cache miss, remote SSH | 500-1400ms | ~7KB |

### Agent Startup

```
cam serve (POST /agents)
  │
  ▼
AgentManager.run_agent()
  │
  ▼
CamcDelegate.run_agent()
  │ SSH ControlMaster (or local subprocess)
  ▼
camc run --tool claude --prompt "..." --path /work --name task-1
  │
  ├─ Create tmux session: cam-{8char-id}
  │   └─ Socket: /tmp/cam-sockets/cam-{id}.sock
  │   └─ history-limit: 50000
  │   └─ Screen: 220x50
  │
  ├─ Startup auto-confirm (trust dialog, permissions)
  │
  ├─ Send prompt via tmux send-keys
  │
  └─ Spawn monitor subprocess
      └─ PID file: ~/.cam/pids/{id}.pid
      └─ Log: ~/.cam/logs/monitor-{id}.log
```

### State Sync (camc → cam serve)

```
CamcPoller (every 5s)
  │
  ├─ Local: read ~/.cam/agents.json directly
  │
  └─ Remote: SSH → camc --json list
       │
       ▼
  For each agent:
    Compare with SQLite
       │
       ├─ Status changed? → update SQLite, publish event
       ├─ State changed?  → update SQLite, publish event
       └─ New agent?      → import to SQLite
              │
              ▼
         EventBus.publish()
              │
              ▼
         WebSocket → Relay → Mobile APP
```

### Real-time Events

```
Monitor subprocess (on target machine)
  │ detect state change / auto-confirm / completion
  ▼
EventStore.append() → ~/.cam/events.jsonl
  │
  ▼
CamcPoller (5s) reads events.jsonl
  │
  ▼
EventBus.publish()
  │
  ├─ WebSocket /api/ws → direct clients
  │
  └─ RelayConnector → Relay → mobile/web clients
```

### Relay NAT Traversal

```
┌────────────────┐        ┌────────────────┐        ┌────────────────┐
│  cam serve     │        │  Relay Server  │        │  Mobile APP    │
│  (private IP)  │        │  (public IP)   │        │  (any network) │
│                │        │                │        │                │
│  relay_loop()──┼──WS──▶│  /server       │        │                │
│                │        │       │        │        │                │
│                │        │       ▼        │◀──WS───┤  /client       │
│                │        │  proxy frames  │        │                │
│                │        │       │        │───WS──▶│  responses     │
│  ASGI dispatch │◀───────│  forward req   │        │                │
│  (in-process)  │────────▶  forward resp  │        │                │
└────────────────┘        └────────────────┘        └────────────────┘

Request:  Client → Relay → Server → ASGI → FastAPI → response
Response: FastAPI → Server WS → Relay → Client WS
```

## Authentication

| Connection | Auth Method | Token Source |
|-----------|-------------|--------------|
| HTTP API | `Authorization: Bearer <token>` | `config.server.auth_token` |
| WebSocket | `?token=<token>` query param | Same as HTTP |
| Relay (server) | `?token=<relay_token>` | `config.server.relay_token` |
| Relay (client) | `?token=<relay_token>` | User-configured |
| SSH | ControlMaster (key/kerberos) | SSH agent |

## Configuration

**Config search order**:
1. `--config` CLI flag
2. `$CAM_CONFIG` env var
3. `~/.cam/config.toml`
4. `/etc/cam/config.toml`

**Key config sections**:
```toml
[server]
host = "0.0.0.0"
port = 8420
auth_token = "..."
relay_url = "ws://relay:8443"
relay_token = "..."
```

**Adapter configs** (`~/.cam/configs/*.toml`):
```toml
# claude.toml — defines how to launch, detect state, auto-confirm, detect completion
[launch]
command = ["claude", "--allowedTools", "..."]
prompt_after_launch = true
startup_wait = 30

[state]
strategy = "pattern"
[[state.patterns]]
pattern = "Compiling|Building|Running tests"
state = "testing"

[[confirm]]
pattern = "Do you want to proceed"
response = ""
send_enter = true

[completion]
strategy = "prompt_count"
prompt_pattern = "^[❯>]"
threshold = 2
```

## Key Design Decisions

1. **camc is source of truth** — `agents.json` on each machine is the authoritative state. SQLite is a read-only cache.
2. **Delegation model** — cam orchestrates, camc executes. All tmux operations happen locally on target machine.
3. **Zero-dep camc** — Single file, Python 3.6+, no pip. Deployable anywhere via `scp`.
4. **SSH ControlMaster sharing** — One persistent SSH connection per machine, shared across all operations.
5. **Hash-based output caching** — Reduces bandwidth 140x for unchanged output (50 bytes vs 7KB).
6. **Relay for NAT traversal** — Mobile can reach private-IP servers via public relay. Stateless proxy.
7. **TOML adapters** — New tools added by config file, no code changes needed.
8. **Cluster-safe** — `hostname` field in agents.json prevents cross-machine interference on shared NFS.
