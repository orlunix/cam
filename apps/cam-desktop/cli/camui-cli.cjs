#!/usr/bin/env node
/**
 * camui — debug CLI for the CAM Desktop embedded Hub.
 *
 * Reuses the same embedded-hub.cjs / credential-store.cjs /
 * ssh-transport.cjs modules that Direct mode uses in Electron.
 * No new product layer, no second data model. Goal is a thin,
 * stable surface for testing the embedded Direct Hub from a
 * terminal before opening the Desktop UI.
 *
 * Source-CLI limitations (this file, invoked via `npm run cli`):
 *   - No Electron, therefore no `safeStorage`. Remembered
 *     passwords/passphrases CANNOT be stored from this CLI — `node
 *     add --remember` returns a clear error. Tests can pass a secret
 *     for a one-shot sync via --password-prompt or --password-env.
 *   - When packaged Electron eventually exposes
 *     `CAM Desktop.exe --cli ...`, the same command surface should
 *     run with full safeStorage support. That packaged path is
 *     deferred to a follow-up; this CLI is dev/debug-only.
 *
 * Command shape (mirrors `cam` conventions):
 *
 *   camui --help
 *   camui [--json] status
 *   camui start               [--json]
 *   camui node list           [--json]
 *   camui node add            --name N --host H --user U
 *                             [--port P]  (default 22)
 *                             [--path P]  (default /home/<user>)
 *                             [--auth key|password|agent]  (default key if --key-file else agent)
 *                             [--key-file FILE]
 *                             [--passphrase-prompt | --passphrase-env VAR]
 *                             [--remember]                 (Electron only; source CLI uses one-shot password sync)
 *                             [--env STR]
 *   camui node sync NAME      [--json]
 *                             [--password-prompt | --password-env VAR]
 *                             [--passphrase-prompt | --passphrase-env VAR]
 *   camui agent list          [--json]
 *   camui logs                [--json]
 *
 * Exit codes: 0 success, 1 usage/argument error, 2 backend/runtime
 * error. The renderer never imports this file.
 */

'use strict';

const path     = require('node:path');
const fs       = require('node:fs');
const os       = require('node:os');
const readline = require('node:readline');

const HUB_ROOT = path.resolve(__dirname, '..');                       // apps/cam-desktop
const ELECTRON = path.join(HUB_ROOT, 'electron');

const embeddedHub     = require(path.join(ELECTRON, 'embedded-hub.cjs'));
const credentialStore = require(path.join(ELECTRON, 'credential-store.cjs'));
const sshTransport    = require(path.join(ELECTRON, 'ssh-transport.cjs'));

const VERSION = require(path.join(HUB_ROOT, 'package.json')).version;

/* ─────────────── small arg parser ─────────────── */

const BOOLEAN_FLAGS = new Set([
  'help',
  'h',
  'version',
  'json',
  'remember',
  'password-prompt',
  'passphrase-prompt',
]);

function parseArgs(argv) {
  const out = { _: [], flags: {} };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq >= 0) {
        out.flags[a.slice(2, eq)] = a.slice(eq + 1);
      } else if (BOOLEAN_FLAGS.has(a.slice(2))) {
        out.flags[a.slice(2)] = true;
      } else {
        const next = argv[i + 1];
        if (next != null && !next.startsWith('--')) {
          out.flags[a.slice(2)] = next;
          i++;
        } else {
          out.flags[a.slice(2)] = true;
        }
      }
    } else {
      out._.push(a);
    }
  }
  return out;
}

function fail(msg, code = 1) {
  process.stderr.write(`camui: ${msg}\n`);
  process.exit(code);
}

function emit(obj, json) {
  if (json) {
    process.stdout.write(JSON.stringify(obj, null, 2) + '\n');
  } else {
    process.stdout.write(humanFormat(obj) + '\n');
  }
}

function humanFormat(obj) {
  if (obj == null) return '';
  if (typeof obj !== 'object') return String(obj);
  if (Array.isArray(obj)) return obj.map(humanFormat).join('\n');
  // simple key: value rendering
  return Object.entries(obj)
    .map(([k, v]) => {
      if (Array.isArray(v) || (v && typeof v === 'object')) {
        return `${k}:\n${indent(humanFormat(v), '  ')}`;
      }
      return `${k}: ${v == null ? '' : v}`;
    })
    .join('\n');
}
function indent(s, pad) { return String(s).split('\n').map(l => pad + l).join('\n'); }

/* ─────────────── help / version ─────────────── */

const HELP = `camui — CAM Desktop embedded Hub debug CLI (v${VERSION})

Usage:
  camui --help
  camui [--json] status
  camui start               [--json]               foreground; Ctrl+C to stop
  camui node list           [--json]
  camui node add            --name N --host H --user U
                            [--port 22] [--path /home/<user>]
                            [--auth key|password|agent]
                            [--key-file FILE]
                            [--passphrase-prompt | --passphrase-env VAR]
                            [--remember]            Electron only; source CLI uses one-shot password sync
                            [--env STR]
  camui node sync NAME      [--json]
                            [--password-prompt | --password-env VAR]
                            [--passphrase-prompt | --passphrase-env VAR]
  camui agent list          [--json]
  camui logs                [--json]

Notes:
  - --json may be placed before the command, matching 'cam --json ...',
    or after the subcommand where shown.
  - This source CLI shares state with Electron via the user-data
    directory (default: ${defaultDataDir()}). Override with
    CAMUI_DATA_DIR.
  - The source CLI cannot use Electron safeStorage, so it cannot
    persist passwords/passphrases. \`node add --auth password\`
    creates a one-shot context (machine.allow_one_shot=true,
    credential_saved=false) — Sync then requires a one-shot
    password on every call:
        camui node add  --auth password --name N --host H --user U
        camui node sync N --password-prompt          # or --password-env VAR
    Adding \`--remember\` from this CLI is refused because
    safeStorage is unavailable; use the Desktop Add Host UI for
    a persisted password.
  - --password-env / --passphrase-env are dev convenience ONLY;
    treat them like any other env-leaked secret. They are NEVER
    persisted by the Hub and never echoed in API responses.
  - \`camui start\` is foreground-only. The embedded Hub lives in
    that process; a separate \`camui ...\` invocation starts its
    own short-lived hub instead. There is no daemon mode in this
    source CLI; the packaged Electron \`--cli ...\` mode (future
    slice) will add cross-process lifecycle control.
`;

function defaultDataDir() {
  if (process.env.CAMUI_DATA_DIR) return process.env.CAMUI_DATA_DIR;
  // Mirror Electron's app.getPath('userData') on each OS, so the
  // CLI shares state with Electron when both run on the same host.
  const plat = process.platform;
  if (plat === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'cam-desktop');
  }
  if (plat === 'win32') {
    const base = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    return path.join(base, 'cam-desktop');
  }
  return path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'cam-desktop');
}

/* ─────────────── credential prompts ─────────────── */

function promptSecret(label) {
  return new Promise((resolve, reject) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
    // Suppress echo for the password line.
    const wasMuted = rl._writeToOutput;
    rl._writeToOutput = function () { /* swallow */ };
    process.stderr.write(`${label}: `);
    rl.question('', (answer) => {
      rl._writeToOutput = wasMuted;
      rl.close();
      process.stderr.write('\n');
      resolve(answer);
    });
    rl.on('error', reject);
  });
}

async function resolveSecret(flags, kind) {
  const promptKey = `${kind}-prompt`;
  const envKey    = `${kind}-env`;
  if (flags[promptKey]) return promptSecret(kind === 'password' ? 'Password' : 'Passphrase');
  if (flags[envKey])    {
    const v = process.env[String(flags[envKey])];
    if (v == null) fail(`env var ${flags[envKey]} is not set`);
    return v;
  }
  return null;
}

/* ─────────────── hub helpers ─────────────── */

let _hub = null;
let _hubInfo = null;
function _safeStorageFromEnv() {
  // Source CLI runs in plain Node; no safeStorage. Return a stub
  // that reports unavailable so the credential store correctly
  // refuses Remember.
  return {
    isEncryptionAvailable() { return false; },
    encryptString() { throw new Error('safe_storage_unavailable'); },
    decryptString() { return ''; },
  };
}
async function ensureHub() {
  if (_hub) return _hub;
  const dataDir = defaultDataDir();
  credentialStore.configure({ safeStorage: _safeStorageFromEnv(), dataDir });
  embeddedHub.configure({ credentialStore, sshTransport });
  const res = await embeddedHub.start({ dataDir });
  if (!res || res.ok !== true) {
    fail(`failed to start embedded Hub: ${res && res.error || 'unknown'}`, 2);
  }
  _hub     = embeddedHub;
  _hubInfo = res;
  return _hub;
}
async function teardownHub() {
  if (_hub) {
    try { await _hub.stop(); } catch {}
    _hub = null;
    _hubInfo = null;
  }
}

async function hubFetch(path_, opts = {}) {
  const init = {
    method: opts.method || 'GET',
    headers: {
      'Authorization': `Bearer ${_hubInfo.apiToken}`,
      'Content-Type':  'application/json',
    },
  };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const r = await fetch(`${_hubInfo.apiUrl}${path_}`, init);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) {}
  return { status: r.status, data };
}

/* ─────────────── commands ─────────────── */

async function cmdStatus(flags) {
  await ensureHub();
  const out = {
    runtime:  'embedded',
    apiUrl:   _hubInfo.apiUrl,
    dataDir:  defaultDataDir(),
    profile:  _hubInfo.state || null,
  };
  emit(out, !!flags.json);
}

async function cmdStart(flags) {
  await ensureHub();
  emit({
    started:  true,
    apiUrl:   _hubInfo.apiUrl,
    dataDir:  defaultDataDir(),
    mode:     'foreground',
  }, !!flags.json);
  // The embedded Hub is owned by THIS process: when this CLI exits,
  // the Hub dies with it. There is intentionally NO cross-process
  // `camui stop` in the source CLI — see the help text for why.
  // Press Ctrl+C to stop.
  if (!flags.json) {
    process.stderr.write(
      '\nEmbedded Hub is running in the foreground.\n' +
      'Press Ctrl+C to stop. (No daemon mode in source CLI.)\n',
    );
  }
  // Stop cleanly on SIGINT so the loopback socket is freed and any
  // pending store writes flush before we exit.
  process.on('SIGINT', async () => {
    try { await embeddedHub.stop(); } catch {}
    process.exit(0);
  });
  await new Promise(() => {});  // wait for SIGINT
}

async function cmdNodeList(flags) {
  await ensureHub();
  const r = await hubFetch('/api/contexts');
  if (r.status !== 200) fail(`/api/contexts: HTTP ${r.status}`, 2);
  emit({ contexts: r.data.contexts || [] }, !!flags.json);
}

async function cmdNodeAdd(flags) {
  if (!flags.name) fail('--name is required');
  if (!flags.host) fail('--host is required');
  if (!flags.user) fail('--user is required');
  const body = {
    name:       String(flags.name),
    path:       String(flags.path || `/home/${flags.user}`),
    host:       String(flags.host),
    user:       String(flags.user),
    port:       flags.port ? Number(flags.port) : 22,
    auth_method: String(flags.auth || (flags['key-file'] ? 'key' : 'agent')),
    key_file:    flags['key-file'] ? String(flags['key-file']) : '',
    env_setup:   flags.env ? String(flags.env) : '',
  };
  if (body.auth_method === 'password') {
    // Source CLI has no Electron safeStorage, so it cannot persist
    // a remembered password. Two valid CLI shapes:
    //   1. --remember: refused locally because safeStorage is
    //      unavailable. Tell the user to use Desktop UI or wait
    //      for packaged Electron --cli mode.
    //   2. (default): create the context with allow_one_shot=true,
    //      no stored credential. Each `camui node sync NAME` then
    //      requires --password-prompt or --password-env.
    if (flags.remember) {
      fail(
        '`camui node add --auth password --remember` requires Electron safeStorage,\n' +
        '       which is not available in this source CLI. Either:\n' +
        '         - add the password node from the Desktop Add Host UI (which uses safeStorage), OR\n' +
        '         - drop --remember to create a one-shot context here:\n' +
        '             camui node add --auth password ...        # records context with allow_one_shot=true; no stored password\n' +
        '             camui node sync NAME --password-prompt    # or --password-env VAR  (required every sync)',
      );
    }
    // One-shot mode: no password sent on create; allow_one_shot
    // recorded so the Hub knows this context's sync demands a
    // body password.
    body.allow_one_shot = true;
  } else if (body.auth_method === 'key') {
    const pp = await resolveSecret(flags, 'passphrase');
    if (pp != null) body.passphrase = pp;
    if (flags.remember) {
      if (!pp) fail('--remember passphrase requires --passphrase-prompt or --passphrase-env');
      body.remember_passphrase = true;
    }
  }
  await ensureHub();
  const r = await hubFetch('/api/contexts', { method: 'POST', body });
  if (r.status === 201) {
    emit({ added: r.data }, !!flags.json);
  } else {
    fail(`add failed: HTTP ${r.status} ${r.data && r.data.error || ''} ${r.data && r.data.detail || ''}`, 2);
  }
}

async function cmdNodeSync(positional, flags) {
  const name = positional[0];
  if (!name) fail('camui node sync NAME (positional arg required)');
  // One-shot credentials (CLI debug path). NEVER persisted by the
  // Hub, NEVER logged, NEVER echoed in the response body. Used:
  //   - on a one-shot password context (machine.allow_one_shot=true),
  //     where Sync REQUIRES --password-prompt or --password-env on
  //     every call; or
  //   - to override a persisted password (the body credential wins),
  //     useful when testing different passwords against a Remember-
  //     persisted context; or
  //   - on a key-auth context whose encrypted key passphrase wasn't
  //     remembered.
  const overrides = {};
  const pw = await resolveSecret(flags, 'password');
  if (pw != null) overrides.password = pw;
  const pp = await resolveSecret(flags, 'passphrase');
  if (pp != null) overrides.passphrase = pp;
  const opts = { method: 'POST' };
  if (overrides.password || overrides.passphrase) opts.body = overrides;
  await ensureHub();
  const r = await hubFetch(`/api/contexts/${encodeURIComponent(name)}/sync`, opts);
  if (r.status !== 200) fail(`sync HTTP ${r.status}`, 2);
  emit(r.data, !!flags.json);
}

async function cmdAgentList(flags) {
  await ensureHub();
  const r = await hubFetch('/api/agents');
  if (r.status !== 200) fail(`/api/agents: HTTP ${r.status}`, 2);
  emit({ agents: r.data.agents || [] }, !!flags.json);
}

async function cmdLogs(flags) {
  await ensureHub();
  // Logs come from the hub's in-memory ring buffer.
  const logs = embeddedHub.getLogs();
  emit(logs, !!flags.json);
}

/* ─────────────── dispatcher ─────────────── */

async function main() {
  const argv  = process.argv.slice(2);
  const parsed = parseArgs(argv);
  const flags = parsed.flags;
  const pos   = parsed._;

  if (flags.help || flags.h || flags.version || pos[0] === 'help' || pos.length === 0) {
    if (flags.version) {
      process.stdout.write(`camui ${VERSION}\n`);
      process.exit(0);
    }
    process.stdout.write(HELP);
    process.exit(0);
  }

  const verb = pos[0];
  const sub  = pos[1];

  try {
    switch (verb) {
      case 'status': await cmdStatus(flags); break;
      case 'start':  await cmdStart(flags);  break;
      case 'stop':
        fail('`camui stop` is not supported in the source CLI. The embedded Hub lives inside `camui start` — press Ctrl+C in that terminal.');
        break;
      case 'node':
        if (sub === 'list') await cmdNodeList(flags);
        else if (sub === 'add') await cmdNodeAdd(flags);
        else if (sub === 'sync') await cmdNodeSync(pos.slice(2), flags);
        else fail(`unknown node subcommand: ${sub || '(none)'}`);
        break;
      case 'agent':
        if (sub === 'list') await cmdAgentList(flags);
        else fail(`unknown agent subcommand: ${sub || '(none)'}`);
        break;
      case 'logs': await cmdLogs(flags); break;
      default: fail(`unknown command: ${verb}`); break;
    }
  } finally {
    if (verb !== 'start') await teardownHub();
  }
}

main().catch((e) => {
  process.stderr.write(`camui: fatal: ${e && e.stack || e}\n`);
  process.exit(2);
});
