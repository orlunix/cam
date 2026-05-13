# Cam Desktop

Cam Desktop is the Windows-first desktop client for `camc`.

Start with the product/API spec before implementation changes:

- [SPEC.md](SPEC.md) - required P0/P1 Cam function coverage, backend
  interface, UI requirements, and acceptance criteria.
- [CHATSHELL_ANALYSIS.md](CHATSHELL_ANALYSIS.md) - analysis of the cloned
  ChatShell baseline and what Cam should keep/drop.
- [docs-chatshell-baseline.md](docs-chatshell-baseline.md) - what we borrow
  from ChatShell as a reference product shape.

This app follows the same broad product shape as ChatShell-style desktop
agent clients:

- Tauri 2 shell with a Rust native boundary.
- React/TypeScript renderer for the chat and agent UI.
- A typed backend interface between the UI and execution backends.
- `camc` remains the source of truth for agents, messages, tmux, and
  lifecycle operations.

The desktop app does not parse terminal screens as a primary API. It calls
`camc` through a small command bridge today, and the adapter can later be
swapped to `camc api serve --stdio` without changing UI components.

## Backend Profiles

Supported profile shapes:

```ts
type BackendProfile =
  | { kind: "local"; camcPath?: string }
  | { kind: "wsl"; distro?: string; camcPath?: string }
  | { kind: "ssh"; host: string; user?: string; port?: number; camcPath?: string };
```

Default Windows path should be WSL:

```text
wsl.exe --exec /usr/bin/env camc ...
```

Default Linux/macOS path should be local:

```text
camc ...
```

SSH is available for remote Linux hosts, but a future stdio API will be
more robust for long-running streams.

## Development

Prerequisites:

- Node.js 20+
- Rust stable
- Tauri prerequisites for the current OS

Install and run:

```bash
npm install
npm run dev
```

Build:

```bash
npm run build
```

## MVP Scope

See [SPEC.md](SPEC.md). P0 is not just chat: the desktop app must cover the
basic Cam lifecycle surface:

- backend health
- list/status/detail
- run/capture/send/key/logs
- stop/kill/remove/reboot/update/history/heal
- inbox/thread/send/reply/mark-read messaging

## Non-Goals For MVP

- Bundling `camc` on Windows.
- Replacing tmux.
- Running agents natively on Windows.
- Building a new cam daemon before the CLI adapter proves the UX.
