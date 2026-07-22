# DEBUGGING.md — how to debug CAM Desktop (connection stability focus)

Audience: an agent asked to diagnose "connection is unstable" on
macOS (or any platform). This doc is the accumulated debugging
experience from the 2026-07-17 → 2026-07-22 sessions. Follow the
playbook before changing code; most "mysteries" below were already
solved once — check the catalog first.

## 1. Architecture in one page

Processes and channels, in order:

```
Renderer (web/desktop.html + web/js/desktop/*.js, xterm.js)
   │  CamBridge (electron/preload.cjs — SANDBOXED, see §6)
   │  ipcRenderer.invoke
Electron main (apps/cam-desktop/electron/)
   ├─ embedded-hub.cjs   HTTP+WS server on 127.0.0.1:8420-8429
   │                     ("local proxy" in the UI). Owns the agent/
   │                     context store, syncs remote agents over SSH.
   ├─ main.cjs           terminal IPC handlers (term:open/input/...),
   │                     tmux window controls, size repair
   └─ ssh-transport.cjs  TWO SSH connection pools per endpoint:
        exec pool      — short ops: camc list/status/capture, file
                         transfer, tmux control commands. Reconnects
                         per op → self-healing.
        terminal pool  — ONE long-lived connection per endpoint that
                         carries every terminal channel (PTY running
                         `~/.cam/camc attach <id>`). Dies first on bad
                         networks and does NOT heal by itself.
Remote node
   └─ ~/.cam/camc       uploaded by the app when missing/older/hash-
                         different. Creates ONE tmux server per agent:
                         session cam-<agentId>, socket
                         /tmp/cam-sockets/cam-<agentId>.sock, and
                         records tmux_session/tmux_socket/tmux_bin in
                         the agent record (~/.cam/agents.json).
```

Datapaths that matter:

- **Rich/plain output view** = exec pool → `camc capture` (tmux
  capture-pane text). No PTY, no long connection.
- **Terminal mode** = terminal pool → PTY channel → `camc attach`
  (interactive tmux attach). PTY + long-lived connection.
- **Tab strip (tmux windows)** = exec pool tmux control commands
  (`display-message`, `list-windows`, `switch-client`, ...).
- **Add node / Sync Host** = exec pool + camc bootstrap upload.

## 2. The single most important mental model

**Rich output working while terminal cannot attach is normal.** They
are different transports (see table above). Debugging "terminal won't
attach" on a flapping link means debugging the terminal pool and the
PTY channel, NOT the app in general — the exec side can be perfectly
healthy at the same time.

tmux window size = **minimum over all attached clients**. Any client
(any device, any app version, a manual `tmux attach`) that attaches
small shrinks the window for everyone. Our app never sends <40 cols
(clamped in 5 places) — a ~10-col window means a *foreign* client.

## 3. Failure catalog (symptom → root cause → evidence)

### 3.1 Top status loops "checking → disconnected" (local proxy)

The renderer cannot reach the embedded hub. Root cause found
2026-07-22: **`require('os')` in preload.cjs**. Preload scripts run in
a sandbox that only allows `electron`, `events`, `timers`, `url`.
One illegal require → the whole preload fails → `CamBridge` is
undefined → renderer cannot call `directHub.start()` → hub never
starts. Verify in 10 seconds:

```bash
netstat -ano | grep LISTEN | grep 842        # hub port 8420-8429
# nothing listening → hub never started → check the renderer console
# for "Unable to load preload script" (see §5 CDP)
```

Prevention now in place: `npm run test:smoke` (boot smoke: launches
the real app in an isolated userData dir, asserts preload loads,
CamBridge exists, hub starts) + a static allowlist assertion in
`mobile-nodes-form.test.cjs`.

Other causes for the same symptom: hub port conflict (another app
holds 8420-8429), or a main-process crash (window would not open at
all).

### 3.2 "Everything times out at 15s"

15s is `DEFAULT_TIMEOUT_MS` (ssh-transport.cjs) and the renderer's
`AbortSignal.timeout(15000)` for hub HTTP. A 15s timeout means the
operation never completed — it says **nothing about auth**. Password
verification is millisecond-fast; a 15s hang is earlier: TCP
connect, SSH handshake, or server-side PAM hanging on a network
backend (LDAP/Kerberos). Never treat "timeout" as "wrong password".

### 3.3 Password auth fails but publickey works (or vice versa)

Server-side auth methods are independent. `PasswordAuthentication
no`, `AuthenticationMethods publickey`, or `UsePAM yes` with
keyboard-interactive-only are all common. Check from the client:

```bash
ssh -v -p <port> -o PreferredAuthentications=none -o PubkeyAuthentication=no user@host 2>&1 \
  | grep "Authentications that can continue"
```

If the list has no `password`, password auth can never succeed there.
Since 2026-07-22 the app sets `tryKeyboard: true` for password auth
and auto-answers prompts with the password — PAM/k-i-only servers
now work. Note this failure mode is an *instant* error
("All configured authentication methods failed"), NOT a timeout.

### 3.4 Stuck at "Connecting terminal to xxx" / "Re-attaching…"

Two stacked bugs, both fixed 2026-07-22 but know the shape:

1. The renderer had **no overall timeout** around `bridge.open`
   (agent-console.js). Any unbounded stall in main (half-dead pooled
   socket, wedged hub op) left the status forever. And the status
   line is **shared across agents** — a hung attach on agent B left
   "Connecting B" visible while agent A worked fine.
2. The main-side stall: a pooled connection that is stale-ready
   (state=ready, socket actually dead after sleep/NAT-drop). The
   channel open then waits for ssh2 keepalive death detection
   (15s × 3 = 45s) or the open timer (was 60s, now 15s).

Fixes in place: open-failure/timeout drops the pool entry
(`_dropEntry(key, 'open_failed'/'open_timeout')`), renderer retries
transient failures once, attach status is owned per agent,
`powerMonitor.on('resume')` drops *idle* pool entries.

If you still see a permanent hang: reproduce with the CDP harness
(§5) and look at what `term:open` is awaiting in main.

### 3.5 "terminal disconnected (exit 1) — reconnecting…"

The channel opened and then closed. The suffix is the diagnosis:

- `(exit 1)` — the remote `camc attach` exited. Causes: agent record
  gone (`agent '<id>' not found`), stale tmux socket, ancient remote
  camc. The app's agent list is a *synced cache* — it drifts when
  records are deleted/healed remotely. Verify on the node:

  ```bash
  ~/.cam/camc list
  ~/.cam/camc attach <agentId>     # run manually, read the error
  ```

- `: <ssh2 error>` / `(exit 255)` — transport drop (network).
- No suffix within seconds of attaching — the reuse race: main's
  `termOpen` reuse branch returned a not-yet-detected dead channel.

Since 2026-07-22 unexpected drops auto-reconnect with bounded
backoff (transport: 0/2/5/12/30s; remote exit: 0/3s), then fall back
to the keystroke prompt.

### 3.6 Window size falls to ~10 columns

tmux sizes a window to the smallest attached client. The app clamps
every size it sends to ≥40 cols × ≥4 rows (5 clamps:
`_terminalOpenSize`, `_terminalResizeSize`, renderer fit guard,
Path-C reconnect, `_terminalRepairCommand`). So a ~10-col window
means a **foreign client**: an old app build on another device, a
manual `tmux attach` from a small terminal, or a zombie client left
by a dropped connection.

Forensics — the repair detaches <40-col clients and logs the culprit:

```bash
# on the REMOTE node, when it happens (before reattaching):
tmux -S /tmp/cam-sockets/cam-<id>.sock list-clients -t cam-<id> \
  -F '#{client_name} #{client_width}x#{client_height}'

# on the DESKTOP side, the repair's evidence log:
#   Windows: %APPDATA%/cam-desktop/cam-desktop.log
#   macOS:   ~/Library/Application Support/cam-desktop/cam-desktop.log
# look for: REPAIR_DETACHED tiny-client name=<tty> size=<WxH>
```

The client name/tty identifies the source device (cross-reference
`who` on the node). There is also a *local* lookalike: the renderer's
fit passes (raf/raf/80ms/220ms, now +700ms) can all miss during slow
layouts, leaving stale local geometry while the remote is fine —
distinguish via the `list-clients` command above.

### 3.7 Renderer fully dead (buttons unresponsive, pages stuck)

Happened once (commit `a666e33`): one TypeError in a shared render
pass (`renderTerminalTabs(null)`) killed the whole renderer. When
the UI is *completely* dead, suspect a renderer exception, not the
network — open the CDP console (§5) and read `PAGE-EXCEPTION`.

### 3.8 "It worked yesterday, today the host is unreachable"

Before blaming code: the user's home ISP (telecom) blocked inbound
port 22 overnight once; the node moved 22→2222→6000. Always verify
the port from another network (`nc -vz host port` / `ssh -v`) before
digging into the app.

## 4. Platform notes

### tmux versions (2.7 / 3.2a / 3.4)

There is **no version detection anywhere** by design. All control
commands use the oldest portable form, verified against tmux 2.7
source and live-tested on 2.7 (Rocky 8.9), 3.2a (hlren), 3.4 (prgn):

- `display-message -p -t <session> fmt` — never `-c <tty>` with a
  format (usage error on tmux < 3.3; this was the "tab strip never
  appears on hlren" root cause, fixed in `a5a3e35`).
- Size repair uses a short-lived control-mode client +
  `refresh-client -C w,h` because tmux 2.7 has no `resize-window`.
- If a future feature needs a newer primitive (e.g. `window-size
  latest`, tmux ≥ 3.1), use **capability probing** (try new form,
  fall back), not version-string comparison — downstreams backport.

### tmux binary resolution

- Agent creation records the *actual* binary + version into the agent
  record (`tmux_bin`, `tmux_version`). Desktop control commands
  prefer the record — always matching the server binary.
- Resolution order (since `347458c`, env-first): effective runtime
  PATH → `/bin/tmux` fallback. Older builds were golden-first
  (`/bin/tmux` preferred). The desktop bundles `src/camc` via
  electron-builder `extraResources` — if you change camc, rebuild and
  **sync `dist/camc` → `src/camc`** or the packaged app keeps
  shipping the old build (this bit us once).
- Remaining bare-PATH spots: `camc attach` (`os.execvp("tmux", …)`)
  and the repair Python (`"tmux"`). A PATH tmux whose version differs
  from the session's server causes protocol-mismatch attach failures.

### The renderer clamps and guards (do not remove)

- Fit/resize guards: `TERMINAL_MIN_NOTIFY_COLS = 40`,
  `TERMINAL_MIN_NOTIFY_WIDTH = 320`, rows ≥ 4. Hidden/parked panes
  must never propagate a size to the PTY.
- Cached tabs are parked off-viewport (`left: -10000px`), never
  `display:none` — parking preserves geometry and avoids reflow.
- Terminal and exec traffic use separate pools so an exec timeout can
  never kill an attached terminal (tab semantics).

## 5. Diagnostic playbook (in order)

**Step 0 — reproduce against the right layer.** Is rich/plain output
working? If yes, the exec pool and remote camc are fine; the problem
is the terminal pool / PTY path only.

**Step 1 — is the hub up?** `netstat` for 8420-8429 (§3.1). If the
top status cycles checking→disconnected, this is the first thing to
check — it is not an SSH problem at all.

**Step 2 — read the renderer console.** The packaged app has no
console; dev-run with CDP instead:

```bash
export PATH="<repo>/.tools/node:$PATH"      # portable node 20
cd apps/cam-desktop
./node_modules/.bin/electron . --remote-debugging-port=9222 &
node --experimental-websocket <repo>/.tools/cdp-drive.cjs   # or /tmp/cdp-listen.cjs pattern
```

`cdp-*.cjs` prints `PAGE-EXCEPTION` / `PAGE-ERROR` / `PAGE-WARNING`.
Most renderer-side bugs are visible here in seconds. Or simply run
`npm run test:smoke` — it automates the boot-chain part.

**Step 3 — test the remote exactly the way the app does:**

```bash
ssh    -p <port> user@host "~/.cam/camc list"              # exec path (rich)
ssh -t -p <port> user@host "~/.cam/camc attach <agentId>"  # PTY path (terminal)
ssh -v -p <port> user@host 2>&1 | grep -E "Authentications|debug1: Connecting"
```

Whichever fails names the layer. Remote shells are often **csh** —
wrap complex commands: `echo <base64> | base64 -d | bash`.

**Step 4 — tmux state on the node:**

```bash
~/.cam/camc list                                              # records
ls /tmp/cam-sockets/                                          # sockets
tmux -S /tmp/cam-sockets/cam-<id>.sock list-clients -t cam-<id> \
  -F '#{client_name} #{client_width}x#{client_height}'        # clients + sizes
~/.cam/camc --json status <id> | grep -iE 'tmux|socket|session'  # metadata present?
```

**Step 5 — desktop-side logs:**

- `cam-desktop.log` in userData (§3.6 for paths): REPAIR_DETACHED,
  OS-resume pool drops.
- `console.warn` from main goes nowhere in a packaged app — on macOS
  run `"/Applications/CAM Desktop.app/Contents/MacOS/CAM Desktop"`
  from a terminal to see it live.

**Step 6 — when it looks like a regression:** `git log --oneline
--since="3 days ago"`, pick the boundary commit (e.g. `9253f82` is
the pre-2026-07-17 baseline), `git diff <sha>..HEAD -- <file>`.
Static analysis has been wrong here before — confirm with the CDP
harness or a dev-run before reverting anything.

## 6. macOS-specific checklist (the current mission)

In order of observed frequency:

1. **System sleep kills connections.** Cloud Macs sleep aggressively.
   `pmset -g | grep sleep`; fix: `sudo pmset -a sleep 0`. After a
   sleep, the app's pooled sockets are half-dead — since 2026-07-22
   the app drops idle pool entries on `powerMonitor 'resume'`, but
   preventing sleep is still the real fix.
2. **Local Network permission (macOS 15 Sequoia+).** LAN destinations
   are silently dropped for apps without the permission → connect
   hangs → 15s timeouts. System Settings → Privacy & Security →
   Local Network → enable CAM Desktop.
3. **IPv6 preference.** A DNS name with an AAAA record but a broken
   v6 path hangs until timeout. Compare `ssh -4` vs `ssh -6`. Node
   tries the first resolved address.
4. **safeStorage/Keychain prompts.** First use of remembered
   passwords prompts for "Electron Safe Storage" keychain access;
   denying it makes decrypt fail. Not a timeout — check for a hidden
   dialog behind windows.
5. **The link itself.** Home-broadband nodes (duckdns + port
   forwarding, ISP blocks ports) are inherently unstable. ping the
   node for an hour to see whether drops correlate with idle periods
   (sleep) or are random (link). Structural fixes: Tailscale/ZeroTier
   on both ends (use the tailnet IP in the node config), a relay
   (repo has `relay/relay.py`), or moving the node to a VPS.

## 7. Do-not-regress rules

- Release gate (RELEASE.md): `npm run lint:electron && npm run
  test:hub && npm run test:term && npm run test:start`, then
  `npm run test:smoke`, then a real-machine attach check. All green
  before moving the `cam-desktop-v0.2.0` tag.
- The test suites are **grep-assertion style**: they pin exact
  source strings. When you change behavior deliberately, update the
  pinned string in the same commit, and add a new assertion for the
  new behavior. Never delete an assertion to make a test pass.
- Preload: only `require('electron'|'events'|'timers'|'url')`.
- Size paths: never send <40 cols / <4 rows to the PTY or tmux.
- Never idle-close pooled SSH connections on desktop (mobile
  semantics differ); only drop on error/timeout/resume-idle.
- Minimal diffs. This codebase values its baseline — every fix above
  was 5-30 lines.

## 8. Environment cheat-sheet (this repo)

- Portable Node 20: `export PATH="<repo>/.tools/node:$PATH"`
  (Node is not on the system PATH).
- Tests run from `apps/cam-desktop`. `test:smoke` opens a real
  window for ~20s and needs a desktop session.
- Local MSI: `npm run build:win-msi` (5-30 min; if a build is
  killed, kill orphaned `electron-builder`/`light.exe` processes and
  `rm -rf dist/__msi-x64` before rebuilding).
- Release: commit → push origin (GitLab) + github →
  `git tag -f cam-desktop-v0.2.0 && git push -f github
  cam-desktop-v0.2.0` → watch
  `api.github.com/repos/orlunix/cam/actions/runs` → assets land on
  the single rolling Release via `--clobber`.
- GitHub token for API polling: `~/.my_tokens.yaml` (`GITHUB:` line),
  outside any repo. Never commit it.
- Remote shell is csh on the NVIDIA nodes: base64-wrap bash.
- Windows Python defaults to GBK: always `encoding='utf-8'` or
  `PYTHONUTF8=1`.
- Web UI JS is ES modules; `node --check` needs a `.mjs` copy.
