# CAM Desktop UI Spec v2

Status: draft for review
Owner split: `camui-rev` owns product direction and review; `camui-dev`
implements only after explicit delegation.

This spec supersedes the archived V1 desktop specs:

- `docs/archive/desktop-ui-v1-spec.md`
- `docs/archive/cam-desktop-v1-scaffold-spec.md`
- `docs/archive/cam-desktop-v1-chatshell-analysis.md`
- `docs/archive/cam-desktop-v1-chatshell-baseline.md`
- `docs/archive/chatshell-reference-evaluation-v1.md`

## Decision

CAM Desktop v2 should be derived from the existing `web/` UI and mobile/relay
product line. It should not continue the separate React-only desktop UI as the
main product path.

The target architecture is:

```text
Shared CAM UI core
  api / state / actions / formatters / cache
        |
        +-- Mobile shell: route/page/touch-first UI
        |
        +-- Desktop shell: workbench/sidebar/output/composer UI
                |
                +-- Electron package for Windows first, macOS later
```

The important distinction is:

- Share product behavior, API clients, state model, relay support, and business
  actions.
- Do not force mobile and desktop to share the same layout.

## Why This Replaces V1

The V1 desktop branch created an independent `apps/cam-desktop` React/Tauri UI.
That proved packaging and a minimal list/output/input loop, but it forks the
product surface from the already mature WebUI.

The existing `web/` app already includes:

- direct HTTP and relay WebSocket API client
- connection settings and saved profiles
- agent dashboard with filters
- agent detail with output polling, send input, send key, direct input mode,
  upload, fullscreen, and mobile keyboard handling
- start-agent form
- contexts, including SSH context fields
- nodes/machines view
- file browser and file preview
- Android/PWA bridge hooks through `window.CamBridge`

Desktop v2 should reuse that work instead of recreating it.

## Goals

- Build a Windows-first desktop app that uses the existing CAM WebUI product
  behavior.
- Keep mobile UI working as-is.
- Add a desktop layout that feels closer to Codex/VS Code/Cursor workbench
  patterns:
  - persistent left navigation
  - agent list/filter always available
  - large main output area
  - input composer fixed at the bottom
  - settings/nodes/contexts as mode pages, not mobile back-stack pages
- Preserve direct and relay connection modes.
- Keep future macOS packaging straightforward by sharing the same bundled web
  assets and Electron shell.
- Treat Desktop as a client application in the first phase. A CAM direct or
  relay endpoint must already be running somewhere reachable.
- Choose Electron for the desktop shell so the product has a stable,
  cross-platform desktop packaging and integration baseline while still reusing
  the existing WebUI product layer.

## Non-Goals

- Do not build a separate desktop product surface that duplicates WebUI logic.
- Do not make the mobile layout carry desktop density requirements.
- Do not design or implement embedded terminal UI in Phase 1.
- Do not start, install, or supervise the CAM relay server from Desktop v2
  Phase 1.
- Do not add TaskHub, Tree/Diff, or a VS Code clone in this milestone.
- Do not rewrite the existing `web/` app into React as part of this step.

## Current WebUI Assets To Reuse

These modules are the source of truth for product behavior:

- `web/js/api.js`
  - direct HTTP mode
  - relay REST-over-WebSocket mode
  - event WebSocket stream
  - retry/cache behavior
  - agent/context/file methods
- `web/js/state.js`
  - agents
  - contexts
  - connection mode
  - filters
  - toast
  - per-agent output cache
- `web/js/views/dashboard.js`
  - agent list
  - stats
  - status/tool/node filters
  - edit/delete agent records
- `web/js/views/agent-detail.js`
  - output polling
  - full output for terminal agents
  - send input
  - send key
  - direct input mode
  - upload
  - fullscreen output
- `web/js/views/start-agent.js`
  - start agent
  - tool/context/prompt/auto-confirm/auto-exit options
- `web/js/views/contexts.js`
  - local and SSH contexts
  - sync/copy/delete/browse actions
- `web/js/views/machines.js`
  - node grouping
  - node filtering
  - context sync and cleanup actions
- `web/js/views/settings.js`
  - direct and relay settings
  - profile save/load
  - connection test
  - update hook through `CamBridge`
- `web/js/views/file-browser.js`
  - list/read files through API
  - code view
  - markdown/html preview

## Target Directory Shape

Do this incrementally. The first implementation does not need a large
restructure, but the end state should move toward:

```text
web/
  index.html                  mobile/PWA entry
  desktop.html                desktop/Electron entry
  css/
    style.css                 shared/mobile styles
    desktop.css               desktop workbench styles
  js/
    api.js                    shared initially; later core/api.js
    state.js                  shared initially; later core/state.js
    views/                    current mobile/page views
    desktop/
      app.js                  desktop router/shell
      shell.js                workbench layout
      agent-console.js        desktop agent console
      settings-page.js        desktop settings adapter
```

Later, shared behavior can be extracted without changing product behavior:

```text
web/js/core/
  api.js
  state.js
  actions/agent-actions.js
  actions/context-actions.js
  format.js
  output-cache.js
  profiles.js
```

Avoid a large upfront refactor. Add the desktop entry first, then extract shared
helpers only when duplication appears.

## Desktop Layout

Desktop should use a workbench layout, not the mobile page stack.

```text
+------------------+----------------------------------------------+
| Activity / Nav   | Main Workspace                               |
|                  |                                              |
| Agents           | selected agent title / status / context      |
| Nodes            |                                              |
| Contexts         |  large output pane                           |
| Files            |                                              |
| Settings         |                                              |
|                  |----------------------------------------------|
| Agent filters    | composer + quick keys                        |
| Agent list       |                                              |
+------------------+----------------------------------------------+
```

### Agent Console Mode

This is the first desktop mode.

Required:

- left navigation
- agent filters
- scannable agent list
- selected agent output in the main area
- bottom composer
- quick keys for Enter, Esc, Ctrl-C, Backspace, arrows, Tab where useful
- connection status
- direct and relay modes work through the shared `api`

The output/composer behavior should initially reuse the proven WebUI
`agent-detail.js` behavior, but the layout should be desktop-specific:

- no mobile back button as the main navigation pattern
- no full-page route jump just to select another agent
- no mobile keyboard hacks in the desktop path unless harmless

### Settings Mode

Settings should be a full desktop page/mode, not squeezed into the agent
console.

Required:

- direct server URL/token
- relay URL/token
- saved profiles
- connection test
- app version/update area

Initial storage can match WebUI `localStorage` behavior. Later desktop builds
can add OS keychain storage behind `CamBridge`.

### Nodes / Machines / Backend Endpoints

Keep two concepts separate:

- Backend endpoint: how the UI connects to CAM, configured as direct server URL
  and/or relay URL in Settings.
- Node or machine: where agents run, derived from CAM contexts and agent
  metadata such as host, user, port, and env setup.

The current WebUI already treats these as separate areas:

- Settings manages direct/relay connection profiles.
- Contexts manages local/SSH execution contexts.
- Nodes/Machines is a derived operational view over contexts plus agents.

Desktop should follow the same model. Do not manage nodes from a modal attached
to the agent console. Use a separate workbench mode/page for Nodes and Contexts
when those modes are brought into the desktop shell. Small popovers are fine for
choosing the active backend profile, but create/edit/delete context or node
operations should live in their own mode so the agent console stays simple.

### Nodes / Contexts / Files

These can initially adapt existing views:

- Nodes mode from `machines.js`
- Contexts mode from `contexts.js`
- Files mode from `file-browser.js`

Desktop polish can come later. The first goal is to avoid duplicating their
API and state logic.

## Mobile Compatibility

Do not regress mobile.

Mobile keeps:

- `web/index.html`
- existing hash routes
- mobile header/menu
- touch and keyboard-specific logic
- Android `CamBridge` hooks
- service worker/PWA behavior

Desktop uses:

- `web/desktop.html`
- desktop-specific shell/layout
- shared `api` and `state`
- desktop-specific CSS

Responsive CSS may share primitives, but do not depend on viewport width alone
to decide product behavior. The Electron app should load the desktop entry
explicitly.

## Desktop Runtime

CAM Desktop v2 should use Electron as the desktop runtime.

Reason:

- this is closer to the VS Code/Cursor/Codex-style desktop architecture the UI
  is moving toward
- Electron gives one desktop shell for Windows first and macOS later
- Node/Electron packaging is a pragmatic fit for a WebUI-derived app
- the WebUI product layer can still be reused; only the native shell changes

The current `apps/cam-desktop` Tauri app is legacy V1. It may remain on disk for
reference until the Electron shell replaces it, but new Desktop v2 product work
should not expand the standalone React/Tauri UI.

Suggested Electron app shape:

```text
apps/cam-desktop/
  package.json
  electron/
    main.js                 app lifecycle, BrowserWindow, IPC
    preload.js              narrow window.CamBridge surface
  renderer -> bundled web/desktop.html assets
```

Do not put CAM product logic in Electron IPC. Product behavior stays in
`web/js/api.js`, `web/js/state.js`, and shared WebUI modules.

## Backend Connectivity Model

Desktop v2 is a client in Phase 1. It should use the same connection model as
the current mobile/WebUI app:

```text
CAM Desktop UI
   |
   +-- direct HTTP/WebSocket -> existing cam serve endpoint
   |
   +-- relay WebSocket/REST-over-WS -> existing relay endpoint
```

The relay server and CAM server are separate runtime services. Desktop Phase 1
does not start them, bundle them, supervise them, or expose server management as
a product mode.

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

## Release UX For Backend Startup

Phase 1 may assume an existing backend endpoint so the desktop UI can land
quickly, but that is not enough for a polished desktop product.

The real release flow should be:

```text
install app -> open app -> app discovers or starts a usable backend -> user works
```

Users should not have to remember and manually run a sequence of shell commands
every time they want to use the desktop app.

Phase 2 should add a backend readiness layer:

- detect saved backend profiles and reconnect automatically
- health-check the active endpoint with clear error states
- detect a local backend where practical:
  - Windows: WSL distro with CAM installed
  - macOS/Linux: local `cam serve`
- offer one-click start for a local backend when the environment already exists
- show the exact setup/start command only as a fallback or diagnostic path
- keep the backend process independent from the UI lifetime when possible, so
  closing the desktop app does not accidentally kill running agents

Do not confuse this with relay/server management as a product module. The UI
does not need a "relay server admin page" in the first desktop releases. It
needs a dependable connection and startup experience.

### Backend Readiness Milestones

The product should move from "developer has a backend running" to "normal user
opens the app and works" in layers.

#### Phase 2A: Backend Readiness

Goal: make an already-installed environment feel automatic.

Scope:

- reconnect saved direct/relay profiles on launch
- show clear backend health in Settings and the sidebar connection bar
- distinguish these states:
  - connected
  - configured but unreachable
  - no profile configured
  - local backend detected but not running
  - local backend not installed
- add a desktop-native readiness seam behind `CamBridge`, not product logic in
  Electron IPC:

```ts
interface BackendReadiness {
  platform: "windows" | "macos" | "linux";
  hasWsl?: boolean;
  wslDistros?: string[];
  selectedDistro?: string;
  hasCam?: boolean;
  hasPython?: boolean;
  localServerRunning?: boolean;
  suggestedCommand?: string;
  message: string;
}
```

Suggested bridge methods:

```ts
CamBridge.checkBackendReadiness(): Promise<BackendReadiness>;
CamBridge.startLocalBackend?(): Promise<{ ok: boolean; url?: string; message: string }>;
CamBridge.openSetupDocs?(): Promise<void>;
```

Windows behavior:

- detect WSL with `wsl.exe --status` / `wsl.exe -l -q`
- detect Python and CAM inside an existing distro with non-mutating commands
- if CAM exists but `cam serve` is not running, offer a `Start backend` action
- save the resulting direct profile, usually `http://127.0.0.1:8420`

macOS/Linux behavior:

- detect whether `cam` is on PATH
- health-check a local `cam serve` endpoint
- offer a local start action only when the environment is already installed

Acceptance:

- no terminal UI
- no relay admin UI
- no mandatory command line for the happy path when the environment already
  exists
- all setup/start failures produce actionable text
- closing the desktop app should not kill already-running agents

#### Phase 2B: Existing WSL Bootstrap

Goal: if WSL exists but CAM does not, let the user install CAM into WSL from the
desktop app.

Scope:

- choose a WSL distro
- create an isolated CAM Desktop environment in WSL, for example
  `~/.cam-desktop/venv`
- install CAM from a packaged wheel/source bundle or a configured internal URL
- verify `python3 --version`, `cam --version`, and `cam serve` startup
- store the working direct profile

Do not require Node on the user's machine. Electron already ships the runtime
needed by the desktop app; Node is only a developer/build dependency.

Acceptance:

- user does not type commands manually when WSL and network/package source are
  available
- logs are captured and visible from the setup UI
- installation is idempotent
- existing user CAM installs are not overwritten without confirmation

#### Phase 2C: Full Windows Setup

Goal: support a Windows machine that does not yet have a usable Linux-like
backend environment.

Scope:

- detect missing WSL
- guide or trigger WSL installation when allowed by Windows policy
- handle the fact that WSL installation may require admin approval, reboot, or
  Microsoft Store / Windows optional feature availability
- after WSL exists, fall through to Phase 2B

Acceptance:

- corporate-policy failures are explained clearly
- user can resume setup after reboot
- the app never leaves the user with an ambiguous half-installed state

#### Phase 2D: Offline / Bundled Bootstrap

Goal: make installation work in restricted or offline environments.

Scope:

- bundle CAM wheel/source package
- bundle Python/wheel dependencies where allowed
- optionally bundle a small Python installer/runtime strategy for WSL
- support an internal package mirror
- verify package signatures or checksums

This is the heaviest option and should come last. It increases installer size,
maintenance burden, security review surface, and platform-specific edge cases.

Acceptance:

- repeatable offline install on a clean test machine
- versioned dependency manifest
- clear upgrade and rollback behavior
- no hidden dependency on public package registries

## Windows Packaging

Windows first:

- package as a single MSI installer
- load bundled `web/desktop.html` assets
- preserve app icon and app metadata
- do not require the installer to install WSL, Python, CAM, or the relay server

macOS later:

- package the same desktop entry as `.app` / `.dmg`
- add signing/notarization when ready
- avoid Windows-specific assumptions in the WebUI layer

### CamBridge

Expose a small `window.CamBridge` surface for desktop-only behavior.

Initial bridge:

```ts
interface CamBridge {
  platform: "windows" | "macos" | "linux";
  getAppVersion(): string;
  restartApp(route?: string): void;
  openExternal(url: string): void;
}
```

Later bridge candidates:

- secure token storage
- choose local folder
- open log folder
- desktop notifications
- auto-update integration

Do not move CAM product behavior into `CamBridge`; it should stay in shared
WebUI/API code.

## Terminal Scope

Do not design or implement terminal UI in Phase 1.

Baseline Desktop v2 should use the existing WebUI model:

```text
agent output polling + send input + send key
```

This is already implemented over direct/relay APIs and works with mobile.

## Implementation Plan

### Phase 0: Archive Old Direction

- Archive V1/ChatShell-era specs.
- Keep `docs/desktop-ui-spec.md` as the canonical v2 spec.
- Mark the current `apps/cam-desktop` React UI as legacy V1 until replaced.

### Phase 1: Desktop Entry Spike

Add:

- `web/desktop.html`
- `web/css/desktop.css`
- `web/js/desktop/app.js`
- `web/js/desktop/shell.js`

Use existing `api.js` and `state.js` directly.

Acceptance:

- desktop entry connects in direct and relay modes
- agent list renders
- selecting an agent updates the main workspace
- selected agent output loads
- composer sends input
- key buttons send keys

### Phase 2: Electron Loads Desktop Entry

Update or replace `apps/cam-desktop` so the packaged desktop app loads the
desktop web entry rather than maintaining a separate product UI.

Acceptance:

- `npm run build:web` or equivalent web asset build passes
- Windows MSI builds
- installed app opens the desktop shell
- settings survive app restart

### Phase 2A: Backend Readiness

Add the readiness layer described above, without installing anything yet.

Acceptance:

- Settings shows backend readiness and connection health
- saved profiles reconnect automatically
- existing local backend can be detected
- if the local environment is already installed, the app can offer a start
  action
- failures provide actionable setup text

### Phase 2B: Existing WSL Bootstrap

Add a guided install path for machines that already have WSL.

Acceptance:

- WSL distro detection works on Windows
- CAM can be installed into an isolated WSL environment
- install logs are visible
- successful install saves a working direct profile

### Phase 2C: Full Windows Setup

Add the outer setup flow for Windows machines without WSL.

Acceptance:

- missing WSL is detected
- setup explains admin/reboot/store/policy blockers
- after WSL exists, the flow resumes into Phase 2B

### Phase 2D: Offline / Bundled Bootstrap

Add offline/internal-mirror packaging after the online WSL flow works.

Acceptance:

- bundled dependency manifest is versioned
- offline install works on a clean test machine
- checksum/signature verification exists for bundled artifacts

### Phase 3: Desktop Mode Pages

Adapt existing WebUI pages:

- Settings
- Nodes
- Contexts
- Files
- Start Agent

Acceptance:

- no duplicated API client
- no duplicated state store
- mobile routes still work
- desktop pages use workbench navigation

### Phase 4: macOS Packaging

After Windows works:

- add macOS bundle workflow
- verify Electron/Chromium behavior on macOS
- handle signing/notarization separately

## Acceptance Criteria

Desktop v2 is acceptable when:

- The desktop app uses the WebUI-derived desktop entry.
- Mobile `web/index.html` still works.
- Shared API/state code is used by both mobile and desktop.
- Desktop console has persistent left navigation plus main output/composer.
- Direct and relay connections both work.
- Windows MSI builds and launches.
- The implementation avoids adding new product behavior only to Desktop unless
  it is truly desktop-native bridge behavior.
- Future macOS packaging does not require a UI rewrite.

## Guidance For `camui-dev`

All new desktop implementation work must happen in the isolated clone:

```text
/data/home_hren/scratch/cam
branch: camui-desktop-v2
```

Do not use `/home/hren/.openclaw/workspace/cam` for desktop implementation.
Do not use `/home/hren/scratch/cam`.

Before implementing, read:

- this spec
- `web/js/api.js`
- `web/js/state.js`
- `web/js/app.js`
- `web/js/views/agent-detail.js`
- `web/js/views/dashboard.js`
- `web/js/views/settings.js`
- `web/js/views/machines.js`
- `web/js/views/contexts.js`
- `web/js/views/file-browser.js`
- `apps/cam-desktop/src-tauri/`

First implementation task should be a small desktop-entry spike, not a full
rewrite.
