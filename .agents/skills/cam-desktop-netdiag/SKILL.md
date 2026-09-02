---
name: cam-desktop-netdiag
description: Diagnose CAM Desktop "connection slow / keeps disconnecting / sync timeout" reports on a user's machine (macOS primary, Windows/Linux notes). Use when a user reports unstable SSH nodes, frequent terminal drops, slow attach, or sync timeouts. Provides a strict triage tree — app log mining, system-ssh baseline, packaged-runtime benchmark — to decide environment-vs-app and localize the fault. Read-only: never changes app config or code.
---

# CAM Desktop Network Diagnostics

Goal: given a user report of "node X is slow / disconnects / times out", produce a
**verdict with evidence**: environment (which segment) or app (which layer), plus the
numbers to prove it. Work read-only. Do not edit app config, do not toggle node
settings, do not "fix" anything — diagnose first.

The method below was built on a real case: app log showed exec RTTs of 1.5–9 s and
two hard connection deaths, while the same node from another machine measured
1.0–1.3 s warm. Root cause was the user's network path, not the server, not the app.
Every step here exists because it eliminated one suspect in that case.

## What CAM Desktop is (read this first)

An Electron app that manages remote AI coding agents over SSH:

- The **renderer** (UI) talks to an embedded **hub** — a plain HTTP server the main
  process binds to `127.0.0.1:8420+` with a per-launch bearer token.
- The hub holds pooled **SSH connections** (bundled `ssh2` library, one exec pool +
  one terminal pool per node) to each configured host.
- On each host the hub uploads and drives `~/.cam/camc`, a stdlib-only Python CLI
  that runs agents inside per-agent **tmux** sessions (socket `/tmp/cam-sockets/`).
  Terminals attach by streaming `camc attach <id>` over an SSH channel.

So every user-visible stall lives in exactly one of three places: the loopback hub
(rare — if the whole UI is fine, it is fine), the **SSH network path** (the usual
suspect — this skill's focus), or the **remote host** (login shell quirks, camc,
tmux). Knowing this shape is what makes the triage below fast.

## Step 0 — Locate the install, config and logs

Install locations (you need the binary path for Step 4):

| OS | default install | how to find |
|---|---|---|
| macOS | `/Applications/CAM Desktop.app` or `~/Applications/CAM Desktop.app` | `ls -d /Applications/CAM\ Desktop.app ~/Applications/CAM\ Desktop.app 2>/dev/null` or `mdfind kMDItemCFBundleIdentifier == 'com.hren.cam'` |
| Windows | `%LOCALAPPDATA%\Programs\CAM Desktop\` (per-user MSI) | `dir "%LOCALAPPDATA%\Programs\CAM Desktop"` |
| Linux | wherever the user put the AppImage | `ls ~/*.AppImage ~/Applications/*.AppImage 2>/dev/null` |

MAS (App Store) builds are sandboxed: their data dir is inside the container —
`~/Library/Containers/com.hren.cam.mas/Data/Library/Application Support/cam-desktop/`
(if that doesn't exist, fall back to the global path above). The Step 4 binary
recipe is the same with the installed `.app` path.

Per-user data directory (Electron `userData`; the dir name is `cam-desktop`,
from package.json `name` — NOT the productName):

| OS | dir |
|---|---|
| macOS | `~/Library/Application Support/cam-desktop/` |
| Windows | `%APPDATA%\cam-desktop\` |
| Linux | `~/.config/cam-desktop/` |

Inside:

- `embedded-hub.json` — all nodes. List them (python3 ships with macOS; jq works
  too if installed):
  ```bash
  python3 -c "
  import json
  d = json.load(open('embedded-hub.json'))
  for c in d.get('contexts', []):
      m = c.get('machine') or {}
      print('\t'.join(str(x or '') for x in [c.get('name'), m.get('host'), m.get('port'), m.get('user'), m.get('auth_method'), m.get('key_file'), m.get('jump')]))
  "
  ```
  Columns: name, host, port, user, auth_method, key_file, jump.
  `jump` = ProxyJump via another node; `auth_method` = key/password/agent;
  `env_setup` (not shown) = extra remote shell init.
- `embedded-hub-credentials.json` — passwords/passphrases, **encrypted (OS keychain
  via Electron safeStorage)**. Never try to read/decrypt. If a password-auth node
  must be tested, ask the user for the password or have them add a temporary key.
- `cam-desktop.log` — the diagnostics log (same content as in-app
  Settings → Diagnostics). A repo-root `debug.log` is dev-machine only, ignore it.

## Step 1 — Mine the log before touching the network

```bash
LOG=~/Library/Application\ Support/cam-desktop/cam-desktop.log
# warm exec latency distribution per host (the app's exec channel):
grep ' exec ' "$LOG" | grep ' ok ' | sed -E 's/.*exec ([^ ]+) ok ([0-9]+)ms.*/\1 \2/' | sort | awk '{h[$1]+=$2; n[$1]++; if($2>mx[$1])mx[$1]=$2} END{for(k in h) printf "%-55s n=%-4d avg=%dms max=%dms\n", k, n[k], h[k]/n[k], mx[k]}'
# connection deaths and causes:
grep -E 'terminal dropped|connect lost|connect ended|Timed out while waiting for handshake' "$LOG" | tail -40
# remote command failures (note the command that follows each):
grep 'remote_nonzero' "$LOG" | tail -10
# sync stalls:
grep 'idle-aborted' "$LOG" | tail -5
```

Healthy reference (measured 2026-08, healthy VPN paths to the same infra):

| metric | healthy | suspicious | bad |
|---|---|---|---|
| warm exec p50 (`list-clients`, `camc list`) | ≈ ping RTT + ≤300 ms (0.7–1.3 s at 200–250 ms RTT) | 1.5–3 s | >3 s or spikes >8 s |
| p95/p50 | < 2 | 2–4 | > 4 (packet loss signature) |
| cold handshake (`connect ready … in Nms`) | 2–4 s | 4–8 s | >8 s / `Timed out while waiting for handshake` |
| `terminal dropped (no-code)` | 0 in hours | — | any, esp. repeating every ~30–90 s |

Pattern recognition:

- Drops where **a new handshake also fails at the same moment** → the whole network
  path died → environment, below the app. The app cannot break TCP handshakes.
- `remote_nonzero` on `python3 - ... <<'PY'` while `bash -c ...` succeeds → remote
  login shell is csh/tcsh (heredoc breaks). Cosmetic repair-path issue, fixed in
  app 0.2.20; **not** a connectivity problem. Verify: `ssh user@host 'echo $SHELL'`.
- `idle-aborted … sync` with slow execs → sync is a victim of latency, not a cause.

## Step 2 — System-ssh baseline (the decisive experiment)

Run on the user's machine, against the reported node, with the user's own key
(`~/.ssh/id_rsa` or the node's `key_file`). This bypasses the app entirely:

```bash
H=pdx-container-xterm-034.prd.it.nvidia.com; P=4368; U=aqiu   # from embedded-hub.json
# 1. bare path
ping -c 50 $H
# 2. warm-connection loop == the app's exec channel (30 rounds)
ssh -o ControlMaster=yes -o ControlPath=/tmp/netdiag-cm -o ControlPersist=120 -p $P $U@$H -fN
for i in $(seq 1 30); do
  /usr/bin/time ssh -o ControlPath=/tmp/netdiag-cm -p $P $U@$H true 2>&1 | tail -1
  sleep 2
done
# 3. long-hold, 3 min == reproduces "terminal dropped" if the path kills connections
ssh -p $P $U@$H "bash -c 'for i in \$(seq 1 36); do date +%T; sleep 5; done'"
```

Verdict:

- **System ssh is also slow / drops** → ENVIRONMENT. Go to Step 3. The app is
  exonerated: its numbers were just measuring the same bad path.
- **System ssh is healthy but the app log is slow/droppy** → APP. Go to Step 4.

## Step 3 — Environment localization tree

Do these in order; each halves the suspect space. Stop when the fault segment is found.

1. **Hotspot A/B** (single most informative test): repeat Step 2 over a phone
   hotspot. Instantly better → fault is in home/office LAN, ISP, or VPN entry.
2. **Wired vs Wi-Fi**: repeat on Ethernet. Better → Wi-Fi (weak signal, 2.4 GHz
   interference, or macOS AWDL scans — AirDrop/Continuity periodically hijack the
   radio and produce regular RTT spikes every ~30–60 s).
3. **VPN transport**: corporate VPNs that fall back from UDP (DTLS/IPsec) to
   TCP/TLS produce exactly the "2–3× slower + stalls of 10 s+" shape. Check the
   client UI (AnyConnect: Statistics → Transport; GlobalProtect: Settings →
   Connection) or `scutil --nc list` + connect log. Fix = different gateway or
   ask IT to allow UDP.
4. **Time of day**: slow only in the evening → ISP international-egress
   congestion. Only fixable by VPN egress choice or off-peak use.
5. **Local interception**: system proxies (Clash/v2ray "enhanced mode"),
   EDR/AV (CrowdStrike et al.) scanning SSH, upstream saturated by backups/calls.
   Check `scutil --proxy`, security-software consoles, Activity Monitor network.
6. If still unresolved: `mtr -n -c 100 $H` (or `traceroute`) to see where RTT/loss
   starts. Attach output to the report.

## Step 4 — App-layer confirmation using the app's OWN runtime

This is the "is it really the app" hammer: run the benchmark through the packaged
app binary as a Node interpreter with the app's bundled ssh2 — same BoringSSL, same
ssh2 build, zero app logic. Asset `netdiag-bench.cjs` ships with this skill; if it
is missing, recreate it from Appendix A.

```bash
# macOS — find the .app path in Step 0 if it's not in the default spot:
APP="/Applications/CAM Desktop.app"
CAM_RESOURCES="$APP/Contents/Resources" ELECTRON_RUN_AS_NODE=1 \
  "$APP/Contents/MacOS/CAM Desktop" netdiag-bench.cjs \
  --host $H --port $P --user $U --key ~/.ssh/id_rsa --rounds 20 --hold 180
```

Windows (PowerShell):
```powershell
$env:ELECTRON_RUN_AS_NODE=1
$env:CAM_RESOURCES="$env:LOCALAPPDATA\Programs\CAM Desktop\resources"
& "$env:LOCALAPPDATA\Programs\CAM Desktop\CAM Desktop.exe" netdiag-bench.cjs `
  --host $H --port $P --user $U --key $env:USERPROFILE\.ssh\id_rsa
```

It prints `NETDIAG handshake …`, per-round warm times, a `p50/p95/max` summary, and
`drops=N` for the hold phase. Keepalive/readyTimeout match the app's own settings.

Interpretation:

- **bench healthy + app log slow** → the fault is in app *logic/config*, not the
  network stack. Collect and report: app version (from package.json or About),
  the node record from `embedded-hub.json` (jump host? env_setup? auth method?),
  `cam-desktop.log` excerpts, and bench output. Suspects then: an old app version
  (pre-0.2.20 csh repair noise), a misconfigured jump chain, or attach-path
  serialization (look for `tmux discovery queue wait exceeded 15s` in the log).
- **bench also slow/droppy** → still environment, but something treats this
  binary differently (EDR/AV whitelists `ssh` but scans the app). Compare with
  Step 2 results and check security software.

## Report template

```
node: user@host:port (auth: key, jump: none)   app version: 0.2.x   os: macOS 15.x
log mining: exec avg/max per host, drop events + times, handshake timeouts
system ssh: ping avg/loss; warm p50/p95; 3-min hold drops
packaged bench: handshake ms; warm p50/p95; hold drops
verdict: environment (segment: ___) / app (layer: ___)
fixes applied / recommended: ___
```

## Appendix A — netdiag-bench.cjs

Canonical copy is the asset file next to this SKILL.md (use it when present).
If you received this skill as pasted text only, recreate the file verbatim:

```js
#!/usr/bin/env node
/* netdiag-bench.cjs — CAM Desktop network diagnostic benchmark.
 * warm: --rounds execs over ONE pooled connection (like the app's exec channel)
 * hold: keep the connection --hold seconds, exec every 5s, count drops
 * Run via the app's own runtime (ELECTRON_RUN_AS_NODE=1 + CAM_RESOURCES) for
 * the decisive app-vs-environment comparison. */
'use strict';

const fs = require('fs');
const path = require('path');

function loadSsh2() {
  try { return require('ssh2'); } catch (_) { /* not resolvable yet */ }
  const Module = require('module');
  const res = process.env.CAM_RESOURCES || path.join(__dirname, 'resources');
  process.env.NODE_PATH = [
    path.join(res, 'app.asar', 'node_modules'),
    path.join(res, 'app.asar.unpacked', 'node_modules'),
  ].join(path.delimiter);
  Module._initPaths();
  return require('ssh2');
}

function parseArgs(argv) {
  const a = { port: 22, rounds: 20, hold: 180, cmd: 'true' };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i], v = argv[i + 1];
    if (k === '--host') a.host = v, i++;
    else if (k === '--port') a.port = parseInt(v, 10), i++;
    else if (k === '--user') a.user = v, i++;
    else if (k === '--key') a.key = v, i++;
    else if (k === '--password') a.password = v, i++;
    else if (k === '--agent') a.agent = true;
    else if (k === '--rounds') a.rounds = parseInt(v, 10), i++;
    else if (k === '--hold') a.hold = parseInt(v, 10), i++;
    else if (k === '--cmd') a.cmd = v, i++;
    else { console.error(`unknown arg: ${k}`); process.exit(2); }
  }
  if (!a.host || !a.user || (!a.key && !a.password && !a.agent)) {
    console.error('need --host, --user, and one of --key/--password/--agent');
    process.exit(2);
  }
  return a;
}

function pct(sorted, p) {
  if (!sorted.length) return 0;
  return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
}

async function main() {
  const a = parseArgs(process.argv);
  const { Client } = loadSsh2();

  const opts = {
    host: a.host,
    port: a.port,
    username: a.user,
    readyTimeout: 20000,        // same as app (ssh-transport.cjs)
    keepaliveInterval: 15000,   // same as app
    keepaliveCountMax: 3,       // same as app
  };
  if (a.key) opts.privateKey = fs.readFileSync(a.key);
  if (a.password) opts.password = a.password;
  if (a.agent) opts.agent = process.env.SSH_AUTH_SOCK;

  const conn = new Client();
  const t0 = Date.now();
  let dropped = 0;
  conn.on('close', () => {
    dropped++;
    console.log(`NETDIAG drop event=close at=${Date.now() - t0}ms`);
  });
  conn.on('error', (e) => console.log(`NETDIAG conn error: ${e.message}`));

  const execOnce = () => new Promise((resolve) => {
    const s = Date.now();
    conn.exec(a.cmd, (err, stream) => {
      if (err) return resolve({ ms: Date.now() - s, ok: false, err: err.message });
      stream.on('close', (code) => resolve({ ms: Date.now() - s, ok: code === 0, err: code ? `exit=${code}` : '' }));
      stream.resume();
      stream.stderr.resume();
    });
  });

  console.log(`NETDIAG target=${a.user}@${a.host}:${a.port} runtime=${process.version}`);
  try {
    await new Promise((res, rej) => { conn.once('ready', res); conn.connect(opts); });
  } catch (e) {
    console.log(`NETDIAG handshake FAIL: ${e.message}`);
    process.exit(1);
  }
  console.log(`NETDIAG handshake ok ${Date.now() - t0}ms (cold; 2-4s typical at 200-300ms RTT, >8s = lossy)`);

  const times = [];
  let fails = 0;
  for (let i = 1; i <= a.rounds; i++) {
    const r = await execOnce();
    if (r.ok) times.push(r.ms); else fails++;
    console.log(`NETDIAG warm ${i}/${a.rounds}: ${r.ms}ms${r.ok ? '' : ' FAIL ' + r.err}`);
    await new Promise((r2) => setTimeout(r2, 1000));
  }
  times.sort((x, y) => x - y);
  console.log(`NETDIAG warm summary rounds=${a.rounds} fails=${fails} min=${pct(times, 0)}ms p50=${pct(times, 0.5)}ms p95=${pct(times, 0.95)}ms max=${pct(times, 1)}ms`);

  console.log(`NETDIAG hold ${a.hold}s (exec every 5s; any close = app "terminal dropped")`);
  const holdEnd = Date.now() + a.hold * 1000;
  let holdFails = 0;
  while (Date.now() < holdEnd && dropped === 0) {
    const r = await execOnce();
    if (!r.ok) { holdFails++; console.log(`NETDIAG hold exec FAIL at=${Date.now() - t0}ms: ${r.err}`); }
    await new Promise((r2) => setTimeout(r2, 5000));
  }
  console.log(`NETDIAG hold summary drops=${dropped} execFails=${holdFails} (healthy: drops=0)`);

  try { conn.end(); } catch (_) {}
  process.exit(dropped === 0 && fails === 0 ? 0 : 1);
}

main().catch((e) => { console.error('NETDIAG fatal:', e.message); process.exit(1); });
```
