# Web UI — Agent Notes

This directory contains three separate web frontends. Know which one you
are working on before editing:

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
  frozen at 2.2.0). Version source of truth: `apps/cam-mobile/VERSION`.
- `mobile.html` + `js/mobile/*` — vanilla JS ES modules, no framework,
  no build step, hash router in `js/mobile/app.js`.
- Two connection modes:
  - **Relay** (deprecated): pure client of the Node embedded Hub started
    by `camui start` (`apps/cam-desktop/electron/embedded-hub.cjs`),
    reached through `relay/relay.py`. API calls use `CamApi` in
    `js/api.js`; phone stores Relay URL + token in localStorage.
  - **Direct**: the phone hosts an embedded hub itself (JS side
    `js/mobile/direct-*.js`, `settings-direct.js`; native side
    `MobileEmbeddedHub.java` in `apps/cam-mobile/`).
- Android Direct terminal uses the hub's authenticated loopback `/term`
  WebSocket: JSON control messages and binary PTY input/output. Do not
  reintroduce `term_*` JavascriptInterface calls or `evaluateJavascript`
  terminal-data delivery. Relay capture polling is a separate path.
- Packaged as an Android APK by `apps/cam-mobile/build.sh` (no Gradle),
  loading the web app via `CamAssetLoader.java` virtual HTTPS
  (`https://appassets.androidplatform.net/...`, NOT `file://`).
- Layout resets use the early inline script in `mobile.html`
  (`__camResetLayout` / `__camScheduleLayoutResets`). IME handling is
  native: `adjustNothing` plus whole-WebView decor-inset translation.
  Do not add JavaScript keyboard-offset handling back.

### Read first

- **`docs/mobile/stability-roadmap.md`** — the living document: current
  fixes, executable verification protocol, and doc trust map.
- `docs/mobile/relay-first-plan.md` — historical Relay plan.
- `docs/mobile/direct-mode.md` and `android-direct-hub-port-plan.md` —
  historical Direct plans; their old gap lists are stale.
- `docs/mobile/README.md` — V1 vs V2 and viewport background, partially
  stale; trust current code and the roadmap.
- `docs/desktop/requirements.md` — canonical desktop requirement IDs.

### Hard boundaries

- Mobile work touches only: `web/mobile.html`, `web/js/mobile/*`,
  shared `web/js/api.js` / `web/js/state.js` (carefully — shared with
  desktop), `web/css/mobile.css`, `docs/mobile/*`, and `apps/cam-mobile/`.
- Never modify: `src/cam/**` (Python server), `embedded-hub.cjs`, camc
  (`src/camc_pkg/*` — owned by another agent, see root AGENTS.md), the
  V1 `web/index.html` tree, or the desktop renderer (`web/js/desktop/*`
  — reference only, do not import from mobile code).
- The native Kotlin UI line is archived on `camui-mobile-native` and
  must not be resurrected.

### Build / verify

- Web PWA has no build step; serve `web/` statically or via
  `python3 relay/relay.py --port 8001 --token <t> --web-root web/`.
- APK: `cd apps/cam-mobile && ./build.sh` → `build/cam.apk`; bump
  `apps/cam-mobile/VERSION` (patch) per iteration; install with
  `adb install -r apps/cam-mobile/build/cam.apk`.
- `build.sh` stamps `?v=<version>` only into CSS/classic-script assets.
  Local ES-module imports and the mobile HTML module entry remain
  query-free canonical URLs. Never reintroduce mixed stamped/bare
  module instances.
- Run the roadmap's JVM + Node gates. `apps/cam-mobile/test/hub-contract/`
  is shared with `apps/cam-desktop/test/mobile-hub-contract.test.cjs`;
  update both runners whenever a shared API contract changes.

### Current stability invariants (2.4.117)

- One app init and one visibility-aware, in-flight-guarded refresh
  scheduler. Direct and Relay both auto-refresh; no background polling.
- Relay socket identity is checked in every handler. Reconnect uses
  visibility-aware exponential backoff and refetches data; one request
  timeout does not kill the socket.
- Requests accept `AbortSignal` across direct HTTP, Relay, and the
  Android Hub shim. Successful mutations invalidate `cam_cache:*`, and
  mobile labels cached responses instead of treating them as live.
- Agent output polling stops at terminal state. Route cleanup removes
  document, viewport, resize, and terminal listeners/timers.
- `shared/term-bridge.js` caches Relay bridges per `CamApi`; mode changes
  close all terminal sessions. Android terminal PTY streams flush the initial
  ready-gate buffer in order and use bounded, backpressured loopback
  WebSockets with incremental UTF-8 decoding. Parked entries never resize
  locally. Android allows one attach-time ready resize, then keeps the grid
  fixed across background recovery and agent switches; only explicit Fit or a
  new attach may resize the local terminal or remote PTY.
- Hub-provided status/tool/id/meta strings are escaped or constrained to
  known class tokens before entering HTML.
- Release WebView debugging and unrestricted mixed content are off.
  Android SSH uses ed25519-capable JSch/BouncyCastle and strict persistent
  TOFU host-key verification; Nodes → Reset Host Key is the recovery path.
- `docs/mobile/stability-roadmap.md` is the living record. Update it and
  the executable tests whenever an invariant changes.

### WebDAV storage

Project file exchange for the mobile work (assets, screenshots, APK
distribution) uses a Jianguoyun (Nutstore) WebDAV account:

- Server: `https://dav.jianguoyun.com/dav/`
- Account: `renhuailu@qq.com`
- App-auth passwords (named `FolderSync`, `wendav`) are **not** stored in
  this repo; see the existing secure reference or ask the user. Do not
  commit, print, or upload passwords or plaintext key files.
