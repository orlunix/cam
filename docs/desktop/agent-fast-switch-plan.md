# Agent Fast-Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make switching between the latest 6 agents in Terminal mode effectively 0-latency (a CSS `visibility` toggle, not a DOM re-parent + reflow), and make LRU eviction non-destructive to visible scrollback by serializing the xterm buffer to a string on evict and restoring it on reopen.

**Architecture:** Two changes, both in `web/js/shared/terminal-mount.js` plus one new UMD addon shipped under `web/vendor/xterm/`. Change 1 stops re-parenting the per-agent container on every show — the 6 containers already live as siblings in the shared `#agent-terminal` host, so switching is a `visibility` toggle + one deferred `fit()`. Change 2 snapshots scrollback via `@xterm/addon-serialize` on eviction (a string), closes only the SSH PTY channel (the pooled control connection already survives), and restores the buffer from the string on reopen before the fresh `camc attach` channel flows new output on top.

**Tech Stack:** Electron 31, xterm.js `@xterm/xterm@^6.0.0`, `@xterm/addon-fit@^0.11.0` (already shipped as UMD at `web/vendor/xterm/`), new `@xterm/addon-serialize@0.14.0` (UMD, version-paired with xterm 6.x). Renderer is vanilla JS modules; no build step for the renderer (scripts loaded as plain `<script>` in `web/desktop.html`). CSP: `script-src 'self'` — the addon must be a local file, not a CDN fetch.

## Global Constraints

- **Pin the addon version exactly:** `@xterm/addon-serialize@0.14.0` (NOT `^0.14.0`, NOT a `0.15.0-beta.*`). xterm addons are version-coupled to the core (the addon reads `_terminal.buffer` / `_core._inputHandler._curAttrData` internals); `0.14.0` was published from the same monorepo commit `f447274f` as `@xterm/xterm@6.0.0` on 2023-11-01. A mismatched version will throw at `loadAddon` time or corrupt the buffer.
- **CSP `script-src 'self'`:** the addon UMD MUST live at `web/vendor/xterm/addon-serialize.js` and load as `<script src="vendor/xterm/addon-serialize.js?v=0.64.0">`. No CDN, no inline.
- **No layout change, no tab strip.** The sidebar-select UI stays. The 6-slot LRU cache (`TERMINAL_CACHE_LIMIT = 6`, `terminal-mount.js:10`) stays 6.
- **No change to the SSH control-connection pool** (`ssh-transport.cjs` `_pool`). Only the per-agent PTY channel + xterm lifecycle change.
- **Cache-bust query strings** must stay in sync: the xterm bundles in `web/desktop.html` use `?v=0.64.0`. Match that exact version string when adding the new `<script>`.
- **No commit until the built MSI is verified** (per `msi-verify-before-commit` memory). The implementation tasks may run `node --check` and local smoke, but the commit + push to `camui-desktop-v2` happens only after Task 6 (MSI build + grep app.asar + install smoke) passes.
- **Branch:** `camui-desktop-v2`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/cam-desktop/package.json` | Modify | Add `@xterm/addon-serialize@0.14.0` to `dependencies` (pinned). |
| `web/vendor/xterm/addon-serialize.js` | Create | Ship the UMD bundle so `window.SerializeAddon` is present under CSP. |
| `web/desktop.html` | Modify | Load the new UMD as a `<script>` before the module bundle, mirroring addon-fit. |
| `web/js/shared/terminal-mount.js` | Modify | Change 1 (stop re-parenting on show) + Change 2 (serialize on evict, restore on reopen). |
| `web/js/shared/terminal-mount.js` (module state) | Modify | Add a `Map` for parked snapshots keyed by agentId. |

No other files change. The Electron main (`main.cjs`), preload, and `ssh-transport.cjs` are untouched — the pool lifecycle is already correct.

---

## Task 1: Ship the SerializeAddon UMD and load it

**Files:**
- Create: `web/vendor/xterm/addon-serialize.js`
- Modify: `apps/cam-desktop/package.json:27-32` (the `dependencies` block)
- Modify: `web/desktop.html:17-18` (the xterm `<script>` block)

**Interfaces:**
- Produces: `window.SerializeAddon` (constructor) available at module mount time, same global-access pattern as `window.FitAddon`. Later tasks consume it as `window.SerializeAddon` (note: unlike `FitAddon` which nests as `window.FitAddon.FitAddon`, the serialize addon exposes its constructor directly on `window.SerializeAddon`).

- [ ] **Step 1: Add the pinned dependency to package.json**

In `apps/cam-desktop/package.json`, the `dependencies` block currently is:

```json
  "dependencies": {
    "@tauri-apps/api": "^2.0.0",
    "@xterm/addon-fit": "^0.11.0",
    "@xterm/xterm": "^6.0.0",
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "ssh2": "^1.16.0"
  },
```

Change it to (insert `@xterm/addon-serialize` alphabetically between `addon-fit` and `xterm`, pinned exact — no `^`):

```json
  "dependencies": {
    "@tauri-apps/api": "^2.0.0",
    "@xterm/addon-fit": "^0.11.0",
    "@xterm/addon-serialize": "0.14.0",
    "@xterm/xterm": "^6.0.0",
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "ssh2": "^1.16.0"
  },
```

- [ ] **Step 2: Obtain the UMD bundle and ship it under web/vendor/xterm/**

Run from the cam repo root:

```bash
cd /tmp && rm -rf xterm-serialize-check && mkdir xterm-serialize-check && cd xterm-serialize-check && \
  npm pack @xterm/addon-serialize@0.14.0 --silent && \
  tar -xzf xterm-addon-serialize-0.14.0.tgz && \
  cp package/lib/addon-serialize.js /home/hren/gitlab/cam/web/vendor/xterm/addon-serialize.js
```

Expected: no output; the file lands at `web/vendor/xterm/addon-serialize.js`.

If the npm classifier blocks `npm pack`, fall back to a direct tarball fetch:

```bash
curl -sL https://registry.npmjs.org/@xterm/addon-serialize/-/addon-serialize-0.14.0.tgz -o /tmp/as.tgz && \
  tar -xzf /tmp/as.tgz -C /tmp/ && \
  cp /tmp/package/lib/addon-serialize.js /home/hren/gitlab/cam/web/vendor/xterm/addon-serialize.js
```

- [ ] **Step 3: Verify the shipped file is a UMD exposing window.SerializeAddon**

Run:

```bash
head -c 300 /home/hren/gitlab/cam/web/vendor/xterm/addon-serialize.js
```

Expected output begins with a UMD header of the shape:

```
!function(e,t){"object"==typeof exports&&"object"==typeof module?module.exports=t():"function"==typeof define&&define.amd?define([],t):"object"==typeof exports?exports...=t():e.SerializeAddon=t()}(globalThis,...
```

The key signature: the global fallback assigns to `e.SerializeAddon` (where `e` is `globalThis`), and the IIFE is invoked with `globalThis`. This matches the already-shipped `web/vendor/xterm/addon-fit.js` pattern (which assigns to `e.FitAddon`).

Also verify the file is non-trivial:

```bash
wc -c /home/hren/gitlab/cam/web/vendor/xterm/addon-serialize.js
```

Expected: a file on the order of tens of KB (typically 30–60 KB), NOT zero or a few hundred bytes (which would indicate a failed fetch).

- [ ] **Step 4: Load the UMD in desktop.html**

In `web/desktop.html`, the current xterm block (lines 14–18) is:

```html
  <!-- CAM-DESK-TERM-002: xterm UMD bundles expose window.Terminal and
       window.FitAddon. Loaded before any module so agent-console.js
       sees them at mount time. -->
  <script src="vendor/xterm/xterm.js?v=0.64.0"></script>
  <script src="vendor/xterm/addon-fit.js?v=0.64.0"></script>
```

Change it to:

```html
  <!-- CAM-DESK-TERM-002: xterm UMD bundles expose window.Terminal,
       window.FitAddon, and window.SerializeAddon. Loaded before any
       module so agent-console.js / terminal-mount.js see them at mount
       time. SerializeAddon (CAM-DESK-TERM-FS) is used to snapshot
       scrollback on LRU eviction and restore it on reopen. -->
  <script src="vendor/xterm/xterm.js?v=0.64.0"></script>
  <script src="vendor/xterm/addon-fit.js?v=0.64.0"></script>
  <script src="vendor/xterm/addon-serialize.js?v=0.64.0"></script>
```

Note: the `?v=0.64.0` cache-bust string MUST match the existing two scripts exactly.

- [ ] **Step 5: Verify the load order and CSP compliance**

Run:

```bash
grep -n "vendor/xterm" /home/hren/gitlab/cam/web/desktop.html
```

Expected: three `<script>`/`<link>` lines for `xterm.css`, `xterm.js`, `addon-fit.js`, plus the new `addon-serialize.js` — all with `?v=0.64.0`, all before the `modulepreload` lines. The CSP at line 6–7 (`script-src 'self'`) admits them because they are same-origin relative paths.

- [ ] **Step 6: Commit**

```bash
cd /home/hren/gitlab/cam && git add apps/cam-desktop/package.json web/vendor/xterm/addon-serialize.js web/desktop.html && \
  git commit -m "cam-desktop: ship @xterm/addon-serialize@0.14.0 UMD for scrollback snapshot

Loads addon-serialize.js as a plain <script> in desktop.html (same
CSP-self pattern as addon-fit, CAM-DESK-TERM-002). Pinned 0.14.0 —
version-coupled to @xterm/xterm@^6.0.0 (same monorepo commit f447274f).
No behavior change yet; window.SerializeAddon is consumed in the
next change (serialize-on-eviction).

Refs: docs/desktop/agent-fast-switch-spec.md"
```

---

## Task 2: Load SerializeAddon onto each new terminal

**Files:**
- Modify: `web/js/shared/terminal-mount.js:538-542` (the FitAddon loadAddon block in `createTerminalEntry`)

**Interfaces:**
- Consumes: `window.SerializeAddon` (from Task 1).
- Produces: each `terminalSessions` entry gains an `entry.serialize` field holding the `SerializeAddon` instance, with the addon loaded onto `entry.term`. Later tasks (Task 4) call `entry.serialize.serialize({...})` to snapshot and rely on the addon being loaded so `entry.term.write(snapshot)` restores.

- [ ] **Step 1: Add the SerializeAddon loadAddon in createTerminalEntry**

In `web/js/shared/terminal-mount.js`, the current FitAddon block (lines 538–542) is:

```js
  const FitCtor = window.FitAddon && window.FitAddon.FitAddon;
  if (FitCtor) {
    entry.fit = new FitCtor();
    entry.term.loadAddon(entry.fit);
  }
  entry.term.open(container);
```

Change it to (append the serialize addon load AFTER `entry.term.open(container)` — the serialize addon must be loaded after the terminal is opened, per xterm addon lifecycle):

```js
  const FitCtor = window.FitAddon && window.FitAddon.FitAddon;
  if (FitCtor) {
    entry.fit = new FitCtor();
    entry.term.loadAddon(entry.fit);
  }
  entry.term.open(container);
  const SerializeCtor = window.SerializeAddon;
  if (SerializeCtor) {
    try {
      entry.serialize = new SerializeCtor();
      entry.term.loadAddon(entry.serialize);
    } catch { /* serialize unavailable — evict will be destructive, see spec Risks */ }
  }
```

Note: `window.SerializeAddon` is the constructor directly (NOT `window.SerializeAddon.SerializeAddon` — the serialize addon's UMD assigns the constructor to the global, unlike FitAddon which nests). Getting this wrong throws "X is not a constructor" at runtime.

- [ ] **Step 2: Verify syntax**

Run:

```bash
node --check /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Expected: no output (exit 0). `node --check` works on plain JS modules even though this is an ES module — it only parses, doesn't evaluate imports.

- [ ] **Step 3: Smoke-verify the addon loads at terminal creation**

This needs a running desktop. Defer the full smoke to Task 5, but do a quick renderer console check if a dev instance is up: open 1 agent in Terminal mode, then in the renderer DevTools console run:

```js
// In the renderer console:
const entries = window.__termDebug && window.__termDebug(); // if a debug hook exists
// Otherwise just confirm no error in the console on agent open.
```

Expected: no "SerializeAddon is not a constructor" or "loadAddon" error in the renderer console when an agent terminal opens. If the desktop dev instance is not running, skip this step and rely on Task 5's smoke — the `node --check` in Step 2 plus the `if (SerializeCtor)` guard makes this safe.

- [ ] **Step 4: Commit**

```bash
cd /home/hren/gitlab/cam && git add web/js/shared/terminal-mount.js && \
  git commit -m "cam-desktop: load SerializeAddon onto each new terminal

createTerminalEntry now instantiates window.SerializeAddon and
loadAddon(entry.serialize) after term.open(container). The
entry.serialize instance is reused by the eviction path (next
change) to snapshot scrollback. Guarded so a missing addon does
not break terminal creation.

Refs: docs/desktop/agent-fast-switch-spec.md Change 2"
```

---

## Task 3: Stop re-parenting on show (Change 1 — the fast path)

**Files:**
- Modify: `web/js/shared/terminal-mount.js:379-394` (`remountTerminalContainer`)
- Modify: `web/js/shared/terminal-mount.js:396-418` (`showTerminalEntry`)

**Interfaces:**
- Consumes: the shared `#agent-terminal` host (already persistent, grabbed once at `agent-console.js:1289`). The 6 per-agent containers are already siblings inside it (appended at `createTerminalEntry` line 500).
- Produces: `showTerminalEntry` becomes a fast path — when the container is already a child of the shared host, it skips `remountTerminalContainer` entirely and only toggles `visibility` + runs one deferred `fit()`. `remountTerminalContainer` loses its orphan-sibling cleanup (which would destroy the other tabs under pre-mount) and becomes a pure fallback for first-mount / host-changed.

**Why the orphan-cleanup must go:** `remountTerminalContainer` lines 381–383 currently do `for (const orphan of hostEl.querySelectorAll('.agent-terminal-pane')) if (orphan !== ent.container) orphan.remove();`. Under the pre-mount model those siblings are the *other cached tabs* — leaving this in would destroy them on every show. This is the one destructive behavior the spec calls out as mandatory to remove.

- [ ] **Step 1: Drop the orphan-cleanup from remountTerminalContainer**

In `web/js/shared/terminal-mount.js`, the current `remountTerminalContainer` (lines 379–394) is:

```js
function remountTerminalContainer(ent, hostEl) {
  if (!ent?.container || !hostEl) return;
  for (const orphan of hostEl.querySelectorAll('.agent-terminal-pane')) {
    if (orphan !== ent.container) orphan.remove();
  }
  if (!hostEl.contains(ent.container)) {
    hostEl.appendChild(ent.container);
  }
  ent.container.hidden = false;
  ent.container.style.display = 'block';
  ent.container.style.visibility = 'visible';
  ent.container.style.height = '100%';
  ent.container.style.width = '100%';
  scheduleTerminalFitDeferred(ent, { keepBottom: true });
  try { ent.term.refresh && ent.term.refresh(0, Math.max(0, (ent.term.rows || 24) - 1)); } catch { /* noop */ }
}
```

Change it to (remove the orphan-cleanup loop — under pre-mount, the siblings are intentional cached tabs):

```js
function remountTerminalContainer(ent, hostEl) {
  if (!ent?.container || !hostEl) return;
  if (!hostEl.contains(ent.container)) {
    hostEl.appendChild(ent.container);
  }
  ent.container.hidden = false;
  ent.container.style.display = 'block';
  ent.container.style.visibility = 'visible';
  ent.container.style.height = '100%';
  ent.container.style.width = '100%';
  scheduleTerminalFitDeferred(ent, { keepBottom: true });
  try { ent.term.refresh && ent.term.refresh(0, Math.max(0, (ent.term.rows || 24) - 1)); } catch { /* noop */ }
}
```

- [ ] **Step 2: Make showTerminalEntry skip the remount when the container is already in the shared host**

In the same file, the current `showTerminalEntry` (lines 396–418) is:

```js
function showTerminalEntry(agentId, hostEl, opts = {}) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return ent;
  hideTerminalEntries();
  if (hostEl) remountTerminalContainer(ent, hostEl);
  termAgentId = agentId;
  ent.lastUsed = Date.now();
  if (opts.keepBottom !== false) {
    ent.needsBottom = true;
    ent.forceBottomUntil = Date.now() + 1200;
    terminalScrollToBottom(ent);
  }
  if (ent.container) {
    ent.container.hidden = false;
    ent.container.style.display = 'block';
    ent.container.style.visibility = '';
  }
  scheduleTerminalFitDeferred(ent, { keepBottom: opts.keepBottom !== false });
  requestAnimationFrame(() => {
    try { ent.term?.focus(); } catch { /* noop */ }
  });
  return ent;
}
```

Change the `remountTerminalContainer` call (line 400) to be conditional — only remount when the container is NOT already in the host (first mount, or host element changed):

```js
function showTerminalEntry(agentId, hostEl, opts = {}) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return ent;
  hideTerminalEntries();
  if (hostEl && !hostEl.contains(ent.container)) {
    remountTerminalContainer(ent, hostEl);
  }
  termAgentId = agentId;
  ent.lastUsed = Date.now();
  if (opts.keepBottom !== false) {
    ent.needsBottom = true;
    ent.forceBottomUntil = Date.now() + 1200;
    terminalScrollToBottom(ent);
  }
  if (ent.container) {
    ent.container.hidden = false;
    ent.container.style.display = 'block';
    ent.container.style.visibility = '';
  }
  scheduleTerminalFitDeferred(ent, { keepBottom: opts.keepBottom !== false });
  requestAnimationFrame(() => {
    try { ent.term?.focus(); } catch { /* noop */ }
  });
  return ent;
}
```

The fast path: when the container is already in the shared host (the cached case — 6 or fewer alive agents), `hostEl.contains(ent.container)` is true, so the remount is skipped. The visibility toggle at lines 408–412 + the one `scheduleTerminalFitDeferred` at line 413 handle the switch. No re-parent, no reflow.

- [ ] **Step 3: Verify syntax**

Run:

```bash
node --check /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Expected: no output (exit 0).

- [ ] **Step 4: Smoke-verify the fast path (deferred to Task 5)**

The full switch-latency smoke (open 6 agents, switch between them, confirm no flash) is in Task 5. Here, just confirm the file parses and the two functions look right by re-reading them:

```bash
sed -n '379,418p' /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Expected: `remountTerminalContainer` has NO `querySelectorAll` line; `showTerminalEntry` has the `if (hostEl && !hostEl.contains(ent.container))` guard around the remount call.

- [ ] **Step 5: Commit**

```bash
cd /home/hren/gitlab/cam && git add web/js/shared/terminal-mount.js && \
  git commit -m "cam-desktop: stop re-parenting terminal container on show (fast-switch)

Change 1: showTerminalEntry now skips remountTerminalContainer when
the container is already a child of the shared #agent-terminal host
(the cached-tab case). Switching becomes a visibility toggle + one
deferred fit() — no DOM re-parent, no reflow. remountTerminalContainer
loses its orphan-sibling cleanup (querySelectorAll .agent-terminal-pane
.remove) because under the pre-mount model those siblings ARE the
other cached tabs; it stays as a fallback for first-mount / host-changed.

Refs: docs/desktop/agent-fast-switch-spec.md Change 1"
```

---

## Task 4: Serialize on eviction, restore on reopen (Change 2)

**Files:**
- Modify: `web/js/shared/terminal-mount.js` (module-top state, `evictTerminalCacheIfNeeded`, `disposeTerminalForAgent`, `createTerminalEntry`)

**Interfaces:**
- Consumes: `entry.serialize` (from Task 2) — the `SerializeAddon` instance loaded on the terminal.
- Produces: a module-level `_parkedSnapshots` `Map` keyed by agentId → snapshot string. `disposeTerminalForAgent` gains an `opts` parameter: `{ evict: true }` snapshots before dispose and parks it; the default (explicit close / app shutdown) does not. `createTerminalEntry`'s fresh-create branch (where `existing?.term` is false) checks `_parkedSnapshots` and writes the snapshot back before live output.

- [ ] **Step 1: Add the parked-snapshots Map at module top**

Find the line near the top of `web/js/shared/terminal-mount.js` that declares `const terminalSessions = new Map();` (it is referenced throughout; locate it with):

```bash
grep -n "const terminalSessions = new Map" /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Add a new line immediately after it:

```js
const terminalSessions = new Map();
// CAM-DESK-TERM-FS: scrollback snapshots parked on LRU eviction, keyed by
// agentId. Consumed on reopen to restore the buffer without a camc attach
// round-trip for the scrollback. One string per evicted agent, deleted on
// restore — bounded by TERMINAL_CACHE_LIMIT.
const _parkedSnapshots = new Map();
```

(If the exact `const terminalSessions = new Map();` line differs in spacing, match the surrounding style — the key is to place `_parkedSnapshots` at module top next to `terminalSessions`.)

- [ ] **Step 2: Make evictTerminalCacheIfNeeded pass { evict: true }**

The current `evictTerminalCacheIfNeeded` (lines 649–657) is:

```js
async function evictTerminalCacheIfNeeded(activeAgentId) {
  const live = [...terminalSessions.values()].filter(ent => ent.agentId !== activeAgentId);
  live.sort((a, b) => (a.lastUsed || 0) - (b.lastUsed || 0));
  while (terminalSessions.size > TERMINAL_CACHE_LIMIT && live.length) {
    const ent = live.shift();
    // eslint-disable-next-line no-await-in-loop
    await disposeTerminalForAgent(ent.agentId);
  }
}
```

Change the `disposeTerminalForAgent` call to pass `{ evict: true }`:

```js
async function evictTerminalCacheIfNeeded(activeAgentId) {
  const live = [...terminalSessions.values()].filter(ent => ent.agentId !== activeAgentId);
  live.sort((a, b) => (a.lastUsed || 0) - (b.lastUsed || 0));
  while (terminalSessions.size > TERMINAL_CACHE_LIMIT && live.length) {
    const ent = live.shift();
    // eslint-disable-next-line no-await-in-loop
    await disposeTerminalForAgent(ent.agentId, { evict: true });
  }
}
```

- [ ] **Step 3: Split disposeTerminalForAgent into evict-snapshot vs explicit-close paths**

The current `disposeTerminalForAgent` (lines 701–718) is:

```js
export async function disposeTerminalForAgent(agentId) {
  cancelAutoReconnect(agentId);
  const ent = terminalSessions.get(agentId);
  if (!ent) return;
  await closeTerminalSession(agentId, { stopKeepAlive: true });
  if (ent._resizeObs) {
    try { ent._resizeObs.disconnect(); } catch { /* noop */ }
  }
  if (ent._hostResizeObs) {
    try { ent._hostResizeObs.disconnect(); } catch { /* noop */ }
  }
  try { ent.term?.dispose(); } catch { /* noop */ }
  if (ent.container?.parentNode) {
    try { ent.container.parentNode.removeChild(ent.container); } catch { /* noop */ }
  }
  terminalSessions.delete(agentId);
  if (termAgentId === agentId) termAgentId = null;
}
```

Change it to (add `opts = {}` param; when `opts.evict`, snapshot BEFORE dispose and park the string; the snapshot uses `excludeAltBuffer` + `excludeModes` per the spec, with `scrollback: 5000` matching the terminal's configured scrollback):

```js
export async function disposeTerminalForAgent(agentId, opts = {}) {
  cancelAutoReconnect(agentId);
  const ent = terminalSessions.get(agentId);
  if (!ent) return;
  if (opts.evict && ent.serialize && ent.term) {
    try {
      const snapshot = ent.serialize.serialize(
        { excludeAltBuffer: true, excludeModes: true, scrollback: 5000 });
      if (snapshot) _parkedSnapshots.set(agentId, snapshot);
    } catch { /* snapshot failed — evict proceeds without restore */ }
  }
  await closeTerminalSession(agentId, { stopKeepAlive: true });
  if (ent._resizeObs) {
    try { ent._resizeObs.disconnect(); } catch { /* noop */ }
  }
  if (ent._hostResizeObs) {
    try { ent._hostResizeObs.disconnect(); } catch { /* noop */ }
  }
  try { ent.term?.dispose(); } catch { /* noop */ }
  if (ent.container?.parentNode) {
    try { ent.container.parentNode.removeChild(ent.container); } catch { /* noop */ }
  }
  terminalSessions.delete(agentId);
  if (termAgentId === agentId) termAgentId = null;
}
```

The eviction path snapshots to `_parkedSnapshots` BEFORE `closeTerminalSession` and `term.dispose()`. The explicit-close path (user closes the agent, app shutdown via `closeAllTerminalSessions`) calls this WITHOUT `opts.evict` (the default), so no snapshot is parked — explicit close discards scrollback as before. `closeAllTerminalSessions` (lines 720–726) iterates and calls `disposeTerminalForAgent(id)` with no opts, so shutdown behavior is unchanged. Do NOT modify `closeAllTerminalSessions`.

- [ ] **Step 4: Restore the snapshot on reopen in createTerminalEntry**

The current `createTerminalEntry` fresh-create branch loads the FitAddon and opens the terminal (lines 538–543, already modified in Task 2 to also load the serialize addon):

```js
  const FitCtor = window.FitAddon && window.FitAddon.FitAddon;
  if (FitCtor) {
    entry.fit = new FitCtor();
    entry.term.loadAddon(entry.fit);
  }
  entry.term.open(container);
  const SerializeCtor = window.SerializeAddon;
  if (SerializeCtor) {
    try {
      entry.serialize = new SerializeCtor();
      entry.term.loadAddon(entry.serialize);
    } catch { /* serialize unavailable — evict will be destructive, see spec Risks */ }
  }
```

Immediately AFTER the serialize-addon load block (and BEFORE the `entry.term.onData(...)` handler that follows), add the snapshot-restore:

```js
  const SerializeCtor = window.SerializeAddon;
  if (SerializeCtor) {
    try {
      entry.serialize = new SerializeCtor();
      entry.term.loadAddon(entry.serialize);
    } catch { /* serialize unavailable — evict will be destructive, see spec Risks */ }
  }
  if (_parkedSnapshots.has(agent.id)) {
    try {
      entry.term.write(_parkedSnapshots.get(agent.id));
    } catch { /* restore failed — fresh terminal proceeds blank */ }
    _parkedSnapshots.delete(agent.id);
  }
```

The `existing?.term` branch at the top of `createTerminalEntry` (lines 478–487) is NOT reached for an evicted agent (the entry was deleted from `terminalSessions` in `disposeTerminalForAgent`), so the fresh-create branch is the right place. The fresh SSH PTY channel is opened as usual via `term:open` (the caller, `ensureTerminal` → `openTerminalForSelected`, does this after `createTerminalEntry`); the remote tmux session is reattached via `camc attach`, so new output flows on top of the restored buffer.

- [ ] **Step 5: Verify syntax**

Run:

```bash
node --check /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Expected: no output (exit 0).

- [ ] **Step 6: Grep-verify the four touchpoints are present**

Run:

```bash
grep -n "_parkedSnapshots\|evict: true\|opts.evict\|excludeAltBuffer" /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Expected: at least 5 matches — one `const _parkedSnapshots = new Map()`, one `_parkedSnapshots.set` (or the `if (opts.evict` block), one `evict: true` in `evictTerminalCacheIfNeeded`, one `if (_parkedSnapshots.has` in `createTerminalEntry`, one `_parkedSnapshots.delete`, and one `excludeAltBuffer: true` in the serialize call.

- [ ] **Step 7: Commit**

```bash
cd /home/hren/gitlab/cam && git add web/js/shared/terminal-mount.js && \
  git commit -m "cam-desktop: serialize scrollback on eviction, restore on reopen

Change 2: disposeTerminalForAgent(agentId, { evict: true }) now
snapshots the xterm buffer via SerializeAddon.serialize({excludeAltBuffer,
excludeModes, scrollback:5000}) to a module-level _parkedSnapshots Map
BEFORE closing the channel and disposing the term. The explicit-close
path (no opts.evict) is unchanged — shutdown / user-close discards
scrollback as before. On reopen, createTerminalEntry writes the parked
snapshot back onto the fresh term before the new camc-attach channel
flows live output on top. Eviction is no longer destructive to visible
context.

Refs: docs/desktop/agent-fast-switch-spec.md Change 2"
```

---

## Task 5: Acceptance smoke (the 6 criteria from the spec)

**Files:**
- No file changes. This task is the behavioral gate.

**Interfaces:**
- Consumes: Tasks 1–4 (the shipped addon, the loaded serialize instance, the fast-path show, the evict/restore paths).

This task requires a running CAM-Desktop instance against a `camc`-reachable machine with at least 7 agents' worth of terminal sessions available (or the same agent re-opened 7 times into distinct slots). It is the "workable" gate per `msi-verify-before-commit`. Run this on a Windows box with display (or a dev instance with the renderer visible). If running a dev instance from prgn, the WSL build is NOT needed for this smoke — `web/desktop.html` + the vendor file are served directly. Build the MSI in Task 6 only after this smoke passes.

- [ ] **Step 1: Start the desktop dev instance (or install a fresh MSI built from this branch)**

If a dev server is available:

```bash
# From the cam repo root, however the desktop dev instance is normally started.
# Confirm the renderer is up and the agent list loads.
```

- [ ] **Step 2: Criterion 1 — 0-latency switch among 6 agents**

Open 6 agents in Terminal mode. Switch between them via the sidebar (the existing select model). Confirm:
- No DOM re-parent flash (the terminal pane does not visibly blink/reconstruct).
- No `camc attach` round-trip on switch (the SSH channel was already alive; you should see scrollback instantly, not a fresh attach banner).

If you have renderer DevTools open, in the Elements panel watch `#agent-terminal` — switching should NOT change which `.agent-terminal-pane` children are present (only their `hidden` / `style.visibility` toggles). It should NOT append/remove children.

Expected: switching is a visibility toggle; target <16ms (no perceptible frame drop on 60Hz).

- [ ] **Step 3: Criterion 2 — eviction keeps the SSH control connection**

Open a 7th agent. The LRU-evicted (oldest) agent's xterm is disposed and its container removed from the shared host. In the main process (Electron), the pooled SSH control connection for that host is NOT destroyed.

If a `poolStats()` debug hook is exposed (grep `ssh-transport.cjs` for a pool stats export), call it before and after the 7th-open and confirm the pool size for that host is unchanged. If no hook is exposed, confirm indirectly: reopen the evicted agent (Step 4) and observe that there is NO SSH re-auth delay / re-handshake (the control connection was reused) — only a fresh PTY channel + `camc attach`.

Expected: pool size unchanged for the host; no re-auth on reopen.

- [ ] **Step 4: Criterion 3 — reopen restores scrollback**

Reopen the evicted agent (the one that was disposed in Step 3). Confirm:
- The scrollback that was visible before eviction is restored as non-live text (the same output that was there before).
- New live output flows on top after `camc attach` reconnects (type into the terminal, see the response).
- The restored scrollback is NOT re-sent over the wire (no `camc attach` round-trip for the buffer — it came from the parked snapshot string).

Expected: scrollback restored from the parked `SerializeAddon` snapshot; fresh live output on top.

- [ ] **Step 5: Criterion 4 — hidden tabs do not resize**

Switch to one of the 6 (so 5 are hidden). Resize the browser window (or the desktop window). Confirm:
- Only the visible tab's PTY is resized (the visible terminal refits).
- The hidden tabs do NOT send `term:resize` (no spurious resize events for them).

If renderer DevTools network/IPC inspector is available, confirm only one `term:resize` IPC per resize tick, for the visible agent. The existing `terminalEntryCanAutoResize` guard (`terminal-mount.js:174`) already gates this — it returns false unless `viewActive !== false` AND `agentId === termAgentId` AND `!container.hidden` AND `visibility !== 'hidden'` AND the host `is-active`. This step verifies that guard holds under the pre-mount model. No new guard code was added; if a hidden tab resizes, that is a regression to fix.

Expected: only the visible tab resizes.

- [ ] **Step 6: Criterion 5 — TERMINAL_CACHE_LIMIT stays 6, shutdown still works**

Confirm `TERMINAL_CACHE_LIMIT` is still 6 (it was not changed):

```bash
grep -n "TERMINAL_CACHE_LIMIT = " /home/hren/gitlab/cam/web/js/shared/terminal-mount.js
```

Expected: `TERMINAL_CACHE_LIMIT = 6` (unchanged).

Close the app (app shutdown). Confirm `closeAllTerminalSessions` still disposes all entries (the app quits cleanly, no leaked xterms / channels). Reopen the app — confirm a clean start with no orphan terminals.

Expected: clean shutdown; `TERMINAL_CACHE_LIMIT = 6` intact.

- [ ] **Step 7: Record smoke results**

Write a one-paragraph smoke report to the session (or to `~/notes/fast-switch-smoke-<date>.md` if the user wants it persisted): which criteria passed, any failures with reproduction. This is the gate for Task 6 (MSI build + commit).

- [ ] **Step 8: No commit yet**

Do NOT commit here. The commit + push to `camui-desktop-v2` happens in Task 6 after the MSI is built and the fix is grep-verified in `app.asar` (per `msi-verify-before-commit`).

---

## Task 6: Build MSI, verify fix bundled, install smoke, commit + push

**Files:**
- No source changes. This task builds, verifies, and commits the work from Tasks 1–4 (the commits from those tasks are already on the branch but NOT pushed).

**Interfaces:**
- Consumes: Tasks 1–4 committed on branch `camui-desktop-v2`. Task 5 smoke passing.

This is the `msi-verify-before-commit` + `msi-develop-flow` gate. Per `cam-msi-build-state`, the MSI build runs from WSL via the detached-console trigger.

- [ ] **Step 1: Build the MSI on WSL**

From prgn, trigger the WSL build:

```bash
bash ~/cam-build-from-wsl.sh
```

Expected: produces `apps/cam-desktop/dist/CAM-Desktop-0.2.0-2-fast-switch-<date>.msi` (test-build naming per `msi-nutstore-release`: `-2` = test build, `fast-switch` = description, `<date>` = today). The build pulls in the new `addon-serialize.js` under `web/vendor/xterm/` and the modified `desktop.html` + `terminal-mount.js`.

- [ ] **Step 2: Verify the fix is bundled in app.asar**

Extract the MSI and grep the bundled `app.asar` for the new code (per `msi-develop-flow`):

```bash
# msiexec /a extract (run on the Windows side, or via msiexec under WSL):
# then grep the extracted app.asar for the fix signatures
grep -a "SerializeAddon" <extracted>/app.asar
grep -a "_parkedSnapshots" <extracted>/app.asar
grep -a "excludeAltBuffer" <extracted>/app.asar
```

Expected: all three greps return matches. `SerializeAddon` confirms the addon + its load block; `_parkedSnapshots` confirms Change 2's state; `excludeAltBuffer` confirms the serialize options. If any grep is empty, the build did not pick up the change — re-build before proceeding.

- [ ] **Step 3: Install smoke on a Windows box with display**

Install the MSI on a Windows box. Launch CAM-Desktop. Repeat the acceptance smoke from Task 5 (criteria 1–4 at minimum: open 6, switch with 0-latency, open 7th, reopen the evicted one with scrollback restored). This is the "workable" gate.

Expected: same results as Task 5 on the real install.

- [ ] **Step 4: Commit any build/doc artifacts and push**

The Tasks 1–4 commits are already on `camui-desktop-v2` locally. Push them now that the MSI is verified:

```bash
cd /home/hren/gitlab/cam && git log --oneline -5 camui-desktop-v2  # confirm the 4 fix commits are at the tip
git push origin camui-desktop-v2
```

Expected: the 4 commits (`ship @xterm/addon-serialize`, `load SerializeAddon`, `stop re-parenting`, `serialize on eviction`) push to `origin/camui-desktop-v2`.

- [ ] **Step 5: Release the MSI to the Nutstore channel (optional, hren's call)**

Per `msi-nutstore-release`, the release MSI lives in the Nutstore app folder. Copy the verified MSI to `C:\Users\hren\Nutstore\1\Nutstore\app\` with the naming convention `CAM-Desktop-0.2.0-2-fast-switch-<date>.msi` (test build, `-2`). This is hren's action — do not push to Nutstore without explicit confirmation.

- [ ] **Step 6: Update memory**

After the install smoke passes and the push lands, update the memory:
- Add a `project` memory `fast-switch-shipped` (or similar) recording: the spec + plan paths, the 4 commit hashes, the MSI name, the date, and that the install smoke passed. Link `[[msi-verify-before-commit]]` and `[[tabby-analysis]]`.
- Update `MEMORY.md` index with the one-line pointer.

---

## Self-Review (run after writing, before handoff)

**1. Spec coverage:**
- Change 1 (stop re-parenting + pre-mount shared host) → Task 3. ✓ (Pre-mount is already true in the code; Task 3 stops the re-parent + drops the orphan-cleanup. The spec's "pre-mount one container per cached agent into the shared host once" is the existing behavior — Task 1 of the spec is a no-op against current code, correctly not a task here.)
- Change 2 (serialize on eviction, restore on reopen) → Tasks 1, 2, 4. ✓
- Non-goal: no tab strip / no layout change → no task touches layout. ✓
- Non-goal: no SSH pool change → no task touches `ssh-transport.cjs`. ✓
- Acceptance criterion 1 (0-latency switch) → Task 5 Step 2. ✓
- Acceptance criterion 2 (eviction keeps pool) → Task 5 Step 3. ✓
- Acceptance criterion 3 (reopen restores scrollback) → Task 5 Step 4. ✓
- Acceptance criterion 4 (hidden tabs don't resize) → Task 5 Step 5. ✓
- Acceptance criterion 5 (cache limit stays 6, shutdown works) → Task 5 Step 6. ✓
- Acceptance criterion 6 (MSI bundles the addon under CSP) → Task 6 Step 2. ✓
- Risk: stale modes on restore → handled by `excludeModes: true` in the serialize call (Task 4 Step 3); the spec's "add `term.reset()` if needed" fallback is a smoke-time decision, noted in Task 5. ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step shows the exact code. Every command shows the exact command + expected output. ✓

**3. Type consistency:**
- `entry.serialize` — used in Task 2 (created), Task 4 (consumed in `disposeTerminalForAgent` as `ent.serialize`). Same name. ✓
- `_parkedSnapshots` — declared in Task 4 Step 1, written in Task 4 Step 3 (`_parkedSnapshots.set`), read + deleted in Task 4 Step 4 (`_parkedSnapshots.has` / `_parkedSnapshots.get` / `_parkedSnapshots.delete`). Same name throughout. ✓
- `disposeTerminalForAgent(agentId, opts = {})` — signature added in Task 4 Step 3; called with `{ evict: true }` in Task 4 Step 2; called with no opts in `closeAllTerminalSessions` (unchanged, explicitly called out). ✓
- `window.SerializeAddon` — referenced in Task 1 (produces), Task 2 Step 1 (consumed as `window.SerializeAddon`). ✓ (Note: NOT `window.SerializeAddon.SerializeAddon` — called out in Task 2 Step 1.)
- `@xterm/addon-serialize@0.14.0` — pinned in Task 1 Step 1, referenced in Global Constraints. ✓

No type/name drift.

---

## Execution Handoff

Plan complete and saved to `docs/desktop/agent-fast-switch-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

(Note: per the project role, this is also a candidate for delegation to a fix agent via `camc run -t claude --path ~/gitlab/cam/apps/cam-desktop --name camui-fast-switch --system-file docs/desktop/agent-fast-switch-plan.md` — the spec's Implementation handoff. That is a third option if you'd rather have it run as a tracked CAM agent than an in-session subagent.)
