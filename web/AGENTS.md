# Web UI — Agent Notes

This directory contains three separate web frontends. Know which one
you are working on before editing:

- `index.html` + `js/views/*` — **V1 legacy PWA** (frozen, has its own
  service worker `sw.js`; `?v=` versions must stay in sync across
  `index.html`, `js/app.js`, `sw.js`).
- `mobile.html` + `js/mobile/*` — **CamUI Mobile V2 (active)**.
- `desktop.html` + `js/desktop/*` — Electron desktop renderer.
- Shared: `js/api.js` (`CamApi`), `js/state.js`, `js/shared/*` — used by
  both mobile and desktop; change carefully.

## Mission: Develop the Mobile UI

The current development goal for this area is the **mobile UI** of CAM
(the coding-agent manager). CAM manages AI coding agents (Claude Code,
Codex, Cursor, arbitrary CLIs) in tmux sessions; the mobile UI is the
phone client for monitoring and controlling those agents.

### What the mobile UI is

- Developed on branch **`camui-desktop-v2`** (NOT `master` — master is
  frozen at 2.2.0). Version source of truth: `android/VERSION`
  (currently 2.4.51).
- `mobile.html` + `js/mobile/*` — vanilla JS ES modules, no framework,
  no build step, hash router in `js/mobile/app.js`.
- Two connection modes:
  - **Relay**: pure client of the Node embedded Hub started by
    `camui start` (`apps/cam-desktop/electron/embedded-hub.cjs`),
    reached through `relay/relay.py`. All API calls go through `CamApi`
    in `js/api.js` (REST-over-WebSocket; phone stores Relay URL + token
    in localStorage).
  - **Direct**: the phone hosts an embedded hub itself (JS side
    `js/mobile/direct-*.js`, `settings-direct.js`; native side
    `MobileEmbeddedHub.java` in `android/`).
- Packaged as an Android APK by `android/build.sh` (no Gradle), loading
  the web app via `CamAssetLoader.java` virtual HTTPS
  (`https://appassets.androidplatform.net/...`, NOT `file://`).
- Resume/viewport drift is handled by the early inline script in
  `mobile.html` (`__camResetLayout` / `__camScheduleLayoutResets`,
  triggered on visibilitychange/pageshow). Note: `docs/mobile/README.md`
  still describes the older `__camReloadOnResume()` full-reload
  approach, which was never implemented — trust the code.

### Read first

- **`docs/mobile/stability-roadmap.md`** — the living document:
  current status, remaining issues, roadmap batches, verification
  protocol, and a trust map for the older (stale) docs below.

- `docs/mobile/relay-first-plan.md` — the locked V2 Relay plan (routes,
  API mapping, polling patterns, localStorage keys).
- `docs/mobile/direct-mode.md` — the Direct-mode plan (this branch).
- `docs/mobile/README.md` — V1 vs V2 rules, viewport model (`100%` +
  flex, **no dvh/vh**). Partially stale, see above.
- `docs/desktop/requirements.md` — canonical requirement IDs.

### Hard boundaries

- Mobile work touches only: `web/mobile.html`, `web/js/mobile/*`,
  shared `web/js/api.js` / `web/js/state.js` (carefully — shared with
  desktop), `web/css/mobile.css`, `docs/mobile/*`, and `android/`
  packaging.
- Never modify: `src/cam/**` (Python server), `embedded-hub.cjs`, camc
  (`src/camc_pkg/*` — owned by another agent, see root AGENTS.md), the
  V1 `web/index.html` tree, or the desktop renderer (`web/js/desktop/*`
  — reference only, do not import from mobile code).
- The native Kotlin UI line is archived on branch
  `camui-mobile-native` (2.1.x, frozen) — do not resurrect it.

### Build / verify

- Web PWA has no build step; serve `web/` statically or via
  `python3 relay/relay.py --port 8001 --token <t> --web-root web/`.
- APK: `cd android && ./build.sh` → `build/cam.apk`; bump
  `android/VERSION` (patch) per iteration; install with
  `adb install -r android/build/cam.apk`.
- `android/build.sh` stamps `?v=<version>` cache-bust query strings —
  do not hand-edit them out.

### Known open stability issues (verified on this branch, 2.4.51)

**P0 — module-instance splits (fix first, one root cause)**

- `web/mobile.html` loads `js/mobile/app.js?v=<version>` (plus
  `modulepreload`), but every submodule imports it back bare
  (`from '../app.js'`) → ES modules key on the full URL, so `app.js`
  is evaluated twice and `init()` (`js/mobile/app.js:385`, no reentry
  guard; `window.__camMobileV2` is written but never checked) runs
  twice: duplicated 10s reconnect/refresh loop, duplicated `hashchange`
  → every view renders twice (two cleanup chains fight over
  `#content`), doubled `api.onEvent`/`state.subscribe`, doubled
  concurrent `api.connect()` handshakes, doubled agent output polling
  with independent `_outputHash` (hash dedup defeated → full refetch).
- Same split affects `mobile-bridge.js` (`?v=2.4.51` vs `?v=2.3.39` —
  stale stamp), `hub-capabilities.js` (stamped vs bare), and other
  `js/shared/*` modules. Consequences: `mobile-bridge` double install
  wipes the instance-private pending-callback map → in-flight Direct
  hub requests never settle (no timeout) and terminal event delivery
  freezes; `hub-capabilities` reads hit the empty bare instance →
  capability gating silently defaults to "allow" in Relay mode.
- Fix direction: consistent import specifiers everywhere (all bare, or
  all stamped — check `android/build.sh` sed coverage), plus a reentry
  guard in `init()`. One consistent scheme fixes this whole cluster.

**P1 — connection / polling**

- `js/api.js`: `_scheduleReconnect()` retries every 1s forever (no
  backoff, no cap, no visibility check); `ws.onclose` doesn't verify
  the closed socket is the current one → a stale close kills the new
  socket's ping timer, rejects its pending requests, and orphans the
  healthy socket (ghost clients accumulate on the relay); one 30s
  request timeout closes the whole WS, rejecting all in-flight
  requests; after reconnect no data is refetched (event-stream only).
  Direct-mode event stream already has backoff (10s→60s) — Relay path
  should match.
- App-level 10s loop (`js/mobile/app.js` `startReconnectLoop`) has no
  `document.hidden` check and no in-flight guard — polls while the
  phone is backgrounded/screen-off; slow-network ticks overlap and
  `connect()` calls clobber each other.
- Agent-detail output polling doesn't stop when the agent reaches a
  terminal state (state subscription never clears `outputTimer`), and
  the "Cancel" affordance is dead code — `_inflightAbort` is never
  assigned an `AbortController`.
- Relay-mode terminal: `getTermBridge()` creates a new bridge per call
  and old sessions are never `dropSession`-ed → each visit leaks a
  permanent 2s poll timer.

**P2 — Direct mode (phone-hosted hub)**

- `MobileEmbeddedHub.apiRequest` is globally `synchronized` while
  doing SSH I/O: one unreachable host (connect timeout up to ~3 min
  with retries) stalls ALL Direct UI requests; JS side has no request
  timeout.
- Direct mode has neither agents polling nor an event stream
  (`/api/ws` unimplemented in the hub, disabled after 3 failures) →
  dashboard never auto-refreshes.
- `MainActivity.onDestroy` doesn't stop the embedded hub
  (ServerSocket/thread pool leak); mode switches don't close native
  terminal SSH sessions (they linger until the 10-min background
  timeout); `term_input` spawns a raw `Thread` per keystroke with
  unsynchronized stream writes.

**P3 — minor / security surface**

- localStorage `cam_cache:*` is never invalidated on mutations;
  `_cached` flag is only consumed by the desktop UI.
- Listener leaks: agent-detail adds a `document` click listener per
  render (never removed); `shared/nodes-mode.js` leaks
  `visualViewport` resize/scroll listeners per visit.
- innerHTML used with unescaped hub-supplied values in a few spots
  (`dashboard.js` tool/machine/`agent.id`/`status`,
  `agent-detail.js` status).
- `setWebContentsDebuggingEnabled(true)` unconditionally on (release
  builds too); `MIXED_CONTENT_ALWAYS_ALLOW`; SSH
  `StrictHostKeyChecking=no`.
- `installApk` (`CamJsBridge.java`) uses `Uri.fromFile` with no
  FileProvider — would throw on Android 7+, but it is currently dead
  code (no JS caller); fix or delete.
- `{ping:true}` heartbeat is not understood by the relay — it is
  forwarded to the source and answered with a discarded response;
  keep-alive only, no liveness semantics.

**Docs note**: `docs/mobile/*` is stale (last updated 2026-06-28) —
it still says "Relay only", "active on master",
"`__camReloadOnResume()` full reload", and "Direct = context CRUD
only"; none of that matches 2.4.51. This file is the living stability
record; update it when fixing issues, and refresh `docs/mobile/` when
a milestone lands.

### WebDAV storage

Project file exchange for the mobile work (assets, screenshots, APK
distribution) uses a Jianguoyun (Nutstore) WebDAV account:

- Server: `https://dav.jianguoyun.com/dav/`
- Account: `renhuailu@qq.com`
- App-auth passwords (named `FolderSync`, `wendav`) are **not** stored
  in this repo; see the screenshot at
  `/home/hren/github/.cam-images/20260725-184757-clipboard-image-20260725-104757.png`
  or ask the user. Do not commit the passwords.
