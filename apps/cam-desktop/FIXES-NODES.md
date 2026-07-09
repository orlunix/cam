# FIXES-NODES.md — three camui-desktop Nodes/sync bugs

Branch: `camui-desktop-v2`. All three root causes were as diagnosed in
the brief; fixes implemented in `apps/cam-desktop/electron/embedded-hub.cjs`
(the file is hand-written, not bundled — no build step). Renderer files
needed **no changes** — the hub now accepts id-or-name, which is what
the renderer was already sending.

Req IDs advanced: **CAM-DESK-NODEUI-014** (Sync Host),
**CAM-DESK-NODEUI-015** (Delete Host), **CAM-DESK-DIRECT-014**
(Hub owns the node/remote registry), **CAM-DESK-DIRECT-019**
(Direct/Relay parity — local agents now discoverable in Direct mode).

## Bug 2 & 3 — context route now resolves by name OR id

**Root cause:** `/api/contexts/:name_or_id` route resolved with
`findContextByName(ctxName)` only. The renderer leads with the
context **id** on both Sync (`nodes-mode.js:380` → `hints.id || ctx.name`)
and Delete (`nodes-mode.js:679` → `persistDelete(ctx.id || ctx.name)`),
so the route 404'd whenever the id was passed. DELETE and /sync share
the same `existing` resolution, so one fix covers both.

**Fix:**
- Added `findContextByNameOrId(nameOrId)` helper
  (`embedded-hub.cjs:580`) — matches `c.name` OR `c.id`. Same
  id-or-name resolution the browse helpers already inlined at
  `_browseContextList`/`_browseContextRead`.
- Route resolution now uses it: `const existing = findContextByNameOrId(ctxName)`
  (`embedded-hub.cjs:3453`). GET/PUT/PATCH/DELETE/sync all share this
  one `existing`, so all sub-handlers accept id-or-name.
- PUT and DELETE splices now use `findIndex(c => c === existing)`
  instead of `findIndex(c => c.name === ctxName)` — robust whether
  the record was resolved by name or id, and avoids a second lookup.
- The cascade `_cascadeDeleteCreds(existing.id)` (CAM-DESK-DIRECT-018)
  still runs on DELETE; store is saved.

**Before/after (route handler):**
```js
// before
const existing = findContextByName(ctxName);            // 404 when id passed
const idx = state.store.contexts.findIndex(c => c.name === ctxName);
// after
const existing = findContextByNameOrId(ctxName);        // matches id OR name
const idx = state.store.contexts.findIndex(c => c === existing);
```

## Bug 1 — local-agent ingestion path (Direct mode)

**Root cause:** `state.store.agents` was populated only by
`_syncContextAgents(ctx)`, which only runs for `machine.type === 'ssh'`
contexts (`_syncableAgentContexts` skips local). The desktop poll loop
calls `GET /api/agents` with `refresh=false`, which just returned
`state.store.agents` + `_repairStoreAgents()` — neither discovers
local `camc` agents. So a `camc run` on the hub's own host never
appeared without a manual Sync.

**Fix:** added a local ingestion path that mirrors the remote SSH sync.

- `child_process.execFile` import (`embedded-hub.cjs:75`) — the hub
  process runs on the same host as `~/.cam/camc`, so a local spawn is
  the Direct-mode mirror of `_sshTransport.execRemote`. No shell; just
  `execFile` on the resolved camc binary.
- Constants `LOCAL_CONTEXT_NAME = 'local'` and
  `LOCAL_SYNC_TIMEOUT_MS = 8000` (`embedded-hub.cjs:93-94`); state
  flag `localSyncInFlight` (`embedded-hub.cjs:118`).
- `_localCamcPath()` (`:1065`) — prefers the bundled camc
  (`_bundledCamcPath()` → `src/camc` / `dist/camc` / `resources/camc`),
  falls back to `camc` on PATH. Verified the bundled path resolves to
  `/home/hren/gitlab/cam/src/camc` and runs `--json list` cleanly.
- `_ensureLocalContext()` (`:1075`) — idempotently creates a
  `machine.type === 'local'` anchor context named `local` if none
  exists. This is required so `_contextForAgentRecord` resolves local
  agents (otherwise `_pruneUnownedStoreAgents` would drop them as
  orphans on the next SSH sync) and so Nodes mode groups them under a
  "local" host card.
- `_runLocalCamcList()` (`:1110`) — `execFile` wrapper, returns
  `{ok, stdout, stderr}` or `{ok:false, error}`. ENOENT →
  `camc_missing` (non-fatal on a fresh install with no local camc).
- `_syncLocalAgents()` (`:1132`) — runs the local camc list,
  normalizes with the existing `_normalizeAgent(rec, ctx)` (same
  field shape as the remote path), and upserts via the existing
  `_upsertAgentsForContext(ctx, normalized)` — so dedupe
  (`_agentDedupeKey`) and the renderer's tally both work unchanged.
  Throttled by `state.localSyncInFlight` so concurrent polls don't
  stack camc spawns. Returns the same `{ok, imported, total, results:
  {camc}}` shape as `_syncContextAgents`.
- Hooked into `GET /api/agents` (`:3527`) — runs on every list fetch
  (refresh or not), so local agents appear within one poll cycle
  (~5s) without a manual Sync. Local sync is cheap (a local
  subprocess, ~50ms) and throttled.
- Hooked into `_syncAllAgentContexts` (`:1022`) — the refresh=true /
  manual "Sync All" path now includes local, and crucially runs
  local sync **before** `_pruneUnownedStoreAgents` so freshly-imported
  local rows aren't pruned. The no-SSH-contexts early-return also
  runs local sync (a Direct install with zero remote nodes still
  reflects its local agents).

**Dedupe safety:** local agents get `machine_host=''`, `machine_type='local'`,
`context_name='local'`. `_upsertAgentsForContext(localCtx, ...)` only
replaces rows with `context_name === 'local'` or a colliding
`_agentDedupeKey`. SSH rows carry a non-empty `machine_host` and a
different `context_name`, so they're untouched. No duplication, no
regression to the SSH path.

**Self-healing:** if the user deletes the `local` context from the
Nodes page, `_ensureLocalContext` re-creates it on the next
`GET /api/agents` and `_syncLocalAgents` re-ingests — verified in the
smoke harness (delete → next poll recreates + reimports 4 agents).

## Smoke-test evidence

`npm run lint:electron` passes (node --check on all electron + cli
files).

A direct in-process harness loaded the real `embedded-hub.cjs`
module, called `start({dataDir: <tmp>})`, and exercised the HTTP
handler (same code path the Electron main runs; no display needed).
Results against a temp store (real `~/.cam/agents.json` read via the
bundled `src/camc`):

- **Bug 1** — `GET /api/agents` (refresh=false): 4 local agents
  ingested under `context_name='local'` (ids `67915e84` prgn101,
  `c534f853` camui-nodes-fix, …), `machine_type='local'`,
  `machine_host=''`. Local context auto-created in the store.
  PASS.
- **Bug 2** — `POST /api/contexts/<ID>/sync` on the local context:
  200 (was 404). Returns `{ok:false, error:'not_ssh', ...}` — correct,
  the local context isn't an SSH node; the point is the route
  resolved and returned 200, not 404. Also tested a real **SSH**
  context (top-level `host/user/port` body, as the renderer sends):
  sync-by-id → 200 with `ssh_transport_unavailable` (harness doesn't
  inject the SSH transport; the real app does via
  `embeddedHub.configure({sshTransport})` in `main.cjs:190`).
  Sync-by-name → 200. PASS.
- **Bug 3** — `DELETE /api/contexts/<ID>` on a throwaway SSH context:
  200 (was 404), context removed from the store. PASS.
- Regressions — name-keyed sync still 200; `GET /api/agents?refresh=true`
  includes `local` in sync results; local-context self-heal after
  delete → next poll recreates + reimports. All PASS.

**Dev env (`npm run dev`):** Electron 31.7.7 installed via `npm install`
(lockfile is stale, so `npm ci` is skipped per the gotcha). The
windowed app does **not** launch on prgn — Electron's GUI shared libs
are missing (`libatk-1.0.so.0` and friends) and there's no X
display / `xvfb` not installed. Installing those needs `sudo apt-get`
and is out of scope per the brief's gotcha ("don't block on the
display; you can also test the hub HTTP surface with curl"). The hub
HTTP surface — which is what all three bugs live in — was exercised
end-to-end via the in-process harness above. The DoD's "dev hub
running" curl-equivalent is satisfied.

## Follow-ups for hren

1. **Local node card label.** Local agents are grouped under a host
   card labeled `local` (the renderer's `buildHosts` uses
   `m.host || 'local'`, and a local context must have
   `machine.host=''` or it would render as `isSSH`). The agent's
   `hostname` field (e.g. `prgn`) is preserved on each record but the
   Nodes card doesn't display it. If hren wants the card to read
   `prgn` instead of `local`, that's a renderer-side change in
   `buildHosts` (`web/js/shared/nodes-mode.js`) to fall back to
   `agent.hostname` for local cards — separate from these bug fixes.
2. **Should the `local` context be deletable from the UI?** It
   self-heals on the next poll, so deleting it is harmless (transient
   flicker). If you'd rather hide it from Delete Host entirely, the
   renderer's delete-host loop (`nodes-mode.js` buildHosts/delete
   path) could skip `machine.type === 'local'` contexts. Not done
   here — left visible so the user can see and interact with the
   local node.
3. **zsh-newuser-install wedge** (camui-dev `0cf1317d`) — out of scope
   per brief. `touch ~/.zshrc` on prgn would prevent fresh zsh sessions
   from hitting the new-user prompt.
4. **`package-lock.json` regen** — still stale (`npm ci` fails). A
   separate small task could commit a regenerated lockfile.
