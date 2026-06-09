# ChatShell Baseline Notes

ChatShell is a reference for the desktop product shape, not a code dependency.

## Borrow

- Tauri 2 plus Rust native boundary.
- Cross-platform desktop installer model.
- Local-first state and secure OS boundary.
- Agent-oriented UX instead of a plain chat wrapper.
- Built-in tools and skills as first-class concepts.
- Lightweight app size and low idle memory as design constraints.

## Do Not Borrow

- LLM provider abstraction as the primary backend.
- Built-in shell/file tools as the core execution model.
- Direct tool execution from renderer code.
- ChatShell storage schema.
- ChatShell code, unless license review explicitly approves reuse.

## Cam-Specific Direction

Cam Desktop treats `camc` as the backend source of truth:

```text
React UI
  -> CamBackend interface
  -> CamcCliBackend adapter
  -> local / WSL / SSH camc
  -> agents.json, messages.jsonl, tmux
```

The first adapter shells out to `camc` with argv, not through a shell. This
keeps quoting stable and preserves the CLI as the compatibility boundary.

Future backend adapters can implement the same interface with:

- `camc api serve --stdio`
- `camc api serve --http`
- direct remote gateway

The UI should not know which transport is underneath.

## First Product Slice

- Agent list.
- Agent capture.
- Direct send to agent.
- Message inbox.
- Thread replay.
- Async message send.
- Thread reply.
- Run agent form.

This is enough to validate the Cam-specific workflow before adding global
shortcut, tray, notifications, screenshot capture, or selected-text actions.

