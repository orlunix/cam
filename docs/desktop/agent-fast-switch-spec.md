# Agent Fast-Switch Spec — 0-latency switching for the latest 6 agents

**Status:** draft (2026-07-04)
**Scope:** CAM-Desktop renderer + Electron main, Terminal mode only.
**Related:** CAM-DESK-TERM-004/006/007 in `docs/desktop/requirements.md`; Tabby analysis in this session's transcript.

## Problem

Switching between agents in Terminal mode is slow and loses local context.
The user's goal: the latest 6 agents should stay alive like 6 tabs and
switch with effectively 0 latency, **without changing the layout** (no
tab strip required). The current sidebar-select model is fine; the
*switch mechanism* is what's broken.

## Root causes (grounded in current code)

The 6-session LRU cache (`TERMINAL_CACHE_LIMIT = 6`,
`web/js/shared/terminal-mount.js:10`) and the pooled SSH transport
(`apps/cam-desktop/electron/ssh-transport.cjs`, `_pool` keyed by
`host|user|port|auth|keyfile|secretDigest`, ref-counted via `inflight`,
idle-reaped at 600s) are **already correct**. The switch is slow for two
specific reasons:

1. **Re-parenting the DOM on every show.** `showTerminalEntry`
   (`terminal-mount.js:396`) calls `remountTerminalContainer`
   (`terminal-mount.js:379`) on every switch. That re-parents the
   container, forces a reflow, runs `scheduleTerminalFitDeferred`, and
   repaints. This is the visible flash/latency on switch. The xterm
   `Terminal` object itself is reused (the `existing?.term` check in
   `createTerminalEntry` line 478), so the cost is the re-parent + fit,
   not a new `Terminal`.

2. **Eviction destroys the xterm and closes the SSH channel.**
   `evictTerminalCacheIfNeeded` (line 649) → `disposeTerminalForAgent`
   (line 700) calls `term?.dispose()` (line 712) and
   `closeTerminalSession` (which sends `term:close`, closing the SSH
   PTY channel). Open a 7th agent → oldest agent's xterm is destroyed
   and its SSH channel closed. Switch back → `term:open` again → new
   SSH channel → `camc attach` round-trip → local scrollback gone
   (remote tmux scrollback still survives, but the local xterm buffer
   + scroll position is lost).

   Note: the pooled SSH **control connection** (the `ssh2.Client`)
   already survives eviction via the `_pool` ref-counting — only the
   per-agent PTY exec channel closes. So re-auth never happens on
   eviction; only the channel + xterm do.

## Design

Two changes, both in `web/js/shared/terminal-mount.js` unless noted.
**No layout change. No tab strip.** The sidebar-select UI stays.

### Change 1 — Stop re-parenting on show; pre-mount a shared host

**Goal:** switching between cached agents becomes a CSS
`visibility:hidden` toggle, not a DOM re-parent + reflow + fit.

**Anchor (already true in the code — no new host to create):**
- `web/js/desktop/agent-console.js:1289` grabs `terminalEl =
  document.getElementById('agent-terminal')` **once** at module init.
  `#agent-terminal` is a single persistent host element, always in
  the DOM, NOT torn down on `selectAgent`.
- `ensureTerminal(agent)` (line 1942) → `createTerminalEntry(agent)`
  appends a per-agent `<div class="agent-terminal-pane">` container
  into `terminalEl` as a child (`terminal-mount.js:503`
  `hostEl.appendChild(container)`).
- So the 6 per-agent containers are **already siblings inside the
  shared `#agent-terminal` host**. The fix does not create a new host;
  it stops re-parenting the already-mounted containers.

**Approach:**
- Pre-mount one container per cached agent into the shared
  `#agent-terminal` host once (on first `createTerminalEntry`).
  All 6 containers live in the DOM under this host for the lifetime
  of the Terminal mode view, with inactive ones `visibility:hidden`.
- In `showTerminalEntry` (line 396): when the container is already in
  the shared host, **drop the `remountTerminalContainer` call** (line
  399). Keep the existing `hideTerminalEntries()` (line 370) +
  `visibility`/`display` toggling (lines 411-414) — that's the fast
  path and it already exists; it's the re-parent that's slow.
- `remountTerminalContainer` (line 379) stays as a fallback for the
  case where the container is *not* yet in the shared host (first
  mount, or host element changed).
- Note: `remountTerminalContainer` currently also removes orphan
  `.agent-terminal-pane` siblings from the host (line 383-385). Under
  the pre-mount model those siblings are *intentional* (the other
  cached tabs), so that orphan-removal MUST be dropped or it will
  destroy the other tabs' containers on every show. This is the one
  destructive behavior in `remountTerminalContainer` that the fix
  must explicitly remove.
- After visibility toggle, run `scheduleTerminalFitDeferred` once on
  the now-visible entry (already done at line 414). Hidden entries
  must **not** fit or send resize. This is **already guaranteed** by
  `terminalEntryCanAutoResize` (line 174), which returns false unless
  `viewActive !== false` AND `agentId === termAgentId` AND
  `!container.hidden` AND `visibility !== 'hidden'` AND the host has
  `is-active`. The `onResize` handler (line 550) and the fit schedulers
  (lines 317, 356) all gate on it. Under the pre-mount+visibility-toggle
  model this needs **no new guard code** — the existing one already
  covers it. The fix agent should NOT add a redundant guard; just
  verify with a smoke test that a hidden tab doesn't send
  `term:resize` during a window resize.

**Net:** switch = toggle `visibility` on two elements + one deferred
`fit()` on the visible one. Effectively 0ms, no reflow from
re-parenting.

### Change 2 — Serialize on eviction, not destroy

**Goal:** when a 7th agent is opened and the LRU slot is evicted, the
evicted agent's visible scrollback is preserved as a serialized
string, and its SSH channel is closed but the pooled control
connection stays. Reopening the evicted agent restores scrollback
from the string without a `camc attach` round-trip for the buffer.

**Approach:**
- Add `@xterm/addon-serialize` to `apps/cam-desktop/package.json`
  runtime deps and ship the UMD under `web/vendor/xterm/` (same pattern
  as `xterm.js`/`addon-fit` per CAM-DESK-TERM-002). Load it as a plain
  `<script>` before the desktop module bundle so
  `window.SerializeAddon` is present at mount time.
- **Pool half is already done — no pool change needed.** Verified
  2026-07-04: `term:close` (`main.cjs:473` `termClose` →
  `_dropSession:383` → `ent.dispose()`) calls `ssh-transport.cjs`
  `dispose()` (line 671) which closes only the **channel**
  (`stream.end()`/`stream.destroy()`) then `release()` (line 663)
  decrements `entry.inflight` and on 0 starts the 600s idle timer
  (`_startIdleTimer`). It does **NOT** call `_dropEntry` (line 219,
  the only thing that destroys the `ssh2.Client`). So the pooled SSH
  control connection already survives channel close for 600s. The
  contract is documented at `main.cjs:241`: "Closing the channel does
  NOT kill the underlying agent — `camc attach` is a tmux attach."
  Change 2's "keep the control connection alive on eviction" is true
  today; only the xterm-side snapshot/restore is new work.
- In `createTerminalEntry` (line 478): load the SerializeAddon onto
  each new `term`: `entry.serialize = new SerializeAddon();
  entry.term.loadAddon(entry.serialize)`.
- Split `disposeTerminalForAgent` (line 700) into two paths:
  - **Eviction path** (called from `evictTerminalCacheIfNeeded` line
    652): snapshot before dispose.
    ```js
    let snapshot = null;
    try {
      snapshot = entry.serialize?.serialize(
        { excludeAltBuffer: true, excludeModes: true, scrollback: 5000 });
    } catch { snapshot = null; }
    // Store snapshot for restore on reopen (see below).
    _parkedSnapshots.set(ent.agentId, snapshot);
    await closeTerminalSession(agentId, { stopKeepAlive: true });
    try { ent.term?.dispose(); } catch {}
    // Remove the pre-mounted container from the shared host.
    if (ent.container?.parentNode) ent.container.parentNode.removeChild(ent.container);
    terminalSessions.delete(agentId);
    ```
  - **Explicit close path** (user closes the agent / app shutdown):
    same as today, no snapshot.
- On reopen of a previously-evicted agent (the `existing?.term` branch
  at line 478 is now false because the entry was deleted): in
  `createTerminalEntry`, after `entry.term.open(container)` and
  `loadAddon(serialize)`, if `_parkedSnapshots.has(agentId)`, call
  `entry.term.write(_parkedSnapshots.get(agentId))` to restore
  scrollback, then delete the parked snapshot. The fresh SSH PTY
  channel is opened as usual via `term:open`; the remote tmux session
  is reattached via `camc attach`, so *new* output flows normally on
  top of the restored buffer.
- Call `term.resetTerminalModes()` equivalent before writing the
  snapshot if xterm exposes it (Tabby does this in
  `connectableTerminalTab.component.ts:120` to clear stale
  mouse-tracking / bracketed-paste modes). If xterm's addon doesn't
  expose this, the `excludeModes: true` flag in `serialize()` is
  sufficient (modes are not serialized, so they don't leak back).

**Net:** eviction is no longer destructive to visible context. A
round-trip through eviction + reopen restores the agent's scrollback
as non-live text, with a fresh live channel on top. The 6-alive common
case (Change 1) never hits this path.

### Non-goals

- **No tab strip UI.** The user explicitly said the layout is not
  important; the 6-slot cache is the "6 tabs." This spec keeps the
  sidebar-select model.
- **No change to the SSH control-connection pool.** It already
  ref-counts and idle-reaps correctly (line 74 `IDLE_CLOSE_MS=600s`).
  Only the per-agent PTY channel lifecycle changes.
- **No change to `camc attach` semantics.** tmux owns the remote
  session; the desktop attaches. This spec only changes the local
  renderer's xterm + channel lifecycle on switch/evict.
- **No change to Plain/Rich output modes.** Terminal mode only.

## Acceptance criteria

1. With 6 or fewer open agents in Terminal mode, switching between
   them produces no DOM re-parent, no reflow flash, and no `camc
   attach` round-trip. Switch latency is dominated only by the
   `visibility` toggle + one deferred `fit()` (target: <16ms, i.e.
   no perceptible frame drop on a 60Hz display).
2. Open a 7th agent → the LRU-evicted agent's xterm is disposed and
   its container removed from the shared host, but the pooled SSH
   control connection for its host is NOT destroyed (verify via
   `poolStats()` — pool size unchanged for that host).
3. Reopen the evicted agent → scrollback is restored from the parked
   `SerializeAddon` snapshot (visible as the same text that was
   there before eviction), and new live output flows on top after
   `camc attach` reconnects.
4. Hidden tabs (any of the 6 not currently visible) do not send
   `term:resize` and do not run `fit()`. Verify by switching to a
   hidden tab, resizing the window, and confirming only the visible
   tab's PTY is resized (existing `terminalEntryCanAutoResize` guard
   must hold under pre-mount).
5. `TERMINAL_CACHE_LIMIT` stays 6. No regression in
   `closeAllTerminalSessions` (app shutdown still disposes all
   entries and the pool's `closeAll`).
6. MSI build succeeds with the new `@xterm/addon-serialize`
  dependency bundled under `web/vendor/xterm/` (per TERM-002 CSP
  pattern) and the fix is grep-verifiable in `app.asar`.

## Risks / open questions

- **Memory:** 6 xterms × 5000-line scrollback in JS heap is bounded
  and acceptable today. The serialize-on-evict path (Change 2) only
  stores a string per evicted agent until reopen — also bounded.
  If the user later wants >6 alive, this design extends by raising
  the cap; serialize-on-evict then bounds the tail.
- **SerializeAddon packaging:** CONFIRMED 2026-07-04 —
  `@xterm/addon-serialize@0.14.0` ships `lib/addon-serialize.js` as a
  **UMD** (header: `module.exports=e()` / AMD `define([],e)` / global
  fallback `t.SerializeAddon=e()` with `t=globalThis`). The package
  advertises only `main` (no `browser`/`unpkg` field), but the file is
  UMD regardless — same shape as the already-shipped
  `web/vendor/xterm/addon-fit.js`. So loading it as a plain
  `<script>` in `desktop.html` will expose `window.SerializeAddon`
  under the existing `script-src 'self'` CSP. No fallback to
  Change-1-only needed; Change 2 is viable as specified.
- **SerializeAddon xterm-version compat:** CONFIRMED 2026-07-04 —
  `@xterm/addon-serialize@0.14.0` is the **latest stable** and was
  published from the **same monorepo build** as `@xterm/xterm@6.0.0`
  (identical `created` timestamp 2023-11-01T18:03Z, same commit
  `f447274f`). xterm addons are version-coupled to the core (the addon
  reads `_terminal.buffer`/`_core._inputHandler._curAttrData`
  internals), so the version MUST match the core major. `0.14.0`
  pairs exactly with the installed `@xterm/xterm@^6.0.0` (same as the
  already-shipped `@xterm/addon-fit@0.11.0` pairs with 6.x). The
  `0.15.0-beta.*` stream is the next in-development version — do NOT
  use a beta. Pin `@xterm/addon-serialize@0.14.0` in
  `apps/cam-desktop/package.json`.
- **Stale modes on restore:** if `serialize({excludeModes:true})`
  proves insufficient (stale mouse-tracking leaks as visible text
  after restore), add an explicit `term.reset()` before
  `write(snapshot)`. To be determined during implementation
  smoke-test.

## Verification plan

1. Implement Change 1 + Change 2 in
   `web/js/shared/terminal-mount.js` + `apps/cam-desktop/package.json`
   + `web/vendor/xterm/`.
2. `node --check` the modified files; `npm run lint:electron` if
   node_modules present.
3. Build MSI on WSL: `bash ~/cam-build-from-wsl.sh` →
   `apps/cam-desktop/dist/CAM-Desktop-0.2.0-2-fast-switch-<date>.msi`
   (test-build naming per `msi-nutstore-release`).
4. Verify fix bundled: `msiexec /a` extract + `grep -a` for
   `SerializeAddon` / the new pre-mount logic in `app.asar`.
5. Install smoke on a Windows box with display: open 6 agents,
   switch between them (confirm 0-latency, no flash), open a 7th
   (confirm eviction), reopen the evicted one (confirm scrollback
   restored). This is the "workable" gate per `msi-verify-before-commit`.
6. Only after install smoke passes: commit (desktop fix separate from
   any camc change) + push to `camui-desktop-v2`.

## Implementation handoff

This is a bounded, single-file-mostly change. Delegate to a fix agent
via `camc run -t claude --path ~/gitlab/cam/apps/cam-desktop --name
camui-fast-switch --system-file docs/desktop/agent-fast-switch-spec.md`.
The agent implements Changes 1+2, runs `node --check`, and reports
done. The MSI verify +
install smoke + commit is the human/manager gate (per
`msi-develop-flow`).
