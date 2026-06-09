# CAM Desktop UI Spec

Status: draft for review
Owner split: `camui-rev` writes and reviews this spec; `camui-dev` implements after approval.

## Summary

CAM Desktop is a desktop control surface for CAM, the "PM2 for AI coding
agents". It manages Claude Code, Codex, Cursor, and other CLI agents that run
inside tmux sessions through `cam` / `camc`.

The desktop app must not replace `camc` or tmux. It should make the common
agent-management loop visible and low-friction:

- see all agents and their state
- inspect one agent's live output
- send direct input or special keys to the selected tmux session
- keep working when `cam serve` is unavailable by falling back to direct `camc`

V1 is intentionally narrower than the earlier preserve-first draft: **agent
list, selected-agent output, and selected-agent input are the only product
surface**. Other scaffold features may remain in code if useful, but they
should not be visible in the first default UI.

## Framework Decision

Use the existing `apps/cam-desktop` scaffold: **Tauri 2 + React + TypeScript**.

Rationale:

- The repository already contains a Tauri/React desktop app with a typed
  `CamBackend` abstraction and a Rust command bridge for direct `camc` calls.
- Tauri is a good fit for a developer desktop tool because it can safely bridge
  to local commands without carrying Electron's runtime size.
- React/TypeScript keeps the UI portable: the domain components can later share
  patterns with the existing `web/` client or a future browser app.

Do not introduce Electron, Flutter, Qt, or another desktop stack for this
iteration.

## Existing Repository Context

Relevant files already present:

- `src/cam/api/server.py`: FastAPI app, `cam serve` entry point.
- `src/cam/api/routes/agents.py`: agent REST endpoints.
- `src/cam/api/ws.py`: WebSocket event/status stream.
- `src/cam/api/routes/contexts.py`: context REST endpoints.
- `src/cam/api/routes/system.py`: health/config endpoints.
- `apps/cam-desktop/src/`: current React desktop UI scaffold.
- `apps/cam-desktop/src-tauri/src/main.rs`: Tauri command bridge.
- `apps/cam-desktop/SPEC.md`: earlier scaffold-level spec, useful background.
- `baseline/chatshell-desktop/`: ChatShell reference app.
- `docs/chatshell-reference-evaluation.md`: feature mapping and reuse
  recommendation.

Current scaffold coverage in `apps/cam-desktop`:

- health via `camc version`
- agent list via `camc --json list`
- run agent
- capture selected agent
- send direct text
- list/read/send/reply `camc msg` threads
- local / WSL / SSH backend profiles

This spec supersedes the scaffold spec for product direction, but the existing
code should be evolved in place rather than recreated.

## Backend Model

The UI depends on a typed `CamBackend` interface. React components must call
domain methods, not build command strings or REST paths directly.

The important architectural choice is to keep the first UI small while putting
one stable seam between the UI and CAM operations. The first release should not
try to expose all of CAM. It should prove this loop:

```text
select agent -> read current output -> send input/key -> refresh output
```

Initial implementation order:

1. **CLI adapter, first implementation path**
   - Uses the existing Tauri Rust bridge to run `camc` with argv.
   - Supports local Linux/macOS first.
   - Keeps WSL and SSH profiles available because the scaffold already has the
     shape, but they are not blockers for the first usable Linux build.
   - Matches CAM's current source-of-truth model: per-machine `camc` owns tmux,
     agent state, logs, and message ledger.

2. **API adapter, later online/multi-machine path**
   - Connects to `cam serve`.
   - Uses REST for agent/context/actions.
   - Uses WebSocket for agent status/event updates.
   - Uses output polling for live tmux output because the current WS endpoint
     streams events/status, not raw terminal bytes.
   - Best mode for multi-machine aggregated state after the direct `camc` loop
     is stable.

This gives the project a simple start without painting the architecture into a
corner: future API/WS work changes the adapter, not the main UI.

Default profile behavior:

- Linux/macOS P0: direct local `camc`.
- Windows P0: direct WSL `camc` where available.
- API mode: opt-in until the UI's direct `camc` interaction loop is solid.
- Manual profile switch is always available.

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

The CLI adapter maps to `camc`, because `camc` is authoritative per machine.
Minimum commands:

- Health: `camc version`
- Agent list: `camc --json list`
- Agent detail: `camc --json status <agent>`
- Run: `camc run --name <name> --path <path> --tool <tool> <prompt>`
- Capture: `camc capture <agent> --lines <n>`
- Logs: `camc logs <agent> --tail <n>`
- Direct text: `camc send <agent> --text <text>`
- Key: `camc key <agent> --key <key>`
- Stop: `camc stop <agent>`
- Kill: `camc kill <agent>`
- Remove: `camc rm <agent> [--archive]`
- Retry/resume: `camc reboot <agent>`
- History: `camc history <agent> --limit <n>`
- Heal: `camc heal`
- Context list: `camc context list`
- Messaging inbox: `camc msg read --json`
- Messaging thread: `camc msg read <msg_id> --json`
- Messaging send: `camc msg send <to> -t <text> --no-wait [--expect-reply]`
- Messaging reply: `camc msg reply <msg_id> -t <text>`

The Tauri bridge must continue to use argv for local and WSL execution. SSH may
use shell quoting until a safer remote protocol exists, but host validation and
clear error reporting are required.

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

P0 backend interface should be this small:

```ts
interface CamBackend {
  health(): Promise<BackendHealth>;
  listAgents(): Promise<AgentSummary[]>;
  captureAgent(id: string, lines?: number): Promise<CaptureResult>;
  sendToAgent(id: string, text: string, opts?: SendOptions): Promise<void>;
  sendKey(id: string, key: string): Promise<void>;
}
```

The interface may contain future methods, but P0 acceptance depends only on the
five methods above.

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

- Renderer code must not execute arbitrary commands.
- Tauri commands expose typed backend operations or a constrained `camc` argv
  execution path.
- No shell for local or WSL command execution.
- No passwords in profiles.
- Tokens are stored only in local app settings and never logged.
- Destructive actions require explicit confirmation:
  - kill
  - remove
  - archive/remove
  - retry/reboot when it may replace a running session
  - stop for an actively running agent

## Implementation Plan After Approval

The first implementation task for `camui-dev` should be narrowly bounded:

1. Keep the existing Tauri/React app.
2. Make Core P0 reliable: health, list, capture, direct send, key.
3. Hide all visible non-core surfaces from the default V1 UI, including
   metadata inspector, advanced panel, run-agent form, message inbox, TaskHub,
   Tree, and Diff. Prefer not deleting existing scaffold/ChatShell-derived code
   unless keeping it creates build failures, dead imports, or user-visible
   confusion.
4. Split the current app into small components/stores if the implementation is
   changing `App.tsx` substantially.
5. Complete local CLI-backed behavior first.
6. Keep UI state and parsing isolated in `src/lib`.
7. Do not modify core Python CAM behavior unless a desktop blocker proves the
   API contract is wrong.

Expected touched area:

- `apps/cam-desktop/src/lib/*`
- `apps/cam-desktop/src/App.tsx`
- `apps/cam-desktop/src/styles.css`
- `apps/cam-desktop/src-tauri/src/main.rs` only if new bridge commands are
  required
- tests/config files only when needed for build verification

## Verification

Before review, `camui-dev` must report:

- `npm run build:web` from `apps/cam-desktop`
- `npm audit --audit-level=high` from `apps/cam-desktop`
- focused manual smoke result for local `camc` profile:
  - health loads
  - agent list loads
  - selecting agent shows capture/output
  - direct send path works or returns a clear backend error
  - key path works or returns a clear backend error
  - no non-core panels are visible in the default V1 UI
- Tauri build result if Rust platform deps are installed:
  - `npm run build`

If a command cannot run because of missing system dependencies, the reply must
include the exact failure and the next command a human should run.

## Acceptance Criteria

P0 is acceptable when:

- The app builds with TypeScript/Vite.
- The app can list agents from at least one backend profile.
- The selected agent output pane works without blocking the UI.
- Direct text and special-key controls are wired to the backend.
- Selecting a different agent switches the output and input target.
- The default UI contains only connection/list, selected output, and composer.
- Dormant scaffold features, if kept in code, do not appear in V1 UI and do
  not affect the main loop.
- The UI remains simple enough that future TaskHub, Tree/Diff, lifecycle, run,
  messaging, context, and API features can be added later without rewriting the
  backend abstraction.
- All errors are visible and actionable.
