# Cam Desktop Spec

Status: draft

Cam Desktop is a Windows-first desktop client for Cam. The app is a client
surface; `camc` remains the backend/source of truth for agent lifecycle,
message threads, tmux transport, logs, and local storage.

## Goals

- Provide a ChatGPT/Doubao-style desktop UI for managing Cam agents.
- Support Windows first through WSL and SSH backends.
- Keep macOS/Linux support possible through the same backend interface.
- Implement the basic `camc` control surface before adding advanced desktop
  features such as global shortcuts, screenshot workflows, or auto-updates.
- Keep UI components decoupled from CLI details by routing all actions through
  a typed `CamBackend` interface.

## Non-Goals

- Replacing `camc`.
- Running tmux-native agents directly on Windows outside WSL/remote Linux.
- Parsing terminal output as the primary API.
- Building a new daemon before the CLI adapter validates the UX.
- Copying ChatShell, ChatGPT, Doubao, or other app code.

## Architecture

```text
React UI
  -> CamBackend TypeScript interface
  -> CamcCliBackend adapter
  -> Tauri Rust command bridge
  -> local / WSL / SSH camc
  -> agents.json, messages.jsonl, tmux, logs
```

The renderer must not execute shell commands directly. The renderer calls
typed methods. The Rust bridge builds argv safely and runs one of:

- Local: `camc <args...>`
- WSL: `wsl.exe --exec /usr/bin/env camc <args...>`
- SSH: `ssh <host> '<camc> <quoted-args...>'`

No shell is used for local or WSL execution. SSH may use a remote shell until a
future stdio/JSON-RPC backend exists.

## Backend Profiles

```ts
type BackendProfile =
  | { kind: "local"; camcPath?: string }
  | { kind: "wsl"; distro?: string; camcPath?: string }
  | {
      kind: "ssh";
      host: string;
      user?: string;
      port?: number;
      camcPath?: string;
    };
```

Profile requirements:

- Windows default: `wsl`.
- Linux/macOS default: `local`.
- `ssh` requires host validation before any command runs.
- The active profile is stored in local app settings.
- UI must display backend health from `camc version`.

## P0 Basic Cam Functions

P0 is the minimum bar before Cam Desktop can be called a usable Cam client.

| Area | UI Capability |
|---|---|
| Health | Show backend version and connection errors |
| Agents | List agents with name, id, tool, status, state, path, host |
| Agent detail | Show full selected-agent metadata |
| Run | Start named agent with tool/path/prompt/tags/auto-exit |
| Capture | Show current tmux screen/scrollback |
| Direct send | Send text to selected agent |
| Key | Send special key: Enter, Escape, Ctrl+C, Ctrl+D |
| Logs | Show recent logs, optional follow later |
| Stop | Gracefully stop tool process while keeping tmux |
| Kill | Force kill agent and tmux session |
| Remove | Remove record and tmux, optional archive |
| Reboot | Restart/resume locally |
| Update | Rename and tag/untag agent |
| History | Show lifecycle/event history |
| Heal | Run health repair when requested |

P0 backend command mapping:

- Health: `camc version`
- Agents: `camc --json list`
- Agent detail: `camc --json status <agent>`
- Run: `camc run --name <name> --path <path> --tool <tool> <prompt>`
- Run tags/auto-exit: add `--tag <tag>` and `--auto-exit` as requested.
- Capture: `camc capture <agent> --lines <n>`
- Direct send: `camc send <agent> --text <text>`
- Key: `camc key <agent> --key <key>`
- Logs: `camc logs <agent> --tail <n>`
- Stop: `camc stop <agent>`
- Kill: `camc kill <agent>`
- Remove: `camc rm <agent> [--archive]`
- Reboot: `camc reboot <agent>`
- Update: `camc update <agent> --name <name> --tag <tag> --untag <tag>`
- History: `camc history <agent> --limit <n>`
- Heal: `camc heal`

P0 destructive actions must require confirmation in the UI:

- `kill`
- `rm`
- `rm --archive`
- `reboot`
- `stop` if the agent is actively running

## P0 Messaging Functions

Cam Desktop must treat `msg_id` as the thread id. The ledger is the source of
truth; tmux pane injection is only a wake-up path.

| Area | UI Capability |
|---|---|
| Inbox | List unread mailbox deliveries |
| Inbox all | Include read deliveries |
| Next unread | Read oldest unread |
| Mark read | Mark selected thread/turn read |
| Thread replay | Render ordered thread turns |
| Async send | Ask an agent without blocking UI |
| Expected reply | Tell receiver to reply through the ledger |
| Reply | Append next turn under same thread id |
| Compat inspect | Raw legacy message details |

P0 messaging command mapping:

- Inbox: `camc msg read --json`
- Inbox all: `camc msg read --all --json`
- Next unread: `camc msg read --next --json`
- Mark read: `camc msg read <msg_id> --mark --json`
- Thread replay: `camc msg read <msg_id> --json`
- Async send: `camc msg send <to> -t <text> --no-wait`
- Expected reply: `camc msg send <to> -t <text> --no-wait --expect-reply`
- Reply: `camc msg reply <msg_id> -t <text>`
- Compat inspect: `camc msg show <msg_id>`

Thread UI requirements:

- Display turns ordered by `seq`.
- Show `from`, `to`, `seq`, timestamp if available, and text.
- Reply composer must use the selected `msg_id`, not create a new logical id.
- Inbox unread state must come from `delivery` minus `read` records, via
  `camc msg read`; the UI must not infer unread solely from pane text.

## P1 Cam Functions

P1 adds power-user and admin workflows after P0 is complete.

| Area | UI Capability | Backend Command |
|---|---|---|
| Attach | Open external terminal attached to tmux session | `camc attach <agent>` |
| Exit | Ask Claude process to exit while tmux remains | `camc exit <agent>` |
| Adopt | Add existing tmux session as an agent | `camc add <session> --tool <tool> --name <name>` |
| Archive list | Browse archives | `camc archive list` |
| Archive inspect | Read archive info/summary/show | `camc archive info/summary/show <archive>` |
| Prune | Clean drift/orphans | `camc prune [--orphans]` |
| Apply | Run DAG task file | `camc apply --file <yaml> --path <path>` |
| Contexts | Manage project contexts | `camc context ...` |
| Machines | Manage remote machines | `camc machine ...` |
| Sync | Sync camc/configs to remotes | `camc sync` |

P1 operations can be exposed in an advanced panel. They must not clutter the
default chat/agent workflow.

## CamBackend Interface

The UI should depend on this domain-level interface, not raw command strings:

```ts
interface CamBackend {
  health(): Promise<BackendHealth>;

  listAgents(): Promise<AgentSummary[]>;
  getAgent(id: string): Promise<AgentDetail>;
  runAgent(req: RunAgentRequest): Promise<void>;
  captureAgent(id: string, lines?: number): Promise<CaptureResult>;
  sendToAgent(id: string, text: string, opts?: SendOptions): Promise<void>;
  sendKey(id: string, key: string): Promise<void>;
  logs(id: string, tail?: number): Promise<LogResult>;
  stopAgent(id: string): Promise<void>;
  killAgent(id: string): Promise<void>;
  removeAgent(id: string, opts?: RemoveOptions): Promise<void>;
  rebootAgent(id: string): Promise<void>;
  updateAgent(id: string, req: UpdateAgentRequest): Promise<void>;
  history(id: string, limit?: number): Promise<HistoryEvent[]>;
  heal(): Promise<CommandResult>;

  listInbox(opts?: InboxOptions): Promise<MessageSummary[]>;
  readThread(msgId: string): Promise<MessageThread>;
  markThreadRead(msgId: string): Promise<void>;
  sendMessage(req: SendMessageRequest): Promise<SendMessageResult>;
  replyMessage(req: ReplyMessageRequest): Promise<ReplyMessageResult>;
}
```

Existing implementation may start smaller, but P0 is not complete until the
interface has all P0 methods and the UI exposes them.

## UI Layout Requirements

Default desktop layout:

- Left: backend profile, health, agent list.
- Center: selected thread, capture output, or agent interaction.
- Bottom center: composer for `msg send` / `msg reply`.
- Right: inbox, selected-agent detail/actions, run-agent form.

Desktop controls:

- Buttons for destructive actions must show confirmation dialogs.
- Agent action buttons must be disabled while backend command is running.
- Long command failures must display stderr/stdout details.
- UI must remain responsive; all backend calls are async.

## Data Handling

- Renderer may cache for UI speed, but `camc` remains authoritative.
- Polling is acceptable for MVP:
  - agents every 2-5 seconds
  - inbox every 1-2 seconds
  - active capture only on demand or selected-agent refresh
- Future event stream can replace polling:
  - `camc watch --json`
  - `camc api serve --stdio`

## Error Handling

Every backend call returns:

```ts
interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}
```

Rules:

- Non-zero exit code becomes a user-visible error.
- Timeout becomes a user-visible error with the attempted action name.
- Parse failures include the raw stdout snippet.
- For profile failures, show remediation: install WSL, configure host, or set
  `camcPath`.

## Security

- Renderer cannot run arbitrary commands.
- Rust bridge accepts only `camc` args through typed backend methods.
- Local and WSL execution must use argv, not shell strings.
- SSH quoting is temporary; a stdio API should replace it before broad
  deployment.
- Stored profile data must not include passwords or tokens.

## Current Scaffold Coverage

Implemented in the first scaffold:

- `health`
- `listAgents`
- `runAgent` basic name/path/tool/prompt
- `captureAgent`
- `sendToAgent`
- `listInbox`
- `readThread`
- `sendMessage --no-wait`
- `replyMessage`

Missing before P0 completion:

- `getAgent` / selected-agent status detail
- `sendKey`
- `logs`
- `stopAgent`
- `killAgent`
- `removeAgent`
- `rebootAgent`
- `updateAgent`
- `history`
- `heal`
- tags and auto-exit in the run form
- mark-read controls
- destructive-action confirmations

## Acceptance Criteria

P0 is accepted when:

- `npm run build:web` passes.
- `npm audit` has no high/critical vulnerabilities.
- Full Tauri build passes on a machine with Rust/Cargo installed.
- Local profile can list, run, capture, send, stop, kill, and remove agents.
- WSL profile can do the same from Windows when `camc` is installed in WSL.
- SSH profile can list/capture/send against a remote host.
- Messaging can send, reply, replay, and mark read without blocking the UI.
- All P0 destructive operations require explicit confirmation.
