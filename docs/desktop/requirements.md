# CAM Desktop Requirements

Status: active requirement registry
Owner split: `camui-rev` owns requirements and review; `camui-dev` implements.

This document is the stable source of truth for Desktop requirements. Milestone
specs such as `docs/desktop-ui-spec.md` explain current design direction, but
implementation and review should cite the IDs here.

## Architecture and Connection Model

CAM is a **hub / control-plane** product. Coding agents always run on a
remote controller/node — never inside the Desktop process. The Desktop is
a thin UI/client over the hub API.

```text
Desktop UI  ──HTTP/WS──▶  CAM Hub/API  ──poll/route──▶  Remote Controller/Node  ──spawns──▶  tmux + agent runtime
```

Term glossary, used consistently across the docs:

| Term | Meaning |
| --- | --- |
| **Node** / **Controller** | The machine/runtime where agents actually run. Hosts the controller-side runtime (`camc`/controller adapter), tmux, Python, and the agent CLIs (Claude, Codex, Cursor, …). |
| **Hub** | The CAM control plane. Aggregates and persists node + agent + context tables, and routes commands back to the right controller. Direct mode embeds this Hub inside Electron/Node; Relay mode reaches an external Hub through a relay. Both expose the same REST/WS surface at `/api/...`. |
| **Desktop** | The Electron app in `apps/cam-desktop/`. Renders the hub's tables, sends user actions through `CamApi`, and (later) opens interactive attach streams to the selected agent's controller. |
| **Local** | A node that happens to be reachable on `localhost` / WSL / a local Mac. It is a **special case of Node**, not a separate architecture. |

The **active** user-visible connection modes are two:

| Mode | User intent | Implementation surface |
| --- | --- | --- |
| **Direct** (default) | "Run CAM from this Desktop and let it manage my nodes." | Electron main starts an embedded Node/Electron CAM Hub, generates/persists its API token, connects the renderer to that local Hub over loopback, and uses Hub APIs to manage remotes/nodes. No local Python, WSL, macOS/Linux shell, or `cam` CLI is required. |
| **Relay** | "Use an existing CAM Hub through a relay endpoint." | Relay URL + relay token + CAM API token. Relay proxies the same REST/WS surface. |

**Managed** is **not** a third UI mode. The term refers to deployment
policy around Direct: enterprise defaults, bundled runtime bits, node
policy, or pre-provisioned credential references. There is no separate
"Managed" tab in Settings.

A future **SSH attach / transport** path is on the roadmap but not in
the active Desktop scope (see SSH-010..013, status `proposed`):

| Mode | User intent | Implementation surface |
| --- | --- | --- |
| SSH attach (future) | "Open a terminal to this agent's controller, or manage a controller without the hub." | Server-mediated WebSocket terminal attach (preferred) or a future SSH transport. Desktop must not own SSH key management as the primary product path. |

Two consequences of this model are first-class requirements (see ARCH-010..013
below):

- The **Desktop flow must not require the user to open a terminal or
  hand-install local runtime dependencies**. Direct must be self-contained
  in the installed Electron app: no WSL, no local Python, no local
  `cam serve`, no local `cam` CLI. tmux, Python, and agent CLI
  dependencies live on the controller/node.
- The renderer is a **client**, not a runtime. It does not parse terminal
  screens as a primary API, does not execute shell commands, and does not
  own SSH keys, vendor credentials, or agent CLI logins.

The earlier "Local" tab is retired as a separate product concept. Its
useful part is promoted into **Direct**: the app-managed embedded CAM Hub is
the Direct path. Users should not need to open a terminal to start CAM.
Desktop may still use bundled JavaScript/Node packages inside the Electron
app, but it must not depend on host-machine WSL, Python, a shell workflow,
or a separately installed CAM runtime.

## Requirement ID Scheme

Format:

```text
CAM-DESK-<AREA>-<NNN>
```

Areas:

- `ARCH`: desktop architecture and process boundaries (hub/controller/desktop model).
- `HUB`: behavior of the CAM hub / control plane the Desktop talks to.
- `NODE`: behavior of a controller/node where agents actually run.
- `DIRECT`: Desktop-managed embedded Electron/Node CAM Hub lifecycle and Direct profile.
- `REMOTE`: Relay connection to an external CAM Hub and compatibility with older connection profiles.
- `SSH`: future SSH-mediated terminal attach and controller transport.
- `CONN`: endpoint/profile connection behavior (legacy — now a subset of REMOTE).
- `AGT`: agent list and selection.
- `RUN`: starting new agents.
- `EDIT`: editing existing agents.
- `OUT`: output capture and rendering.
- `INP`: input composer and key sending.
- `FILE`: uploads, attachments, and context files.
- `SET`: settings mode.
- `PKG`: packaging and install.
- `TERM`: future interactive terminal.
- `NODEUI`: active Nodes / Remotes workspace mode in the Desktop UI (read-mostly view of hub-provided nodes built from `state.contexts` + `state.agents`).
- `LOC`: historical Local-tab experiment. Superseded by the active Direct-local-Hub requirements.
- `SEC`: security constraints.
- `VFY`: verification requirements.

Statuses:

- `proposed`: requirement is being shaped.
- `approved`: requirement is ready for implementation.
- `implemented`: code claims to satisfy it.
- `verified`: reviewer has checked it.
- `deferred`: intentionally later.

Rules:

- IDs are stable once assigned. Do not renumber; add a new ID if behavior changes.
- Implementation tasks must list the IDs they intend to satisfy.
- Review replies must mark each touched ID as pass/fail/deferred.
- Product code comments do not need to include IDs unless the link prevents ambiguity.

## Phase Map

Active phases:

| Phase | Scope |
| --- | --- |
| Phase 1 | Electron shell reusing WebUI client code; agent list, selected output, composer, quick keys, settings mode. All driven through a hub URL + CAM API token. |
| Phase 3 | Rich output (verified) and future interactive terminal. Terminal attach must go through hub-provided attach metadata or the future SSH transport — not through a local PTY assumption. |
| Phase 4 | Desktop feature parity with the mobile/WebUI surface: start agent, edit agent, input attachments, selected context/file workflows. All hub-mediated. |
| Phase 2E | Direct embedded CAM Hub: Desktop starts/owns an Electron/Node Hub, connects to it directly over loopback, and manages remotes/nodes through Hub APIs. |
| Phase 5 (proposed) | SSH attach and remote-controller management — see the SSH requirement area below. |
| Windows packaging | MSI build + per-user install verified on the VDI. Documented under PKG-010..; not a numbered phase. |

Deferred / historical scope (kept for requirement stability; not active as
separate user-facing modes):

| Phase | Scope | Status |
| --- | --- | --- |
| Phase 2A | Detect-only readiness probe for a same-machine controller. | deferred |
| Phase 2B | Bootstrap CAM into an existing WSL distro. | deferred |
| Phase 2C | Full Windows setup when WSL is absent. | deferred |
| Phase 2D | Offline / internal-mirror bootstrap. | deferred |

## Approved / Verified Baseline

| ID | Status | Requirement | Evidence |
| --- | --- | --- | --- |
| CAM-DESK-ARCH-001 | verified | Desktop runtime is Electron, not Tauri, for the active product path. Dormant Tauri files may remain as reference only. | `apps/cam-desktop/package.json` uses `electron/main.cjs` and Electron scripts. |
| CAM-DESK-ARCH-002 | verified | Desktop renderer reuses existing WebUI `CamApi` and `AppState`; it does not fork a separate CAM client stack. | `web/js/desktop/app.js` imports `../api.js` and `../state.js`. |
| CAM-DESK-CONN-001 | verified | Desktop stores connection profile state using the same localStorage keys as WebUI for compatibility. | Settings mode reads/writes `cam_server_url`, `cam_token`, `cam_relay_url`, `cam_relay_token`. Direct now treats those fields as internal app-managed Hub profile state. |
| CAM-DESK-AGT-001 | verified | Agents mode shows a left-side agent list and selects one agent as the main target. | Desktop shell and agent console reviewed in Phase 1/2A. |
| CAM-DESK-OUT-001 | verified | Selected agent output is shown through existing `/api/agents/{id}/output` and `/fulloutput` polling. | `web/js/desktop/agent-console.js` uses `api.agentOutput()` and `api.agentFullOutput()`. |
| CAM-DESK-INP-001 | verified | Composer sends selected-agent text through existing WebUI API and supports Enter-to-send with Shift+Enter newline. | `web/js/desktop/agent-console.js`. |
| CAM-DESK-INP-002 | verified | Quick-key controls send special keys to the selected agent and are disabled when no active agent is selected. Desktop mirrors the mobile quick-key palette: y/n/1 + Enter/Escape/C-c/Backspace in the primary row, plus a foldable extra key/input grid for 2/3, Tab, Shift-Tab, Delete, arrows, Home/End, PageUp/PageDown, and common symbols. Direct mode implements `POST /api/agents/{id}/key` by running remote `~/.cam/camc key <id> --key <key>` through the embedded SSH transport. | `web/js/desktop/agent-console.js`, `apps/cam-desktop/electron/embedded-hub.cjs`. |
| CAM-DESK-SET-001 | verified | Settings is a mode/page in the main pane, not a modal required for normal use. | `web/desktop.html`, `web/js/desktop/settings-mode.js`. |
| CAM-DESK-SET-002 | approved | Settings includes an **Appearance** tab for desktop-only display preferences. It must provide theme selection (`dark`, `light`, `system`) with dark as the default, plus conservative UI and agent-output font-size controls. Settings persist in localStorage, apply immediately without reload through `body.desktop` data attributes/CSS custom properties, and must not edit mobile/PWA files. Light mode is a v1 variable override for readable surfaces, borders, inputs, buttons, sidebars, Settings/Start/Nodes, Plain/Rich agent output, floating output controls, rich-code blocks, and xterm terminal surfaces. | `web/desktop.html`, `web/js/desktop/settings-mode.js`, `web/css/desktop.css`. |
| CAM-DESK-UI-001 | approved | Phase 1 modern UI polish is a CSS-first, high-ROI pass only: app-wide thin rounded scrollbars, normalized small-button/input radius and hover states, softer shared surface/border tokens, and clearer selected/hover states for Agents and Nodes. It must not redesign workflows, add animations, add dependencies, change mobile/PWA files, or reduce density in Agents/Nodes. Start and Settings remain centered fixed-width rails with left-aligned content; Agents remains full-width for output/terminal use. | `web/css/desktop.css`; optional small HTML/class tweaks only if a CSS hook is missing. |
| CAM-DESK-PKG-001 | verified | Windows build produces a single MSI artifact. | VDI produced `CAM Desktop 0.2.0.msi`. |
| CAM-DESK-PKG-002 | verified | MSI installs per-user without admin on locked-down Windows VDI. | `msiexec exit=0`; install path `%LOCALAPPDATA%\Programs\cam-desktop`. |
| CAM-DESK-PKG-003 | verified | Installed app launches on the VDI after MSI install. | `CAM Desktop.exe` stayed alive after 8s with Electron process tree. |
| CAM-DESK-SEC-001 | verified | Renderer is isolated: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`. | `apps/cam-desktop/electron/main.cjs`. |
| CAM-DESK-SEC-002 | verified | Renderer cannot pass arbitrary commands to the main process. | `CamBridge` exposes fixed readiness/start methods only. |

## Hub / Controller / Node Requirements

These requirements pin the architecture model: Desktop is a thin client over
a CAM hub, the hub aggregates state from one or more controllers/nodes, and
agents always run on a controller — never inside the Desktop process.

### Desktop architecture (ARCH)

CAM-DESK-ARCH-001 and CAM-DESK-ARCH-002 are kept (Electron runtime, WebUI
client reuse) in the Approved/Verified Baseline above. The IDs below are
new this revision.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-ARCH-010 | approved | The active architecture is `Desktop UI → CAM Hub/API → Remote Controller/Node → tmux/agent runtime`. Desktop is a thin UI/client; coding agents run on the controller, never inside the Desktop process. |
| CAM-DESK-ARCH-011 | approved | The active Desktop UX must not require the user to open a terminal or install WSL, local Python, local `cam`, or local `cam serve`. Direct is self-contained in the installed Electron app. Remote agent runtime dependencies (tmux, Python, agent CLIs, etc.) live on the controller/node. |
| CAM-DESK-ARCH-012 | approved | "Node" is any machine reachable as a CAM controller (including `localhost` / WSL / a local Mac). Local is a special case of Node, not a separate architecture. Desktop must not invent a second machine registry; nodes/contexts are owned by the hub. |
| CAM-DESK-ARCH-013 | approved | The renderer never executes shell commands. All controller-side operations go through hub API calls (or, in the future, an explicit SSH transport gated by SSH-010..). Desktop's `CamBridge` is restricted to narrow, fixed methods. |

### Hub (HUB)

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-HUB-010 | approved | The hub is the source of truth for the node / agent / context tables. In Direct mode this hub is embedded in Electron main/Node and persists state under the app data directory. In Relay mode the hub is external. Both modes expose the same data model to the renderer. |
| CAM-DESK-HUB-011 | approved | The hub exposes the REST/WS surface Desktop already consumes through `web/js/api.js` (`/api/agents`, `/api/contexts`, `/api/system/health`, `/api/ws`, agent output / input / key / upload, etc.). New Desktop features must use this surface rather than reach around it. The Direct implementation must be Electron-native, not a wrapper around local Python `cam serve`. |
| CAM-DESK-HUB-012 | approved | The hub routes user actions back to the correct controller. Desktop renderer sends the action to the local embedded hub or relay-backed hub; controller transport details stay in hub/main-process code. |
| CAM-DESK-HUB-013 | proposed | The hub should surface attach metadata for the selected agent (controller host, transport hints, capability flags) so the Desktop terminal/attach paths in TERM/SSH can connect without baking controller details into the renderer. |

### Controller / Node (NODE)

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-NODE-010 | approved | A controller/node is the machine running `cam` (the CLI / `camc`) plus tmux and the agent runtimes. The hub talks to a controller via the existing transports; Desktop does not talk to controllers directly. |
| CAM-DESK-NODE-011 | approved | Nodes are represented by existing CAM **contexts** (and, where relevant, the `node` concept already in the codebase). Desktop's `Remotes` UI must use the hub's context/node store; it must not maintain a Desktop-only inventory. |
| CAM-DESK-NODE-012 | approved | Controller-side prerequisites (Python 3.10+, tmux, `cam[server]`, agent CLIs logged in) are the responsibility of whoever provisions the node. Desktop does not install them. |
| CAM-DESK-NODE-013 | proposed | A managed enterprise deployment is expected to provide a unified, pre-provisioned set of controllers behind the hub. Desktop UI should treat that set as "Remotes" without requiring per-user setup. |

### Direct embedded Hub (DIRECT)

Direct is the default no-relay path. Desktop starts and owns an embedded CAM
Hub implemented in Electron/Node, connects the renderer to it through the
existing CamApi surface, and lets that Hub manage remote hosts/nodes.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-DIRECT-010 | approved | Settings must expose **Direct** as the default mode. Direct means an app-managed embedded CAM Hub, not a user-entered external URL. |
| CAM-DESK-DIRECT-011 | approved | Electron main owns Direct Hub lifecycle: start/check/stop the embedded Electron/Node hub, generate an API token, bind only to loopback, and keep lifecycle state inside the app. Direct must not spawn local `cam serve`, require local Python, require WSL, or require a host shell. The renderer cannot pass command strings, argv, env, or filesystem paths. |
| CAM-DESK-DIRECT-012 | approved | The renderer connects to the Direct Hub through the same CamApi REST/WS surface used by all other modes. The local Hub URL and generated API token are internal profile state; they may appear only in Advanced/Diagnostics with token redaction/fingerprint. |
| CAM-DESK-DIRECT-013 | approved | Direct must not require a user-visible terminal workflow or local runtime setup. The Direct readiness path checks only the embedded hub and app-owned storage/ports. It must not show "CAM missing" because the host lacks a `cam` CLI. Direct must also be the **default startup mode**: when no Relay profile and no Direct profile are saved, Desktop must auto-start the embedded Hub and connect, landing the user on Agents (or whichever workspace mode was last persistent) — not on Settings. An empty `contexts:[]` / `agents:[]` Hub is a valid running state. |
| CAM-DESK-DIRECT-014 | approved | Direct Hub owns the node/remote registry. Desktop must not keep a parallel private node registry; Nodes/Remotes UI reads/writes Hub contexts/nodes through Hub APIs. |
| CAM-DESK-DIRECT-015 | approved | Direct must present a clear Node/Host vs Context boundary. A Node/Host owns transport and identity fields: node display name, transport (`ssh` or `local`), host, port, SSH user, auth method, key-file path reference, credential metadata/reference, tags/owner. A Context owns workspace fields on that node: context name, remote workspace/path, and environment setup. The current embedded-Hub storage may still duplicate `machine` fields on each context for compatibility, but the user-facing model and renderer actions must treat those host fields as Node-owned. Raw private keys are never returned to the renderer. The registry is persisted by the embedded hub under app data, not by renderer localStorage. A normalized `/api/nodes` store remains future work. |
| CAM-DESK-DIRECT-016 | approved | The Nodes page renders a **host-card list**. Each card represents one host (endpoint identity `(type \| user \| host \| port)` — `user` is part of the key, so `hren@h:22` and `vmhren@h:22` are two distinct cards). Cards fold/unfold at two levels: (a) the host card itself — collapsed shows host identity + counts; clicking the header expands to reveal the host-action stack and the contexts list; at most one card is expanded at a time and the first card is expanded by default; (b) each context row inside an expanded card folds independently — collapsed shows context name + remote path + a one-line `last sync: …` summary + small [edit]/[delete] buttons; clicking the row header reveals env_setup and the full last-sync diagnostic (status code + detail + relative time). A single primary **Add Host** button toggles an inline manage panel containing two segments: **Add manually** and **Import from SSH config**. Because the compatibility API still creates context records, Add Host creates a host plus one initial workspace/context (`name`, `path`, `env_setup`). Host-level actions live on the expanded card body: **Filter Agents**, **Edit Host**, **Sync Host**, **New Context**, **Delete Host**. Context-level actions live on each context row: **Edit Context** (context name read-only, Remote path + Env setup editable; host fields visible but disabled/grey) and **Delete Context** (single-context delete). There is no per-context Sync; Sync Host covers all contexts on the host and surfaces the result back on each row's last-sync badge. Edit Host reuses the manage panel with workspace fields hidden and applies the change to every context on the host via `api.updateContext` while preserving each context's name/path/env_setup. Edit Context reuses the same manage panel with host fields shown read-only/disabled as context-bound metadata and is a single PUT touching only `path` + `env_setup`; context name is shown read-only and is not renamed. New Context reuses the manage panel in add-context mode: host fields are hidden (inherited from the parent host) and only Name/Remote path/Env setup are entered; for password-auth hosts the password sub-form is shown and Remember password is required (the embedded Hub does not auto-clone credential references to new contexts, so the user must re-enter the password). Delete Host prompts with the endpoint identity plus affected context names, then calls `api.deleteContext(name)` for each context. Delete Context prompts with that context's name only and removes only that record. Sync Host syncs one representative context per host and marks sibling contexts as `covered`/`covered_failed` in their persistent badge to avoid duplicating the same remote `camc` agent list under multiple `context_name`s. Direct mode also refreshes one representative SSH context per endpoint on the desktop poll cadence so imported agents receive fresh `updated_at` values for sidebar sorting without duplicating sibling contexts. Sync responses are HTTP 200 with `{ok, imported, results:{camc:'updated'\|'unchanged'\|'failed'}, error?, detail?}`; per-context diagnostics and the status line persist after toasts disappear. No remote/destructive cleanup (`deleteAgentHistory`, controller-side cleanup) is performed by any action. Test-connection, normalized `/api/nodes`, and destructive Delete-agents/Clean/Stop-all actions still require separate explicit requirements. |
| CAM-DESK-DIRECT-017 | approved | SSH config import belongs to embedded Hub node management. The embedded Hub exposes `GET /api/system/ssh-config` which parses the host OS's `~/.ssh/config` using Node `fs`/`os` only (no shell, no WSL). Only concrete `Host` aliases without wildcards are returned; multiple aliases on one `Host` line correctly inherit the same `HostName/User/Port/IdentityFile`; `Include` directives are not followed in this pass. Each suggestion carries `{alias, host, user, port, identity_file}` where `identity_file` is a **path reference only** — file contents are never read or returned. The renderer must never receive private key material. The Nodes page Add Host panel also exposes a Browse button that calls `CamBridge.files.pickPrivateKey()` — a parameter-free Electron `dialog.showOpenDialog` that returns the chosen path or null. The renderer cannot influence the dialog filters, title, or default location. |
| CAM-DESK-DIRECT-018 | approved | SSH credentials are managed by Electron main. The embedded Hub stores **metadata only** in `embedded-hub.json`: `auth_method` ∈ `{key, password, agent}`, `key_file` (path reference for `auth_method=key`), and — if the user opts into Remember — `credential_ref`, `credential_kind` (`password` or `passphrase`), and `credential_saved` boolean. Raw password / passphrase values are encrypted via Electron `safeStorage` (`safeStorage.encryptString`) and persisted to a separate `<userData>/embedded-hub-credentials.json` blob store; the embedded Hub never returns the raw secret from `/api/contexts`, the renderer never persists secrets to `localStorage`, and `embedded-hub.json` never contains plaintext password/passphrase bytes. If `safeStorage.isEncryptionAvailable() === false` and the user checks Remember, the POST/PUT returns a clear 400 (`safe_storage_unavailable`) and nothing is saved. Context delete cascades to drop the related credential refs. ssh-agent / default-key-search remains an auth option (`auth_method=agent`) that stores no secrets. |
| CAM-DESK-DIRECT-019 | approved | Direct and Relay both resolve to the same downstream agent UI once connected. Agent list, Start, Edit, Nodes, output, and input flows should not branch on transport except for connection/diagnostics labels. The embedded Hub uses the pure-Node `ssh2` client (no system `ssh`, no WSL, no PowerShell) to reach remote nodes. The ssh2 module is bundled with the Electron app under `asarUnpack` so its optional `cpu-features` native module is loadable from disk. Plaintext password/passphrase is read from `credentialStore.get(credential_ref)` inside Electron main only at the moment ssh2 connects; it is never sent in API responses, persisted in `embedded-hub.json`, or written to log files. **The embedded Hub maintains an in-process long-lived `ssh2.Client` pool — the self-contained equivalent of CAM's OpenSSH `ControlMaster` / `ControlPersist=600` behavior used by `src/camc_pkg/remote.py` and `src/cam/transport/ssh.py`.** One pooled connection per endpoint/auth identity, keyed by `(host, user, port, auth_method, key_file, secret-digest)` where the secret-digest is a SHA-256 truncated digest of any present password/passphrase so raw secrets never enter the pool key, pool diagnostics, or log files. The same pooled client serves both `execRemote()` and `writeRemoteFile()`; each operation opens its own ssh2 channel (`conn.exec` or `conn.sftp`) but does **not** open a new TCP/auth handshake. Entries idle-close after ~600s of zero in-flight operations. Pool entries are dropped on `close`/`end`/`error` from the underlying client, on connect failure, on auth failure, and when an operation timeout requires destroying the client. Each response may carry an optional `timings:{pooled,connect_ms,op_ms,total_ms}` field for diagnostics; renderer code is unaffected. Test helpers `_setSsh2ForTests(mod)`, `closeAll()`, and `poolStats()` are exposed for focused smoke tests without changing the renderer API. Before any remote `camc` operation, the embedded Hub verifies `~/.cam/camc` by comparing its 12-char MD5 prefix with the bundled `resources/camc/camc`; it uploads the bundled copy when the remote file is missing, not executable, or hash-different, matching CAM's deploy/upgrade sync policy. |

Implementation notes:

- The previous Direct prototype that launched `cam serve --host 127.0.0.1`
  from Electron main is superseded. It proved the Settings and lifecycle
  shape, but it is not acceptable for the active product because it depends
  on local Python / local `cam`.
- Direct provides an Electron-native hub service in the app process tree
  (`apps/cam-desktop/electron/embedded-hub.cjs`). It listens on a loopback
  port so the existing `CamApi` direct HTTP/WS client continues to work,
  but the server implementation is pure Node — `http`, `crypto`, `fs`,
  `path`. No `child_process`, no shell, no host runtime dependency.
- DIRECT-014..017 are now **implemented in the embedded Hub** for the
  context/node registry: `POST /api/contexts`, `GET /api/contexts/{name}`,
  `PUT/PATCH /api/contexts/{name}`, `DELETE /api/contexts/{name}`, and
  `GET /api/system/ssh-config` (read-only suggestion list parsed from
  the host OS's `~/.ssh/config`). Records carry
  `{id (uuid), name, path, machine:{type,host,user,port,key_file,env_setup}, tags, created_at, last_used_at}`.
  Validation: `name` matches `[A-Za-z0-9_-]{1,64}`; SSH contexts
  require host + user; duplicate name returns 400; port must be 1..65535.
  The Nodes page exposes Add Host, Import SSH Config, Refresh, Filter
  Agents, and Sync.
- DIRECT-018 (credential backend: ssh-agent / OS keychain / enterprise
  secret store) remains **next-stage**. The current slice stores
  `key_file` as a path reference only; the embedded Hub never reads
  private key contents and never returns them to the renderer. A
  follow-up brief will pin down the credential backend and the
  test-connection workflow.
- Agent-side mutating routes (`POST /api/agents`, `PATCH
  /api/agents/{id}`, `DELETE /api/agents/{id}`, `/input`, `/key`,
  `/upload`, `/restart`) still return 501 in this slice — they require
  a remote-controller transport from the embedded Hub. The renderer
  surfaces 501 responses through the existing CamApi error path.
- The first embedded-Hub slice does not implement `/api/ws`; CamApi's
  event-stream fallback handles the ws-close cleanly and polling
  continues at the existing 5s cadence. WS will land alongside event
  push from the embedded Hub when there is real state to push.
- Storage in the first slice: a single JSON file at
  `<userData>/embedded-hub.json` with `{ contexts: [], agents: [],
  adapters: [...] }`. No native database dependency.

### Relay / external connection (REMOTE)

REMOTE supersedes the legacy `CONN` area for new work; `CAM-DESK-CONN-001`
in the Approved Baseline still applies (existing localStorage profile keys).

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-REMOTE-010 | superseded | Older pure-client Direct profile (user-entered hub URL + token). Superseded by CAM-DESK-DIRECT-010..019. |
| CAM-DESK-REMOTE-011 | superseded | Older Direct mode connected to a reachable external hub URL. Superseded by app-managed Direct Hub lifecycle. |
| CAM-DESK-REMOTE-012 | approved | **Relay** mode wraps the same hub/controller surface behind a relay endpoint (relay URL + relay token + CAM API token). The relay forwards REST/WS for unreachable hubs. The CAM API token is required because proxied REST calls still need bearer auth. |
| CAM-DESK-REMOTE-013 | approved | The downstream UI must not branch on which connection kind created the session (Direct or Relay). Both resolve into the same `CamApi` surface. |
| CAM-DESK-REMOTE-014 | approved | The active Settings UI exposes exactly two connection modes: **Direct** and **Relay**. There is no separate "Managed" tab and no separate "Local" tab. |

### SSH attach / transport (SSH)

SSH is the future path for interactive terminal attach and (optionally) for
managing a controller without going through the hub. It is **not** a
replacement for the hub model and is **not** about rewriting CAM core.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-SSH-010 | proposed | Terminal attach to the selected agent must connect to that agent's controller/node. Desktop must obtain the attach endpoint and any necessary one-shot credential from the hub (HUB-013) — it must not require local tmux and must not own the user's SSH keys. |
| CAM-DESK-SSH-011 | proposed | If a server-mediated WebSocket attach is available (preferred), Desktop uses it. The renderer streams only terminal bytes + resize + agent ID; main-process IPC mirrors the existing CamBridge.terminal shape from the v3 stash and remains argv-only. |
| CAM-DESK-SSH-012 | proposed | If a direct SSH transport is added later, it must be opt-in, advanced, and use a system SSH client (`ssh` binary) or a vetted library. Keys are read from the user's existing SSH config; Desktop does not store, generate, or import private keys. |
| CAM-DESK-SSH-013 | proposed | SSH attach must not require local tmux on the user's machine. The remote `cam` CLI on the controller already runs the tmux client; Desktop just pipes the PTY bytes. |

## Rich Output Requirements

Goal: make the desktop output pane visually preserve terminal color/style without
turning the first implementation into a full interactive terminal.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-OUT-010 | verified | Add a rich-output capture path that preserves ANSI SGR/style sequences from tmux. The existing plain-text output path must remain unchanged for mobile, search, copy, and low-risk fallback. |
| CAM-DESK-OUT-011 | verified | The output API contract is explicit: `GET /api/agents/{id}/output` and `/fulloutput` return plain text by default with no `format` query. The Hub may tolerate an optional `format` query for compatibility, but Desktop Rich mode must not depend on it. |
| CAM-DESK-OUT-012 | verified | Backend capture must use tmux style-preserving capture (`capture-pane -e` or equivalent) and must not strip ANSI in the rich path. Existing monitor/detection paths must continue using plain/stripped output. |
| CAM-DESK-OUT-013 | verified | Output hash/cache must be format-aware so a plain-text hash cannot incorrectly suppress an ANSI response, and vice versa. |
| CAM-DESK-OUT-014 | verified | Desktop UI must render rich output in a contained read-only terminal-like pane. Prefer `xterm.js` if adding a dependency is acceptable; otherwise use an ANSI-to-HTML renderer with escaping and a narrow supported escape set. |
| CAM-DESK-OUT-015 | verified | The rich renderer must preserve scroll behavior: if the user is near the bottom, new output follows; if the user scrolls up, polling must not yank the viewport back down. |
| CAM-DESK-OUT-016 | verified | The user must be able to fall back to plain text from the same agent detail view. Rich output failures must not break input/key sending. |
| CAM-DESK-OUT-017 | verified | Rich output is snapshot rendering, not interactive attach. It must not introduce a PTY, SSH client, terminal tab, or arbitrary command execution. |
| CAM-DESK-OUT-018 | verified | Mobile/PWA existing files must remain compatible. If shared API code changes, existing mobile output rendering must continue to receive plain text by default. |
| CAM-DESK-OUT-019 | verified | Very large ANSI output must be bounded similarly to existing output. Full-output rich mode may be omitted in the first pass if it risks large-memory rendering; live 200-line rich output is the minimum. |
| CAM-DESK-OUT-020 | approved | Rich mode v0 is a safe line-based renderer over the same captured string Plain mode uses. Remote capture must stay plain-text compatible: Desktop calls `camc capture ... --lines N` without `--format plain`; Rich mode must not require a second remote capture format. It must normalize line endings, parse shallow block structures (fenced code, tables, dividers, shell commands, quotes, lists, headings, indented code, normal terminal lines), preserve any ANSI SGR already present through the existing safe ANSI span renderer, HTML-escape all raw text, preserve scroll/follow behavior, and fall back to escaped plain text on parser errors. Plain mode remains the source of truth and is unchanged. |
| CAM-DESK-OUT-021 | draft | Rich mode v2 must follow the fixture-first renderer plan in [rich-renderer-v2-spec.md](./rich-renderer-v2-spec.md). It keeps the same low-bandwidth plain `camc capture` source, then applies a deterministic local block/inline classifier so commands, status lines, code, quotes, lists, tables, paths, URLs, important keywords, metrics, and low-signal logs no longer render as one flat wall of text. |
| CAM-DESK-OUT-022 | approved | Agent output history expansion is **manual**, not automatic. Scrolling, mouse-wheel, and PageUp move the viewport only — they must not trigger a history fetch. The renderer always starts with a bounded live tail of 200 lines, including terminal-status agents. When the user reaches the top edge of the output pane and the local cache does not yet contain full scrollback, a compact floating top-right `More +` button appears and remains discoverable at that same location. Clicking it grows the local tail window through a bounded ladder (`200 -> 1000 -> 2000 -> 4000 -> 8000` lines) using `/output?lines=N`; only after the ladder is exhausted may it call `/fulloutput` as a final check. The `More +` control uses the same floating-control style as the bottom-right jump button, must not cover or block the output scrollbar, and remains in a stable disabled position while loading or while one pending request is queued behind an in-flight poll. The same top-right `More +` button slot reports `Loading...`, then `More content loaded` when the fetched response contains more lines than the local tail, or becomes disabled as `No more content` once the server returns fewer lines than requested (meaning the oldest available tmux content is already rendered) or the final `/fulloutput` check succeeds; there must be no second status pill that shifts the user's eye away from the button location. Loading older history must preserve the visible scroll anchor (`previousHeight`/`previousTop`), and Plain and Rich must continue to render from the same cached captured string — switching Plain↔Rich must render from cache immediately and must NOT trigger an extra fetch unless the cache is empty. While the user is reading older output (scrolled away from the bottom), normal 2-second polling must NOT replace the visible pane — poll-driven `loadOutput()` is suppressed until the user returns to the bottom. A floating bottom-right "Jump to bottom" button appears when the user is away from the bottom; clicking it scrolls the active pane to the bottom, sets `autoScroll=true`, and triggers exactly one non-poll refresh so the tail is fresh. Full-scrollback sessions are exempt from poll suppression (their text is immutable); terminal-status agents still use the same manual ladder so the `More +` affordance remains available until an explicit manual request proves the oldest remote content is rendered. The initial 200-line tail response must not by itself replace `More +` with `No more content`, because only a manual larger-window request or final `/fulloutput` check proves the oldest remote content is rendered. All in-flight state must be guarded so concurrent manual + automatic requests cannot interleave or double-render. |

### Rich Mode Render Spec v0

Rich mode is a presentation layer, not a second output source. The renderer
starts from the same captured string used by Plain mode, normalizes `CRLF` /
`CR` to `LF`, strips ANSI only for structure detection, and renders safe HTML
into the `agent-output-rich` pane. When Plain output is already cached, switching
to Rich must render that cached text immediately rather than showing `Loading…`.
Rich mode uses the same plain remote capture response as Plain mode; it must not
ask remote `camc capture` for `--format plain` or `--format ansi`. Plain mode
continues to assign raw text with `textContent`.

Block parsing is line-based and ordered by priority:

1. fenced code blocks: matching triple backtick or triple tilde fences, optional language label;
2. pipe tables with a valid separator row;
3. dividers such as `---`, `===`, `***`, or long box-drawing separators;
4. shell command lines beginning with `$`, `PS>`, `cmd>`, `❯`, or command-looking `>` prompts; normal `>` prose remains a quote;
5. consecutive quote lines beginning with `>`;
6. unordered, ordered, and checkbox list lines with shallow indentation;
7. compact headings `#`, `##`, `###`;
8. grouped four-space indented code;
9. normal terminal lines rendered with whitespace preserved.

Inline rendering is intentionally shallow outside code blocks: ANSI SGR spans
remain supported, and non-ANSI text may render inline code, bold, conservative
italic, and safe external links. The renderer must not use raw `innerHTML` from
agent output; every raw fragment is escaped, and parser failure produces escaped
plain text rather than blank output. Rich mode remains snapshot rendering, not
interactive terminal attach; no PTY, SSH client, command execution, or full
CommonMark dependency is introduced in v0.


Implementation notes:

- `camc_pkg.transport.capture_tmux()` / `camc capture` remains plain-text by
  default. Desktop must not depend on a remote `--format` flag for Rich mode.
- `web/js/api.js` keeps optional `format` plumbing for compatibility, but
  `web/js/desktop/agent-console.js` should call output APIs without a format
  argument so Plain and Rich share one plain capture stream.
- `web/js/desktop/agent-console.js` is the first desktop renderer target.
- Do not alter completion detection, auto-confirm, or monitor logic for this
  feature.

### Rich Mode Render Spec v1 (polish)

v1 layers small, bounded enhancements on top of the v0 line-based renderer.
No new dependencies, no markdown library, no remote format negotiation. The
same captured plain string still drives both Plain and Rich modes; v1 only
adds line classifiers and per-line tokenizers, all scoped to `.rich-*`
classes in `web/css/desktop.css`. Plain mode is unchanged.

Block / line additions, in detection order (run after the existing v0
detectors so headings, tables, fenced code, etc. still win):

1. **Status / keyword lines.** A leading `PASS`, `FAIL`, `ERROR`,
   `WARN`/`WARNING`, `STATUS`, `IMPLEMENTATION`, `IMPLEMENTATION_SUMMARY`,
   `FILES_CHANGED`, `TESTS`, `SMOKE`, `BUILD`, `INSTALL`, `BLOCKERS`,
   `REQ_STATUS`, `MOBILE_COMPAT`, `NOTES`, or `REVIEW_STATUS` (followed by
   word boundary or `:`) gets a compact uppercase pill at the start of the
   line and a soft row tint matching the severity class (`pass`, `fail`,
   `error`, `warn`, `info`). Exit-code lines (`exit code N`, `Exit Code: N`,
   `exit status N`) and HTTP status lines (`HTTP/1.x NNN …`) reuse the same
   badge with a `pass`/`fail`/`warn`/`error` class chosen from the number.
2. **Low-signal lines.** npm `WARN`/`notice`/`info`/`http`/`verb`,
   `electron-builder`, `app-builder`, node `DeprecationWarning`/`[DEP…]`,
   `node_modules/…` traces, common spinner glyphs, and bar-progress lines
   like `[##### 50/100]` get a `.rich-dim` class. They stay visible and
   selectable — only the colour is dimmed.

Per-line tokenizer additions (replace v0's `renderPlainInline` only inside
the matching block):

3. **Shell-command segments.** Inside any `rich-shell` line, the command
   text is split into tokens. The first non-env token is the command name
   (`.rich-cmd-name`); leading `KEY=value` tokens are env assignments
   (`.rich-cmd-env` + `.rich-cmd-env-val`); flags matching `--long` or
   `-x`, optionally with `=value`, are styled as flags
   (`.rich-cmd-flag` + `.rich-cmd-flag-val`); paths (start with `~`, `/`,
   `./`, `../`, contain a `/`, or end with a short file extension) get
   `.rich-cmd-path`. URLs/backticks inside commands are intentionally not
   re-processed by `renderPlainInline` in this segment; the command line
   reads as a command, not as prose.
4. **Code-block lines.** Fenced code blocks always render as code.
   Four-space-indented lines render as code only when they contain a cheap
   code-like signal (keyword, assignment, braces, function call, diff prefix,
   shell-ish token). Path-only attachment lines, whitespace-only terminal
   residue, and ordinary indented prose must stay normal/dim lines so Rich
   mode does not create empty scrollable code panels. Code lines pass through
   a per-line classifier. Diff prefixes are recognised first
   (`@@…@@` hunk, `+`/`-` add/del, `+++`/`---` meta). Whole-line comments
   for `//`, `#`, `--`, `;` (excluding `#!` shebangs) get
   `.rich-code-comment`. Inside other lines, a single combined regex emits
   `.rich-code-string` for `"…"` / `'…'` / `` `…` `` literals and
   `.rich-code-kw` for a small JS / Python / shell keyword set. Lines
   longer than 500 characters skip tokenizing and just HTML-escape.

Performance guard:

- Each line runs at most a small constant number of regex tests (≤ 5).
- Per-line tokenizer regexes always advance at least one character per
  match step, so total work is linear in characters.
- Per-line highlighting bails out at `LINE_HL_MAX = 500` chars: that line
  falls back to plain `escapeHtml`, which preserves selectability.
- No detector calls back into another detector; the order in
  `renderRichOutput` is the entire control flow.
- All raw text is still HTML-escaped before going into the DOM; parser
  failure still falls back to the v0 escaped-plain-text fallback.

Terminal separator handling:

- Short markdown dividers may render as a normal horizontal rule. Long terminal
  separator lines (for example a full-width line of underscores/dashes emitted
  by a TUI) render as a full-width low-emphasis `rich-terminal-rule` using a
  normal CSS border. They must never render as text inside a `pre`/code block,
  because that can create empty scrollable panels or horizontal scrollbars.

Constraints retained from v0:

- Plain mode is unchanged; Plain and Rich share one plain capture stream.
- No remote `--format` flag is introduced.
- No markdown / syntax-highlighter dependency is added.
- Output is selectable / copyable in both modes.
- Rich mode is presentation only — no PTY, SSH, or command execution.

## Active Nodes Workspace Requirements

Goal: surface the hub's nodes/remotes inside the Desktop as a real
left-nav workspace mode (peer of Agents/Settings/Start), mirroring the
mobile/PWA "Machines" view (`web/js/views/machines.js`) without
inventing a Desktop-private machine registry.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-NODEUI-010 | approved | Nodes is an **active workspace mode**, selected from the left-nav, not a connection mode. Selecting Nodes swaps the main pane to the Nodes view; selecting Agents restores the agent list/output. Settings remains exactly Direct + Relay (REMOTE-014) and must not gain a Nodes tab. |
| CAM-DESK-NODEUI-011 | approved | The Nodes view is built from hub-provided data already in `AppState`: `state.contexts` and `state.agents`. Until a normalized `/api/nodes` store lands, contexts are grouped into user-facing nodes by their `machine` block (type/user/host/port). Host-owned fields are presented and edited at the node level; context-owned fields remain workspace/path/env metadata. Each agent whose `(machine_host, machine_port)` does not match an existing context contributes an orphan node from its agent fields. Desktop must not create a separate remotes/nodes registry. |
| CAM-DESK-NODEUI-012 | approved | The host-card list shows one card per host. Collapsed card header: chevron + short host (or "local") + type badge (`SSH` / `local`) + host identity (`user@host:port`) + context-count chip + running/total agent badge when applicable. Expanded card adds a body containing the host-action bar (Filter Agents / Edit Host / Sync Host / New Context / Delete Host for SSH hosts with ≥1 context; just Filter Agents for local or empty SSH hosts), an agent-summary line, and one context row per context. Each context row has its own fold: collapsed shows context name + remote path + a one-line `last sync: …` summary + small [edit]/[delete] buttons; expanded reveals env_setup and the full last-sync diagnostic (status badge + detail + relative time). SSH hosts sort before local; ties broken by host then user. |
| CAM-DESK-NODEUI-013 | approved | Each card exposes a "Filter Agents" action that sets `state.filters.machine` to the node's host (or `local`) and switches the workspace mode back to Agents. The Agents shell must honor `filters.machine` alongside the existing status/tool filters; provide an explicit way to clear or change the machine filter (an "All nodes" option in a sidebar machine selector). |
| CAM-DESK-NODEUI-014 | approved | Sync is exposed at the host scope only. **Sync Host** (on the expanded host card) calls `api.syncContext(name)` once for a representative context on the host, marks sibling contexts on the same host as `covered`/`covered_failed` in their per-row last-sync badge, refreshes contexts + agents, and surfaces a persistent host-level status line — both the row badges and the bottom status line persist after toasts disappear. This avoids duplicating the same remote `camc --json list` output under multiple `context_name`s in `state.store.agents`. There is no per-context Sync button; if a single context's sync needs to be retried independently, Sync Host re-runs against the host's representative context and refreshes every row. Sync must go through the normal hub API; it must not shell out from the renderer or use `CamBridge`. |
| CAM-DESK-NODEUI-015 | approved | Nodes mode exposes destructive actions at two scopes. **Delete Host** (endpoint header), after a browser `confirm()` listing the endpoint identity and the associated context name(s), calls `api.deleteContext(name)` for each context on the endpoint and refreshes contexts + agents. **Delete Context** (per row), after a browser `confirm()` showing just that context name, calls `api.deleteContext(name)` for that single context and leaves sibling contexts on the same endpoint untouched. In either case, previously imported agent rows that originated from those contexts remain in `state.store.agents` until they age out or are removed by a separate explicit action — no `deleteAgentHistory`, no remote / controller-side destructive cleanup is invoked, and no remote files are touched. Other destructive affordances ("Clean completed agents", "Stop all agents") that exist in the mobile view remain intentionally omitted here pending an explicit follow-up requirement. |
| CAM-DESK-NODEUI-016 | approved | While the Nodes mode is active, the agent list, filter bar, and selected-agent output/composer surfaces must be hidden. Conversely, when Agents is active, the Nodes panel must be hidden. Mode switching is handled by the existing `applyModeToDom()` mechanism in `web/js/desktop/app.js`; Nodes must register the same way as Agents/Start/Settings (data-mode panel + persistent mode key). |
| CAM-DESK-NODEUI-017 | approved | The Nodes module must not import any mobile/PWA file. Reuse of the mobile data shape is by behavior, not by `import`, so `web/index.html`, `web/js/app.js`, `web/js/state.js`, and `web/js/views/*` remain byte-equal. Desktop CSS is the only allowed cross-cutting change beyond the new module + small wiring edits to `app.js`, `shell.js`, and `desktop.html`. |

Implementation notes:

- New renderer module: `web/js/desktop/nodes-mode.js`. Mounted from
  `web/js/desktop/app.js` next to `mountAgentConsole` etc. Receives
  `{ state, api, showToast, setMode, loadContextsAndAdapters,
  loadAgents }`.
- The mode panel lives in `web/desktop.html` as
  `<section class="mode-panel" id="mode-nodes" data-mode="nodes" hidden>`.
- The left-nav button for Nodes changes from a disabled placeholder
  to `<button class="mode-nav-btn" data-mode="nodes" …>`.
- `app.js`: `MODES` and `PERSISTENT_MODES` both include `'nodes'`.
- `shell.js`: extended to honor `state.filters.machine` and to
  populate a `#filter-machine` `<select>` from the unique machines
  represented by current agents (with an "All nodes" sentinel).

## Future Interactive Terminal Requirements

The Phase-3-future direction is **hub-mediated, controller-side terminal
attach**. Desktop never assumes local tmux. TERM-001..005 are the canonical
forward requirements and remain valid under the hub/controller model.
TERM-010..017 (the v3 local-PTY experiment kept on `camui-desktop-v3`) are
marked **superseded** below because they bake in a local-runtime assumption
that conflicts with ARCH-011 / SSH-013.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-TERM-001 | approved | Add a third Agent Detail output mode `Terminal` alongside Plain/Rich, driven by `xterm.js` (`@xterm/xterm` + `@xterm/addon-fit`). CamUI Direct is the JavaScript/Electron implementation of `cam` behavior: Terminal attach follows `cam attach` semantics, using the live agent record's `machine_host` / `machine_user` / `machine_port` as the source of truth for where the agent actually runs; context records provide credentials and workspace metadata only and must not redirect attach to a stale endpoint. The renderer never opens its own SSH or PTY — every byte goes through Electron main (`CamBridge.term.{open,input,resize,close,onData,onStatus}` → ipcMain `term:*` handlers → `ssh-transport.openTerminalChannel()`). On the remote node, the attached command is `~/.cam/camc attach <agent-id>` (same `~/.cam/camc` shim path that sync / output use). The Desktop does not reject completed/failed/stale-looking records before attach; `camc attach` owns the authoritative tmux-session check and returns the useful stale-session error. The Terminal button is disabled/greyed when the renderer cannot satisfy the contract (for example, outside the Electron preload or in Relay-only mode). |
| CAM-DESK-TERM-002 | approved | xterm packaging. `@xterm/xterm` and `@xterm/addon-fit` are declared as runtime dependencies of `apps/cam-desktop/package.json`. The UMD bundles and `xterm.css` are shipped under `web/vendor/xterm/` so the file://-served renderer can load them under the existing `script-src 'self'` CSP. `xterm.css` is linked from `desktop.html`; `xterm.js` + `addon-fit.js` are loaded as plain `<script>` tags before the desktop module bundle so `window.Terminal` and `window.FitAddon.FitAddon` are present at mount time. |
| CAM-DESK-TERM-003 | approved | Channel reuse + secret handling. The terminal channel is opened against the same per-endpoint long-lived `ssh2.Client` pool that serves `execRemote` / `writeRemoteFile` (CAM-DESK-DIRECT-019) — a fresh PTY exec channel is multiplexed onto the pooled client, NOT a new TCP/auth handshake. Plaintext password/passphrase is read from `credentialStore.get()` inside Electron main only at connect time and never crosses the IPC boundary. The renderer sees only the opaque `sessionId` and the UTF-8 byte stream. There is **no** `child_process`, no system `ssh`, no shell, no WSL on the active Desktop terminal path. |
| CAM-DESK-TERM-004 | approved | Lifecycle. Closing the Terminal mode (switching to Plain/Rich, selecting a different agent, leaving Agents mode, app shutdown, or Direct→Relay flip) sends `term:close` → `stream.end()` / `stream.destroy()` on the channel. This detaches the local view only; it does **not** kill the tmux session and does **not** stop the underlying agent — `camc attach` is a tmux-level attach. Opening a Terminal session for a new agent while one is already open closes the previous channel automatically (single-attach-per-renderer). On `before-quit`, every open channel is disposed before the embedded Hub teardown so pooled clients can flush END/CLOSE frames cleanly. |
| CAM-DESK-TERM-005 | approved | Renderer focus. While Terminal mode is active, the composer surface is hidden completely — no quick-key tab, attachment button, input textarea, or Send button — so xterm owns keyboard input. The Terminal pane uses the full available Agent-detail output width, not the Plain/Rich reading rail. The xterm host must not add layout padding that can make the fit addon over-count rows; the renderer refits after Terminal mode becomes visible and after resize/layout events so the last terminal row remains fully visible even when the Desktop window is not maximized. Plain↔Rich switching restores the composer. The Terminal button has an inline disabled tooltip ("Terminal mode is available only with Direct connection") when the active connection mode is Relay-only. Per-pane state machines (output history, autoScroll, viaPoll suppression) remain Plain/Rich only — Terminal mode bypasses them entirely because the channel is push-based. |
| CAM-DESK-TERM-010 | superseded | (Local-PTY experiment from `camui-desktop-v3`.) Replace selected-agent surface with an embedded terminal driven by a local PTY. Superseded by TERM-001 + SSH-010..013: terminal attach must go through the hub/controller path, not a local PTY assumption. |
| CAM-DESK-TERM-011 | superseded | (Local-PTY experiment.) Argv-only spawn equivalent to `cam a <id>` on the user's machine. Superseded by hub-provided attach metadata (HUB-013). The same v3 stash remains as reference code for the renderer-side xterm + IPC shape. |
| CAM-DESK-TERM-012 | superseded | (Local-PTY experiment.) Narrow CamBridge surface for open/input/resize/close. The shape is still the right starting point for SSH-011, but the local-spawn target is replaced. |
| CAM-DESK-TERM-013 | superseded | (Local-PTY experiment.) Agent switch closes PTY client without killing tmux. Subsumed under TERM-004. |
| CAM-DESK-TERM-014 | superseded | (Local-PTY experiment.) xterm.js + fit addon with resize tracking. Carries forward into TERM-001 / SSH-011. |
| CAM-DESK-TERM-015 | superseded | (Local-PTY experiment.) Actionable inline attach error. Carries forward unchanged into TERM-001 / SSH-010. |
| CAM-DESK-TERM-016 | superseded | (Local-PTY experiment.) No server-side terminal APIs / relay UI / SSH-key UI added. Subsumed under TERM-001..005 and SSH-010..013. |
| CAM-DESK-TERM-017 | superseded | (Local-PTY experiment.) Windows MSI rebuild on dep change. Generalized into VFY-005. |

## Historical Local Tab Requirements

> **SUPERSEDED — do not implement as a separate Local tab.** This section
> is preserved for requirement-ID stability and to record a prior
> experiment. The useful lifecycle pieces have moved into the active
> Direct-local-Hub requirements (CAM-DESK-DIRECT-010..019). The active
> Desktop product exposes Direct / Relay in Settings; there is no
> separate Local tab.
>
> All LOC-010..024 IDs below are recorded as `deferred` or `superseded`.
> Implementers should treat them as historical: do not build new
> features against them, do not surface a Local tab in Settings, and do
> use CAM-DESK-DIRECT-010..019 for app-managed Hub lifecycle work.
> IDs are kept intact — they are not renumbered or deleted — so cross
> references in older docs and commits remain resolvable.

Related historical references (also marked deferred):

- Architecture / design notes for the Local tab experiment:
  [`docs/desktop/local-integrated-mode-spec.md`](./local-integrated-mode-spec.md)
  (banner-marked superseded).
- Setup notes for the deferred Local tab path (WSL/macOS prerequisites,
  troubleshooting): [`docs/desktop/local-runtime-user-guide.md`](./local-runtime-user-guide.md)
  (banner-marked superseded).

History note: an earlier iteration of LOC required Local to route the
renderer through a loopback `relay.py` instance even on the same machine
(superseded by the direct-only single-port shape — LOC-013, LOC-019).
The product direction has since moved further: Local is no longer a
separate product mode. Direct now owns the app-managed local Hub flow.
The whole LOC block is retained only to keep requirement IDs stable and
to document what was tried.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-LOC-010 | deferred | (Historical Local-tab experiment.) Desktop would add a user-facing `Local` connection mode alongside `Direct` and `Relay`. Active Desktop does not ship this tab; app-managed Hub lifecycle belongs to Direct. |
| CAM-DESK-LOC-011 | superseded | (Local-tab experiment.) Electron main owned backend orchestration: detect CAM availability, generate an API token, and start an owned `cam serve` bound to `127.0.0.1:8420`. Superseded by DIRECT-011. |
| CAM-DESK-LOC-012 | deferred | (Historical Local-tab experiment.) The renderer requested `check/start/stop/restart/logs/getProfile` through a narrow `CamBridge.localBackend` API. Active Desktop may reuse a renamed/narrowed Direct-Hub bridge for DIRECT-011, but must not expose it as a separate Local tab. |
| CAM-DESK-LOC-013 | superseded | (Replaced earlier by direct-only single-port shape; further deferred now that Local is out of active scope.) Previously required a loopback `relay.py` instance. |
| CAM-DESK-LOC-014 | superseded | (Local-tab experiment.) A managed local `cam serve` would be started with a generated API token via fixed argv; tokens redacted in logs. Superseded by DIRECT-011 / DIRECT-012. |
| CAM-DESK-LOC-015 | deferred | (Historical Local-tab experiment.) Local mode would reuse existing CAM discovery/polling and not invent a second machine registry. Active Desktop relies on the Hub's existing registry regardless. |
| CAM-DESK-LOC-016 | deferred | (Historical Local-tab experiment.) Required CAM to be pre-installed locally or in WSL; full bootstrap remained deferred. Active Direct supersedes this by embedding the Hub in Electron and requiring no local CAM/WSL/Python runtime. |
| CAM-DESK-LOC-017 | superseded | (Local-tab experiment.) Desktop would only stop the `cam serve` it started. Superseded by DIRECT-011 lifecycle ownership. |
| CAM-DESK-LOC-018 | superseded | (Local-tab experiment.) Idempotent start within the current Desktop process; foreign-cam-api on relaunch; persistent ownership marker deferred. Superseded by DIRECT-011 lifecycle ownership. |
| CAM-DESK-LOC-019 | superseded | (Replaced earlier by single-layer `/api/system/health` probe; further deferred now that Local is out of active scope.) Previously required a two-layer relay+API health probe. |
| CAM-DESK-LOC-020 | deferred | (Historical Local-tab experiment.) Settings would expose Local alongside Direct/Relay with a build-policy default. Active Settings exposes only Direct and Relay (REMOTE-014), with embedded Hub lifecycle under Direct. |
| CAM-DESK-LOC-021 | superseded | (Local-tab experiment.) Local would resolve to a Direct-compatible profile so the downstream UI did not branch. Superseded by DIRECT-012 / DIRECT-019. |
| CAM-DESK-LOC-022 | deferred | (Historical Local-tab experiment.) Local would hide server/token/port details from the default user flow. The active Direct UI carries this principle under DIRECT-012. |
| CAM-DESK-LOC-023 | deferred | (Historical Local-tab experiment.) Remotes as the primary user-managed object in Local. Subsumed by active Hub/node behavior in DIRECT-014..018 and NODEUI-010..017. |
| CAM-DESK-LOC-024 | deferred | (Historical Local-tab experiment.) Server/token/port shown only under an Advanced/Diagnostics disclosure. The active Direct UI carries this principle under DIRECT-012. |

Implementation notes (historical, do not act on these in active builds):

- The earlier direct-only implementation spawned one `cam serve --host
  127.0.0.1 --port 8420 --token <apiToken>` and connected the renderer
  via the Direct CamApi path. That implementation is now superseded by
  DIRECT-011's Electron-native embedded Hub requirement; keep only the
  user-facing lifecycle shape, not the local Python/CAM dependency.
- The `cam_profile_kind` localStorage marker (`local | direct | relay`)
  should collapse to `direct | relay` for active Settings. New code must
  not branch on `local`.
- A persistent ownership marker for a Desktop-spawned `cam serve` was
  deferred in the Local-tab experiment and is not relevant to the active
  embedded-Hub direction. Embedded Hub lifecycle state should be owned by
  Electron main and app data.

## Desktop Feature Parity Requirements

Goal: bring the Desktop shell closer to the already-working mobile/WebUI app
without forking behavior. Desktop should reuse `web/js/api.js`, existing REST
endpoints, and existing localStorage profile state wherever possible.

Existing mobile/WebUI references:

- New agent: `web/js/views/start-agent.js`.
- Agent edit: `web/js/views/dashboard.js`.
- Input upload/attachment: `web/js/views/agent-detail.js`.
- Context file browsing: `web/js/views/file-browser.js`.

### Start Agent

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-RUN-010 | verified | Desktop must expose a Start Agent mode reachable from the left navigation. It should feel like a separate workbench page, not a modal blocking the current output pane. |
| CAM-DESK-RUN-011 | verified | The Start Agent form must cover the mobile/WebUI field set: tool, context, prompt, auto-confirm, auto-exit, optional name, optional timeout, and retry count. |
| CAM-DESK-RUN-012 | verified | Starting an agent must use the existing HTTP API path through `CamApi.startAgent()`. Desktop must not run `camc run` or local shell commands for this feature. |
| CAM-DESK-RUN-013 | verified | After a successful start, Desktop must refresh the agent list, select the new agent, and switch to Agents mode so its output can be viewed immediately. |
| CAM-DESK-RUN-014 | verified | Start failures must leave the form contents intact, re-enable controls, and show an actionable error. |
| CAM-DESK-RUN-015 | verified | The Start Agent mode must work with both direct and relay connections as far as the existing API supports. If disconnected, it should show a clear disabled/empty state rather than a broken form. |

Implementation notes:

- Reuse the mobile defaults from `web/js/views/start-agent.js` unless there is
  a desktop-specific reason to change them.
- Context and adapter/tool options should come from the same source as WebUI
  state/health. Do not hard-code a desktop-only tool list except as fallback.
- The left navigation may add a real `Start` or `New Agent` mode. Keep disabled
  future modes visually secondary.

### Agent Edit

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-EDIT-010 | verified | Desktop must expose an Edit Agent action for the selected agent from the Agents view. |
| CAM-DESK-EDIT-011 | verified | The first edit form must cover the existing mobile/WebUI editable fields: name and auto-confirm. |
| CAM-DESK-EDIT-012 | verified | Saving edits must use `CamApi.updateAgent(id, { name, auto_confirm })` / `PATCH /api/agents/{id}`. Desktop must not mutate local state only. |
| CAM-DESK-EDIT-013 | verified | After save, Desktop must refresh the agent list and keep the same agent selected when it still exists. |
| CAM-DESK-EDIT-014 | verified | Cancel must return to the previous selected-agent output without losing output mode, scroll state, or unsent composer text. |
| CAM-DESK-EDIT-015 | verified | Edit errors must keep the form open and visible, with controls re-enabled and an actionable error. |

Implementation notes:

- Prefer a main-pane mode or selected-agent subview that replaces the output
  pane while editing. This matches the Settings-mode architecture and avoids
  stacking modals.
- Do not add stop/kill/delete/retry controls in this pass unless explicitly
  requested; keep lifecycle/destructive actions separate.

### Input Attachments

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-INP-010 | verified | Desktop composer must support attaching at least image files using the existing upload API. Attachment surfaces: (a) the paperclip button + hidden file picker, and (b) **paste-to-attach** (CAM-DESK-INP-016) — when the user pastes clipboard content that includes image/file items into the composer, each file is uploaded through the same path. |
| CAM-DESK-INP-011 | verified | Attachment upload must use `CamApi.uploadFile(agentId, filename, base64data)` / `POST /api/agents/{id}/upload`. Desktop must not write directly into the agent workspace from Electron. Paste-to-attach uses the same upload call. In Direct mode, the embedded Hub implements this route by decoding the base64 payload and writing it through the configured SSH transport to `<context_path>/.cam-images/<timestamp>-<safe-filename>`. |
| CAM-DESK-INP-012 | verified | After upload succeeds, Desktop must insert or send the returned path to the selected agent using the existing input API, matching the mobile/WebUI behavior. The initial behavior may send the path without Enter if that matches mobile. Paste-to-attach follows the same send-without-Enter behavior. |
| CAM-DESK-INP-013 | verified | Attachment UI must show upload progress/status and failure text without clearing the user's unsent typed input. Paste-to-attach must also preserve typed composer text. |
| CAM-DESK-INP-014 | verified | Attachment controls must be disabled when no running agent is selected or when the app is disconnected. The paste-to-attach path checks the same `disabled` condition before uploading; a paste that arrives while the agent is inactive surfaces an explanatory status message and the upload is skipped. |
| CAM-DESK-INP-015 | verified | The upload path must be safe for direct and relay mode as implemented by the existing API. No local filesystem path should be exposed to the agent; only the returned workspace path may be sent. |
| CAM-DESK-INP-016 | approved | The composer textarea must intercept the `paste` event. When `clipboardData.items` contains at least one `kind === 'file'` entry, Desktop must `preventDefault()` and upload each file sequentially through `CamApi.uploadFile()` then send the returned workspace path through `CamApi.sendInput(agentId, path, false)` — same shape as the paperclip path (INP-010..015). If no file items are present, the paste must fall through untouched so ordinary text paste keeps working. Clipboard files with no `.name` (typically OS-shortcut screenshots) must be uploaded under a generated stable filename of the form `pasted-image-YYYYMMDD-HHMMSS.<ext>`, where the extension is mapped from the file's MIME type (`image/png → png`, `image/jpeg → jpg`, `image/gif → gif`, `image/webp → webp`, `image/svg+xml → svg`, `image/bmp → bmp`, `application/pdf → pdf`, `text/plain → txt`, `text/markdown → md`, `text/csv → csv`, `application/json → json`, else `bin`). Multiple files in a single paste must be processed sequentially with a progress prefix in the upload status (`Uploading pasted file 2/3: …`). Errors surface via the existing `composer-upload-status` element and `showToast`. The composer text remains untouched throughout (CAM-DESK-INP-013). |

Implementation notes:

- Mobile currently accepts `image/*`. Desktop should start with image files for
  parity. Broader file types need a separate requirement because backend
  storage currently writes under `.cam-images/`.
- Use browser `FileReader` in the renderer as the mobile app does. Do not add
  Electron `fs` access for this pass.

### Context / File Parity

These are documented now but intentionally lower priority than Start/Edit/Input
attachments.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-FILE-010 | approved | Desktop should expose a Contexts/File Browser mode after Start/Edit/Attachment parity lands. |
| CAM-DESK-FILE-011 | approved | File Browser must reuse `CamApi.listFiles()` and `CamApi.readFile()` and remain read-only in the first desktop pass. |
| CAM-DESK-FILE-012 | approved | File Browser must be reachable as a separate mode/page, not embedded permanently into the agent output pane. |

## Verification Requirements

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-VFY-001 | verified | For rich output, provide a deterministic local smoke case using tmux that emits at least red text, bold text, and normal text. |
| CAM-DESK-VFY-002 | verified | Verify plain output remains ANSI-free by default. |
| CAM-DESK-VFY-003 | verified | Verify rich output contains ANSI/style information at the API boundary and renders visibly distinct in Desktop. |
| CAM-DESK-VFY-004 | verified | Verify mobile/PWA entry files are unchanged or explicitly explain any shared API change. |
| CAM-DESK-VFY-005 | approved | If a Windows installer is affected by dependency changes, rebuild and install on VDI before approval. |

## Current Implementation Task Template

Use this shape when assigning work to `camui-dev`:

```text
REQ_IDS:
- CAM-DESK-OUT-010
- CAM-DESK-OUT-011

SCOPE:
- Files/directories allowed to change
- Explicit out-of-scope items

ACCEPTANCE:
- Per-Req pass/fail criteria

TESTS:
- Commands to run
- Manual smoke expected results

COMPLETION FORMAT:
STATUS: needs-review | blocked
REQ_STATUS:
- CAM-DESK-...: implemented | partial | blocked
FILES_CHANGED:
TESTS:
SMOKE:
BLOCKERS:
```
