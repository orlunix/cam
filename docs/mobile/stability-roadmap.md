# Mobile V2 — Stability Status & Roadmap

**Living document.** Current line: branch `camui-desktop-v2`, version
2.4.64 (2026-07-25). This file supersedes the older `docs/mobile/*`
plans, which are stale (last touched 2026-06-28) — see §6 for what to
trust where.

## 1. Fixed recently (2.4.52–2.4.55)

- **Hub global lock** (2.4.54): `MobileEmbeddedHub.apiRequest` was globally
  `synchronized` — one blackhole host's SSH attempt stalled every Direct
  request (measured 117s in the repro). Removed the global monitor;
  per-host ordering stays where it belongs (the SSH layer's
  `MobileSshPool.lockFor()`); `ensureStoreLoaded`/`saveStore`
  synchronized. Regression test: `android/test/lockrepro/` (real hub
  classes on the JVM + Android stubs; `run.sh`).
- **Copy-mode browsing UX, final form** (2.4.56–2.4.64): terminal
  History button (`⤒` left / `⤓` right in the floating status row)
  enters tmux copy mode via key-stream (`C-b [` — camc tmux.conf sets
  no prefix); **vertical drag** on the terminal maps distance to line
  counts over the live PTY (webpage-like scrolling, gated on
  copyModeActive so selection/taps are untouched); **horizontal
  swipes** jump cursor to top/bottom line of the current screen
  (`send-keys -X top-line|bottom-line` via hub exec, stays in copy
  mode); `⤓` tap exits (`q`). A fling/velocity experiment (2.4.61–63)
  was tried and reverted — final model has no velocity detection.
  Buttons + status pill use a 30% dark tint, no blur, light outline
  (iterated 2.4.57–59 on-device). The verified command path
  (`MobileEmbeddedHub.terminalCopyMode`: enter|up|cancel|top|bottom,
  pane state read back via `#{pane_in_mode}`) remains available.
- **"Connecting via SSH…" dead state** (2.4.52–2.4.53): native-bridge pending
  callback maps were module-instance state and got orphaned on every
  shim reinstall; with no timeout, the attach promise never settled and
  only a manual page refresh recovered. Fixed by moving pending
  maps/seq/handler sets to `window.__camBridgeShared` and adding
  timeouts to every native bridge call (term_open 60s, input/resize
  10s, close 15s, hub ops 60s, hub API 90s, with `console.warn`
  diagnostics).
- **Duplicate `init()`** (048a3e3): `app.js` is evaluated twice
  (`?v=` entry + bare imports from views). Added an exactly-once guard,
  deferred via microtask — the bare instance evaluates before the view
  modules settle (TDZ), which caused the 2.4.52 black-screen
  regression when the guard was first added without the defer.
- Side effect observed on device: post-screenshot viewport drift no
  longer reproduces (likely the duplicated layout-reset sequences were
  fighting each other; root cause still unproven — see §5).

## 2. Remaining issues

### 2.1 Bridge layer (JavascriptInterface string bridge) — structural

These are inherent to the string-callback bridge, not bugs with a
local fix:

1. **String-only transport** — terminal byte stream crosses as
   strings (binary needs base64: +33% size, CPU on both ends).
   Throughput ceiling; heat/jank under heavy tmux output.
2. **No backpressure** — a fast native reader can flood the WebView
   JS thread; no flow control.
3. **`term_input` spawns a raw `Thread` per keystroke with
   unsynchronized stream writes** (`CamJsBridge.java`) — fast typing
   can interleave bytes. Easy fix: single-thread executor.
4. **JS timeout cannot cancel native work** — a timed-out `term_open`
   keeps running natively; the session may appear later unattributed
   (10-min idle reaper is the backstop).
5. **Background bridge delivery suspension** — WebView `onPause`
   suspends callback delivery; currently propped up by the 10-min
   grace + keep-alive. A real fix means a foreground service
   (Termius-style persistent notification) — product decision.
6. Cleanup tails — Relay↔Direct mode switches don't close native
   terminal sessions; `MainActivity.onDestroy` doesn't stop the
   embedded hub (ServerSocket/thread-pool leak until process death).

### 2.2 Direct mode (phone-hosted hub)

1. ~~**Global `synchronized apiRequest`** (`MobileEmbeddedHub.java`) —
   one unreachable host stalls ALL Direct UI.~~ **Fixed in 2.4.54** (see
   §1). Repro preserved in `android/test/lockrepro/`.
2. **No agents auto-refresh** — Direct has neither agents polling nor
   an event stream (`/api/ws` unimplemented in the hub, disabled
   after 3 failures): dashboard state goes stale until manual refresh.
3. **Drift risk** — `MobileEmbeddedHub.java` (~2.3k lines) reimplements
   `embedded-hub.cjs`; no shared spec, behavior will drift.

### 2.3 Web layer

1. **Module specifier split (P0 cluster, band-aided)** — `?v=` stamps
   cover only 4 files (`build.sh` sed); the rest import bare. Still
   live: `hub-capabilities.js` double instance breaks capability
   gating in Relay mode (reads hit the empty bare instance);
   `mobile-bridge.js` double instance made harmless by the shared
   table. Real fix: consistent specifiers everywhere + build.sh sed
   coverage (Batch 2).
2. **App-level 10s loop** — no `document.hidden` check (polls while
   backgrounded), no in-flight guard (slow-network ticks overlap and
   `connect()` calls clobber each other).
3. **agent-detail** — output polling continues after the agent reaches
   a terminal state; the Cancel affordance is dead code
   (`_inflightAbort` never assigned an `AbortController`); a
   `document` click listener leaks per render.
4. **localStorage `cam_cache:*`** — never invalidated on mutations;
   `_cached` flag only consumed by desktop.
5. ~~Relay reconnect (fixed 1s, no backoff, no socket-identity
   check)~~ — **deprioritized: Relay is retiring.**

### 2.4 Security surface (hardening, not urgent)

- `setWebContentsDebuggingEnabled(true)` unconditionally (release too);
  `MIXED_CONTENT_ALWAYS_ALLOW`; SSH `StrictHostKeyChecking=no`; a few
  unescaped innerHTML insertions of hub-supplied values
  (`dashboard.js` tool/machine/id/status, `agent-detail.js` status).

## 3. Roadmap

**Batch 1 — Direct daily-use (next release)**
- ~~Un-serialize the hub~~ — **done in 2.4.54**.
- Direct-mode agents polling in `refreshAgents` (~30s, visibility
  gated) until `/api/ws` exists in the hub.
- Quick wins: 10s loop visibility check + in-flight guard; stop output
  polling in terminal state; wire `_inflightAbort`; `term_input`
  single-thread executor (§2.1.3).

**Batch 2 — module specifier unification (kills the P0 cluster)**
- Consistent import specifiers across all of `web/js/mobile/**` and
  `web/js/shared/**`; extend `build.sh` version stamping to cover
  every stamped file. Acceptance: single evaluation of every module
  (init guard's `console.warn` never fires), capability gating works
  in Relay mode.

**Batch 3 — Plan B: terminal over loopback WebSocket (structural)**
- Add a `/term` WebSocket endpoint to `MobileEmbeddedHub`; xterm.js
  attaches via `ws://127.0.0.1` with binary frames. Replaces the
  `term_*` string bridge — removes §2.1 items 1, 2, 4 and the whole
  pending-callback failure class by mechanism. Unifies with Relay
  mode's WS path (one terminal transport). Gate via hub capabilities;
  keep the string bridge as fallback for one release.
- Plan A (native terminal view via a `terminal-emulator` library,
  ConnectBot/Termux style) remains the escape hatch if B still can't
  meet the bar for long-running ops sessions.

**Deferred / product decisions**
- Foreground service for background keep-alive (persistent
  notification) vs. the current 10-min grace workaround.
- `android/probe/` A/B/C root-causing of viewport drift (§5) if it
  resurfaces.

## 4. Verification protocol (lesson from the 2.4.52 regression)

No upload without an on-device smoke pass:

1. Fresh launch → dashboard loads (no black screen).
2. Open agent detail → output streams; attach terminal → interactive.
3. Screenshot ×5 + Home/resume ×5 → no viewport drift.
4. Nodes → sync a blackhole host → other hosts stay usable.
5. `chrome://inspect` console: no `[mobile] init failed`, no
   unexpected `[mobile-bridge] … timed out` warnings.

## 5. Historical context

Viewport drift (screenshot/app-switch scale/scroll corruption) was the
long-standing issue: mitigations went native-chrome + WebView
destroy/recreate (2.1.x) → single height model + layout resets
(2.2.x–2.4.x). Root cause never isolated; `android/probe/` exists for
it. As of 2.4.53 it no longer reproduces on the test device (see §1) —
treat as "dormant, unproven".

## 6. Doc trust map (as of 2026-07-25)

| Doc | Status |
|---|---|
| **this file** | **authoritative** |
| `web/AGENTS.md`, `android/AGENTS.md` | authoritative (agent onboarding) |
| `README.md` | stale: "Relay only", resume section |
| `native-plan.md`, `webview-resume.md` | historical (native line archived) |
| `webview-probe.md` | method still valid; "fixed in 2.2.1+" claims predate reality |
| `direct-mode.md`, `android-direct-hub-port-plan.md` | "current gap" sections predate MobileEmbeddedHub |
| `desktop-parity-review.md` | feature matrix stale (Direct is full-capability now) |
| `relay-first-plan.md` | historical (Relay retiring) |
