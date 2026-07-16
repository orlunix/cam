# Terminal Action Bar and Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three scattered terminal actions with one fixed four-button bar and add a safe manual hard re-attach action.

**Architecture:** Keep the shared terminal chrome and existing Electron `term:open`/`term:close` contracts. The renderer owns a fixed action-bar container and two narrow pending flags, derives every disabled state from the active terminal entry, and implements Refresh by awaiting the existing `openTerminalForSelected({ force: true })` lifecycle.

**Tech Stack:** Vanilla JavaScript, CSS, Electron IPC, xterm.js, Node source-regression tests.

## Global Constraints

- Keep the existing left and bottom margins and the current `28px` button size.
- Keep the action bar inside the centered `var(--desktop-rail-wide)` terminal chrome.
- Use fixed slots in this order: Attach, History, To Bottom, Refresh.
- Disable unavailable buttons instead of hiding individual slots.
- Refresh may clear local xterm scrollback but must not stop or restart the remote agent/tmux session.
- Reuse `openTerminalForSelected({ force: true })`; add no IPC channel.
- Do not change Mobile, camc, remote tmux configuration, or agent lifecycle behavior.
- Do not commit or push implementation changes without explicit user approval.

---

### Task 1: Fixed terminal action bar

**Files:**
- Modify: `apps/cam-desktop/test/terminal-follow.test.cjs`
- Modify: `web/js/desktop/agent-console.js`
- Modify: `web/css/desktop.css`

**Interfaces:**
- Consumes: existing shared `.terminal-tmux-chrome` and `updateTerminalTmuxControls()` state refresh.
- Produces: `terminalActionBar`, fixed Attach/History/Bottom/Refresh DOM slots, and shared disabled-state rendering.

- [ ] **Step 1: Write failing source-regression assertions**

Add assertions that require:

```js
const actionBarStart = source.indexOf('<div class="terminal-action-bar"');
const attachSlot = source.indexOf('terminal-attach-icon', actionBarStart);
const historySlot = source.indexOf('terminal-history-btn', actionBarStart);
const bottomSlot = source.indexOf('terminal-bottom-btn', actionBarStart);
const refreshSlot = source.indexOf('terminal-refresh-btn', actionBarStart);
ok('terminal action bar has four stable ordered slots',
  actionBarStart >= 0 && attachSlot < historySlot && historySlot < bottomSlot && bottomSlot < refreshSlot);
ok('terminal action slots stay visible and use disabled state',
  source.includes('terminalActionBar.hidden = !terminalVisible')
  && !source.includes('terminalHistoryBtn.hidden =')
  && !source.includes('terminalBottomBtn.hidden ='));
ok('terminal action buttons keep the current size',
  terminalChromeCss.includes('width: 28px;') && terminalChromeCss.includes('height: 28px;'));
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: failures for the missing action-bar wrapper, Refresh slot, and fixed disabled-state behavior.

- [ ] **Step 3: Implement the fixed action-bar DOM and CSS**

In terminal chrome, replace individually positioned buttons with:

```html
<div class="terminal-action-bar" hidden>
  <button class="terminal-attach-icon">...</button>
  <button class="terminal-history-btn">...</button>
  <button class="terminal-bottom-btn">...</button>
  <button class="terminal-refresh-btn">...</button>
</div>
```

Position `.terminal-action-bar` at the existing `left: 16px` and
`bottom: var(--terminal-chrome-bottom-inset)`, with `display: flex` and the
same `4px` gap used by the tmux tabs. Remove absolute positioning from the
three existing buttons. Include Refresh in the existing shared `28px` button
rules, hover rules, and hidden-container rule.

Update `updateTerminalTmuxControls()` so only the action-bar container is
hidden outside Terminal mode. Keep all four slots rendered and set History
and Bottom disabled based on tmux readiness, copy mode, and viewport bottom.

- [ ] **Step 4: Run focused test and confirm GREEN**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: all terminal-follow assertions pass.

---

### Task 2: Safe manual hard re-attach

**Files:**
- Modify: `apps/cam-desktop/test/terminal-follow.test.cjs`
- Modify: `web/js/desktop/agent-console.js`
- Modify: `web/css/desktop.css`

**Interfaces:**
- Consumes: `openTerminalForSelected({ force: true })`, `setTerminalAttachStatus(text, kind, ttl)`, `terminalEntryBySession(sessionId)`.
- Produces: `terminalRefreshPending: boolean`, `terminalAttachmentPending: boolean`, and Refresh click lifecycle.

- [ ] **Step 1: Write failing Refresh lifecycle assertions**

Add source assertions requiring:

```js
ok('Refresh reuses the force-open lifecycle',
  refreshClick.includes('await openTerminalForSelected({ force: true })'));
ok('Refresh prevents overlapping reconnects',
  source.includes('let terminalRefreshPending = false')
  && refreshClick.includes('if (terminalRefreshPending'));
ok('Refresh reports progress and completion',
  refreshClick.includes('Re-attaching terminal...')
  && refreshClick.includes('Terminal re-attached.'));
ok('Refresh adds no second main-process protocol',
  !preload.includes('refresh(payload)') && !main.includes('term:refresh'));
ok('stale old-session status cannot detach the replacement',
  source.includes('const ent = terminalEntryBySession(msg.sessionId)')
  && source.includes('if (!ent) return;'));
```

Also require all four buttons to become disabled while
`terminalRefreshPending`, and require the spinner class/keyframes.

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: failures for missing pending state, click handler, status text, and spinner.

- [ ] **Step 3: Implement Refresh and pending-state rendering**

Add renderer flags:

```js
let terminalRefreshPending = false;
let terminalAttachmentPending = false;
```

The Refresh handler must capture the selected agent id, guard repeated clicks,
set the progress status, call `await openTerminalForSelected({ force: true })`,
then verify `terminalSessions.get(agentId)?.sessionId`. It reports success only
for a live replacement session, reports a retryable error otherwise, and always
clears `terminalRefreshPending` in `finally`.

Set `terminalAttachmentPending` around native terminal attachment selection and
upload so shared state refreshes cannot accidentally re-enable Attach. During a
Refresh, disable all four buttons. Toggle `.is-refreshing` on the Refresh button
and add a one-turn CSS rotation animation with a reduced-motion override.

- [ ] **Step 4: Run focused test and confirm GREEN**

Run: `cd apps/cam-desktop && node test/terminal-follow.test.cjs`

Expected: all assertions pass.

---

### Task 3: Regression, package, and artifact verification

**Files:**
- Verify: `apps/cam-desktop/electron/main.cjs`
- Verify: `apps/cam-desktop/electron/preload.cjs`
- Verify: `web/js/desktop/agent-console.js`
- Verify: `web/css/desktop.css`

**Interfaces:**
- Consumes: completed renderer and CSS changes.
- Produces: verified source tree and installable test MSI.

- [ ] **Step 1: Run the full local Desktop gate**

Run:

```bash
cd apps/cam-desktop
npm run lint:electron
npm run test:term
npm run test:hub
npm run test:start
node --check ../../web/js/desktop/agent-console.js
git diff --check
```

Expected: every command exits `0`, with zero failed assertions.

- [ ] **Step 2: Transfer only changed runtime files to the established builder**

Transfer exact verified copies of `web/js/desktop/agent-console.js` and
`web/css/desktop.css` through `127.0.0.1:2222`, then compare SHA-256 values on
both machines before building.

- [ ] **Step 3: Build and inspect Release 22**

Run `bash ~/cam-build-from-wsl.sh`, administratively extract the resulting MSI
into a fresh directory, and verify the bundled web resources contain:

```text
terminal-action-bar
terminal-refresh-btn
openTerminalForSelected({ force: true })
Re-attaching terminal...
```

The extracted `agent-console.js` and `desktop.css` hashes must equal the local
verified files.

- [ ] **Step 4: Publish the test artifact**

Copy the verified MSI to:

```text
C:\Users\hren\Nutstore\1\Nutstore\app\CAM-Desktop-0.2.0-22-terminal-action-bar-refresh-20260715.msi
```

Report its SHA-256 and leave implementation changes uncommitted for user smoke testing.
