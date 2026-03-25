# CAM Platform Architecture Spec

> Version: 0.1 Draft
> Date: 2026-02-13

## 1. Overview

CAM (Coding Agent Manager) currently operates as a local CLI tool managing AI coding agents (Claude Code, Codex, Aider) via TMUX sessions. This spec defines the architecture for evolving CAM into a **platform** that supports:

- **CLI** (current) — local terminal interface
- **API Server** — programmatic access via REST + WebSocket
- **Mobile/Web UI** — remote monitoring and control from any device

The core principle: **CAM Core runs on one machine (the Server), all other interfaces are thin clients.**

## 2. Network Topology

```
┌──────────────────────────────────────────────────────────────┐
│                        SERVER                                │
│  (No public IP — runs everything)                            │
│                                                              │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐  │
│  │ CAM Core│  │ SQLite   │  │  TMUX   │  │ Agent        │  │
│  │ Manager │  │ Storage  │  │ Sessions│  │ Monitors     │  │
│  │ Monitor │  │          │  │         │  │ (background) │  │
│  │ Events  │  │          │  │         │  │              │  │
│  └────┬────┘  └────┬─────┘  └────┬────┘  └──────┬───────┘  │
│       │            │             │               │           │
│  ┌────┴────────────┴─────────────┴───────────────┴────┐     │
│  │              API Server (FastAPI)                   │     │
│  │         REST endpoints + WebSocket events           │     │
│  └────┬───────────────────────────────────────────┬───┘     │
│       │                                           │          │
│  ┌────┴────┐                                ┌────┴────┐     │
│  │  CLI    │                                │ SSH     │     │
│  │ (local) │                                │Transport│     │
│  └─────────┘                                └────┬────┘     │
│                                                  │           │
└──────────────────────────────────────────────────┼───────────┘
                                                   │
                                          ┌────────┴────────┐
                                          │  Remote Machines │
                                          │  (via SSH)       │
                                          └─────────────────┘


┌─────────────┐          ┌──────────────┐          ┌──────────┐
│ Mobile App  │────WS───▶│  Relay       │◀───WS────│ SERVER   │
│ Web Browser │          │  (Public IP) │          │          │
│ Remote CLI  │          │  Pure proxy  │          │          │
└─────────────┘          └──────────────┘          └──────────┘
```

### Key constraints

- **Server** has no public IP. It initiates all outbound connections.
- **Relay** is a stateless proxy (zero business logic, zero storage). It simply forwards WebSocket frames between Server and clients.
- **CLI on Server** can bypass relay entirely and talk to API Server over localhost.
- **Remote machines** are accessed from Server via SSH transport (existing).

## 3. Component Layers

### 3.1 Existing Layers (unchanged)

| Layer | Modules | Responsibility |
|-------|---------|---------------|
| **Models** | `core/models.py` | Agent, AgentEvent, TaskDefinition, Context, etc. |
| **Config** | `core/config.py`, `constants.py` | Hierarchical config: defaults → TOML → env → CLI |
| **Storage** | `storage/agent_store.py`, `storage/context_store.py` | SQLite CRUD for agents, contexts |
| **Transport** | `transport/local.py`, `transport/ssh.py` | TMUX session management (local + remote) |
| **Adapters** | `adapters/claude.py`, `adapters/codex.py`, etc. | Tool-specific behavior (completion, state, auto-confirm) |
| **Manager** | `core/agent_manager.py` | Orchestration: create session → send prompt → start monitor |
| **Monitor** | `core/monitor.py` | Poll loop: capture output, detect state/completion, auto-confirm, probe |
| **Events** | `core/events.py` | In-process pub/sub EventBus |
| **Logging** | `utils/logging.py` | Per-agent JSONL structured logs |

**Critical property**: Core has **zero CLI dependencies**. It imports only stdlib, Pydantic, and internal modules. This means the API Server can import and use Core directly.

### 3.2 New Layer: API Server

A FastAPI application that wraps Core and exposes it over HTTP + WebSocket.

```
cam/src/cam/
├── api/
│   ├── __init__.py
│   ├── server.py          # FastAPI app, lifespan, middleware
│   ├── routes/
│   │   ├── agents.py      # /api/agents/* REST endpoints
│   │   ├── contexts.py    # /api/contexts/* REST endpoints
│   │   ├── tasks.py       # /api/tasks/* REST endpoints (DAG)
│   │   └── system.py      # /api/system/* (health, config, doctor)
│   ├── ws.py              # WebSocket event stream endpoint
│   └── auth.py            # Token-based authentication
```

### 3.3 New Layer: Relay

A standalone, minimal process. Not part of the CAM package — could be a ~50-line asyncio WebSocket proxy or an off-the-shelf tool (e.g., nginx stream proxy, Cloudflare Tunnel, frp, rathole).

```
relay/
├── relay.py               # Stateless WS proxy (~50 lines)
└── Dockerfile
```

## 4. API Server Design

### 4.1 REST Endpoints

```
# Agents
POST   /api/agents                   # Run a new agent (≈ cam run)
GET    /api/agents                   # List agents (≈ cam list)
GET    /api/agents/{id}              # Agent detail (≈ cam status)
DELETE /api/agents/{id}              # Stop/kill agent (≈ cam stop)
GET    /api/agents/{id}/logs         # Read JSONL logs (≈ cam logs)
GET    /api/agents/{id}/output       # Latest captured TMUX output
POST   /api/agents/{id}/input        # Send input to agent

# Contexts
GET    /api/contexts                 # List contexts
POST   /api/contexts                 # Add context
DELETE /api/contexts/{id}            # Remove context

# Tasks (DAG)
POST   /api/tasks/apply              # Submit task file (≈ cam apply)
GET    /api/tasks/{id}               # Task graph status

# System
GET    /api/system/health            # Health check
GET    /api/system/config            # Current config
GET    /api/system/doctor            # Environment check
```

### 4.2 WebSocket Event Stream

```
WS /api/ws?token=<auth_token>
```

Streams real-time events from the EventBus as JSON frames:

```json
{
  "type": "event",
  "agent_id": "abc-123",
  "event_type": "state_change",
  "timestamp": "2026-02-13T10:00:00Z",
  "detail": {"from": "planning", "to": "editing"}
}
```

```json
{
  "type": "event",
  "agent_id": "abc-123",
  "event_type": "probe",
  "timestamp": "2026-02-13T10:01:00Z",
  "detail": {"result": "busy", "probe_count": 3, "consecutive_completed": 0}
}
```

```json
{
  "type": "log",
  "agent_id": "abc-123",
  "entry": {"ts": "...", "type": "output", "output": "Working on files..."}
}
```

**Client subscription**: Clients can optionally filter by agent_id via query param:

```
WS /api/ws?token=<token>&agent_id=abc-123
```

### 4.3 Authentication

Token-based, same pattern as existing `websocket_server.py`:

- Server generates a token on startup (or reads from config)
- All HTTP requests require `Authorization: Bearer <token>` header
- WebSocket connections pass token via query param or initial message
- Relay forwards auth transparently (doesn't inspect)

### 4.4 How API Server Uses Core

```python
# api/server.py (simplified)
from fastapi import FastAPI
from cam.core.agent_manager import AgentManager
from cam.core.events import EventBus
from cam.storage.agent_store import AgentStore
from cam.storage.context_store import ContextStore

app = FastAPI()

# Shared instances (created in lifespan)
agent_manager: AgentManager
event_bus: EventBus
agent_store: AgentStore

@app.post("/api/agents")
async def run_agent(request: RunAgentRequest):
    agent = await agent_manager.run_agent(
        tool=request.tool,
        prompt=request.prompt,
        context_name=request.context,
        detach=True,  # API agents always run in background
    )
    return {"agent_id": agent.id, "status": agent.status}
```

The EventBus bridges to WebSocket:

```python
# api/ws.py (simplified)
from cam.core.events import EventBus

async def ws_endpoint(websocket, event_bus: EventBus):
    queue = asyncio.Queue()
    event_bus.subscribe("*", lambda e: queue.put_nowait(e))
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event.model_dump(mode="json"))
    finally:
        event_bus.unsubscribe("*", handler)
```

## 5. Relay Design

The relay is a **stateless WebSocket proxy**. The Server connects outbound to the relay, and clients connect to the relay. The relay matches them and forwards frames.

### 5.1 Connection Flow

```
1. Server starts → connects to relay: ws://relay:443/server?token=SECRET
2. Relay registers the server connection
3. Client connects: ws://relay:443/client?token=SECRET
4. Relay pairs client ↔ server, forwards frames bidirectionally
5. If server disconnects, relay drops all clients (they reconnect)
```

### 5.2 Why Not a Generic Reverse Proxy?

A generic reverse proxy (nginx, Caddy) requires the server to have a public IP or use complex tunneling. The relay approach is simpler:

- Server initiates outbound connection (works behind NAT/firewall)
- Relay is ~50 lines of code, easy to audit
- Can also use existing tools: **rathole**, **frp**, or **Cloudflare Tunnel** as drop-in alternatives

### 5.3 Protocol

The relay does NOT understand CAM's protocol. It forwards raw WebSocket frames. This means:

- Relay never parses JSON
- Relay never stores data
- Relay can be replaced with any L4 proxy that supports WebSocket
- All business logic stays on the Server

## 6. Existing WebSocket Infrastructure

CAM already has WebSocket transport code that handles TMUX proxying:

| File | Role | Reuse |
|------|------|-------|
| `transport/websocket_server.py` | AgentServer: accepts WS, manages local TMUX | **Merge into API Server** — the TMUX operations become internal, exposed via REST/WS |
| `transport/websocket_client.py` | WebSocketClient: Transport impl over WS | **Repurpose for relay** — client side that tunnels Transport calls through relay |

The existing `AgentServer` protocol (`{"action": "create_session", ...}`) is a low-level TMUX proxy. The new API Server operates at a higher level (agents, tasks, events) while internally using the same TMUX operations via `LocalTransport` and `SSHTransport`.

## 7. Mobile / Web UI

### 7.1 Capabilities

| Feature | REST | WebSocket |
|---------|------|-----------|
| List agents | `GET /api/agents` | — |
| Agent detail/status | `GET /api/agents/{id}` | Real-time updates |
| Start agent | `POST /api/agents` | — |
| Stop agent | `DELETE /api/agents/{id}` | — |
| Follow logs | — | `WS /api/ws?agent_id=X` |
| Live state changes | — | Event stream |
| Probe results | — | Event stream |
| Send input | `POST /api/agents/{id}/input` | — |

### 7.2 UI Framework

TBD. Options:
- **React Native** — native iOS/Android from single codebase
- **Progressive Web App** — works on any device, no app store
- **Flutter** — native performance, single codebase

Recommendation: Start with **PWA** (accessible via mobile browser, no installation needed). Add native apps later if needed.

### 7.3 Key UI Screens

1. **Dashboard** — List of agents with status badges, live-updating
2. **Agent Detail** — State, last output snippet, event timeline, probe results
3. **Log Viewer** — Scrollable log with probe/state/output entries, color-coded
4. **New Agent** — Context picker, tool picker, prompt input
5. **Settings** — Server connection, auth token, notification preferences

## 8. Data Flow Examples

### 8.1 User Starts Agent from Mobile

```
Mobile App                  Relay              Server
    │                         │                   │
    │── POST /api/agents ────▶│──────forward─────▶│
    │                         │                   │── AgentManager.run_agent()
    │                         │                   │── Create TMUX session
    │                         │                   │── Start background monitor
    │◀── 200 {agent_id} ─────│◀─────forward──────│
    │                         │                   │
    │── WS /api/ws ──────────▶│──────forward─────▶│
    │                         │                   │── EventBus.subscribe("*")
    │                         │                   │
    │◀── {"event_type":       │◀─────forward──────│── Monitor detects state change
    │     "state_change"} ────│                   │
    │                         │                   │
    │◀── {"event_type":       │◀─────forward──────│── Monitor fires probe
    │     "probe"} ───────────│                   │
    │                         │                   │
    │◀── {"event_type":       │◀─────forward──────│── Agent completes
    │     "finalize"} ────────│                   │
```

### 8.2 CLI on Server (No Relay)

```
CLI (local)              API Server (localhost)
    │                         │
    │── POST /api/agents ────▶│── AgentManager.run_agent()
    │◀── 200 ────────────────│
    │                         │
    │── WS /api/ws ──────────▶│── EventBus stream
    │◀── events ──────────────│
```

CLI can also continue using Core directly (current behavior) without going through the API Server. Both paths coexist.

## 9. Implementation Phases

### Phase 1: API Server (Local Only)

- FastAPI app with REST endpoints for agents, contexts
- WebSocket event stream bridging EventBus
- Token auth
- CLI can optionally use API instead of direct Core access
- **Test**: `curl` and `wscat` against localhost

### Phase 2: Relay + Remote Access

- Implement or deploy relay proxy
- Server connects outbound to relay on startup
- Remote CLI can connect through relay
- **Test**: Access from a different machine via relay

### Phase 3: Mobile/Web UI

- PWA with dashboard, agent detail, log viewer
- Connects to relay for real-time events
- Push notifications for agent completion (optional)
- **Test**: Open on phone browser, start and monitor agent

### Phase 4: Enhanced Features

- Multi-user support (per-user tokens, agent ownership)
- Agent output streaming (not just events, but actual terminal output)
- File diff viewer (show what agent changed)
- Cost tracking dashboard
- Notification integrations (Slack, Discord, email)

## 10. SDK Streaming Mode (Claude JSON Protocol)

> Status: Planned
> Priority: High — eliminates regex fragility for Claude agents

### 10.1 Background

Claude CLI supports `--output-format stream-json --permission-prompt-tool stdio` flags that replace the interactive terminal UI with a structured NDJSON protocol over stdin/stdout. This gives us:

- **Typed events** instead of regex pattern matching on terminal text
- **Explicit permission requests** instead of heuristic auto-confirm
- **Definitive completion** (`result` message) instead of probe-based idle detection
- **Tool call metadata** (name, input, id) instead of guessing from output text

Reference implementation: [happy-cli](https://github.com/openclaw/happy) uses this protocol for full programmatic control of Claude.

### 10.2 Protocol Overview

Claude CLI emits line-delimited JSON on stdout:

```jsonl
{"type":"system","subtype":"init","session_id":"...","tools":["Bash","Edit","Read",...]}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'll fix that bug..."},{"type":"tool_use","id":"tu_1","name":"Read","input":{"file_path":"/src/main.py"}}]}}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu_1","content":"..."}]}}
{"type":"control_request","request_id":"cr_1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"pytest"}}}
{"type":"result","subtype":"success","num_turns":5,"total_cost_usd":0.12,"duration_ms":45000}
```

Permission responses are written as JSON to stdin:

```json
{"type":"control_response","response":{"subtype":"success","request_id":"cr_1","response":{"behavior":"allow","updatedInput":{}}}}
```

### 10.3 Architecture

SDK mode adds a parallel path alongside the existing terminal mode. No existing code is modified — it's an additive change.

```
Terminal mode (default):  tmux → capture-pane → ANSI strip → regex → state/confirm/completion
SDK mode (--sdk):         subprocess pipe → JSON readline → typed events → direct mapping
```

#### New files

| File | Purpose |
|------|---------|
| `src/cam/transport/sdk.py` | `SdkTransport` — manages subprocess with piped stdin/stdout instead of tmux |
| `src/cam/core/sdk_monitor.py` | `SdkMonitor` — event-driven monitor consuming JSON events from a queue |

#### Modified files (minimal changes)

| File | Change |
|------|--------|
| `src/cam/core/models.py` | Add `mode: str \| None = None` to `TaskDefinition` |
| `src/cam/core/agent_manager.py` | Branch in `run_agent()`: if `mode == "sdk"`, use `SdkTransport` + `SdkMonitor` |
| `src/cam/core/monitor_runner.py` | Handle SDK mode in background monitor subprocess |
| `src/cam/cli/run_cmd.py` | Add `--sdk` flag to `cam run` |
| `src/cam/adapters/configs/claude.toml` | Add `[sdk]` section with streaming command |

### 10.4 Component Design

#### SdkTransport (`transport/sdk.py`)

Implements the `Transport` ABC with a `subprocess.Popen` instead of tmux:

```python
class SdkTransport(Transport):
    async def create_session(self, session_id, command, workdir):
        self._proc = subprocess.Popen(
            command, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=workdir
        )
        # Background thread reads stdout lines → asyncio.Queue
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()

    async def capture_output(self, session_id):
        # Render accumulated events as text (API backward compat)
        return self._render_events()

    async def send_input(self, session_id, text):
        # Write JSON to stdin (for permission responses)
        self._proc.stdin.write(text.encode() + b"\n")
        self._proc.stdin.flush()

    async def session_exists(self, session_id):
        return self._proc.poll() is None

    async def kill_session(self, session_id):
        self._proc.terminate()
```

#### SdkMonitor (`core/sdk_monitor.py`)

Same public interface as `AgentMonitor` but event-driven instead of polling:

```python
class SdkMonitor:
    async def run(self) -> AgentStatus:
        while True:
            event = await self._transport.event_queue.get()

            if event["type"] == "assistant":
                # Extract tool calls → map to AgentState
                for block in event["message"]["content"]:
                    if block["type"] == "tool_use":
                        self._update_state(block["name"])

            elif event["type"] == "control_request":
                # Permission needed → check auto_confirm → respond
                response = self._handle_permission(event)
                await self._transport.send_input(session_id, json.dumps(response))

            elif event["type"] == "result":
                # Definitive completion — no probe needed
                if event["subtype"] == "success":
                    return AgentStatus.COMPLETED
                return AgentStatus.FAILED
```

#### Permission Handling

No regex matching. The `control_request` contains `tool_name` and `input` explicitly:

```python
def _handle_permission(self, event):
    tool = event["request"]["tool_name"]
    input_data = event["request"]["input"]

    if self._auto_confirm:
        behavior = "allow"
    else:
        behavior = "deny"  # or prompt user via API

    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": event["request_id"],
            "response": {"behavior": behavior, "updatedInput": input_data}
        }
    }
```

### 10.5 What Does NOT Change

- All non-Claude tools (Codex, Cursor, generic) — still tmux + terminal scraping
- Claude in default terminal mode — unchanged, remains the default
- `ToolAdapter` ABC, `ConfigurableAdapter`, existing `AgentMonitor`
- Storage layer, event bus, DB schema (events are already `list[dict]`)
- Web frontend, relay, API endpoints (output endpoint works via `capture_output()`)
- `camc` standalone CLI — no SDK mode (requires subprocess pipe management)

### 10.6 Tradeoffs

| | Terminal mode | SDK mode |
|---|---|---|
| Tool support | Any CLI tool | Claude only |
| `cam attach` | Full interactive TUI | Read-only event stream |
| Reliability | Best-effort (regex, probes) | 100% (protocol contract) |
| Confirm detection | Pattern matching + cooldown | Explicit JSON request/response |
| Completion detection | Prompt counting + probe | Definitive `result` message |
| State detection | Regex on output text | Tool name from `tool_use` blocks |
| Output for humans | Raw terminal (rich) | Rendered from events (structured) |

### 10.7 Implementation Phases

1. **Phase A**: `SdkTransport` + `SdkMonitor` — core functionality, testable in isolation
2. **Phase B**: Model + AgentManager wiring — `mode` field, branching logic
3. **Phase C**: CLI `--sdk` flag + `claude.toml` SDK command
4. **Phase D**: API `events` endpoint for richer structured output to web clients
5. **Phase E**: `cam attach --sdk` read-only event viewer

Phases A–C form the MVP. D–E are polish.

### 10.8 Activation

```bash
cam run claude "Fix the bug" --sdk              # SDK streaming mode
cam run claude "Fix the bug"                     # Default terminal mode (unchanged)
```

TOML config addition:

```toml
[sdk]
enabled = true
command = ["claude", "--output-format", "stream-json", "--permission-prompt-tool", "stdio", "-p", "{prompt}"]
```

## 11. Dependencies

### API Server
- `fastapi` — HTTP framework
- `uvicorn` — ASGI server
- `websockets` — WebSocket support (already optional dep)

### Relay
- `websockets` (or use off-the-shelf: rathole, frp, Cloudflare Tunnel)

### Mobile/Web UI
- TBD based on Phase 3 framework choice

### Install groups (pyproject.toml)
```toml
[project.optional-dependencies]
server = ["fastapi", "uvicorn[standard]"]
remote = ["websockets"]  # already exists
```

## 11. Security Considerations

- **Auth tokens** must be generated securely (secrets.token_urlsafe)
- **Relay** should enforce TLS (wss://) — no plaintext in production
- **CORS** on API Server: restrict to known origins
- **Rate limiting** on relay to prevent abuse
- **No secrets in relay** — relay never sees decrypted payloads if E2E encryption is added later
- **Agent sandboxing** — out of scope for this spec, but TMUX isolation provides basic process separation
