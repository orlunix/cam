# Desktop Terminal tmux Interactions Implementation Plan

> **For implementation workers:** execute the tasks in order, keep Feature A
> and Feature B independently reviewable, and run the listed checks before
> requesting integration.

**Goal:** Add tmux window tabs and mouse-first remote copy-mode browsing to
CAM-Desktop Terminal mode while retaining each existing SSH PTY.

**Execution order:** Task 1 → Task 4 → Task 2 → Task 3 → Task 5 → Task 6.
Task numbers group feature concerns; attached-client discovery in Task 4 is a
hard prerequisite for both the safe copy-mode controls and tabs.

## Guardrails

- Work in Desktop Direct/Electron only. Do not change Mobile or
  `web/js/shared/terminal-mount.js` in this pass.
- Do not change `camc attach`, CAMC's send/message fast path, or the SSH pool
  for Feature B.
- Preserve the dirty user files currently outside this scope.
- Do not commit or push without explicit user approval.

## Files and ownership

| File | Change |
| --- | --- |
| `web/js/desktop/agent-console.js` | Terminal copy-mode state, input interception, controls, and later tab rendering. |
| `web/css/desktop.css` | Floating history controls and tmux tab-strip styling. |
| `apps/cam-desktop/electron/preload.cjs` | Expose structured tmux-window IPC only for Feature A. |
| `apps/cam-desktop/electron/main.cjs` | Own session-to-tmux-client mapping and secure tmux-window IPC handlers. |
| `apps/cam-desktop/test/terminal-follow.test.cjs` | Extend focused source/behavior regression checks. |
| `apps/cam-desktop/test/terminal-tmux-controls.test.cjs` | New focused tests for input sequences, state and IPC validation. |

## Task 1 — baseline and test seam

- [ ] Run `npm run test:term` in `apps/cam-desktop` and save the result.
- [ ] Confirm `agent-console.js` is the Desktop terminal owner; do not edit
  the separate shared/mobile terminal module.
- [ ] Add focused test helpers that can exercise pure copy-mode state/input
  mapping without an Electron window. Keep source-shape assertions only where
  no practical unit seam exists.

## Task 2 — copy-mode state and PTY input (Feature B core)

**Files:** `web/js/desktop/agent-console.js`, tests.

- [ ] Add per-terminal-entry state: `copyBrowsing`, plus disposable wheel and
  key handlers. Initialize false and clear it on close/detach.
- [ ] Implement `enterTerminalHistory(entry)`:
  require a live `sessionId` plus discovered client/pane metadata, run a
  fixed tmux `copy-mode -t <pane>` command on the pooled SSH connection, set
  state only after success, and request a controls refresh.
- [ ] Implement `exitTerminalHistory(entry)`:
  run fixed tmux `send-keys -X cancel -t <pane>` on the pooled connection,
  clear state, call the existing `terminalScrollToBottom(entry)`, then restore
  normal auto-follow. Never send a raw `q`, because it can reach the agent
  after a user manually exits copy-mode.
- [ ] Change the terminal `term:data` callback so `copyBrowsing` forces
  `shouldFollow=false`. Do not suppress or discard incoming data.
- [ ] Extend the **existing single** xterm custom key hook rather than adding
  another. Preserve Ctrl-V attachment handling; when browsing, forward
  PageUp/PageDown to the PTY and suppress xterm local scrolling. Do not infer
  copy-mode exit from a raw `q`/Escape key; the safe bottom control cancels
  mode through tmux even if the UI state is stale.
- [ ] Install a removable, capture-phase wheel handler on the xterm element.
  Only while browsing, prevent xterm local scrolling and send bounded Up/Down
  sequences to the same session. Remove it on terminal disposal.

**Tests:** enter/exit issue exactly the fixed client-derived tmux commands
(never raw `q`); PageUp is not consumed as local viewport
scrolling; wheel is ignored outside browsing; incoming output does not call
`scrollToBottom` while browsing; Ctrl-V remains handled by the existing
attachment flow; switching away and back restores the selected entry's
correct control state without adding duplicate handlers.

## Task 3 — Terminal floating controls (Feature B UI)

**Files:** `web/js/desktop/agent-console.js`, `web/css/desktop.css`.

- [ ] Add `↑ History`, `↓ To bottom`, icon-only Attach, and the compact status
  to a Terminal-canvas overlay whose **control rail** is centered and has the
  exact `min(100%, var(--desktop-rail-wide))` width of the agent header and
  Plain/Rich content. The Terminal canvas itself remains full width; only its
  controls are rail-constrained. Do not reuse Rich/Plain buttons because their
  semantics are local output history rather than tmux copy-mode. The existing
  `.output-float-controls` already models this rail; the Terminal chrome may
  use equivalent centered geometry, but must stay anchored vertically to the
  visible `.agent-terminal` canvas.
- [ ] Add one `updateTerminalHistoryControls()` called from selection changes,
  output-mode changes, session changes, and enter/exit actions.
- [ ] Show only when Terminal is active, Direct is available, the selected
  terminal has a live session, and attached-client discovery succeeded. In
  live state show `↑ History`; in browsing state show `↓ To bottom`. On
  discovery failure, keep normal Terminal/Attach working and show neither
  history control.
- [ ] Style controls as small tab-shaped absolute overlays: `↑ History`
  top-right, Attach icon bottom-left, compact transient status upper-left
  directly below the tab strip, and `↓ To bottom` bottom-right. Bottom
  controls share a baseline/height and sit 1.5× the configured terminal font
  size above the canvas edge; status remains content-width and must not become
  a full-width banner. Preserve
  z-index, pointer events, contrast, keyboard focus, tooltip, and screen
  reader text. Position all of these against the centered Terminal control
  rail, so their horizontal edges align with the header's agent name/status
  and Plain/Rich/Terminal/Browse mode controls—not the full-width Terminal
  canvas. Give the upper-left status a bounded rail width, ellipsis/accessible
  full text, and test narrow Desktop window widths.
- [ ] When Feature A adds the tab strip, anchor history to the output canvas
  below the strip, not the top-right of the chrome row; tabs and history must
  never overlap.

**Manual smoke:** attach, browse with wheel/Up/PageUp, let the agent emit
output, exit via the bottom control, then repeat after switching agents.

## Task 4 — attached-client discovery and safe tmux controls (shared foundation)

**Files:** `apps/cam-desktop/electron/main.cjs`, preload, tests.

- [ ] At `term:open`, retain the resolved agent's `tmux_session`,
  `tmux_socket`, and `tmux_bin` alongside the terminal session. Before opening
  the PTY, snapshot `list-clients`; after attach, poll for one new
  `client_tty`. Serialize discovery per tmux session so concurrent Desktop
  attaches cannot make the before/after diff ambiguous. Remove metadata in
  `_dropSession`/channel close.
- [ ] Resolve the client-scoped active window and pane with
  `display-message -c <client_tty>`. `list-windows` alone is insufficient:
  its `window_active` is session-scoped and can describe a different user's
  client.
- [ ] If discovery has zero/multiple candidates, times out, or lacks required
  metadata, leave normal Terminal attach fully working but disable tabs and
  history controls with a clear status; never fall back to a guessed client.
- [ ] Validate ownership on every IPC request: a renderer may control only its
  own `sessionId`; never accept shell text from the renderer.
- [ ] Add main/preload methods for client-scoped copy-mode enter, cancel, and
  state lookup. They accept only `sessionId`; the main process obtains the
  mapped pane and constructs the tmux command itself.
- [ ] Build only internally quoted tmux commands from retained metadata and
  validated renderer values. Verify `list-clients`, `display-message -c`,
  `list-windows`, `switch-client -c`, `copy-mode`, `send-keys -X cancel`, and
  `new-window -P -F` on tmux 2.7 before freezing the command builder.

## Task 5 — tmux window controls and tab UI (Feature A)

**Files:** main, preload, `agent-console.js`, CSS, tests.

- [ ] Add narrow IPC methods: `term:listWindows`, `term:selectWindow`, and
  `term:createWindow`. Return structured `{ ok, windows, activeIndex }` data;
  failures return stable error/detail fields.
- [ ] Main process implements `list-windows` over the pooled SSH control
  connection, derives active state with `display-message -c`, switches with
  `switch-client -c`, and creates through `new-window -P -F` followed by a
  client-scoped switch. Validate a tab index against the immediately preceding
  list result before constructing its tmux target. Do not open a second
  `camc attach` channel.
- [ ] Render a Terminal-only tab strip from returned metadata. Click selects a
  window only after the tmux action is confirmed by refreshed metadata; `+`
  creates then refreshes.
- [ ] Refresh after explicit controls and on terminal reattach. Bound any
  passive refresh to visible Terminal mode.

**Tests:** IPC rejects unknown/foreign session IDs; no renderer-provided shell
fragment reaches the command builder; select/create failure preserves the
active tab; control response shape works with zero, one, and many windows;
zero/multiple-client discovery disables controls without affecting attach; the
select/create path targets only the discovered `client_tty` and does not open
another `camc attach` channel.

## Task 6 — verification and handoff

- [ ] `cd apps/cam-desktop && npm run test:term`
- [ ] `cd apps/cam-desktop && node --check electron/main.cjs && node --check electron/preload.cjs`
- [ ] Run the new focused test file.
- [ ] Manual Desktop smoke against tmux 2.7: measure attach-to-tab-list,
  select-window, and create-window timings; confirm no new SSH connection and
  no new `camc attach` per action.
- [ ] On the same tmux 2.7 target, verify client discovery, client-scoped
  selection, creation, copy-mode entry, page navigation, wheel mapping, and
  safe cancel. Record the exact tmux version. Also test the ambiguity path
  with two simultaneous fresh clients: controls must disable rather than guess.
- [ ] Regression smoke: Ctrl-V attachment, terminal resize, agent fast switch,
  Rich/Plain More+/Jump-to-bottom, terminal reconnect, and terminal close.
- [ ] Layout smoke at a wide Desktop viewport: confirm Terminal canvas remains
  full width while tabs, History, Attach, bottom status, and To bottom align
  horizontally with the agent header and Plain/Rich rail. Repeat at a narrow
  viewport to confirm the rail contracts without clipping controls.
- [ ] Report changed files, exact test output, manual timing observations, and
  known limitations. Do not commit/push unless separately authorized.
