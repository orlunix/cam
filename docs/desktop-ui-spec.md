# CAM Desktop UI Spec

Status: v2 — Electron + WebUI reuse, hub/controller architecture, active Settings modes **Direct + Relay** (Direct = self-contained embedded Electron Hub; Relay = external relay)
Owner split: `camui-rev` writes and reviews this spec; `camui-dev` implements after approval.

Canonical requirement IDs live in `docs/desktop/requirements.md`. This file is
a milestone/product spec; implementation tasks and review replies should cite
the requirement IDs rather than treating this document as a checklist.

## Architecture

CAM is a **hub / control plane** product. Coding agents always run on a
remote controller/node — never inside the Desktop process. Desktop is a
thin UI/client over the hub API.

```text
Desktop UI  ──HTTP/WS──▶  CAM Hub/API  ──poll/route──▶  Remote Controller/Node  ──spawns──▶  tmux + agent runtime
```

Terminology used throughout this spec:

- **Node / Controller** — the machine/runtime where agents actually run
  (Python, tmux, controller adapter / `camc`, agent CLIs).
- **Hub** — the CAM control plane. Aggregates node/agent/context tables
  and routes user actions back to the right controller. In Direct mode
  this Hub is embedded in Electron/Node; in Relay mode it is external.
- **Desktop** — the Electron app in `apps/cam-desktop/`. In Direct mode
  it starts an embedded Node/Electron CAM Hub and renders that Hub's
  tables. In Relay mode it reaches a Hub through an external relay
  endpoint.
- **Local Node** — a node that happens to be reachable on `localhost` /
  WSL / a local Mac. It is a special case of Node, not a separate
  Settings mode.

See `docs/desktop/requirements.md` for the canonical
ARCH/HUB/NODE/DIRECT/REMOTE requirement IDs.

## Summary

CAM Desktop is a desktop control surface for CAM. It manages Claude Code,
Codex, Cursor, and other CLI agents that run inside tmux sessions on a
controller/node through `cam` / `camc`.

The desktop app must not replace `camc` or tmux. It must not require any
of them to live on the user's machine. It should make the common
agent-management loop visible and low-friction:

- see all agents and their state across every controller the hub knows about
- inspect one agent's live output
- send input or special keys to the selected agent's tmux session via the
  hub

Desktop v2 renders the CAM hub's existing HTTP/WebSocket API. The
renderer stays a client in all modes. In Direct mode, Electron main owns
the embedded CAM Hub lifecycle through a narrow fixed IPC surface; the
renderer never becomes a command runner.

The operating model is straightforward: Direct starts an embedded
Electron/Node CAM Hub and connects the renderer to it; Relay connects to
a CAM Hub through an external relay endpoint. In both cases, the Hub presents managed
controllers/nodes and their agents. Desktop reuses the same `CamApi`
client once connected.

Active Settings exposes exactly two connection modes:

```text
Direct / Relay  ·  (SSH attach, future)
```

- **Direct** — Desktop-managed embedded CAM Hub. Electron main checks
  readiness, starts the Node/Electron Hub on loopback, generates/persists
  the API token, and connects the renderer directly to that Hub. Direct
  has no local WSL, Python, shell, `cam`, or `cam serve` prerequisite.
- **Relay** — relay URL + relay token, used when the hub is unreachable.
  The CAM API token is source/profile-managed and injected by the
  relay on `/api/*` forwarding (CAM-DESK-REMOTE-012, 2026-06-12), so
  the Desktop/mobile UI only asks for the two relay fields.
- **SSH attach** (future) — server-mediated WebSocket attach
  to the selected agent's controller. See TERM-001..005 + SSH-010..013.

Direct mode must not require the user to open a terminal or install local
runtime bits. Node/remote management happens through the embedded Hub:
import SSH config, add/edit
nodes, test connection, sync, and start agents through Hub APIs. Desktop
does not expose raw private keys to the renderer.

Settings exposes exactly the two active modes; tokens are never
displayed in plaintext.

The left-nav workspace modes are: **Agents** (default — list/output/
composer), **Start** (start a new agent), **Settings** (Direct + Relay
profile), and **Nodes** (a read-mostly node/remote list built from
hub-provided contexts + agents, peer of the mobile/PWA Machines view).
Selecting a workspace mode swaps the main pane to that feature; the
Agent list/composer is hidden outside Agents. Contexts remains a
disabled placeholder until a dedicated requirement is approved.
Workspace modes are not connection modes — Nodes does not affect how
Desktop reaches the hub (CAM-DESK-NODEUI-010..017).

V1 is intentionally narrower than the earlier preserve-first draft: **agent
list, selected-agent output, and selected-agent input are the only product
surface**, plus a minimal connection profile needed to reach the backend.
Other scaffold features may remain in code if useful, but they should not be
visible in the first default UI.

**Direct starts by default** (CAM-DESK-DIRECT-013). On a cold launch
with no Relay profile and no saved Direct profile, `app.js`
auto-starts the embedded CAM Hub via `CamBridge.directHub.start()`,
persists the generated loopback URL + token, and calls `connect()`.
The user is never sent to Settings just because the app has not been
configured. An empty `contexts:[]` / `agents:[]` Hub is a valid
running state — Add Host and Import SSH Config on the Nodes page
register remote nodes after the fact
(CAM-DESK-DIRECT-014..017).

## Framework Decision

Use **Electron** as the desktop shell, loading the bundled WebUI-derived
`web/desktop.html`. The runtime under `apps/cam-desktop/` is Electron going
forward.

Rationale:

- The existing `web/` directory already implements direct/relay connection,
  agent list, agent detail/output/input/key, contexts, and PWA behavior.
  Reusing this code gives Desktop v2 immediate feature parity with the
  mobile/PWA app and one place to evolve UI.
- Electron is intentionally chosen over Tauri so future iterations can host
  Node-side primitives such as `node-pty`, `ssh2`, or `xterm.js` for
  embedded terminal/SSH — capabilities Tauri cannot supply natively in the
  same form. Embedded terminal/SSH is **out of scope for Phase 1**, but
  framework selection should not block it later.
- React/TypeScript components from the earlier Tauri scaffold may remain in
  the tree for reference, but are not the future product direction and must
  not be expanded.

Do not introduce Flutter, Qt, or another desktop stack for this iteration.

## Backend / Server Boundary

The desktop binary is primarily a client of the **hub**:

- The active Direct flow starts/owns an embedded Electron/Node CAM Hub and
  connects to it over loopback. Desktop does not ask the user to run
  terminal commands and does not depend on local WSL, Python, `cam`, or
  `cam serve`.
- The renderer is sandboxed and never executes shell commands. Electron
  main may start the embedded Hub only through fixed, typed IPC methods.
  Controller-side operations go through Hub REST/WS calls (CamApi).
- Public relay infrastructure is not Desktop's concern. External relay
  remains a user-provided endpoint, surfaced under the Relay tab as
  relay URL + relay token only (CAM-DESK-REMOTE-012, 2026-06-12).
  The CAM API token is source/profile-managed and injected by the
  relay on `/api/*` forwarding — not user-facing.
- Direct Hub lifecycle is active scope (CAM-DESK-DIRECT-010..019):
  embedded-hub readiness, start, stop/restart, generated token,
  Advanced/Diagnostics, and Nodes/Remotes management through Hub APIs.
- Future versions may add an embedded terminal, but the terminal must
  connect to the **selected agent's controller** through hub-provided
  attach metadata (HUB-013 + TERM-001..005 + SSH-010..013). Desktop must
  not assume local tmux. The v3 local-PTY experiment on
  `camui-desktop-v3` is preserved as reference code; the matching
  TERM-010..017 IDs are marked `superseded` for that reason.

## Existing Repository Context

Relevant files already present:

- `src/cam/api/server.py`: FastAPI app, `cam serve` entry point for the
  external/Python hub implementation. Direct Desktop must not require it
  at runtime; use it only as behavioral reference for endpoint shapes.
- `src/cam/api/routes/agents.py`: agent REST endpoints.
- `src/cam/api/ws.py`: WebSocket event/status stream.
- `src/cam/api/routes/contexts.py`: context REST endpoints.
- `src/cam/api/routes/system.py`: health/config endpoints.
- `web/`: production WebUI/PWA. Implements direct/relay connection,
  agent list, agent detail, output/input/key, contexts, settings, machines.
- `web/js/api.js`: `CamApi` — direct HTTP + relay-over-WebSocket client.
- `web/js/state.js`: `AppState` — reactive state store.
- `apps/cam-desktop/`: desktop app workspace. Phase 1 hosts the Electron
  shell. The earlier Tauri/React files remain as reference but are dormant.
- `baseline/chatshell-desktop/`: ChatShell reference app.
- `docs/chatshell-reference-evaluation.md`: feature mapping and reuse
  recommendation.

## Backend Model

Phase 1 reuses the existing WebUI `CamApi` (`web/js/api.js`) as the only
backend seam. The Electron renderer loads `web/desktop.html`, which imports
the same `api.js` and `state.js` modules used by `web/index.html`. The UI
calls domain methods on `CamApi` — it must not build raw REST paths.

The first release proves this loop end-to-end against a hub:

```text
select agent -> read current output -> send input/key -> refresh output
```

Active connection modes (canonical IDs in DIRECT-010..019 and REMOTE-012..014):

1. **Direct** (DIRECT-010..019) — app-managed embedded Electron/Node CAM
   Hub. URL/token are internal generated profile state; users manage
   Nodes/Remotes rather than typing a Hub URL.
2. **Relay** (REMOTE-012) — relay URL + relay token. The relay proxies
   REST/WS for an unreachable hub. The CAM API token is source/profile-
   managed (`--api-token` / `CAMUI_API_TOKEN` / `--profile NAME`
   writes `~/.cam/camui/relay/<NAME>/profile.json`) and the relay
   injects it on `/api/*` forwarding — the Desktop/mobile UI only
   asks for the two relay fields (2026-06-12).

A previously-explored separate **Local** tab (LOC-010..024) is
superseded. Its lifecycle pieces now belong to Direct.

`CamApi.connect()` must use the active profile kind. Direct uses the
app-managed local Hub profile; Relay uses relay fields. It must not race
stale Direct fields against Relay or vice versa.
Profiles are stored in `localStorage` exactly as the WebUI does. The
Electron shell does not introduce a separate profile store. The Settings
tabs distinguish Direct and Relay; there is no Managed tab (Managed = preconfigured Direct) and no Local-managed
profile in the active product. (A legacy `cam_profile_kind` localStorage
marker may still be present in code as `local | direct | relay`; new
code must not branch on `local`.)

Native bridge (`window.CamBridge`) is intentionally narrow. It may own
the embedded Direct Hub lifecycle, but it must not become a general command
runner or duplicate hub/controller product behavior:

- `getPlatform()` — `"win32" | "darwin" | "linux"`.
- `getAppVersion()` — Electron app version string.
- `openExternal(url)` — open an http(s) URL in the system browser.
- `restartApp(route?)` — restart the renderer (matches the existing WebView
  contract used by the mobile wrapper).
- `directHub.check()` — returns embedded Direct Hub readiness and ownership state.
- `directHub.start()` — generates an API token, starts the embedded
  loopback Node/Electron Hub, polls health, and returns the internal Direct
  profile `{ apiUrl, apiToken }`.
- `directHub.stop()` / `directHub.restart()` — affect only the embedded
  Hub owned by the current app process.
- `directHub.logs()` / `directHub.getProfile()` — Diagnostics-only,
  redacted output.

The old Phase 2A `checkBackendReadiness` / `startLocalBackend` names and
the old separate-Local `localBackend.*` renderer wiring are historical.
The prior Python `cam serve` Direct prototype is superseded. The active
UI should present Direct Hub management backed by the embedded Electron
Hub, not a Local tab and not a local Python process.
Terminal IPC surfaces explored in the v3 stash remain deferred (TERM /
SSH requirements).

Anything beyond the above lives in HTTP/WS calls through `CamApi`.

## API Contract

The API adapter should use these existing endpoints:

- Health: `GET /api/system/health`
- Config: `GET /api/system/config`
- Agent list: `GET /api/agents`
- Agent detail: `GET /api/agents/{agent_id}`
- Run agent: `POST /api/agents`
- Update agent: `PATCH /api/agents/{agent_id}`
- Stop/kill: `DELETE /api/agents/{agent_id}?force=false|true`
- Retry/restart: `POST /api/agents/{agent_id}/restart`
- Logs: `GET /api/agents/{agent_id}/logs?tail=...`
- Live output: `GET /api/agents/{agent_id}/output?lines=...&hash=...`
- Full output: `GET /api/agents/{agent_id}/fulloutput`
- Direct text: `POST /api/agents/{agent_id}/input`
- Special key: `POST /api/agents/{agent_id}/key`
- Context list/detail/create/update/delete: `/api/contexts...`
- WebSocket: `/api/ws?token=...` with optional `agent_id=...`

The API adapter must include bearer auth where required. The UI should store
only the server URL and token in local app settings. It must not log tokens.

## CLI Contract

Phase 1 has no in-app CLI adapter. The desktop does not exec `camc` from
the renderer or main process. Direct `camc` access is reserved for a later
phase and is not part of this iteration.

## Scope Strategy

The first product slice should optimize for the smallest complete CAM desktop
loop, not for maximum feature preservation. The default posture for V1 is:
**show only list/output/input; keep future affordances out of the way**.

Do not build TaskHub, Tree view, Diff view, lifecycle management, run-agent
forms, or message-thread UI for V1. If existing scaffold or ChatShell-derived
code already contains those paths, prefer leaving it unchanged and dormant
rather than deleting it just to shrink the codebase. The V1 requirement is that
non-core surfaces must not appear in the default interface and must not make the
main list/output/input loop harder to use.

Mapping examples:

- ChatShell conversation list -> CAM agent list and future message-thread list.
- ChatShell chat view -> selected-agent output and future `camc msg` thread
  replay.
- ChatShell chat input -> direct tmux send, message send/reply, and run-agent
  prompt.
- ChatShell model/settings controls -> backend profile, tool kind, context, and
  execution options.
- ChatShell attachments -> future upload to selected agent/context.
- ChatShell search -> future search over agents, logs, and messages.
- ChatShell prompts/skills UI -> future prompt templates or read-only skill
  visibility.

Non-mappable runtime areas must not be copied into CAM Desktop:

- LLM provider/model runtime.
- Built-in shell/file/web tools runtime.
- MCP runtime.
- Skills runtime.
- SQLite conversation store as source of truth.

Those responsibilities belong to CAM, `camc`, `cam serve`, or the managed
coding agents.

This follows the ChatShell evaluation only where it directly helps the core
loop: borrow the desktop shell, sidebar/list behavior, input composer,
scroll behavior, and settings/error patterns. Do not borrow ChatShell's LLM
provider runtime, built-in tool runtime, MCP runtime, skills runtime, or SQLite
conversation store as CAM's source of truth.

## Core P0 Scope

Core P0 is agent list plus one interaction surface. The goal is to confirm the
desktop app starts on the right abstraction with no extra panels competing for
attention.

Required views:

- **Connection Bar**
  - Shows current hub connection state and the active connection kind
    (Direct or Relay) — not a local CLI/WSL/SSH mode. Desktop is a
    client over the hub; the controller-side runtime lives on the Node,
    not on the user's machine.
  - Shows health/version, connection errors, and last refresh time.
  - Lets the user retry connection.
  - Profile switching may remain if already implemented, but it should be
    visually secondary to the list/output/input loop.

- **Agent List**
  - Dense, scannable list of agents (across every controller the hub
    knows about — there is no separate Desktop-only registry).
  - Fields: name, short id, tool, status, state, context/path, host.
  - Selecting an agent updates the interaction surface.
  - Poll every 2-5 seconds.

- **Interaction Surface**
  - Primary pane displays current captured output for the selected
    agent, read through `CamApi` (`/api/agents/{id}/output`,
    `/api/agents/{id}/fulloutput`). There is no Desktop CLI mode —
    output always comes through the hub API.
  - Refresh button is required.
  - Auto-refresh is optional and must be easy to pause.
  - Borrow ChatShell's scroll rule: auto-scroll only if the user is already
    near the bottom; never fight manual scrolling.

- **Direct Command Composer**
  - Send text to selected agent.
  - Toggle send Enter vs no Enter if supported by the backend method.
  - Buttons for Enter, Escape, Ctrl-C, Ctrl-D.
  - Enter sends; modifier+Enter or Shift+Enter inserts a newline.
  - IME composition must not accidentally submit.
  - Disable when no running agent is selected.

- **Minimal Agent Metadata**
  - Show only what is needed inline: selected agent name/id, status, state, and
    context/path.
  - Do not show a separate metadata inspector in V1.
  - No lifecycle controls are required for P0.

P0 acceptance depends on these `CamApi` methods (already implemented in
`web/js/api.js`):

- `health()` — `GET /api/system/health`
- `listAgents()` — `GET /api/agents`
- `agentOutput(id, lines, hash)` — `GET /api/agents/{id}/output`
- `sendInput(id, text, sendEnter)` — `POST /api/agents/{id}/input`
- `sendKey(id, key)` — `POST /api/agents/{id}/key`

## P1 / Later Scope

- TaskHub / task wall / multi-task tabs.
- Tree view and Diff view.
- `camc msg` inbox/thread/reply panel.
- Run-agent form.
- Hub API evolution: managed node discovery, multi-controller aggregation.
- Agent lifecycle controls: stop, kill, retry/reboot, remove.
- Log tab from `camc logs` / `/logs`.
- Attach session via hub-mediated terminal stream.
- Minimal context selector.
- Polished settings/profile dialog with app-data persistence.
- Rich rendering for message threads and final summaries.
- File/image upload to selected agents.
- Full context create/edit/delete UI.
- Node and machine management.
- DAG workflow editor for `cam apply`.
- Archive browser.
- Prune/orphan cleanup UI.
- Embedded terminal/PTY.
- Direct Hub lifecycle refinements: persistent ownership marker,
  optional user-level service registration, tray affordances, and
  packaged/runtime setup improvements.
- App auto-update and signed installers.
- Rich diff/file-change browser.
- Multi-agent batch operations.

## UX Requirements

- The first screen is the operational console, not a landing page.
- Use a compact, work-focused desktop layout:
  - left: connection + agent list
  - center: selected agent output + composer
  - no right inspector, advanced panel, TaskHub, Tree, or Diff in V1
- Prefer ChatShell-style component split over one large `App.tsx`:
  - `ConnectionPanel`
  - `AgentList`
  - `AgentOutputPane`
  - `AgentComposer`
  - small stores/hooks for backend profile, agents, and selection
- Avoid decorative hero sections, nested cards, and marketing copy.
- Use stable dimensions for agent rows, action bars, and output panes to avoid
  layout jumps during polling.
- Show command/API failures with stderr/stdout or response details.
- Keep the UI responsive while backend commands run.
- Disable duplicate actions while an action is in flight.

## Security Requirements

- Renderer runs with `contextIsolation: true`, `nodeIntegration: false`, and
  `sandbox: true`. No Node primitives in the renderer.
- The preload script exposes only the narrow `window.CamBridge` surface
  listed in **Backend Model**. It does not expose `child_process`, `fs`,
  shell, or arbitrary IPC.
- `openExternal` accepts only `http(s)://` URLs.
- No passwords in profiles.
- Tokens live only in `localStorage`, exactly as the WebUI handles them,
  and must not be logged.
- Destructive actions require explicit confirmation:
  - kill
  - remove
  - archive/remove
  - retry/reboot when it may replace a running session
  - stop for an actively running agent

## Workspace Service Gateway

Future workspace-level features such as todos, skills, workspace metadata,
search indexes, or other CLI-backed tools should share one generic service
model instead of each feature inventing its own database sync and SSH tunnel.

The preferred model is:

```text
one workspace primary endpoint
one remote gateway process
one remote loopback port
one local SSH tunnel
many registered services behind the gateway
```

In other words, avoid this:

```text
todos  -> remote port A -> local tunnel A
skills -> remote port B -> local tunnel B
index  -> remote port C -> local tunnel C
```

Prefer this:

```text
workspace gateway
  remote: 127.0.0.1:<remote_port>
  tunnel: 127.0.0.1:<local_port> -> remote 127.0.0.1:<remote_port>

  /health
  /services
  /services/todos/...
  /services/skills/...
  /services/index/...
```

### Primary Endpoint

The UI client and backend services are intentionally decoupled:

- relay server can run on the same machine as the UI
- relay server can run on a different machine
- CAM direct server can run in WSL, Linux, macOS, or a remote host
- Desktop only needs a reachable URL/profile and credentials

Expected user flow:

1. User starts CAM/CAM relay in an existing Linux-like environment:
   - WSL on Windows
   - Linux host
   - macOS host
   - remote machine reachable over the network
2. User opens CAM Desktop.
3. User picks or enters a direct/relay profile.
4. Desktop uses the existing WebUI API paths to list agents, show output, and
   send input/keys.

This keeps the first desktop milestone aligned with the already-working mobile
app and avoids coupling the UI/installer to server bootstrap.

Later, Desktop may add optional helper flows:

- connection profile wizard
- "how to start relay" guidance

Those helpers must remain optional; the baseline assumption is that an existing
CAM endpoint is already running.

## Workspace Service Gateway

Future workspace-level features such as todos, skills, workspace metadata,
search indexes, or other CLI-backed tools should share one generic service
model instead of each feature inventing its own database sync and SSH tunnel.

The preferred model is:

```text
one workspace primary endpoint
one remote gateway process
one remote loopback port
one local SSH tunnel
many registered services behind the gateway
```

In other words, avoid this:

```text
todos  -> remote port A -> local tunnel A
skills -> remote port B -> local tunnel B
index  -> remote port C -> local tunnel C
```

Prefer this:

```text
workspace gateway
  remote: 127.0.0.1:<remote_port>
  tunnel: 127.0.0.1:<local_port> -> remote 127.0.0.1:<remote_port>

  /health
  /services
  /services/todos/...
  /services/skills/...
  /services/index/...
```

### Primary Endpoint

Some workspace services need a single authoritative database. For example, a
todo system may keep files under:

```text
<workspace>/.cam/todos/
  .todocli.yml
  .todo.db
  .todo1.db
```

When multiple nodes can work on the same logical workspace, CamUI should not
copy or merge raw database files between nodes. Instead, one node is configured
as the workspace primary endpoint for that service family. Other nodes access
the service through the gateway/tunnel, or the feature is shown as unavailable
when the primary endpoint cannot be reached.

The primary endpoint is feature/workspace configuration, not a global "main
machine" for all CAM operations.

Example:

```yaml
version: 1

primary:
  context: pdx119
  endpoint: hren@pdx119:22
  workspace: /home/hren/project
```

### Service Registry

Service registration should be declarative so future CLI tools can participate
without CamUI gaining feature-specific tunnel code.

Suggested workspace config:

```yaml
# <workspace>/.cam/services.yml
version: 1

primary:
  context: pdx119
  endpoint: hren@pdx119:22
  workspace: /home/hren/project

gateway:
  command: camw serve --workspace . --host 127.0.0.1 --port ${PORT}
  port_file: .cam/services/gateway.port
  pid_file: .cam/services/gateway.pid

services:
  todos:
    enabled: true
    module: todocli
    data: .cam/todos

  skills:
    enabled: true
    module: skillcli
    data: .cam/skills
```

The exact gateway binary name is not decided by this spec. `camw` is a
placeholder for "CAM workspace gateway".

Each registered service must provide:

- a stable route prefix under `/services/<name>/`
- a health response
- a workspace-relative data path
- a local-only server surface, bound to `127.0.0.1`
- its own database and schema semantics

CamUI owns service lifecycle and connectivity. The service owns its data model.

### Gateway API Contract

The gateway should expose a small common API:

```text
GET /health
GET /services

GET /services/:service/health
GET /services/:service/api/...
POST /services/:service/api/...
PATCH /services/:service/api/...
DELETE /services/:service/api/...
```

Example todo calls through the same local tunnel:

```text
GET  http://127.0.0.1:<local_port>/services/todos/api/todos
POST http://127.0.0.1:<local_port>/services/todos/api/todos
```

Example skills calls:

```text
GET http://127.0.0.1:<local_port>/services/skills/api/skills
```

### CamUI / Hub Responsibilities

The renderer should not manage SSH, ports, daemon process state, or secrets.
Those responsibilities belong in the Hub layer.

CamUI Hub should provide generic service-session management:

```text
ensureWorkspaceGateway(workspace)
  -> read .cam/services.yml
  -> resolve primary endpoint/context
  -> open/reuse the long-lived SSH connection
  -> ensure gateway process exists on the primary endpoint
  -> create/recreate one local port forward
  -> health-check /health and /services
  -> expose status and proxy APIs to the renderer
```

Generic Hub API shape:

```text
GET  /api/workspace-services
GET  /api/workspace-services/:service/status
POST /api/workspace-services/:service/start
POST /api/workspace-services/:service/stop
POST /api/workspace-services/:service/retry

ANY  /api/workspace-services/:service/proxy/*
```

Feature UI can then call the proxy route instead of opening its own connection.

### Lifecycle State

The service session should be explicit about state:

```text
disabled
resolving_config
connecting_primary
ensuring_gateway
forwarding
ready
degraded
healing
unavailable
```

Heal rules:

- SSH disconnected: reopen the pooled SSH connection and recreate the tunnel.
- Local forward closed: recreate the forward.
- Gateway not responding: re-run the gateway ensure/start command.
- Primary unreachable: mark unavailable and do not create a second local DB.
- Service unhealthy: keep the gateway alive, mark only that service degraded.

CamUI must not silently fall back to a new local database when a primary-node
service is unreachable. That would create split-brain state.

### Security Rules

- Gateway and service processes bind only to remote `127.0.0.1`.
- CamUI exposes access through its existing authenticated Hub API, not by
  opening public ports.
- Raw secrets stay in the Hub/main process. They are never passed to renderer
  code.
- Services should not receive arbitrary shell strings from the renderer.
- Database files remain private to the primary node/workspace. Other nodes use
  the service API, not file copying.

### Todo Example

For a todo feature, the DB remains workspace-local on the primary endpoint:

```text
/home/hren/project/.cam/todos/.todocli.yml
/home/hren/project/.cam/todos/.todo.db
```

The CLI tool should also work outside CamUI:

```bash
cd /home/hren/project
todocli list
```

When running on a non-primary node, `todocli` can read the same
`.cam/services.yml`, discover that todos are primary-node backed, open or reuse
the gateway tunnel, and call the gateway API. CamUI uses the same model through
the Hub.

This keeps the command-line and GUI behavior aligned:

```text
CLI:   todocli list
GUI:   Todos panel
Both:  one gateway, one tunnel, one authoritative DB
```

## Release UX For Backend Startup

Phase 1 may assume an existing backend endpoint so the desktop UI can land
quickly, but that is not enough for a polished desktop product.

The real release flow should be:

### Workspace Primary Endpoint (Shared Service Files)

Some workspace services need a single authoritative database. For example, a
todo system may keep files under:

```text
<workspace>/.cam/todos/
  .todocli.yml
  .todo.db
  .todo1.db
```

When multiple nodes can work on the same logical workspace, CamUI should not
copy or merge raw database files between nodes. Instead, one node is configured
as the workspace primary endpoint for that service family. Other nodes access
the service through the gateway/tunnel, or the feature is shown as unavailable
when the primary endpoint cannot be reached.

The primary endpoint is feature/workspace configuration, not a global "main
machine" for all CAM operations.

Example:

```yaml
version: 1

primary:
  context: pdx119
  endpoint: hren@pdx119:22
  workspace: /home/hren/project
```

### Service Registry

Service registration should be declarative so future CLI tools can participate
without CamUI gaining feature-specific tunnel code.

Suggested workspace config:

```yaml
# <workspace>/.cam/services.yml
version: 1

primary:
  context: pdx119
  endpoint: hren@pdx119:22
  workspace: /home/hren/project

gateway:
  command: camw serve --workspace . --host 127.0.0.1 --port ${PORT}
  port_file: .cam/services/gateway.port
  pid_file: .cam/services/gateway.pid

services:
  todos:
    enabled: true
    module: todocli
    data: .cam/todos

  skills:
    enabled: true
    module: skillcli
    data: .cam/skills
```

The exact gateway binary name is not decided by this spec. `camw` is a
placeholder for "CAM workspace gateway".

Each registered service must provide:

- a stable route prefix under `/services/<name>/`
- a health response
- a workspace-relative data path
- a local-only server surface, bound to `127.0.0.1`
- its own database and schema semantics

CamUI owns service lifecycle and connectivity. The service owns its data model.

### Gateway API Contract

The gateway should expose a small common API:

```text
GET /health
GET /services

GET /services/:service/health
GET /services/:service/api/...
POST /services/:service/api/...
PATCH /services/:service/api/...
DELETE /services/:service/api/...
```

Example todo calls through the same local tunnel:

```text
GET  http://127.0.0.1:<local_port>/services/todos/api/todos
POST http://127.0.0.1:<local_port>/services/todos/api/todos
```

Example skills calls:

```text
GET http://127.0.0.1:<local_port>/services/skills/api/skills
```

### CamUI / Hub Responsibilities

The renderer should not manage SSH, ports, daemon process state, or secrets.
Those responsibilities belong in the Hub layer.

CamUI Hub should provide generic service-session management:

```text
ensureWorkspaceGateway(workspace)
  -> read .cam/services.yml
  -> resolve primary endpoint/context
  -> open/reuse the long-lived SSH connection
  -> ensure gateway process exists on the primary endpoint
  -> create/recreate one local port forward
  -> health-check /health and /services
  -> expose status and proxy APIs to the renderer
```

Generic Hub API shape:

```text
GET  /api/workspace-services
GET  /api/workspace-services/:service/status
POST /api/workspace-services/:service/start
POST /api/workspace-services/:service/stop
POST /api/workspace-services/:service/retry

ANY  /api/workspace-services/:service/proxy/*
```

Feature UI can then call the proxy route instead of opening its own connection.

### Lifecycle State

The service session should be explicit about state:

```text
disabled
resolving_config
connecting_primary
ensuring_gateway
forwarding
ready
degraded
healing
unavailable
```

Heal rules:

- SSH disconnected: reopen the pooled SSH connection and recreate the tunnel.
- Local forward closed: recreate the forward.
- Gateway not responding: re-run the gateway ensure/start command.
- Primary unreachable: mark unavailable and do not create a second local DB.
- Service unhealthy: keep the gateway alive, mark only that service degraded.

CamUI must not silently fall back to a new local database when a primary-node
service is unreachable. That would create split-brain state.

### Security Rules

- Gateway and service processes bind only to remote `127.0.0.1`.
- CamUI exposes access through its existing authenticated Hub API, not by
  opening public ports.
- Raw secrets stay in the Hub/main process. They are never passed to renderer
  code.
- Services should not receive arbitrary shell strings from the renderer.
- Database files remain private to the primary node/workspace. Other nodes use
  the service API, not file copying.

### Todo Example

For a todo feature, the DB remains workspace-local on the primary endpoint:

```text
/home/hren/project/.cam/todos/.todocli.yml
/home/hren/project/.cam/todos/.todo.db
```

The CLI tool should also work outside CamUI:

```bash
cd /home/hren/project
todocli list
```

When running on a non-primary node, `todocli` can read the same
`.cam/services.yml`, discover that todos are primary-node backed, open or reuse
the gateway tunnel, and call the gateway API. CamUI uses the same model through
the Hub.

This keeps the command-line and GUI behavior aligned:

```text
CLI:   todocli list
GUI:   Todos panel
Both:  one gateway, one tunnel, one authoritative DB
```

## Roadmap / Milestones

Phase 1 — **Electron client (approved)**

- Workbench shell with mode navigation (Agents, Settings, placeholders).
- Agent list + selected-agent output + textarea composer + quick keys.
- Direct / relay profile reuse from the WebUI.
- No terminal UI, no server-lifecycle UI.

Phase 2E — **Direct embedded CAM Hub (approved)**

- Direct is the default Settings mode.
- Electron main starts/owns the embedded Node/Electron Hub on loopback
  through a narrow typed bridge.
- Renderer connects to that local Hub through the existing `CamApi`
  path.
- Server URL, generated token, and logs are hidden under Advanced /
  Diagnostics with redaction.
- Nodes/Remotes management goes through Hub APIs; Desktop never stores
  raw private keys in the renderer.

Phase 2A / 2B / 2C / 2D — **Deferred / historical**

The earlier detect-only backend-readiness probe, WSL bootstrap,
full Windows setup, and offline bootstrap are deferred. The separate
Local tab experiment is historical; its app-managed-Hub lifecycle was
promoted into Direct and is tracked by CAM-DESK-DIRECT-010..019.

## Implementation Plan After Approval

1. Add the WebUI-derived desktop entry under `web/` (HTML + CSS + JS),
   reusing `web/js/api.js` and `web/js/state.js`. Do not touch
   `web/index.html`, which still serves the mobile/PWA app.
2. Add an Electron shell under `apps/cam-desktop/electron/` that loads
   `web/desktop.html`. Preload exposes only the narrow `CamBridge` surface.
3. The Direct Settings tab owns the local Hub lifecycle UI: Check /
   Start / Stop / Restart, status, and Advanced / Diagnostics. It does
   not ask the user to type an external URL in the normal path.
4. Do not add terminal UI, terminal tabs, terminal transport, embedded
   PTY/SSH, or relay-server lifecycle UI in this phase.
5. The earlier Tauri scaffold under `apps/cam-desktop/src/` and
   `apps/cam-desktop/src-tauri/` may remain as reference but must not be
   wired to the Electron build and must not be the future direction.

Expected touched area:

- `web/desktop.html`, `web/css/desktop.css`
- `web/js/desktop/app.js`, `web/js/desktop/shell.js`, optional
  `web/js/desktop/agent-console.js`
- `apps/cam-desktop/electron/main.js`, `apps/cam-desktop/electron/preload.js`
- `apps/cam-desktop/package.json` (Electron deps + scripts)
- `docs/desktop-ui-spec.md` (this file)

## Verification

Before review, `camui-dev` must report:

- `node -c apps/cam-desktop/electron/main.js` and `node -c
  apps/cam-desktop/electron/preload.js` (syntax check, no Electron install
  required).
- A static check that `web/index.html` and `web/js/app.js` are untouched
  by the desktop work, so the mobile/PWA entry still loads.
- If `npm install` / `npm run build` succeeds in `apps/cam-desktop`,
  report it; if it fails due to no network/registry access, report the
  exact command and the gap.
- Packaging gap: Phase 1 ships the runnable Electron entry. MSI packaging
  via `electron-builder` is the intended Windows artifact but may be
  deferred if dependency install is blocked — in which case document the
  next packaging command (e.g. `npm install electron electron-builder &&
  npm run dist:win`) and leave it for a follow-up task.

If a command cannot run because of missing system dependencies or no
network access, the reply must include the exact failure and the next
command a human should run.

## Acceptance Criteria

P0 is acceptable when:

- `web/desktop.html` loads in a normal browser against an existing direct
  or relay endpoint without any Electron involvement (proves the WebUI
  reuse story).
- The Electron main + preload pass `node -c` syntax check.
- The default screen shows: agent list (left), selected-agent output
  (main pane), composer with quick-key buttons (bottom), and a minimal
  connection status / settings entry.
- Direct text and special-key controls call `sendInput` / `sendKey` from
  `web/js/api.js`.
- Selecting a different agent switches the output and input target.
- The default UI contains no terminal, no relay/server lifecycle UI, no
  TaskHub, no Tree, and no Diff.
- `web/index.html` and the existing PWA stay loadable and unchanged.
- All errors are visible and actionable.
