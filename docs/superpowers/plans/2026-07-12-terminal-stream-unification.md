# CAM Terminal Stream Unification Implementation Plan

> **For agentic workers:** Review this plan against the current Mobile implementation before changing code. Implement only the approved Phase 1 scope; leave the Relay/WebSocket phase for a separate design and review.

**Goal:** Make Native Mobile command input use its already-open `camc attach` terminal channel in every output mode, eliminating repeated `camc send` process launches on the interactive path while keeping a safe CAMC fallback.

**Architecture:** `camc attach <agent>` remains the only terminal/session authority. Electron main owns the long-lived SSH PTY and emits `term:data`; the renderer writes bytes through the opaque `term:input` bridge. The UI must not construct or execute `tmux` commands. Existing `camc send` and `camc capture` remain fallbacks for clients that lack a ready native terminal channel and for non-stream history retrieval.

**Tech Stack:** Electron main/preload IPC, ssh2 pooled SSH channels, `camc attach`, vanilla ES modules, Android WebView bridge, xterm.js, Node test runner.

## Why this change

The current terminal path already provides a persistent SSH PTY:

`camc attach` → SSH channel → Electron `term:data` / `term:input` → `terminal-mount.js`.

The same terminal can be opened by Mobile even while its visible output mode is Live. However, `web/js/mobile/views/agent-detail.js` currently sends composer text through `sendTerminalInput()` only when `outputMode === 'terminal'`; Live mode sends `api.sendInput()`, which invokes a fresh remote `camc send` command. That loses the benefit of the persistent channel.

Observed local benchmark (one active agent): `camc send` completed in about 752 ms; direct tmux write plus visibility was about 33 ms. This is directional evidence, not an end-to-end Mobile latency claim. The target is to remove the per-message CAMC process startup from the Native Mobile interactive path.

## Global constraints

- Do not expose `tmux`, remote shell strings, SSH credentials, socket paths, or session names to browser/WebView code.
- Keep `camc attach` as the session-validation contract; do not replace it with direct `tmux send-keys`.
- Preserve `api.sendInput()` as the fallback when the native bridge or a ready terminal session is unavailable.
- Do not change Browser/Relay transport in Phase 1. The embedded Hub WebSocket terminal endpoint is not implemented yet.
- Do not replace full-history/manual capture behavior; `camc capture` remains the fallback for initial snapshots and older history.
- Maintain existing terminal input semantics: append `\r` only for an Enter action; raw inserts/keys remain raw.
- Do not commit unrelated working-tree changes, including `apps/cam-desktop/package-lock.json`.

---

## Current verified code anchors

- `apps/cam-desktop/electron/main.cjs:233-475` opens one `~/.cam/camc attach <agent>` SSH PTY per renderer/agent, keeps it in `_terminals`, forwards output as `term:data`, and writes input through `term:input`.
- `apps/cam-desktop/electron/ssh-transport.cjs:616+` opens this channel through the existing SSH connection pool, so it does not establish a new SSH connection per terminal write.
- `web/js/shared/terminal-mount.js:1040+` exposes `terminalSessionReady(agentId)` and `sendTerminalInput(agentId, data, { enter })`.
- `web/js/mobile/views/agent-detail.js:684-689` has the incorrect mode gate: it requires `isTerminalMode()` before choosing `sendTerminalInput()`.
- `web/js/mobile/views/agent-detail.js:748+` calls `ensureAgentTerminalAttach()` even outside Terminal display mode, so a reusable session can already exist while Live is visible.
- `apps/cam-desktop/electron/embedded-hub.cjs:3001+` implements `camc capture` with only a 750 ms cache/coalescing layer. It is a poll optimization, not a persistent output stream.
- `web/js/desktop/agent-console.js:2361+` polls Plain/Rich output through `api.agentOutput`; `renderRichOutput()` is renderer-only and introduces no additional CAMC command.

## Decision and staged scope

### Phase 1 — Native Mobile input reuse (implement now after review)

Change only the Mobile input/key routing condition. If all of the following are true:

1. `window.CamBridge.term_open` is available,
2. `terminalSessionReady(agentId)` is true, and
3. the agent detail is active,

then send text and special keys through the existing terminal session regardless of whether the visible output mode is `terminal`, `live`, or `full`.

Otherwise retain the existing `api.sendInput()` / `api.sendKey()` fallback. The UI display mode must remain independent from the selected input transport.

Expected minimal implementation shape:

```js
function canUseAttachedTerminalInput() {
  return mobileTerminalInput() && terminalSessionReady(agentId);
}

async function sendAgentInput(text, withEnter = true) {
  if (canUseAttachedTerminalInput()) {
    return sendTerminalInput(agentId, text, { enter: withEnter });
  }
  return api.sendInput(agentId, text, withEnter, agentHints());
}
```

Use the same helper for `sendAgentKey()`. Do not force an attach merely because the user sent input: an opening/failed/not-ready session should continue through the CAMC fallback. Existing auto-attach continues to make the fast path ready soon after the detail view opens.

### Phase 2 — Desktop and Native Mobile shared live output (design/review next)

Reuse the established `term:data` byte stream as the source for Terminal, Plain, Rich, and Mobile Live rendering once an attach session exists. This needs a small shared stream controller with:

- a per-agent sequence number and bounded raw-byte ring (suggested 1–4 MB);
- ANSI-aware terminal/screen state for terminal rendering;
- batched text deltas for Plain/Rich rendering, avoiding a full 8,000-line re-render per byte chunk;
- session lifecycle, reconnection, LRU limits, and a capture-based initial snapshot/fallback.

This phase must not assume raw bytes alone reconstruct a terminal after a mid-escape-sequence reconnect. A client needs a checkpoint/snapshot plus subsequent sequence-numbered bytes. Older manual history continues to call `capture --lines N` or full output.

### Phase 3 — Browser/Relay terminal relay (separate project)

The Hub must own `camc attach` and expose an authenticated WebSocket stream. It needs server-side per-agent ring buffers, subscriptions/resume tokens, an explicit single input-writer lease, lifecycle cleanup, and authorization per agent/context. Current Hub WebSocket handling does not provide this, so Phase 3 is not a Mobile-only patch.

## File map

### Phase 1 files

- Modify: `web/js/mobile/views/agent-detail.js` — decouple input transport selection from `outputMode`; preserve API fallback.
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs` if it already has reusable terminal bridge test helpers; otherwise add a narrow Mobile routing test in the repository's established test location after inspection.

### Phase 2 likely files (do not modify in Phase 1)

- `web/js/shared/terminal-mount.js` — publish safe stream events/snapshots to view consumers.
- `web/js/mobile/views/agent-detail.js` — consume streamed output in Live mode.
- `web/js/desktop/agent-console.js` — consume streamed Plain/Rich deltas when available and retain polling fallback.
- `apps/cam-desktop/electron/main.cjs` — if needed, multiplex channel data beyond its originating terminal view without exposing process/SSH details.
- `apps/cam-desktop/electron/embedded-hub.cjs` — retain capture fallback; do not turn its short capture cache into an unbounded stream buffer.

## Phase 1 task checklist

### Task 1: Verify and test transport selection

- [ ] Locate existing terminal bridge/mobile view tests and add a focused failing test for all three states: ready native session uses terminal input in Live mode; unavailable/not-ready session uses `api.sendInput`; `withEnter` is passed unchanged.
- [ ] Run the focused test and confirm the Live-mode terminal assertion fails before the implementation.
- [ ] Add `canUseAttachedTerminalInput()` (or equivalent clearly named predicate) in `agent-detail.js` and use it in both `sendAgentInput()` and `sendAgentKey()`.
- [ ] Run the focused test, then the relevant terminal/mobile test group.
- [ ] Manual Native smoke: open a running agent in Live mode, wait for attach readiness, submit a short non-destructive prompt, and verify it travels through `term:input`; disable/close the terminal session and verify composer fallback still succeeds through the API.

### Task 2: Guard against UI and lifecycle regressions

- [ ] Verify no code changes force Terminal UI visibility or focus while Live/Full output is selected.
- [ ] Verify a terminal close/reconnect flips routing back to `api.sendInput()` until `terminalSessionReady()` becomes true again.
- [ ] Verify special key actions use the attached channel when ready and preserve existing API fallback otherwise.
- [ ] Run syntax checks for the changed module and the applicable Electron/terminal tests.
- [ ] Commit only the Phase 1 code and test files with `fix(mobile): reuse attached terminal input in live mode` if the user authorizes a commit.

## Acceptance criteria

- In Native Mobile Live mode with a ready attach session, normal text, raw inserts, and special keys use `term:input` rather than `/input` → `camc send`.
- With no native bridge, no attach session, a closed session, or attach in progress, behavior remains the current API/CAMC fallback and the composer stays functional.
- The visible output mode does not change as a side effect of sending input.
- No renderer gains command execution or remote-session metadata.
- Desktop Plain/Rich polling behavior remains unchanged in Phase 1.
- The test suite and a device/manual smoke verify both fast path and fallback.

## Review questions for `camui-mobile-cs`

1. Is `terminalSessionReady(agentId)` sufficient to guarantee the existing session belongs to this active view and can accept input, or is another lifecycle/ownership guard required?
2. Does the current Native bridge keep an attach session open in every relevant Live/Full navigation path, including after background/foreground transitions?
3. What test harness best verifies `term:input` versus `api.sendInput` routing without introducing brittle DOM tests?
4. Identify any Mobile-specific risks that make Phase 1 unsafe. If none, implement only Phase 1; report exact files, tests, and remaining Phase 2/3 blockers.

## Self-review

- Scope is deliberately split: a Mobile routing fix is independent from Desktop stream rendering and Relay transport.
- The plan relies on existing authenticated attach/IPC paths and does not add a UI-to-tmux escape hatch.
- The measured latency is labeled directional; no unsupported end-to-end performance claim is made.
- Fallback, reconnection, output-mode independence, and tests are explicit.
