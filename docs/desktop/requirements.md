# CAM Desktop Requirements

Status: active requirement registry
Owner split: `camui-rev` owns requirements and review; `camui-dev` implements.

This document is the stable source of truth for Desktop requirements. Milestone
specs such as `docs/desktop-ui-spec.md` explain current design direction, but
implementation and review should cite the IDs here.

## Requirement ID Scheme

Format:

```text
CAM-DESK-<AREA>-<NNN>
```

Areas:

- `ARCH`: desktop architecture and process boundaries.
- `CONN`: endpoint/profile connection behavior.
- `AGT`: agent list and selection.
- `RUN`: starting new agents.
- `EDIT`: editing existing agents.
- `OUT`: output capture and rendering.
- `INP`: input composer and key sending.
- `FILE`: uploads, attachments, and context files.
- `SET`: settings mode.
- `PKG`: packaging and install.
- `TERM`: future interactive terminal.
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

| Phase | Scope |
| --- | --- |
| Phase 1 | Electron shell reusing WebUI client code; agent list, selected output, composer, quick keys, settings mode. |
| Phase 2A | Detect/start already-installed local backend; Windows MSI packaging/install on locked-down VDI. |
| Phase 2B | Bootstrap CAM into an existing WSL distro. Deferred. |
| Phase 2C | Full Windows setup when WSL is absent. Deferred. |
| Phase 2D | Offline/internal-mirror bootstrap. Deferred. |
| Phase 3 | Rich output and interactive terminal. Rich snapshot is verified; live terminal remains deferred. |
| Phase 4 | Desktop feature parity with the mobile/WebUI surface: start agent, edit agent, input attachments, and selected context/file workflows. |

## Approved / Verified Baseline

| ID | Status | Requirement | Evidence |
| --- | --- | --- | --- |
| CAM-DESK-ARCH-001 | verified | Desktop runtime is Electron, not Tauri, for the active product path. Dormant Tauri files may remain as reference only. | `apps/cam-desktop/package.json` uses `electron/main.cjs` and Electron scripts. |
| CAM-DESK-ARCH-002 | verified | Desktop renderer reuses existing WebUI `CamApi` and `AppState`; it does not fork a separate CAM client stack. | `web/js/desktop/app.js` imports `../api.js` and `../state.js`. |
| CAM-DESK-CONN-001 | verified | Desktop connects to an existing direct or relay endpoint using the same localStorage keys as WebUI. | Settings mode reads/writes `cam_server_url`, `cam_token`, `cam_relay_url`, `cam_relay_token`. |
| CAM-DESK-AGT-001 | verified | Agents mode shows a left-side agent list and selects one agent as the main target. | Desktop shell and agent console reviewed in Phase 1/2A. |
| CAM-DESK-OUT-001 | verified | Selected agent output is shown through existing `/api/agents/{id}/output` and `/fulloutput` polling. | `web/js/desktop/agent-console.js` uses `api.agentOutput()` and `api.agentFullOutput()`. |
| CAM-DESK-INP-001 | verified | Composer sends selected-agent text through existing WebUI API and supports Enter-to-send with Shift+Enter newline. | `web/js/desktop/agent-console.js`. |
| CAM-DESK-INP-002 | verified | Quick-key controls send special keys to the selected agent and are disabled when no active agent is selected. | `web/js/desktop/agent-console.js`. |
| CAM-DESK-SET-001 | verified | Settings is a mode/page in the main pane, not a modal required for normal use. | `web/desktop.html`, `web/js/desktop/settings-mode.js`. |
| CAM-DESK-PKG-001 | verified | Windows build produces a single MSI artifact. | VDI produced `CAM Desktop 0.2.0.msi`. |
| CAM-DESK-PKG-002 | verified | MSI installs per-user without admin on locked-down Windows VDI. | `msiexec exit=0`; install path `%LOCALAPPDATA%\Programs\cam-desktop`. |
| CAM-DESK-PKG-003 | verified | Installed app launches on the VDI after MSI install. | `CAM Desktop.exe` stayed alive after 8s with Electron process tree. |
| CAM-DESK-SEC-001 | verified | Renderer is isolated: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`. | `apps/cam-desktop/electron/main.cjs`. |
| CAM-DESK-SEC-002 | verified | Renderer cannot pass arbitrary commands to the main process. | `CamBridge` exposes fixed readiness/start methods only. |

## Rich Output Requirements

Goal: make the desktop output pane visually preserve terminal color/style without
turning the first implementation into a full interactive terminal.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-OUT-010 | verified | Add a rich-output capture path that preserves ANSI SGR/style sequences from tmux. The existing plain-text output path must remain unchanged for mobile, search, copy, and low-risk fallback. |
| CAM-DESK-OUT-011 | verified | The API contract must be explicit, e.g. `GET /api/agents/{id}/output?format=ansi` and, if implemented, `/fulloutput?format=ansi`. The default with no `format` stays plain text. |
| CAM-DESK-OUT-012 | verified | Backend capture must use tmux style-preserving capture (`capture-pane -e` or equivalent) and must not strip ANSI in the rich path. Existing monitor/detection paths must continue using plain/stripped output. |
| CAM-DESK-OUT-013 | verified | Output hash/cache must be format-aware so a plain-text hash cannot incorrectly suppress an ANSI response, and vice versa. |
| CAM-DESK-OUT-014 | verified | Desktop UI must render rich output in a contained read-only terminal-like pane. Prefer `xterm.js` if adding a dependency is acceptable; otherwise use an ANSI-to-HTML renderer with escaping and a narrow supported escape set. |
| CAM-DESK-OUT-015 | verified | The rich renderer must preserve scroll behavior: if the user is near the bottom, new output follows; if the user scrolls up, polling must not yank the viewport back down. |
| CAM-DESK-OUT-016 | verified | The user must be able to fall back to plain text from the same agent detail view. Rich output failures must not break input/key sending. |
| CAM-DESK-OUT-017 | verified | Rich output is snapshot rendering, not interactive attach. It must not introduce a PTY, SSH client, terminal tab, or arbitrary command execution. |
| CAM-DESK-OUT-018 | verified | Mobile/PWA existing files must remain compatible. If shared API code changes, existing mobile output rendering must continue to receive plain text by default. |
| CAM-DESK-OUT-019 | verified | Very large ANSI output must be bounded similarly to existing output. Full-output rich mode may be omitted in the first pass if it risks large-memory rendering; live 200-line rich output is the minimum. |

Implementation notes:

- `camc_pkg.transport.capture_tmux()` currently strips ANSI. Add a separate
  mode or function for rich capture instead of changing its default behavior.
- `src/cam/api/routes/agents.py` is the likely API surface for the `format`
  query parameter and cache-key change.
- `web/js/api.js` should expose an option rather than hard-code `format=ansi`
  everywhere.
- `web/js/desktop/agent-console.js` is the first desktop renderer target.
- Do not alter completion detection, auto-confirm, or monitor logic for this
  feature.

## Future Interactive Terminal Requirements

These are deliberately deferred until rich snapshot output is stable.

| ID | Status | Requirement |
| --- | --- | --- |
| CAM-DESK-TERM-001 | deferred | Add an Agent Detail `Terminal` tab using `xterm.js` over a server-side WebSocket. |
| CAM-DESK-TERM-002 | deferred | Terminal attach must be server-mediated. Desktop should not directly own SSH keys or execute `ssh camc attach` as the primary product path. |
| CAM-DESK-TERM-003 | deferred | The renderer may send only terminal bytes, resize events, and agent ID to the terminal WebSocket. It must not pass shell commands. |
| CAM-DESK-TERM-004 | deferred | Closing the tab detaches the client only; it must not kill the tmux session or agent. |
| CAM-DESK-TERM-005 | deferred | Relay mode must be considered in the terminal design, so the desktop can connect even when it cannot directly reach the agent host. |

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
| CAM-DESK-INP-010 | verified | Desktop composer must support attaching at least image files using the existing upload API. |
| CAM-DESK-INP-011 | verified | Attachment upload must use `CamApi.uploadFile(agentId, filename, base64data)` / `POST /api/agents/{id}/upload`. Desktop must not write directly into the agent workspace from Electron. |
| CAM-DESK-INP-012 | verified | After upload succeeds, Desktop must insert or send the returned path to the selected agent using the existing input API, matching the mobile/WebUI behavior. The initial behavior may send the path without Enter if that matches mobile. |
| CAM-DESK-INP-013 | verified | Attachment UI must show upload progress/status and failure text without clearing the user's unsent typed input. |
| CAM-DESK-INP-014 | verified | Attachment controls must be disabled when no running agent is selected or when the app is disconnected. |
| CAM-DESK-INP-015 | verified | The upload path must be safe for direct and relay mode as implemented by the existing API. No local filesystem path should be exposed to the agent; only the returned workspace path may be sent. |

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
