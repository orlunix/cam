#!/usr/bin/env node
/* netdiag-bench.cjs — CAM Desktop network diagnostic benchmark.
 *
 * Two phases against one SSH node:
 *   warm : --rounds execs over ONE pooled connection (same shape as the app's
 *          exec channel), prints per-round ms + min/p50/p95/max
 *   hold : keep the connection --hold seconds, exec every 5s, count drops
 *          (a drop here reproduces the app's "terminal dropped (no-code)")
 *
 * Plain node:
 *   node netdiag-bench.cjs --host H [--port 22] --user U \
 *        [--key FILE | --password PW | --agent] [--rounds 20] [--hold 180]
 *
 * Decisive app-vs-environment run — the app's OWN bundled ssh2 + runtime:
 *   macOS:
 *     CAM_RESOURCES="/Applications/CAM Desktop.app/Contents/Resources" \
 *     ELECTRON_RUN_AS_NODE=1 \
 *     "/Applications/CAM Desktop.app/Contents/MacOS/CAM Desktop" netdiag-bench.cjs ...
 *   Windows (PowerShell, install dir e.g. %LOCALAPPDATA%\Programs\CAM Desktop):
 *     $env:ELECTRON_RUN_AS_NODE=1
 *     $env:CAM_RESOURCES="$env:LOCALAPPDATA\Programs\CAM Desktop\resources"
 *     & "$env:LOCALAPPDATA\Programs\CAM Desktop\CAM Desktop.exe" netdiag-bench.cjs ...
 *
 * Keepalive/readyTimeout mirror the app's ssh-transport defaults so numbers
 * are directly comparable with `cam-desktop.log` exec timings.
 */
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

  console.log(`NETDIAG target=${a.user}@${a.host}:${a.port} runtime=${process.version} ssh2=app-bundled-or-system`);
  try {
    await new Promise((res, rej) => { conn.once('ready', res); conn.connect(opts); });
  } catch (e) {
    console.log(`NETDIAG handshake FAIL: ${e.message}`);
    process.exit(1);
  }
  const handshakeMs = Date.now() - t0;
  console.log(`NETDIAG handshake ok ${handshakeMs}ms (cold; 2-4s typical at 200-300ms RTT, >8s = lossy)`);

  // ---- phase warm ----
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
  console.log(`NETDIAG warm verdict: p50 should be ~= ping RTT + <=300ms; p95/p50 > 2 suggests packet loss`);

  // ---- phase hold ----
  console.log(`NETDIAG hold ${a.hold}s (exec every 5s; any close event = reproduces app "terminal dropped")`);
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
