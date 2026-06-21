# CamUI Mobile V2 — Relay-First Implementation Plan

Status: **draft** — client-only scope  
Audience: mobile implementers (`camui-mobile-*`)  
Product name: **CamUI Mobile V2** (not an upgrade patch to legacy `web/index.html`)

## Summary

Build **CamUI Mobile V2** as a Relay-only pure client against the **CamUI-start embedded Hub**. The phone stores Relay URL + Relay token; the workstation runs `camui start --profile … --relay-url … --relay-token …`, which starts the same embedded Hub used by Desktop Direct mode and registers with the relay.

V2 targets **Desktop / embedded-Hub API parity**: Agent Settings (Attributes, System Prompt, Automation/Loop, Workflow), Skills, Nodes, Todos (when Hub exposes proxy routes), etc.

**Hard boundary for the mobile team:** implement only **client** files (`web/mobile.html`, `web/js/mobile/*`, native shells). The Hub behind Relay is **already validated** — if Desktop works in Relay mode against the same `camui start` source, Mobile V2 uses the **same `CamApi` calls** with no server, embedded-hub, or camc changes.

---

## Hub assumption (locked)

```text
Desktop Relay  ──same API──▶  Mobile V2 Relay
         └─ both talk to camui start → embedded Hub
```

If a feature works in Desktop Relay, Mobile gaps are **UI/porting only**. Do not open Hub tickets unless Desktop Relay also fails for that route.

---

## V1 vs V2

| | **V1 — legacy mobile** | **V2 — this plan** |
|---|------------------------|---------------------|
| HTML entry | `web/index.html` | `web/mobile.html` |
| JS tree | `web/js/app.js`, `web/js/views/*` | `web/js/mobile/*` |
| Hub behind relay | Often **`cam serve`** (Python `src/cam/api`) — narrow API | **`camui start` → embedded Hub** (`apps/cam-desktop/electron/embedded-hub.cjs`) — Desktop API surface |
| Settings | Relay URL + token **and** Direct server URL + API token | **Relay only** |
| Agent Settings tabs | Name / auto-confirm only (via limited PATCH) | Attributes, Prompt, Automation, Workflow |
| Skills / cron / workspace files | Not available | Available when Hub implements routes |
| Start Server on phone | N/A | **Never** — Hub runs on source machine via `camui start` |

Legacy V1 remains in the tree until V2 APK/PWA cutover; do not extend V1 for new features.

---

## CamUI-start stack (V2 backend contract)

```text
[Phone]  CamUI Mobile V2
            Relay URL + Relay token
                │
                ▼
         [Relay server]
                │  injects CAM API token from source profile
                ▼
[Workstation]  camui start --profile NAME --relay-url … --relay-token …
                │
                ▼
         embedded Hub (embedded-hub.cjs)
           GET/PATCH /api/agents…
           /api/agents/{id}/cron
           /api/agents/{id}/workspace/files*
           /api/skillm/*
           /api/contexts*
                │
                ▼
         ssh2 transport → remote camc / tmux on nodes
```

Source quick start (from `apps/cam-desktop/README.md`, CAM-DESK-REMOTE-012a):

```bash
node apps/cam-desktop/cli/camui-cli.cjs start \
  --profile hren7001 \
  --relay-url ws://127.0.0.1:7001 \
  --relay-token <RELAY_TOKEN>
```

Token lives in `~/.cam/camui/relay/<profile>/profile.json` (mode 0600). Mobile never displays or stores it.

**Implication:** V2 feature matrix follows **`embedded-hub.cjs` + Desktop renderer**, not `src/cam/api` (Python). Do not test V2 Agent Settings against `cam serve` alone.

---

## Why Relay-only on the phone

| Reason | Detail |
|--------|--------|
| **Same as V1 UX** | User only enters Relay URL + Relay token; relay injects Hub token (CAM-DESK-REMOTE-012). |
| **Hub stays on workstation** | `camui start` runs embedded Hub where SSH/camc/skillm already live; phone is a remote control surface. |
| **Matches Desktop Relay path** | Desktop in Relay mode uses the same `CamApi` + polling patterns (`web/js/desktop/app.js`). |
| **No phone-side Hub** | No `CamBridge.directHub`, no loopback, no WSL/Python on device. |

Direct URL + API token on the phone is **out of V2 scope** (optional far-future Advanced panel).

---

## What V2 adds (vs V1 legacy mobile)

V1 covered: agent list, detail (output/input/keys/upload), start agent, contexts, nodes (read-mostly), settings (Relay + Direct), context file browse — all against the **Python API** where applicable.

V2 adds the **embedded-Hub / Desktop** surfaces:

### Agent Settings (per agent)

Four tabs in Desktop (`web/js/desktop/shell.js`, `web/desktop.html`):

| Tab | User-facing name | API |
|-----|------------------|-----|
| Attributes | Name, auto-confirm, tags | `PATCH /api/agents/{id}` — `{ name?, auto_confirm?, tags_add?, tags_remove? }` |
| System Prompt | camc marker block in AGENTS.md / CLAUDE.md | Read: `GET /api/agents/{id}/workspace/files/read?path=…`<br>Save: `PATCH /api/agents/{id}` — `{ system_prompt }` |
| Automation | Agent **loop** + host **cron** | `GET/POST/DELETE /api/agents/{id}/cron`<br>POST body includes `type: 'loop' \| 'cron'` plus schedule/name/text or command/cwd |
| Workflow | `workflow.yaml` in agent workspace | Read/write via `/api/agents/{id}/workspace/files/read` and `…/write` |

### Workspace-level modes

| Mode | Desktop reference | API (today / planned) |
|------|-------------------|------------------------|
| **Skills** | `web/js/desktop/skills-mode.js` | `/api/skillm/*` (status, repos, list, install, …) — **available on Hub today** |
| **Todos** | `web/js/desktop/todos-mode.js` (UI only; V0 used localStorage) | Planned: `/api/workspace-services/todos/proxy/*` → gateway `/services/todos/api/todos` (see `docs/desktop-ui-spec.md`) |
| **Bots** | `web/js/desktop/bots-mode.js` (fixtures until Hub wired) | Planned: `/api/bots`, `/api/bots/{id}/dry-run`, `/api/bots/{id}/launch` |
| **Nodes** | `web/js/desktop/nodes-mode.js` | `/api/contexts*`, optional `GET /api/system/ssh-config` |

V2 **must not** ship Desktop-only mocks (Todos localStorage seed, Bots fixtures). Use real Hub routes or hide the feature.

---

## Architecture

```text
Phone — CamUI Mobile V2
  └─ web/mobile.html → web/js/mobile/app.js
       ├─ web/js/api.js
       ├─ web/js/state.js
       └─ web/js/mobile/*

Relay URL + Relay token
  └─ Relay
       └─ camui start (source) → embedded Hub → ssh2 → camc/tmux
```

Parallel to Desktop:

| Desktop | Mobile V2 |
|---------|-----------|
| `web/desktop.html` | `web/mobile.html` |
| `web/js/desktop/*` | `web/js/mobile/*` |
| Direct **or** Relay Settings | **Relay-only Settings** |
| `CamBridge.directHub` (local Hub) | **Not used** — Hub on source via `camui start` |
| Left-nav workspace modes | Hash router + mobile nav |

Do not modify `web/index.html` (V1) for V2 work.

---

## Client-only backlog (Desktop → Mobile V2)

All items below are **renderer work** under `web/js/mobile/`. Reference Desktop modules; do not import `web/js/desktop/*` directly.

| Priority | Desktop reference | Mobile V2 deliverable | API (already works if Desktop Relay works) |
|----------|-------------------|----------------------|---------------------------------------------|
| P0 done | `views/*` + relay `app.js` | `mobile/views/*`, `mobile/app.js` | list/output/input/key/upload |
| P0 done | — | `mobile/settings.js` Relay-only | connect, profiles |
| P0 | `desktop/start-agent-mode.js` | `#/start` already ported; verify payload | `POST /api/agents` |
| P1 | `desktop/shell.js` Agent Settings | `#/agent/:id/settings` + 4 tabs | PATCH, cron, workspace files |
| P1 | `desktop/skills-mode.js` | `#/skills` | `/api/skillm/*` |
| P1 | `desktop/nodes-mode.js` | enhance `#/machines` | `/api/contexts*`, ssh-config |
| P2 | `desktop/agent-console.js` rich/browse | optional output modes | output format, workspace files |
| P2 | `desktop/todos-mode.js` | `#/todos` | workspace-services proxy **when Desktop uses it** |
| P2 | `desktop/bots-mode.js` | `#/bots` | `/api/bots*` **when Desktop uses it** |

Shared extractions (client-only, optional):

- `web/js/shared/agent-helpers.js` — `agentName`, tags, tool normalize (from `shell.js`)
- `web/js/shared/workflow-yaml.js` — workflow parse/serialize (from `shell.js`)

---

## Known embedded-Hub gaps (track for V2; fix on Hub side)

**Deprecated section** — superseded by “Hub assumption (locked)” above. If Desktop Relay works, ignore stub notes in old repo snapshots. Re-open only when **Desktop and Mobile** both fail the same call.

---

## Connection model (Relay-only V2)

### Stored config (localStorage)

| Key | v1 |
|-----|-----|
| `cam_relay_url` | **Required** — `http(s)://…` or `ws(s)://…` |
| `cam_relay_token` | **Required** |
| `cam_profile_kind` | Always `'relay'` for V2 (set on save) |
| `cam_server_url` | Empty — do not use V1 Direct keys |
| `cam_token` | Empty — relay injects token from `camui start` profile |
| `cam_profiles` / `cam_active_profile` | Optional multi-profile (port from legacy `settings.js`) |

### Connect flow

Reuse `CamApi.connect()` in `web/js/api.js`:

1. Configure `{ relayUrl, relayToken }` only.
2. Prefer relay HTTP if URL is `http(s)://` (`/_relay/status`, `/api/system/health`).
3. Fall back to REST-over-WS relay if HTTP fails.
4. On success: `mode === 'relay'`, call `api._requestRelayEventStream()` for WS events.
5. Probe `api.relayStatus()` — toast if relay up but CAM server offline (legacy behavior).

### Polling (Relay-optimized)

Port patterns from `web/js/desktop/app.js` when an agent is selected:

- **Selected agent visible:** prioritize `getAgent(id)` + `agentOutput(id, hash)`; avoid heavy global refresh on every tick.
- **Background:** `listAgents({ refresh: true })` on a slower cadence (~30s).
- **Never retry** live `/output` GETs aggressively (relay timeout multiplication).

### Out of scope for V2 connection

- `CamBridge.directHub.start/stop`
- Auto-detect `location.origin` as direct server (legacy PWA behavior) — disabled in mobile entry
- Embedded Hub / Start Server UI

---

## Navigation and screens

### Top-level routes (hash router)

| Route | Screen | Priority |
|-------|--------|----------|
| `#/` | Agents dashboard (list + filters) | P0 |
| `#/agent/:id` | Agent detail — output, input, quick keys, upload | P0 |
| `#/agent/:id/settings` | Agent Settings shell (4 tabs) | P0 |
| `#/agent/:id/settings/:tab` | `attributes` \| `prompt` \| `automation` \| `workflow` | P0 |
| `#/start` | Start agent | P0 |
| `#/nodes` | Nodes / hosts / contexts | P1 |
| `#/skills` | Skills (Repos + Install) | P1 |
| `#/todos` | Todos worklog | P2 (gated on Hub) |
| `#/settings` | Relay profiles & connect | P0 |

Optional later: `#/bots`, `#/agent/:id/files` (workspace browse).

### Agent detail (P0)

Keep proven mobile UX from `web/js/views/agent-detail.js`:

- `visualViewport` keyboard shrink
- Pinch-to-zoom output font size
- Floating copy button (Android WebView fullscreen)
- Quick-key palette + expandable extra keys
- File upload via `uploadFile`
- Overflow: Restart, Stop, Remove, **Settings** (new — opens settings route)

### Agent Settings (P0) — mobile layout

**One panel at a time** (no Desktop split view):

| Tab | Mobile UI |
|-----|-----------|
| Attributes | Form: name, auto-confirm toggle, comma-separated tags → map to `tags_add` / `tags_remove` on save |
| System Prompt | Textarea + Load / Save / Clear; show target file hint from tool (AGENTS.md vs CLAUDE.md) |
| Automation | List of loops + cron cards; Add sheet with type toggle; reuse Desktop field validation semantics |
| Workflow | **Raw YAML only** in v1 (textarea + Load/Save/Validate); visual DAG editor deferred |

Logic reference: `web/js/desktop/shell.js` (do not import desktop modules — copy shared helpers into `web/js/shared/` if needed).

### Nodes (P1)

Merge behaviors:

- Read-only aggregation from legacy `web/js/views/machines.js`
- CRUD from `web/js/desktop/nodes-mode.js` (create/update/delete context, sync, delete host)

If `GET /api/system/ssh-config` returns 404, hide Import SSH; show manual add only.

### Skills (P1)

Port `skills-mode.js` two-tab layout:

- **Repositories** — add/edit/remove/refresh; Git token one-shot (never persist)
- **Install** — repo filter, search, multi-select, install targets

Gate tab on `skillmStatus(context)` — hide or show unavailable if Hub returns `not_ssh` / error.

### Todos (P2)

**Do not** use Desktop localStorage mock.

1. On connect, `GET /api/workspace-services` (when Hub implements it).
2. If `todos` service `ready`, proxy CRUD via `/api/workspace-services/todos/proxy/…`.
3. UI: single-column outline + detail sheet (Inbox / Tasks / Notes / Projects / Archive tabs from CAM-DESK-TODOS-011).
4. If unavailable: show empty state with “Todos not enabled on this Hub”.

Exact proxy subpaths must match the Hub implementation; mobile uses a thin `workspaceServiceProxy()` wrapper so subpath changes stay in one file.

### Settings (P0)

Relay-only form:

- Relay URL
- Relay token
- Save & Connect / Test
- Saved profiles (optional, from legacy settings)
- Connection status + `relayStatus()` server_connected indicator
- Reload (Android: `CamBridge.restartApp`)

No Direct tab, no server URL, no API token field in v1.

---

## API surface (Relay client)

All calls go through existing `CamApi.request()` — relay HTTP or relay WS transparent to the renderer.

### Already in `web/js/api.js` (use as-is)

**Agents:** `listAgents`, `getAgent`, `startAgent`, `stopAgent`, `updateAgent`, `restartAgent`, `deleteAgentHistory`, `agentLogs`, `agentOutput`, `agentFullOutput`, `sendInput`, `sendKey`, `uploadFile`

**Agent automation:** `agentCronJobs`, `createAgentCronJob`, `deleteAgentCronJob`

**Contexts / nodes:** `listContexts`, `getContext`, `createContext`, `updateContext`, `deleteContext`, `syncContext`, `sshConfigHosts`

**Workspace files (agent-scoped):** `agentListWorkspaceFiles`, `agentReadWorkspaceFile`, `agentWriteWorkspaceFile`

**Skills:** `skillmStatus`, `skillmRepos`, `skillmList`, `skillmRepoAdd/Update/Remove/Refresh`, `skillmInstall`, …

**System:** `health`, `relayStatus`, `config`, `onEvent`

### To add in mobile client only (`web/js/mobile/api-extensions.js`)

Documented here; implement when building Todos/Bots — **no server changes**:

```text
GET  /api/workspace-services
GET  /api/workspace-services/:service/status
ANY  /api/workspace-services/:service/proxy/*
```

```text
GET  /api/bots?context=…
GET  /api/bots/:id
POST /api/bots/:id/dry-run
POST /api/bots/:id/launch
```

Feature detection: call once after connect; cache `{ todos, skills, bots }` availability in mobile app state.

---

## Shared state model

Extend `web/js/state.js` minimally (or mobile-local overlay):

```javascript
{
  agents, contexts, adapters,
  connectionMode,          // 'relay' | 'disconnected' (v1)
  filters: { status, tool, machine },
  toast,
  hubFeatures: {          // mobile-only
    todos: 'unknown' | 'ready' | 'unavailable',
    bots: 'unknown' | 'ready' | 'unavailable',
    skillm: 'unknown' | 'ready' | 'unavailable',
  },
}
```

Keep `selectedAgentId` in route params (`#/agent/:id`) rather than global state when possible.

---

## Implementation phases (V2, Relay-only)

### Phase 0 — Scaffold

| Task | Deliverable |
|------|-------------|
| New entry | `web/mobile.html`, `web/css/mobile.css` |
| Bootstrap | `web/js/mobile/app.js` — Relay-only init, hash router |
| Settings | `web/js/mobile/settings.js` — Relay form + profiles |
| Android | Point `android/build.sh` asset copy at `mobile.html` (or ship both entries) |
| Shared API | Import `web/js/api.js` unchanged |

**Exit criteria:** Connect to relay, show empty agents list, settings persist.

### Phase 1 — Core agent loop (P0)

| Task | Deliverable |
|------|-------------|
| Dashboard | Port/refine `dashboard.js` filters + list |
| Agent detail | Port `agent-detail.js` relay polling + mobile UX |
| Start agent | Port `start-agent.js` |
| Agent Settings | New `agent-settings.js` — 4 tabs wired to APIs above |
| Events | Relay event stream + status_update handling |

**Exit criteria:** Full loop on Relay — list → detail → input/keys → settings edit → start/stop/restart.

### Phase 2 — Nodes + Skills (P1)

| Task | Deliverable |
|------|-------------|
| Nodes | `nodes.js` — host cards, context CRUD, sync |
| Skills | `skills.js` — repos + install |

**Exit criteria:** Add SSH context from phone; install a skill on setup node via relay.

### Phase 3 — Todos + polish (P2)

| Task | Deliverable |
|------|-------------|
| Todos | `todos.js` via workspace-services proxy |
| Feature gates | Hide tabs when Hub lacks service |
| iOS | WKWebView shell mirroring Android `CamBridge` |
| Rich output | Optional `format=rich` toggle (default plain) |

### Deferred (post–Relay v1)

- Direct connection (Advanced settings)
- Bots UI (until `/api/bots` live on Hub)
- Workflow visual editor
- Terminal / xterm attach
- Embedded Hub / Start Server

---

## Client file plan

| Action | Path |
|--------|------|
| **Create** | `docs/mobile/relay-first-plan.md` (this file) |
| **Create** | `web/mobile.html` |
| **Create** | `web/css/mobile.css` |
| **Create** | `web/js/mobile/app.js`, `settings.js`, `dashboard.js`, `agent-detail.js`, `agent-settings.js`, `start-agent.js`, `nodes.js`, `skills.js`, `api-extensions.js` |
| **Create** | `web/js/shared/workflow-yaml.js` (optional extract from Desktop shell logic) |
| **Reuse unchanged** | `web/js/api.js`, `web/js/state.js` |
| **Reference only** | `web/js/desktop/*`, `web/js/views/*` |
| **Do not modify** | `apps/cam-desktop/electron/*`, `embedded-hub.cjs`, `src/cam/**`, camc |

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Relay latency makes Settings tabs feel slow | Lazy-load tab content; show skeleton only for active tab |
| Todos/Bots proxy shape differs from ui-spec | Centralize paths in `api-extensions.js`; feature-detect at runtime |
| Workflow YAML editing on small screens | Raw editor only in v1; monospace + validate before save |
| Accidental Direct/Hub code in mobile | Code review gate: no `CamBridge.directHub`, no `cam_profile_kind=direct` in v1 |
| Fork drift from legacy `index.html` | New entry `mobile.html`; legacy untouched until explicit cutover |
| Skillm token leakage | One-shot fields; clear on submit; never write token to localStorage |

---

## Success criteria (V2)

1. Connects **only** with Relay URL + Relay token to a **`camui start`** source.
2. Agent loop: list → detail → output poll → input/key/upload.
3. Agent Settings tabs work against embedded Hub (not Python `cam serve`).
4. Start agent + restart when Hub routes are live (or hidden until un-stubbed).
5. Skills via `/api/skillm/*` on setup node.
6. Mobile PRs touch only `web/mobile.*`, `web/js/mobile/*`, `docs/mobile/*`, Android packaging.
7. APK ships `mobile.html`; V1 `index.html` untouched until cutover.

---

## Open questions

1. V2 beta: parallel APK (`mobile.html`) vs replace V1 entry in store?
2. Bottom tabs vs hamburger — product pick before Phase 1 UI polish.
3. Which Desktop-only modes ship in V2.1 first: Agent Settings vs Skills vs Nodes CRUD?

---

## Revision history

| Date | Change |
|------|--------|
| 2026-06-20 | Initial Relay-first plan |
| 2026-06-20 | Renamed to **CamUI Mobile V2**; backend = **CamUI start / embedded Hub** (not legacy `cam serve`); added Hub stub gap table |
| 2026-06-20 | **V2.0.0 APK** built (`android/build/camui-v2-2.0.0.apk`); Hub assumed good via Desktop Relay — **client-only** backlog |
