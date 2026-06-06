# ChatShell Reference Evaluation

Status: reviewed for CAM Desktop planning
Baseline: `baseline/chatshell-desktop`
Revision: `235635d chore: bump version to 0.7.0`
License: Apache 2.0

## Executive Summary

ChatShell is a mature Tauri 2 + React desktop AI client with a large Rust
backend. It is valuable as a product and UI reference, but not as a backend
blueprint for CAM Desktop.

The best use for CAM is:

- adopt the desktop shell shape, sidebar layout, input composer patterns,
  scroll behavior, toasts/errors, and settings/profile conventions
- keep CAM's backend boundary centered on `camc` / `cam serve`
- avoid copying ChatShell's LLM provider runtime, local conversation database,
  built-in tool runtime, MCP runtime, and skills runtime into CAM Desktop

The right first milestone is not "as small as possible". It is the highest
leverage slice:

```text
connection/profile
  + agent list
  + selected-agent output
  + input/key composer
  + simple metadata/actions slots
```

If run-agent or messaging is already present in the scaffold and does not
destabilize the core loop, keep it visible behind secondary panels. Do not
delete usable scaffold work just to make the milestone look smaller.

## Reference Size And Shape

Observed structure:

- Frontend TypeScript/TSX files: 236
- Rust backend files: 159
- UI primitive files: 31
- Tauri 2 app with React, Radix UI, Tailwind, Zustand, i18n, toasts
- Rust backend with SQLite, provider/model management, LLM streaming, MCP,
  skills, search, attachments, keychain, and local storage

This is too large to transplant wholesale. CAM Desktop should learn from the
shape and interaction patterns, not inherit the full runtime.

## Feature Mapping

| ChatShell area | CAM mapping | Decision | Reason |
|---|---|---|---|
| Tauri 2 + React shell | Existing `apps/cam-desktop` | Keep now | Already matches repo direction and keeps app lightweight. |
| Sidebar + active item layout | Agent list + backend profile + future inbox | Keep now | Directly maps to CAM's choose-an-agent workflow. |
| Conversation list item pattern | Agent rows | Keep now | Name/status/timestamp/preview maps well to agent list. |
| Chat view scroll behavior | Terminal output pane / future message thread | Keep now, simplified | Auto-scroll only near bottom is useful for live output. |
| Input composer | Direct send, key buttons, future msg/run modes | Keep now | Highest UX leverage; CAM's core value is interaction. |
| IME-safe keyboard handling | Send Enter, newline modifier | Keep now | Low cost, avoids bad input behavior for Chinese/IME users. |
| Toast/error surfaces | CLI/API failure display | Keep now | Required for shelling out to `camc`. |
| Settings/profile dialog | Backend profiles, camc path, WSL, SSH, API token | Keep soon | Profile state is central; initial localStorage is okay, dialog can follow. |
| Local app data | Persist profiles/settings securely | P1 | Useful before packaged release; not required for first dev loop. |
| Keychain/encryption | API tokens, future credentials | P1/P2 | Needed for server tokens later, not for local `camc` P0. |
| Search dialog | Search agents/logs/messages | P2 | Valuable, but requires a CAM-specific index/source. |
| Rich markdown renderer | `camc msg` threads / summaries | P1 | Useful once messaging panel is first-class. |
| Attachments and image paste | Upload files/images to selected agent | P1 | CAM API already has upload; CLI path needs design. |
| Working directory tag | Selected context/path display | Keep now, simplified | CAM users care which repo/context an agent is in. |
| Pending message queue | Queue sends while busy | P2 | Nice, but direct tmux input should stay explicit at first. |
| Export/screenshot | Save output/log snapshots | P2 | Useful, not core to control surface. |
| Conversation fork/resend | Agent/message thread branching | P2+ | Needs CAM semantics before UI work. |
| LLM provider/model runtime | None; agents own models/tools | Drop | Would duplicate CAM/agent responsibilities. |
| Built-in Bash/Read/Edit tools | None; agent CLI already has tools | Drop | CAM Desktop is a manager, not an agent runtime. |
| SQLite conversation store | CAM agent/message state | Drop as source of truth | `camc` and `cam serve` remain authoritative. |
| MCP runtime | Future CAM/backend capability | Drop from desktop P0 | Do not create a second tool platform. |
| Skills runtime | Existing skillm/Codex/Claude skills | Drop from desktop P0 | Skill management belongs elsewhere for now. |
| Web search/fetch backend | Agent tool capability | Drop | Desktop should not browse on behalf of agents in P0. |

## Highest-ROI First Slice

The first shippable slice should include:

- connection/profile health
- agent list with dense status rows
- selected-agent output pane with manual refresh and optional auto-refresh
- input composer with:
  - send text
  - send with/without Enter if supported
  - Enter / Escape / Ctrl-C / Ctrl-D buttons
  - IME-safe keyboard handling
- selected-agent metadata panel
- consistent error/toast surface
- stable layout slots for future run, messaging, logs, and lifecycle controls

The first slice may also keep these if already implemented and low-risk:

- run-agent form
- message inbox/thread/reply panel
- local/WSL/SSH profile selector

These should be treated as opportunistic retained features, not blockers for
accepting the core loop. If they break the core loop, hide them behind an
advanced panel rather than deleting their code.

## Architecture Implications

Use ChatShell's UI decomposition idea, but keep CAM's backend model:

```text
React UI
  -> feature stores/hooks
  -> CamBackend interface
  -> CLI adapter now
  -> API adapter later
  -> camc / cam serve
```

Recommended frontend modules:

- `ConnectionPanel`
- `AgentList`
- `AgentOutputPane`
- `AgentComposer`
- `AgentMetadataPanel`
- `BackendProfileStore`
- `AgentStore`
- `UiSelectionStore`

Avoid a single large `App.tsx` carrying all state. ChatShell shows that the UI
becomes easier to evolve once sidebar, content, input, and stores are separate.

## Reuse Policy

Default: learn patterns and write CAM-specific code.

Direct code reuse is allowed only when:

- the file/pattern is small and isolated
- Apache 2.0 attribution is preserved
- CAM-specific naming and data model are applied
- the copied code does not pull in ChatShell's provider/database/runtime stack

Best candidates for direct or near-direct reuse:

- small UI primitives
- IME-safe keyboard handling
- scroll-to-bottom behavior
- toasts/error presentation

Poor reuse candidates:

- Rust database modules
- LLM provider modules
- chat streaming commands
- MCP/skills runtimes
- attachment persistence stack

## Planning Recommendation

Update CAM Desktop planning from "tiny P0 only" to:

- **Core P0**: list/select/output/input/key, with clean architecture.
- **Retained P0**: keep existing scaffold run/messaging/profile panels if they
  build and do not block the core loop.
- **P1**: polish high-value ChatShell-inspired UX: component split, settings
  dialog, logs tab, lifecycle controls, rich message rendering, upload.
- **P2**: search, export, attachments parity, API/WS multi-machine mode,
  keychain-backed tokens.

