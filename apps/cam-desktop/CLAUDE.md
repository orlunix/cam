
<!-- camc:c534f853 begin -->
# Task: Fix three camui-desktop Nodes/sync bugs

You are a CAM Desktop (camui) dev agent, launched by hren's CAMC Agent
Manager. Work on the **`camui-desktop-v2`** branch on prgn (this Linux
box). Repo root: `~/gitlab/cam`. Desktop app: `apps/cam-desktop`.
Renderer (shared with web): `web/js/`. Hub: `apps/cam-desktop/electron/`.

## Three bugs to fix (root causes already diagnosed — verify, don't re-derive)

### Bug 1: Agent list does not reflect local camc agents in Direct mode
**Symptom:** A `camc run` agent launched on prgn itself (the same box
the Direct-mode embedded hub runs on) does not appear in the camui
agent list until an explicit Sync. hren reports the `camui-dev` agent
(id `0cf1317d`, running on prgn) never shows under node prgn.

**Root cause (confirmed):** `state.store.agents` is populated ONLY by
`_syncContextAgents(ctx)`, and `_syncableAgentContexts()`
(`apps/cam-desktop/electron/embedded-hub.cjs:950`) SKIPS every context
whose `machine.type !== 'ssh'`:

```js
function _syncableAgentContexts() {
  const ctxs = (state.store && Array.isArray(state.store.contexts)) ? state.store.contexts : [];
  const byEndpoint = new Map();
  for (const ctx of ctxs) {
    const m = (ctx && ctx.machine) || {};
    if ((m.type || 'local') !== 'ssh') continue;     // <-- local contexts never synced
    ...
  }
}
```

The desktop poll loop (`web/js/desktop/app.js:510`) calls
`loadAgents()` → `api.listAgents({limit:100})` with `refresh: false`
by default, so `GET /api/agents` just returns `state.store.agents` +
`_repairStoreAgents()` — and `_repairStoreAgents` (line 206) only
repairs EXISTING rows; it does NOT discover local camc agents. So a
local agent never enters the store unless the user manually triggers a
Sync Host that happens to include a self-SSH context.

**Fix direction:** add a local-agent ingestion path. When the hub is in
Direct mode on a host, it should periodically (or on `GET /api/agents`
when `refresh` is set, or on the poll cadence) run the **local** `camc
--json list` (the hub process is on prgn; `~/.cam/camc` is on PATH —
verify with `which camc` from the hub's env) and merge those agents
into `state.store.agents` tagged with the local context. Do NOT
duplicate SSH-synced rows (use `_agentDedupeKey`). Match the existing
`_syncContextAgents` result shape so the renderer's tally works.
Coordinate with hren if a "local context" record needs to exist first
(the compatibility API creates context records on Add Host — check
whether a local-type context is present in `state.store.contexts` on a
fresh install; if not, that's part of the fix).

### Bug 2: Nodes "Sync Host" shows "not found, HTTP 404"
**Symptom:** On the Nodes page, clicking Sync Host on a host card
fails with "not found, HTTP 404".

**Root cause (confirmed):** The renderer calls
`api.syncContext(hints.id || ctx.name, hints)` in
`web/js/shared/nodes-mode.js:380`, and `contextSyncHints()` (line 346)
returns `{ id: ctx?.id || '', ... }` — so it passes the context **id**
first. The URL becomes `/api/contexts/<ID>/sync`. The hub route
(`embedded-hub.cjs:3263-3267`) resolves the context with:

```js
const ctxName = decodeURIComponent(ctxMatch[1]);
const existing = findContextByName(ctxName);
```

and `findContextByName` (line 549-552) matches ONLY by `c.name`:

```js
function findContextByName(name) {
  if (!state.store) return null;
  return state.store.contexts.find(c => c.name === name) || null;
}
```

No context's `name` equals its `id`, so `findContextByName(<ID>)`
returns null → `send404` at line 3299. That's the 404.

**Fix:** make the `/api/contexts/:name_or_id` route accept EITHER name
or id. Add a `findContextByNameOrId(nameOrId)` helper (match `c.name`
OR `c.id`), and use it at line 3267 (and wherever else the route needs
`existing`). The GET/PUT/DELETE/sync sub-handlers all share `existing`
from that one resolution, so a single helper fixes GET, PUT, DELETE,
and /sync. Verify against `CAM-DESK-DIRECT-014` (Hub owns the
node/remote registry) — the route is the compatibility surface the
renderer relies on.

### Bug 3: Nodes page cannot delete a host ("Delete Host" fails)
**Symptom:** Clicking Delete Host on a node card does not remove the
node; hren reports it "never be able to delete a node" and tried it to
work around bug 2.

**Root cause:** Same as bug 2. `api.deleteContext(name)` in
`web/js/views/machines.js:250` and `web/js/shared/nodes-mode.js:116`
(`persistDelete`) hit `/api/contexts/<name_or_id>` DELETE, which
resolves `existing = findContextByName(ctxName)` and 404s when the
caller passes an id or a stale/edited name. The DELETE handler
(`embedded-hub.cjs:3286-3297`) returns `send404` before splicing.
Fixing the `findContextByNameOrId` resolution (bug 2's fix) fixes the
DELETE path too. After the fix, verify the cascade still runs
(`_cascadeDeleteCreds(existing.id)` at line 3294) and the store is
saved.

## Files you will touch (likely)
- `apps/cam-desktop/electron/embedded-hub.cjs` — the big one. Add
  `findContextByNameOrId`, fix the context route resolution, add
  local-agent ingestion. **DO NOT hand-edit if a build step bundles
  this from `src/cam/`** — check first. (The file is 161 KB; it
  appears to be hand-written, not bundled, but verify by looking for a
  build script in `apps/cam-desktop/package.json` and any
  `build:embedded` / `build:hub` npm script. If it's bundled, fix the
  source and rebuild; if hand-written, edit in place.)
- `web/js/shared/nodes-mode.js` — verify the sync/delete calls use the
  right key after the hub fix (may not need changes if the hub accepts
  id-or-name; but if `hints.id` is unreliable, consider passing
  `ctx.name` only and drop the id preference).
- `web/js/views/machines.js` — same verification.
- `apps/cam-desktop/CLAUDE.md` exists in the workdir (untracked, created
  by camc when this agent type was first launched) — IGNORE it, don't
  commit it. hren will delete it after the task.

## Definition of done
1. All three symptoms repro'd-then-fixed. For each: a concrete
   verification step (see below) passes.
2. `npm run dev` still launches the desktop window on prgn (use
   `xvfb-run` if there's no display — see ramp-up note below).
3. **Smoke test:** with the dev hub running, (a) launch a local camc
   agent on prgn and confirm it appears in the camui agent list within
   one poll cycle WITHOUT a manual Sync (bug 1); (b) on the Nodes page,
   Sync Host on a host card succeeds with HTTP 200 (no 404) (bug 2);
   (c) Delete Host removes the node card and the context(s) from the
   hub store (bug 3). Capture the hub log / network tab evidence.
4. No regression: existing SSH-context Sync Host still works (don't
   break the SSH path while adding the local path). Run any existing
   desktop tests if present (`apps/cam-desktop` test script in
   package.json).
5. Commit on `camui-desktop-v2` with a clear message referencing the
   req IDs (CAM-DESK-NODEUI-014, -015; CAM-DESK-DIRECT-014, -019).

## Ramp-up (quick — hren's prior agent already mapped this)
- Canonical overview: `~/gitlab/cam/CLAUDE.md`.
- Desktop arch in one screen: `~/gitlab/cam/docs/desktop/README.md`.
- Req registry (cite IDs): `~/gitlab/cam/docs/desktop/requirements.md`.
  Relevant: CAM-DESK-NODEUI-014 (Sync Host), -015 (Delete Host),
  CAM-DESK-DIRECT-014 (Hub owns registry), -019 (Direct/Relay parity).
- Hub source: `apps/cam-desktop/electron/embedded-hub.cjs` (161 KB,
  the embedded Hub — core of Direct mode). Renderer:
  `web/js/desktop/app.js`, `web/js/shared/nodes-mode.js`,
  `web/js/views/machines.js`, `web/js/api.js` (`CamApi` client).
- Dev env: `cd ~/gitlab/cam/apps/cam-desktop && npm install (NOT npm
  ci — lockfile stale) && npm run dev`. prgn is a server with no
  display — use `xvfb-run -a npm run dev` (install xvfb if missing:
  `sudo apt-get install -y xvfb` — ask hren first if sudo is needed).
  Don't block on the display; you can also test the hub HTTP surface
  with `curl` against the dev hub port without a window.

## Gotchas
- **`npm ci` fails** (lockfile stale, `@emnapi/wasi-threads@1.2.1`
  missing). Use `npm install`. Don't run `npm audit fix`.
- **Don't switch branches.** `camui-desktop-v2` is current.
- **`src/camc` and `dist/skillm` are git-tracked** (extraResources) —
  don't "clean" them.
- **`apps/cam-desktop/CLAUDE.md` is untracked** — don't commit it.
- **Direct vs Relay:** Direct = app-embedded Hub (default). Relay =
  external Hub through a relay endpoint. Both expose the same
  `/api/*` surface. Don't branch renderer code on transport.
- The camui-dev agent (`0cf1317d`) is WEDGED (zsh-newuser-install
  intercepted its session and the env line got garbled to
  `nv CLAUDE_CODE...` → `command not found: nv`). It's a separate
  issue — don't fix it as part of this task, but be aware prgn has no
  `~/.zshrc`, so any agent launch from a fresh zsh may hit the
  zsh-newuser-install prompt. (hren may want a `touch ~/.zshrc` fix
  separately.)

## How to report back
Write a short summary to `~/gitlab/cam/apps/cam-desktop/FIXES-NODES.md`
(or a memory note) with: (1) what changed per bug (file + line +
before/after), (2) smoke-test evidence, (3) any follow-up hren should
decide (e.g. whether a local-context record should auto-create on
first launch, whether to fix the zsh-newuser-install wedge). Keep your
final message to hren concise.

Commit when done. Don't push.
<!-- camc:c534f853 end -->
