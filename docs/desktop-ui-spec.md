# CAM Desktop UI Spec

Status: v2 — Electron + WebUI reuse, Phase 2A backend readiness
Owner split: `camui-rev` writes and reviews this spec; `camui-dev` implements after approval.

Canonical requirement IDs live in `docs/desktop/requirements.md`. This file is
a milestone/product spec; implementation tasks and review replies should cite
the requirement IDs rather than treating this document as a checklist.

## Summary

CAM Desktop is a desktop control surface for CAM, the "PM2 for AI coding
agents". It manages Claude Code, Codex, Cursor, and other CLI agents that run
inside tmux sessions through `cam` / `camc`.

The desktop app must not replace `camc` or tmux. It should make the common
agent-management loop visible and low-friction:

- see all agents and their state
- inspect one agent's live output
- send direct input or special keys to the selected tmux session

Desktop v2 is a thin **client** of the existing CAM HTTP/WebSocket API.
It does not start, supervise, or bootstrap any CAM or relay server. The
operating model matches the mobile/PWA app: connect to an existing direct
endpoint (e.g. `cam serve` running in WSL, Linux, macOS, or a remote host)
or to an existing relay endpoint, using the same direct/relay settings the
WebUI already supports.

V1 is intentionally narrower than the earlier preserve-first draft: **agent
list, selected-agent output, and selected-agent input are the only product
surface**, plus a minimal connection profile needed to reach the backend.
Other scaffold features may remain in code if useful, but they should not be
visible in the first default UI.

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

The desktop binary is primarily a client:

- It does **not** install CAM, Python, WSL, Docker, or any backend runtime,
  in Phase 1 or Phase 2A. Bootstrapping is reserved for Phase 2B+.
- It does **not** manage the relay server. No relay control panel, no
  relay lifecycle UI.
- Direct and relay are connection endpoints. The UI surfaces them as
  profile settings (server URL + token, relay URL + token), reusing the
  existing WebUI `CamApi` connect flow.
- Phase 2A is allowed to **detect** an already-installed local backend and
  optionally **start** it (e.g. `cam serve --port 8420` on Linux/macOS, or
  `wsl -d <distro> -- cam serve --port 8420` on Windows). It must not
  install or upgrade anything. If CAM is not present locally, Phase 2A
  shows a setup hint only; full setup belongs to Phase 2B/2C/2D.
- Future versions may add an embedded terminal or SSH client, but Phase 1
  and Phase 2A must not include terminal UI, terminal tabs, or terminal
  transport code.

## Existing Repository Context

Relevant files already present:

- `src/cam/api/server.py`: FastAPI app, `cam serve` entry point.
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

The first release should prove this loop:

```text
select agent -> read current output -> send input/key -> refresh output
```

Connection modes:

1. **Direct** — `serverUrl + token` against a reachable `cam serve` (local
   Linux/macOS, WSL, or remote host).
2. **Relay** — `relayUrl + relayToken` against a reachable relay; CAM server
   sits behind the relay.

`CamApi.connect()` races both options if configured; the winner is used.
Profiles are stored in `localStorage` exactly as the WebUI does. The
Electron shell does not introduce a separate profile store.

Native bridge (`window.CamBridge`) is intentionally narrow and must not
duplicate CAM product behavior:

Phase 1:

- `getPlatform()` — `"win32" | "darwin" | "linux"`.
- `getAppVersion()` — Electron app version string.
- `openExternal(url)` — open an http(s) URL in the system browser.
- `restartApp(route?)` — restart the renderer (matches the existing WebView
  contract used by the mobile wrapper).

Phase 2A additions (local backend readiness only):

- `checkBackendReadiness()` — returns a `BackendReadiness` snapshot:
  - `platform`: `"win32" | "darwin" | "linux"`.
  - `hasWsl`: WSL is usable on this host (Windows only).
  - `wslDistros`: array of distro names detected via `wsl -l -q`.
  - `selectedDistro`: the distro the bridge would target (first non-
    `docker-desktop*`), or `null`.
  - `hasPython`: a usable Python interpreter is reachable from the same
    shell the bridge would use to launch `cam`.
  - `hasCam`: the `cam` CLI is reachable from that same shell.
  - `localServerRunning`: a CAM server is confirmed listening at
    `http://127.0.0.1:8420/api/system/health` — the probe must require
    HTTP 200 and that the JSON body contains CAM's known shape
    (`version` plus `adapters` or `agents_running`), so a foreign HTTP
    responder cannot be mistaken for CAM.
  - `localPortOccupiedByOther`: port 8420 has *some* HTTP responder but
    it does not look like CAM.
  - `suggestedCommand`: a copy-friendly command (e.g.
    `pip install --user "cam[server]" && cam serve --port 8420`).
  - `message`: short, human-readable summary.
- `startLocalBackend()` — optional; only the bridge attempts a start when
  `hasCam` is true, `localServerRunning` is false, and the port is not
  occupied by a non-CAM service. The main process **generates a fresh
  random token** (URL-safe base64, 24 bytes) and launches `cam serve
  --port 8420 --token <token>`. It never parses stdout. On success it
  returns `{ ok: true, url, token, message }`; the renderer persists the
  token in `localStorage.cam_token`, fills `serverUrl` with the local URL
  if blank, and reconnects via `CamApi`. On Windows the spawn target is
  `wsl.exe -d <distro> -- bash -lc "cam serve --port 8420 --token <token>"`;
  on Linux/macOS it is `bash -lc "cam serve --port 8420 --token <token>"`.
  If a CAM server is already running, the bridge refuses with
  `ok: false` because it cannot know the existing token from outside the
  process — the user must either supply that token or stop the existing
  server.

Both methods are exposed via `ipcMain.handle` / `ipcRenderer.invoke` and
execute argv directly with `child_process.execFile` / `spawn`. They never
take user-provided command strings — the renderer cannot pass a command
to run.

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
  - Shows current backend mode: local `camc`, WSL, or SSH.
  - Shows health/version, connection errors, and last refresh time.
  - Lets the user retry connection.
  - Profile switching may remain if already implemented, but it should be
    visually secondary to the list/output/input loop.

- **Agent List**
  - Dense, scannable list of agents.
  - Fields: name, short id, tool, status, state, context/path, host.
  - Selecting an agent updates the interaction surface.
  - Poll every 2-5 seconds.

- **Interaction Surface**
  - Primary pane displays current captured output from the selected agent.
  - CLI mode uses `camc capture <agent> --lines <n>`.
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
- API adapter for `cam serve` online/multi-machine mode.
- Agent lifecycle controls: stop, kill, retry/reboot, remove.
- Log tab from `camc logs` / `/logs`.
- Attach session via external terminal or copyable command.
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
- Server auto-start and tray daemon lifecycle.
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

## Roadmap / Milestones

Phase 1 — **Electron client (approved)**

- Workbench shell with mode navigation (Agents, Settings, placeholders).
- Agent list + selected-agent output + textarea composer + quick keys.
- Direct / relay profile reuse from the WebUI.
- No terminal UI, no server-lifecycle UI.

Phase 2A — **Backend readiness (this milestone)**

- Saved direct/relay profile reconnect on launch (Phase 1 already does
  this — Phase 2A keeps it and surfaces a richer "checking / connected /
  disconnected" state in the connection bar).
- `CamBridge.checkBackendReadiness()` for local detection (Python, cam
  CLI, WSL distros on Windows, listening local server).
- `CamBridge.startLocalBackend()` for one-click start when `cam` is
  installed but no server is listening. Polls `/api/system/health` to
  confirm.
- Settings mode gains a **Local backend readiness** section that drives
  these probes, surfaces actionable status, and offers a "Start backend"
  button only when starting is appropriate.
- Does not install, upgrade, or bootstrap any runtime.

Phase 2B — **Existing WSL bootstrap (deferred)**

- Install CAM into an existing WSL distro the user already has. Pip
  install, environment validation, first-run `cam serve` setup.
- Out of scope for Phase 2A.

Phase 2C — **Full Windows setup (deferred)**

- Handle the case where the user has no usable WSL distro yet. Guided
  WSL feature enablement, distro install, then drop into 2B.
- Out of scope for Phase 2A.

Phase 2D — **Offline / bundled bootstrap (deferred)**

- Internal-mirror / offline-bundle installation flows for environments
  without public registry access. Likely combined with signed MSI from
  Phase 1's documented packaging path.
- Out of scope for Phase 2A.

## Implementation Plan After Approval

1. Add the WebUI-derived desktop entry under `web/` (HTML + CSS + JS),
   reusing `web/js/api.js` and `web/js/state.js`. Do not touch
   `web/index.html`, which still serves the mobile/PWA app.
2. Add an Electron shell under `apps/cam-desktop/electron/` that loads
   `web/desktop.html`. Preload exposes only the narrow `CamBridge` surface.
3. The Phase 1 default screen is: agent list, selected-agent output,
   bottom composer, quick-key buttons (Enter, Esc, Ctrl-C, Backspace), and
   a minimal connection bar with a settings dialog backed by the existing
   `localStorage` keys.
4. Do not add terminal UI, terminal tabs, terminal transport, embedded
   PTY/SSH, relay/server lifecycle UI, or server bootstrap in this phase.
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
