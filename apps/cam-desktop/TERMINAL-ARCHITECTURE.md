# CAM Desktop — Terminal tab architecture

Design document for the terminal subsystem: the model, the invariants,
and the reasoning that produced them. Written 2026-07-17 after the
Tabby-reference redesign. Companion records: `FIXES-ATTACH-LATENCY.md`
(bug-by-bug history), `LOCAL-NODE-DATAPATH.md` (local runtime).

## 1. The model

CAM Desktop **is** a terminal. The sidebar agent list is the (vertical)
tab strip; each tab is bound to one camc/tmux session on a node:

```
Sidebar agent row  ⇔  terminal tab  ⇔  tmux session on some node
```

Everything else — hub, contexts, sync, workflows — exists to feed this
model. When in doubt, the app defers to terminal semantics, not to
app-specific conventions ("what would Tabby do?").

## 2. Why "a terminal with tabs" was not trivial

A local terminal emulator has it easy: one process, syscalls for
everything (openpty, TIOCSWINSZ, SIGHUP). Our tab spans **three nested
terminal emulators plus a network**:

```
xterm.js (renderer)                — emulator #1, owns the screen grid
    │  SSH channel / wsl.exe pipe  — the network, ~1s RTT on VPN links
tmux on the node                   — emulator #2, owns pane/window state,
                                     window size = min over ALL clients
    │
agent TUI (claude/codex/cursor)    — emulator #3, draws the actual UI
```

So one "tab" is really **four state machines** that must agree: the SSH
connection, the tmux client, the xterm grid, and the renderer's session
record. Every bug we shipped was two of them disagreeing — never a
"rendering bug":

| Symptom | Actual mismatch |
|---|---|
| attach ~10s | housekeeping round trips serialized ahead of the channel |
| tab switch detaches | exec traffic and terminal sharing one TCP connection |
| tiled/wrapped status bar | PTY width ≠ xterm grid width |
| ghost "terminal detached" | planned reopen indistinguishable from unplanned drop |
| `�` in Chinese output | multibyte char split across transport chunks |

Method consequence: **fix state ownership, not symptoms**. Each fix
below assigns one owner per piece of state.

## 3. The invariants (final architecture)

### 3.1 A tab never dies while it is open

Panes are **parked off-viewport** (`.agent-terminal-pane.parked {
left:-10000px; visibility:hidden }`), never `display:none` and never
unmounted. The xterm layout and grid survive every switch; switching is
a DOM visibility change only. (Tabby: `appRoot.component.scss`
`left:-1000%`.) The session/channel has exactly two terminal states:
tab closed, or remote EOF. Nothing else may close it — not a mode
switch, not a sync tick, not another tab.

### 3.2 One connection per purpose, not one per app

Two SSH pools (`ssh-transport.cjs`): `_pool` for exec traffic
(list/capture/sync/bootstrap), `_termPool` for interactive terminal
channels, one connection per endpoint each. An exec timeout or reset
can never kill an attached terminal. (Tabby: `SSHSession` connection vs
`SSHShellSession` channel, refcounted.) This is what makes "closing/
refreshing something else killed my tab" impossible by construction.

### 3.3 The xterm grid is the only source of size

Chain: `ResizeObserver → fit() → xterm.onResize → 150ms debounce →
channel resize`. The PTY is born with the fitted grid
(`term.cols/rows`), never a default and never a second estimate — an
earlier rect÷cell-size computation diverged from fit() and produced the
tiled-status corruption; it was removed in favor of the single source.
Only the anti-poison floor (≥40 cols / ≥4 rows) applies. Same-size
notifications are dropped at every layer (renderer dedup, main
`_appliedCols/_appliedRows`); for local script-PTY channels, resize is
a transparent reopen with hysteresis (±2 cols / ±1 row ignored).
(Tabby: `ResizeObserver → fit → onResize → auditTime(100) →
resizePTY`.)

### 3.4 First output waits for the frontend

tmux repaints the entire pane the moment a client attaches. Bytes are
buffered in main until the renderer signals `term:ready` (xterm opened
+ fitted), then flushed in order — 256 KB cap, 2 s fallback
(`_termGateOpen/_termGateSend/_termGateFlush`, `main.cjs`). (Tabby:
`BaseSession.initialDataBuffer`.)

### 3.5 Transport events carry generations

Every channel open (initial or reopen) binds its callbacks to a token;
the entry's live token flips on reopen, and events from retired
generations are discarded by token mismatch. A planned resize-reopen
can therefore never surface as an unplanned "terminal detached" — the
failure mode that pure time-based guards could not make deterministic.

### 3.6 Disconnect is a session state, not a dead end

Unexpected drop ⇒ the pane keeps its scrollback and shows "press any
key to reconnect"; the next keystroke writes `resetTerminalModes()`
(mouse tracking / bracketed paste off) and opens a fresh channel into
the same xterm. Clean exits (`code 0`) show a plain detached note with
no reconnect offer. (Tabby: `ConnectableTerminalTabComponent`.)
Scrollback lives in xterm, so reconnection is pure upside — tmux keeps
the session alive server-side.

### 3.7 Bytes are decoded as a stream

Per-channel `TextDecoder('utf-8')` with `{stream:true}` replaces
per-chunk `buf.toString('utf8')`; multibyte characters split across
chunks decode correctly. (Tabby: `UTF8SplitterMiddleware`.)

### 3.8 camc freshness is a version rule, not a hash rule

`_ensureRemoteCamc` uploads the bundled camc only when the remote is
missing or strictly older (`__version__` compare). A newer remote is
never downgraded — hash equality would ping-pong against the host's own
cam sync. Explicit Sync Host still force-installs.

## 4. Data flow (steady state)

**Attach**: `term:open` → `getAttachConnectOpts` (existence probe; camc
refresh in background) → `openTerminalChannel` on `_termPool`
(handshake retry ×1, keepalive 15s) → gate buffers → `term:ready` →
flush → live stream. Measured on the prgn link: ~1.5–1.9s warm,
~5.5–7.7s cold (handshake-bound; ~1s/RTT is the link's floor).

**Resize**: window drag → ResizeObserver → fit() → onResize → debounce
150ms → `term:resize` → SSH `setWindow` / local reopen (hysteresis) →
tmux redraws once.

**Reconnect**: channel close (abrupt) → renderer marks `dead` + offers
reconnect → keystroke → `resetTerminalModes` → new channel → same
xterm, scrollback intact.

## 5. Method notes (how the redesign was driven)

- **Reference-first**: before writing code we studied Tabby's actual
  architecture (tab/session split, parking, size chain, recovery) and
  mapped each of our bug classes to a Tabby invariant. Several fixes
  from earlier in the day (pool split, resize dedup, generation tokens)
  turned out to be independent reinventions of the same patterns —
  adopting Tabby's framing unified them.
- **Measure, don't guess**: every latency claim comes from the app's
  own transport run against prgn (handshake 3s, warm RTT ~1s, attach
  1.5s); every size claim from `tmux list-clients`/`display` inspection
  during reproduction.
- **One owner per state**: each fix assigned a single owner (grid owns
  size, token owns channel identity, gate owns first output, pool owns
  connection lifetime). Bugs in this system are always dual-ownership
  bugs.
- **Self-correction is part of the record**: the rect/cell size
  estimator (my own earlier "improvement") caused the worst visual
  corruption and was removed; the incident is documented in
  `FIXES-ATTACH-LATENCY.md` follow-up 5, not edited out.

## 6. Deliberately not done (phase 2)

- **Recovery tokens** (serialized scrollback across app restarts,
  Tabby `TabRecoveryService`) — the model supports it; storage schema
  not designed yet.
- **Write-path backpressure** (ack-based pause/resume) — xterm's
  internal batching has been sufficient so far; revisit if a
  background tab can stall the foreground one.
- **Inline connection banner** (` SSH ` service messages in the
  terminal, Tabby style).
- **Frontend class abstraction** (a `Frontend` interface with
  decorators) — the invariants landed in the existing vanilla-JS
  structure; a class rewrite adds churn, not behavior.

## 7. Where things live

| Piece | File |
|---|---|
| Terminal entry model, parking, reconnect, ready emit | `web/js/desktop/agent-console.js` |
| Pane parking CSS | `web/css/desktop.css` (`.agent-terminal-pane.parked`) |
| Channel lifecycle, ready-gate, UTF-8, local reopen | `apps/cam-desktop/electron/main.cjs` |
| SSH pools, keepalive, handshake retry | `apps/cam-desktop/electron/ssh-transport.cjs` |
| Local runtime (WSL/native exec, attach channel) | `apps/cam-desktop/electron/local-runtime.cjs` |
| Hub routes, attach opts, camc version rule | `apps/cam-desktop/electron/embedded-hub.cjs` |
| Bug history with measurements | `apps/cam-desktop/FIXES-ATTACH-LATENCY.md` |
