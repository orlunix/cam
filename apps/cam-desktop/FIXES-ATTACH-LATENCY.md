# FIXES-ATTACH-LATENCY.md — remote attach took ~10s + random detaches

Branch: `camui-desktop-v2`. Date: 2026-07-17.

## Symptoms (user report)

- Every remote (SSH) terminal attach took ~10s. Expectation: only the
  first SSH connection is slow; subsequent attaches on a warm pool
  should be millisecond-level.
- Attached terminals dropped at random ("动不动就 detach").

## Root causes found

1. **Housekeeping blocked the attach critical path** (`main.cjs`
   `termOpen`, pre-fix :666-670): before the terminal channel was even
   opened, the code awaited, in sequence —
   - `_tmuxClientSet` (SSH exec `list-clients`, 3s timeout) — baseline
     for the window-controls feature;
   - `_repairRemoteTerminalSize` (SSH exec, 8s timeout).

   On a warm pool that's 2 extra round trips; on a half-open pooled
   socket (see 2) each hangs until its own timeout, summing to ~11s —
   the observed 10s. These were introduced by commit `3e8d351`
   ("stabilize tmux terminal controls"), which is why the slowdown
   appeared "suddenly".

2. **Pooled connections died silently** (`ssh-transport.cjs`
   `keepaliveInterval: 0`). With keepalives disabled, NAT/firewall idle
   timeouts kill the socket without anyone noticing: an attached
   terminal then drops out of nowhere ("detach"), and the next
   operation discovers the half-open socket only via its own timeout
   (then reconnects — slow).

## Fixes

- **`main.cjs` `termOpen`**: the `list-clients` baseline probe now runs
  in PARALLEL with `openTerminalChannel` instead of blocking it, and
  `_repairRemoteTerminalSize` is fire-and-forget after the channel is
  live. Critical path is now `getAttachConnectOpts` (one warm-pool
  probe) + channel open (one round trip). Window-control discovery is
  unchanged (parallel baseline + `selectOnlyClient` fallback).
  `term:open` also returns `timings: { open_ms }` so future latency
  reports carry data instead of guesses.
- **`ssh-transport.cjs`**: `keepaliveInterval: 15000` +
  `keepaliveCountMax: 3` on pooled connections. Dead sockets now
  surface within ~45s instead of at the next operation's timeout, and
  NAT mappings stay warm, eliminating the silent-drop class of
  detaches.

## Verification

- `lint:electron` + all test suites green (hub 66, term 80+6, start 6,
  local-runtime).
- Packaged MSI rebuilt; asar verified to contain both changes.
- **Live numbers** (this machine → prgn.nvidia.com, real ssh2 pool):
  - Link baseline: ~1s per SSH round trip (OpenSSH ControlMaster warm
    exec 0.8s; ssh2 pooled warm exec 1.03s). Cold handshake 3-4.5s.
  - Old path estimate on this link: probe + list-clients + repair +
    open ≈ 4 round trips ≈ 4.5s warm, 10s+ on any stall.
  - After the fix, live attach to a real session:
    `open_ms=1531 first_byte_ms=1773` (and 1665/1894 on repeat) —
    i.e. ~1.5-1.9s, which is the floor for this link.
  - Follow-up in the same pass: `getAttachConnectOpts` now skips the
    per-attach `_ensureRemoteCamc` probe on a ready-cache hit (it was
    one more round trip); the probe/upload still runs once per host
    per hub run.

## Not done / follow-ups

- No WAN attach measurement from inside the packaged app yet — the
  numbers above come from the same modules run directly on this
  machine; a quick in-app sanity check is still worthwhile.
- The remaining theoretical floor on this link is one round trip
  (~1-1.9s) — sub-second attaches are not achievable over a ~1s-RTT
  link with any SSH implementation (OpenSSH ControlMaster warm exec
  measures the same).

## Same-day follow-up: redundant resize churn on tab switches

Every fit/entry path sent a resize to the PTY unconditionally, and
every `setWindow` makes tmux resize + redraw the whole pane — over a
~1s-RTT link that redraw burst made cached tab switches feel like a
reconnect. Now:

- `agent-console.js` `fitTerminalAndNotify` only notifies when the
  fitted cols/rows actually changed (compared against the entry's
  last-notified size).
- `main.cjs` tracks `_appliedCols/_appliedRows` per terminal and skips
  unchanged `setWindow` calls both in `termResize` and in the
  `term:open` reuse path.

A same-size cached tab switch is now purely a local xterm buffer swap —
zero network. Combined with the live session cache
(`TERMINAL_CACHE_LIMIT = 6`, channels stay open on the pooled
connection), switching between attached agents is ms-level as expected.

## Follow-up 6 (2026-07-17): terminal redesigned as a real terminal (Tabby reference)

Architecture pass using `C:/Users/Thinkpad/gitlab/tabby` as the reference
(see the approved plan in the session plans). Adopted invariants:

- **Tab parking without `display:none`** — terminal panes are parked
  off-viewport (`.agent-terminal-pane.parked { left:-10000px;
  visibility:hidden }`, CSS `desktop.css`) instead of the `hidden`
  attribute. The xterm layout/grid survives tab switches untouched:
  zero reflow, zero ResizeObserver churn, zero re-attach on switch
  (Tabby `appRoot.component.scss` pattern).
- **Session states on the terminal entry** — `'idle' | 'connecting' |
  'live' | 'dead' | 'reconnecting'` (`agent-console.js`).
- **In-place reconnect** (Tabby `ConnectableTerminalTabComponent`) — on
  an unexpected channel drop the pane keeps its scrollback and shows
  "press any key to reconnect"; the next keystroke writes
  `resetTerminalModes()` sequences (`\x1b[?1000l … \x1b[?2004l`) and
  opens a fresh channel for the same agent into the same xterm. Clean
  exits (code 0) keep the plain "terminal detached" note without a
  reconnect offer.
- **Ready-gate** (Tabby `initialDataBuffer`) — main buffers each new
  channel's bytes until the renderer signals `term:ready` (xterm opened
  + fitted), then flushes in order; 256 KB cap + 2 s fallback so a
  stuck renderer can't wedge the channel. Both SSH and local branches.
- **Streaming UTF-8 decode** (Tabby `UTF8SplitterMiddleware`) —
  per-channel `TextDecoder({stream:true})` replaces per-chunk
  `buf.toString('utf8')`; multibyte chars split across transport chunks
  no longer render as `�` (verified: `你好，世界` split mid-codepoint).

Deferred to phase 2 (noted): recovery tokens with serialized scrollback,
write-path ack backpressure, inline ` SSH ` service-message banner.

## Same-day follow-up 5: size corruption (tiled/wrapped tmux status) + upload rule

- **Root cause of the tiled status bar**: the open-size code computed a
  grid from container-rect ÷ estimated-cell-size and FORCED xterm to it
  — a second size source that could diverge from xterm's own fit and
  never self-correct, leaving PTY width ≠ pane width (tmux status then
  wraps/tiles; redraw bursts garble TUIs). Fix: the open size is now
  xterm's own fitted grid (`term.cols/rows` — the single source of
  truth), only floored at the anti-poison ≥40×4.
- **Resize storm control**: layout settling (scrollbar/font) emitted a
  resize per intermediate size — each one a full tmux redraw, and on
  local script-PTY a full detach/reattach. Renderer resize notifies are
  now debounced (150ms stable size only), and the local reopen has
  hysteresis (sub-2-col / sub-1-row deltas are ignored).
- **camc upload rule** (`_ensureRemoteCamc`): was hash-mismatch →
  upload, which downgraded hosts whose own cam sync had installed a
  NEWER camc (ping-pong). Now uploads only when the remote is missing
  or strictly older than the bundled `__version__` (explicit Sync Host
  `force` still always installs).
- **Terminal/exec SSH isolation**: terminal channels moved to a
  dedicated per-endpoint pool (`_termPool`); an exec timeout/reset can
  no longer kill an attached terminal (the tab-switch detach).

## Same-day follow-up 4: first attach no longer blocked on camc upload

`getAttachConnectOpts` used to run the full `_ensureRemoteCamc`
(md5 probe + SFTP upload of ~800KB on hash mismatch) inline on the
first attach to a host — several seconds on a ~1s-RTT link, on top of
the ~3s handshake and the ~1.5s channel open: the observed ~10s first
attach. Now: one cheap existence probe (`test -x ~/.cam/camc`, one
round trip); if a usable camc is present the attach proceeds
immediately and the refresh runs in the background; only a host with
no camc at all waits for the install. Measured after the change
(prgn.nvidia.com): handshake+probe 5.7s + open 1.9s ≈ 7.7s first
attach (network jitter varies; the multi-second upload is gone from
the path). Subsequent attaches stay at ~1.5-1.9s.

## Same-day follow-up 3: handshake timeout + reopen 'detached' race

- `ssh-transport.cjs` `openTerminalChannel` now retries once on
  handshake/connect failure (drop pool entry, fresh TCP connect) —
  previously a single transient VPN/NAT drop surfaced as
  `Terminal attach failed: Timed out while waiting for handshake` even
  though a retry would succeed. `readyTimeout` floor raised 10s → 20s
  for slow VPN handshakes.
- `main.cjs` local resize-reopen race: disposing the old script-PTY
  channel first made its `onClose` surface in the renderer as a spurious
  "terminal detached" (and nulled the session). Channels now carry
  generation tokens — the replacement opens first, the live token flips
  (retired channel's events are ignored deterministically), then the old
  channel is disposed.

## Same-day follow-up 2: open always at the screen's real size

`openTerminalForSelected` used `Math.max(80, lastCols || term.cols ||
100)` / `Math.max(20, ...)` for the PTY's birth size — floors and
hardcoded fallbacks that could make the PTY open wider/narrower than the
actual pane (a "default" size, the very thing that later needs
reconciling). Now the open size is computed from the container's
bounding rect and the measured cell size, xterm is resized to exactly
that grid, and the PTY opens with the same numbers — screen and PTY
agree at birth. Only the anti-poison floor (>=40 cols / >=4 rows)
remains; no 80x24-style defaults.
