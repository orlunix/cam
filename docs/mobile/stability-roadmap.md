# Mobile V2 — Stability Status & Roadmap

**Living document.** Current line: branch `camui-desktop-v2`, version
2.4.117 (2026-10-19). This file supersedes the older `docs/mobile/*`
plans — see §5 for what to trust where.

## 1. Stability fixes in 2.4.117

The 2.4.111 stability fixes and the 2.4.110 native whole-view IME translation
are retained. Android terminal focus stays user-initiated, and terminal output
is preserved across first attach, cached re-entry, and app background recovery.

### Terminal transport and lifecycle

- **Stable terminal grid after attach** — Android performs one attach-time
  ready resize to open the native output gate, then disables automatic local
  and remote resize. Background/foreground transitions and agent switching
  keep the same SSH attach, xterm buffer, and grid. Only an explicit Fit or a
  new attach recomputes the size, preventing TUI input rows and echo state from
  being rearranged by lifecycle resize events.
- **Cached park/resume buffer preservation** — parked terminal entries cannot
  run deferred local fits or resize while hidden. Cached resume reuses the same
  attach and preserves the existing terminal rows.
- **First-attach byte preservation** — the native ready-gate flushes buffered
  PTY bytes before live output instead of discarding them. JS reveal replays
  the complete accumulated stream rather than clearing everything received
  before the final fit. Failed initial flushes drop the unusable native
  session. This prevents a partial first tmux repaint from leaving static
  bottom rows blank until refresh.
- **Loopback binary WebSocket** — Android Direct PTY traffic now uses
  `ws://127.0.0.1:<hub>/term?token=…`. `open/resize/copymode/close` are
  ordered JSON control messages; input and output are binary frames. The old
  `term_open/term_input/...` JavascriptInterface calls and the per-chunk
  `evaluateJavascript` data path are gone.
- **Backpressure and byte correctness** — each terminal has its own socket
  and PTY reader. A slow client blocks only that reader/socket, providing TCP
  backpressure instead of an unbounded JS callback queue. JS uses incremental
  UTF-8 decoding, so characters split across frames are preserved. Input is
  serialized by the socket reader, not one raw thread per keystroke.
- **Foreground service** — active terminals no longer die at the old 10-minute
  background timeout. A persistent notification keeps terminal transports
  alive; without a terminal the normal WebView pause path is restored.
- **Cleanup** — mode changes, hub stop/restart, and `MainActivity.onDestroy`
  close WebSockets, PTYs, client sockets, and the executor. Relay capture
  bridges are cached per `CamApi`, so revisiting a view cannot leak their 2s
  poll timers.

### Direct, API, and web stability

- **Direct auto-refresh** — the single app scheduler polls both Direct and
  Relay. It is visibility-gated and has an in-flight guard; idle agents are
  refreshed less often and foregrounding refreshes immediately.
- **Hub drift contract** — `apps/cam-mobile/test/hub-contract/contract.json`
  drives both the real Android `MobileEmbeddedHub` and the real desktop
  `embedded-hub.cjs`. Health/capabilities, context CRUD/sync-status, agent
  list, and output/input/key/upload status/error shapes must match.
- **Canonical module URLs** — mobile local ES modules and the HTML module
  entry use query-free canonical URLs. The old stamped-vs-bare duplicate
  module instance class is removed by construction; the exactly-once init
  guard remains as an invariant.
- **Relay recovery** — socket identity checks prevent a stale close from
  killing its healthy replacement. Reconnect uses visibility-aware exponential
  backoff and refetches data; one request timeout no longer closes the whole
  Relay socket.
- **Request cancellation and cleanup** — optional `AbortSignal` reaches direct
  HTTP, Relay, and the Android native Hub shim. Pending callbacks are removed
  on abort and late replies are ignored. Agent output polling stops at a
  terminal agent state; document/viewport listeners have explicit cleanup.
- **Cache coherence** — successful mutations invalidate `cam_cache:*`.
  Mobile identifies `_cached/_cachedAt` responses as cached data instead of
  presenting them as live.

### SSH and platform security

- **ed25519** — BouncyCastle is bundled beside mwiede JSch. Real sshd tests
  cover ed25519 direct and two-hop ProxyJump authentication.
- **Persistent TOFU host keys** — `StrictHostKeyChecking=yes` is backed by an
  app-private trust store. First contact records the key; a later change is
  rejected as `host_key_changed`. Nodes → Reset Host Key clears one host/port
  after out-of-band verification. Every ProxyJump hop is checked.
- **Release WebView** — remote debugging is off and unrestricted mixed content
  is off. Only the user-enabled Android Direct loopback profile temporarily
  selects Chromium compatibility mode for its `ws://127.0.0.1` terminal.
- **XSS and dead installer cleanup** — hub-supplied status/tool/id/meta fields
  are escaped or constrained to known class tokens. The unused `installApk`
  bridge and its install permission were removed.

## 2. Verification protocol

### Automated gates

Run from the repository root unless noted:

```sh
PATH="$HOME/tools/jdk17/bin:$PATH" bash apps/cam-mobile/test/lockrepro/run.sh
cd apps/cam-desktop
$HOME/tools/node/bin/node test/mobile-stability.test.cjs
$HOME/tools/node/bin/node test/mobile-terminal-reveal.test.cjs
$HOME/tools/node/bin/node test/mobile-terminal-park.test.cjs
$HOME/tools/node/bin/node test/api-stability.test.cjs
$HOME/tools/node/bin/node test/terminal-ws.test.cjs
$HOME/tools/node/bin/node test/mobile-hub-contract.test.cjs
$HOME/tools/node/bin/node test/mobile-nodes-form.test.cjs
$HOME/tools/node/bin/node test/mobile-rich-output-isolation.test.cjs
$HOME/tools/node/bin/node test/mobile-sync-host-timing.test.cjs
$HOME/tools/node/bin/node test/terminal-follow.test.cjs
$HOME/tools/node/bin/node test/tmux-controls.test.cjs
$HOME/tools/node/bin/node test/hub.test.cjs
```

The JVM suite also starts real user-level sshd instances when available. It
covers WebSocket framing/backpressure-sensitive transport code, terminal
ready-gate ordering and flush failure cleanup, hub restart, blackhole
concurrency, the shared contract, ed25519 direct/jump, changed-host key
rejection, and explicit retrust.

`apps/cam-mobile/build.sh` performs the full mobile JS syntax gate, javac/d8,
APK zipalign, signing, and signature verification. Packaging uses Python; a
host `zip` or `rsync` binary is not required.

### Required on-device smoke pass before release upload

1. Fresh launch → dashboard loads (no black screen or duplicate init).
2. Open agent detail → output streams; attach terminal → interactive input,
   resize, copy mode, and sustained large output all work.
3. Background an active terminal for more than 10 minutes → the persistent
   notification remains and the same session resumes without reattach.
4. Direct dashboard auto-refreshes. Sync a blackhole host → other hosts stay
   usable and background ticks do not overlap.
5. Verify ed25519 direct and ProxyJump nodes. A changed host key must fail;
   after checking the new fingerprint out of band, Reset Host Key must recover.
6. Screenshot ×5 + Home/resume ×5 → no viewport drift. Open/close the IME in
   terminal and Nodes forms → the whole view translates correctly.
7. Use `adb logcat` for release diagnostics (remote WebView debugging is off):
   no `[mobile] init failed`, duplicate-init warning, or unexpected native Hub
   timeout. Relay, if used, must use an HTTPS/WSS endpoint.

## 3. Current invariants and non-goals

- The Android terminal has no silent fallback to the removed string bridge;
  Relay capture remains a separate, explicitly selected path.
- The shared contract allows documented capability differences (for example
  mobile `agent_terminal_ws=true`, `event_stream=false`, `skillm=false`), but
  not accidental route/status/response-shape drift.
- TOFU detects every key change after first contact. It does not prove the
  first key by itself; verify a new host's fingerprint out of band when that
  threat matters.
- Relay is still deprecated. The remaining work is product migration, not a
  reason to reintroduce its old reconnect or polling bugs.
- The iOS port remains a plan, not part of this Android release.

No item from the old backlog is intentionally left open in 2.4.111. A new
issue should be recorded here with a repro and an automated regression test
before calling the release complete.

## 4. Historical context

- The global Direct Hub monitor (`synchronized apiRequest`) was removed in
  2.4.54; the blackhole regression remains in `lockrepro` and must stay green.
- ProxyJump banner loss on slow links was fixed in 2.4.96 by capturing channel
  streams before `connect()`. The real two-hop test remains mandatory.
- Viewport drift was a long-running WebView problem. The 2.4.110 model is
  `adjustNothing` plus native decor-inset translation of the entire WebView,
  with no JavaScript keyboard-offset or terminal-dimension changes.
- Older documents describing `term_*`, a 10-minute background kill, missing
  ed25519, disabled Direct refresh, or mixed/stamped module URLs are historical.

## 5. Doc trust map

| Doc | Status |
|---|---|
| **this file** | **authoritative** |
| `ios-plan.md` | **authoritative plan** (iOS not implemented) |
| `web/AGENTS.md`, `apps/cam-mobile/AGENTS.md` | authoritative onboarding |
| `README.md` | stale: Relay-only and resume descriptions |
| `native-plan.md`, `webview-resume.md` | historical; native Kotlin line archived |
| `webview-probe.md` | debugging method only; old fixed-version claims are stale |
| `direct-mode.md`, `android-direct-hub-port-plan.md` | historical gap lists |
| `desktop-parity-review.md` | use the executable shared contract instead |
| `relay-first-plan.md` | historical; Relay is retiring |
