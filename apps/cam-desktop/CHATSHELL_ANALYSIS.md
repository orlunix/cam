# ChatShell Analysis For Cam Desktop

Baseline clone:

```text
baseline/chatshell-desktop
```

Baseline revision:

```text
235635d chore: bump version to 0.7.0
```

License: Apache 2.0.

This repository is a reference baseline only. Do not copy code into Cam
Desktop unless we explicitly decide to reuse a specific file/pattern and keep
license attribution. The default mode is to learn the architecture and write
Cam-specific code.

## What ChatShell Is

ChatShell is a full local LLM desktop client:

- Tauri 2 shell.
- React frontend.
- Rust backend with SQLite.
- Built-in LLM providers and model registry.
- Built-in tools: web search, web fetch, bash, file read/edit/write, grep,
  glob, kill shell.
- MCP client support.
- Skills system.
- Conversation history, search, attachments, rich markdown rendering, and
  local encryption/keychain support.

Cam Desktop is different. Cam already has `camc` as the backend. Cam Desktop
should be a control surface for agents and threads, not another LLM provider
runtime.

## Keep

### Tauri 2 + React Shape

Keep the overall product architecture:

```text
React renderer
  -> typed Tauri commands
  -> Rust native boundary
  -> local OS process/files/network integration
```

Evidence in ChatShell:

- `src-tauri/tauri.conf.json`
- `src-tauri/src/lib.rs`
- `src-tauri/src/main.rs`
- `src/`

For Cam this maps cleanly to:

```text
React renderer
  -> CamBackend interface
  -> camc_exec Tauri command
  -> local / WSL / SSH camc
```

### Modular UI Components

Keep the separation between:

- sidebar
- chat view
- input composer
- settings dialogs
- reusable UI primitives

Useful ChatShell references:

- `src/components/app-sidebar.tsx`
- `src/components/chat-view/chat-view.tsx`
- `src/components/chat-input/ChatInput.tsx`
- `src/components/ui/`

Cam-specific adaptation:

- sidebar becomes backend profile + agent list
- chat view becomes thread/capture view
- input composer becomes mode-aware Cam input pane
- settings becomes backend profile/settings manager

### Dedicated Input Composer

Keep the idea that input is a first-class component, not incidental markup in
the chat view.

ChatShell has:

- `src/components/chat-input/ChatInput.tsx`
- `src/components/chat-input/InputToolbar.tsx`
- `src/components/chat-input/useKeyboardHandlers.ts`
- `src/components/chat-input/useSubmitHandler.ts`

Cam Desktop should implement:

- `InputPane`
- mode selector: message, reply, direct send, key, run
- target selector: selected agent or selected thread
- keyboard handling
- busy/error state

### Store Layer

Keep a frontend store layer once the UI grows beyond the current scaffold.

ChatShell uses Zustand/Immer stores:

- `src/stores/conversation/`
- `src/stores/message/`
- `src/stores/settingsStore.ts`

Cam should use smaller stores:

- `backendProfileStore`
- `agentStore`
- `threadStore`
- `inboxStore`
- `uiSelectionStore`

Do not put raw `camc` commands in stores. Stores call `CamBackend`.

### Rich Thread Rendering

Keep the rich rendering direction:

- markdown
- syntax highlighting
- copy button
- image/file previews later
- scroll behavior
- pending/queued message display

Useful ChatShell references:

- `src/components/markdown-content/`
- `src/components/chat-view/MessageItem.tsx`
- `src/components/chat-view/StreamingMessage.tsx`

Cam-specific simplification:

- render `camc msg` turns ordered by `seq`
- render capture/logs as terminal text
- no LLM provider streaming needed in P0

### Local App Settings

Keep local settings through Tauri app data.

ChatShell uses app data and settings storage in Rust:

- `src-tauri/src/lib.rs`
- `src-tauri/src/db/settings.rs`

Cam P0 can start with browser localStorage for prototype settings. Before
installer release, move profile/settings to Rust-side app data.

### Logging

Keep Rust-side app logging for backend command failures.

ChatShell initializes a logger in `src-tauri/src/lib.rs`. Cam should add
logging once command execution becomes more than the one current bridge.

## Drop

### LLM Provider Runtime

Drop ChatShell's provider/model stack.

Evidence:

- `src-tauri/src/llm/`
- `src-tauri/src/commands/providers.rs`
- `src-tauri/src/commands/models.rs`
- `src/stores/providerStore.ts`
- `src/stores/modelStore.ts`

Reason: Cam agents are launched and managed by `camc`. The desktop app should
not own OpenAI/Anthropic/Ollama provider configuration in P0.

### Built-in Agent Tools

Drop built-in Bash/Read/Edit/Write/Grep/Glob tools as an app feature.

Evidence:

- `src-tauri/src/llm/tools/`
- `src-tauri/src/commands/chat/`

Reason: Cam agents already run in tmux with their own tool environments. The
desktop UI should not create a second tool execution surface.

### ChatShell SQLite Conversation Store

Drop ChatShell's conversation database as source of truth.

Evidence:

- `src-tauri/src/db/`
- `src-tauri/src/models/conversation.rs`
- `src-tauri/src/models/message.rs`

Reason: Cam already has:

- `agents.json`
- `messages.jsonl`
- tmux capture/logs

The UI can cache, but it must not fork authoritative agent/message state.

### MCP Client Runtime

Drop MCP runtime from P0.

Evidence:

- `src-tauri/src/mcp/`
- `src-tauri/src/commands/mcp.rs`
- `src/stores/mcpStore.ts`

Reason: MCP belongs either inside individual coding agents or a future Cam
backend capability. It is not required for desktop agent management.

### Skills Management Runtime

Drop ChatShell skill installation/scanning from P0.

Evidence:

- `src-tauri/src/skills/`
- `src-tauri/builtin-skills/`
- `src/stores/skillStore.ts`

Reason: Cam/skillm already has its own skill management story. Cam Desktop can
expose skill status later, but should not become a second skill manager now.

### Web Search / Web Fetch

Drop ChatShell web search/fetch backend.

Evidence:

- `src-tauri/src/web_search/`
- `src-tauri/src/web_fetch/`

Reason: Cam Desktop is not the agent runtime. Agents can browse/search through
their own tools or future backend capabilities.

### Full Assistant/Prompt Library

Drop ChatShell assistant/persona/prompt library for P0.

Evidence:

- `src/stores/assistantStore.ts`
- `src/stores/promptStore.ts`
- `src/components/assistant-*`
- `src/components/prompt-*`

Reason: Cam P0 needs named agents, threads, run forms, and operational control.
Prompt templates can be a later productivity feature.

## Adapt

### Sidebar

ChatShell sidebar is conversation/provider/library oriented. Cam sidebar should
be operational:

- backend profile health
- agents
- filters: running, idle, completed, failed
- tags
- unread counts

### Chat View

ChatShell chat view is model conversation oriented. Cam chat view should be
thread/capture oriented:

- `msg read <msg_id>` thread
- `capture <agent>` screen
- `logs <agent>`
- status/history panels

### Input

ChatShell input targets an LLM conversation. Cam input targets an operation:

- message selected agent
- reply selected thread
- direct send selected tmux agent
- send key
- run new agent

### Storage

ChatShell stores all app data in SQLite. Cam should use:

- `camc` as authority
- local UI settings in app data
- optional local cache for display speed only

### Event Model

ChatShell streams model responses. Cam P0 can poll:

- agents every 2-5 seconds
- inbox every 1-2 seconds
- capture/logs on demand

Future Cam backend can add:

- `camc watch --json`
- `camc api serve --stdio`

Then the adapter changes, not the UI contract.

## Immediate Changes To Cam Desktop

Based on this baseline, update our scaffold in this order:

1. Split `App.tsx` into panes/components:
   - `AgentSidebar`
   - `MainView`
   - `InspectorPane`
   - `InputPane`
2. Add a small Zustand store layer.
3. Expand `CamBackend` to all P0 methods from `SPEC.md`.
4. Implement missing P0 backend methods in `CamcCliBackend`.
5. Replace the current ad hoc bottom composers with a dedicated mode-aware
   `InputPane`.
6. Add confirmation dialogs for destructive actions.
7. Add selected-agent detail/status/history/logs views.

## Decision

Use ChatShell as a reference for desktop architecture and UI structure, not as
a backend model. Cam Desktop must stay `camc`-centered.

