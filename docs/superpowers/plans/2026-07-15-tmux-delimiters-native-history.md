# tmux Printable Delimiters and Native History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Desktop tmux tabs on locale-free tmux 2.7 hosts and make the History/To Bottom controls enter and leave real tmux copy mode without reconnecting the terminal.

**Architecture:** Keep Electron main as the sole owner of tmux commands and targets. Use printable colon-delimited tmux formats for structured control output, enter/cancel copy mode through short commands on the pooled SSH connection, and send high-frequency navigation through the existing long-lived PTY.

**Tech Stack:** Electron 31, Node.js CommonJS main/preload, browser ES modules, xterm.js 6, ssh2 1.16, tmux 2.7+

## Global Constraints

- Desktop Direct/Electron only; do not change Mobile or `web/js/shared/terminal-mount.js`.
- Do not invoke CAMC, reconnect SSH, reopen the PTY, or restart `camc attach` for History controls.
- Renderer supplies only an opaque terminal `sessionId`; main derives the client and pane target.
- Wheel and PageUp/PageDown navigation use the existing PTY, not per-event SSH exec commands.
- To Bottom never sends a raw `q`; it uses tmux 2.7-compatible `send-keys -X -t <pane> cancel` only when `pane_in_mode` is true.
- tmux 2.7 is the compatibility floor.
- Preserve unrelated dirty worktree files. Do not commit or push without separate user authorization.

---

### Task 1: Parse printable tmux control records

**Files:**
- Modify: `apps/cam-desktop/electron/tmux-controls.cjs`
- Modify: `apps/cam-desktop/electron/main.cjs`
- Test: `apps/cam-desktop/test/tmux-controls.test.cjs`

**Interfaces:**
- Produces: `parseClientState(raw) -> {activeIndex:number,paneId:string,copyMode:boolean}|null`
- Produces: `parseWindowRows(raw) -> Array<{index:number,name:string}>` using the first colon only
- Consumes: existing `tmuxCommand(meta, args)` and `_tmuxExec(ent, args)`

- [ ] **Step 1: Write failing parser tests**

Add `parseClientState` to the test import and assertions equivalent to:

```js
assert.deepStrictEqual(parseClientState("0:%0:0\n"), {
  activeIndex: 0, paneId: "%0", copyMode: false,
});
assert.deepStrictEqual(parseClientState("2:%17:1\n"), {
  activeIndex: 2, paneId: "%17", copyMode: true,
});
assert.strictEqual(parseClientState("0_%0_0\n"), null);
assert.deepStrictEqual(parseWindowRows("0:main\n2:node:server\n"), [
  { index: 0, name: "main" }, { index: 2, name: "node:server" },
]);
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `cd apps/cam-desktop && node test/tmux-controls.test.cjs`

Expected: FAIL because `parseClientState` is not exported and the existing window parser expects a tab.

- [ ] **Step 3: Implement bounded printable parsers**

Implement a complete-record regex for client state and split each window row at its first colon. Reject client indexes outside `0..9999`, pane IDs that do not match `%<digits>`, and mode values other than `0|1`. Preserve all colons after the first one in a window name.

- [ ] **Step 4: Request and parse printable formats in main**

Change the client-state format to:

```js
'#{window_index}:#{pane_id}:#{pane_in_mode}'
```

Change the window-row format to:

```js
'#{window_index}:#{window_name}'
```

Replace inline tab splitting in `_tmuxClientState()` with `parseClientState(result.stdout)` and retain the existing `client_state/tmux_parse_failed` diagnostic on `null`.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run: `cd apps/cam-desktop && node test/tmux-controls.test.cjs`

Expected: all parser/command tests pass with no warnings.

---

### Task 2: Add safe native copy-mode IPC

**Files:**
- Modify: `apps/cam-desktop/electron/main.cjs`
- Modify: `apps/cam-desktop/electron/preload.cjs`
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs`

**Interfaces:**
- Produces: `CamBridge.term.copyMode({sessionId}) -> {ok,copyMode:true}|failure`
- Produces: `CamBridge.term.cancelCopyMode({sessionId}) -> {ok,copyMode:false}|failure`
- Consumes: `_ownedTerminal`, `_tmuxClientState`, `_tmuxExec`, and `_tmuxFailure`

- [ ] **Step 1: Replace local-only negative assertions with failing native IPC assertions**

Require the preload to expose `copyMode(payload)` and `cancelCopyMode(payload)`. Require main to register `term:copyMode` and `term:cancelCopyMode`, use `copy-mode -u`, use `send-keys -X -t <pane> cancel`, and avoid any `term:copyScroll` channel.

- [ ] **Step 2: Run the terminal test and verify RED**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: FAIL on missing copy-mode preload/main channels and commands.

- [ ] **Step 3: Implement enter copy mode in main**

Add `termEnterCopyMode(event, payload)` that validates renderer ownership through `_ownedTerminal`, obtains the active pane through `_tmuxClientState`, returns success immediately if it is already in copy mode, otherwise runs:

```js
await _tmuxExec(ent, ['copy-mode', '-u', '-t', state.paneId]);
```

Return `{ok:true, copyMode:true, paneId:state.paneId}` on success and a staged `copy_mode_enter` failure otherwise.

- [ ] **Step 4: Implement safe cancel in main**

Add `termCancelCopyMode(event, payload)` that obtains current state, returns `{ok:true,copyMode:false}` without sending input when `pane_in_mode` is already false, and otherwise runs:

```js
await _tmuxExec(ent, ['send-keys', '-X', '-t', state.paneId, 'cancel']);
```

Return a staged `copy_mode_cancel` failure if tmux rejects the command. Never write `q` to the PTY.

- [ ] **Step 5: Register and expose the narrow IPC methods**

Register `term:copyMode` and `term:cancelCopyMode` in main and expose matching methods in `CamBridge.term`. Do not expose socket, pane, client TTY, command text, or key names to the renderer.

- [ ] **Step 6: Run the terminal test and confirm the IPC assertions pass**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: native IPC assertions pass; renderer assertions remain RED until Task 3.

---

### Task 3: Drive History through tmux and navigation through the PTY

**Files:**
- Modify: `web/js/desktop/agent-console.js`
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs`

**Interfaces:**
- Consumes: `CamBridge.term.copyMode`, `cancelCopyMode`, `input`, and `listWindows`
- Produces: renderer `copyBrowsing` state reconciled from `result.copyMode`

- [ ] **Step 1: Add failing renderer assertions**

Require these source behaviors:

```text
History awaits bridge.copyMode before setting copyBrowsing.
To Bottom awaits bridge.cancelCopyMode before clearing copyBrowsing.
Wheel in copy mode sends five Up/Down escape sequences in one bridge.input call.
PageUp/PageDown send standard escape sequences through bridge.input.
Up/Down are not converted to local page scrolling.
Successful listWindows refresh assigns ent.copyBrowsing = !!result.copyMode.
```

- [ ] **Step 2: Run the terminal test and verify RED**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: FAIL because the renderer still uses `term.scrollLines()` for History and never invokes native copy-mode IPC.

- [ ] **Step 3: Make History and To Bottom asynchronous native actions**

History calls `bridge.copyMode({sessionId})`; only on success does it set `copyBrowsing=true`, disable follow state, update controls, and focus xterm. It does not call local `scrollLines()` because `copy-mode -u` performs the initial page-up remotely.

To Bottom calls `bridge.cancelCopyMode({sessionId})`; on success it clears `copyBrowsing`, calls `terminalScrollToBottom(ent)`, updates controls, and focuses xterm. Failures preserve current state and display a terminal error status.

- [ ] **Step 4: Send copy navigation through the existing PTY**

While `copyBrowsing` is true:

```js
const sequence = direction === 'up' ? '\x1b[A' : '\x1b[B';
bridge.input({ sessionId: entry.sessionId, data: sequence.repeat(5) });
```

Capture PageUp/PageDown in the existing custom key handler and send `\x1b[5~` or `\x1b[6~` once through `bridge.input`. Let ordinary Up/Down continue through xterm's normal `onData` path so tmux moves one line per key. Preserve Ctrl-V handling and normal-mode local wheel scrolling.

- [ ] **Step 5: Reconcile manual tmux state**

After a successful `listWindows`, assign `ent.copyBrowsing = !!result.copyMode` before rendering controls. This lets manual `Ctrl-b [` entry and keyboard exit converge at the existing 2-second refresh without parsing ANSI.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `cd apps/cam-desktop && npm run test:term`

Expected: terminal-follow and tmux-controls tests both pass with no failures.

---

### Task 4: Verify transport compatibility and Desktop regressions

**Files:**
- No production changes expected

**Interfaces:**
- Verifies the complete renderer → preload → main → ssh2 → tmux flow

- [ ] **Step 1: Run syntax and focused suites**

Run:

```bash
cd apps/cam-desktop
npm run lint:electron
npm run test:term
npm run test:hub
npm run test:start
node --check ../../web/js/desktop/agent-console.js
```

Expected: every command exits `0` and reports no failed assertions.

- [ ] **Step 2: Run read-only locale-free parser probes against PDX098**

Use the exact `ssh2` transport to request colon-delimited client and window state. Expected client output matches `^[0-9]+:%[0-9]+:[01]$`; window rows preserve names containing printable punctuation. No tmux state is changed in this step.

- [ ] **Step 3: Review the scoped diff**

Confirm only the approved parser, copy-mode bridge, renderer behavior, tests, and planning documentation changed in this pass. Confirm no CAMC, Mobile, shared terminal, SSH pool, timeout, or terminal-open code changed.

- [ ] **Step 4: Build and publish a numbered MSI for manual verification**

Use the established Windows build script and place the uniquely named MSI in the existing Nutstore app directory. Record the Windows path and SHA-256. Do not commit or push.

- [ ] **Step 5: Manual acceptance on dvnet**

Verify tabs appear, History enters tmux copy mode one page up, wheel moves five lines, Up/Down move one line, PageUp/PageDown move one page, To Bottom safely returns to live output, manual `Ctrl-b [` state is reconciled, and no terminal detach/reconnect occurs.
