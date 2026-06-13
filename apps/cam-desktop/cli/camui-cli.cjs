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
const crypto   = require('node:crypto');

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
  'show-token',
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
                            [--relay-url ws://host:port]
                            [--relay-token TOKEN]
                            [--api-token TOKEN]    stable CAM API token (also CAMUI_API_TOKEN env)
                            [--profile NAME]       read/write ~/.cam/camui/relay/<NAME>/profile.json
                            [--show-token]         debug: print CAM API token for relay clients
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
// CAM-DESK-REMOTE-012 (2026-06-12): resolve a STABLE cam_api_token for
// the embedded Hub so source restarts don't churn the bearer Desktop /
// mobile / relay forwarding rely on. Order of precedence:
//   1. --api-token <TOKEN>     (explicit per-invocation override)
//   2. CAMUI_API_TOKEN env     (CI / container friendly)
//   3. --profile <NAME>        (persistent: reads / writes
//                               ~/.cam/camui/relay/<NAME>/profile.json,
//                               generating cam_api_token once on first
//                               use, file mode 0600)
//   4. (no override) fall through to embedded-hub's genToken()
// Full tokens are never logged. Diagnostics use sha256:<24hex>.
function _tokenFingerprint(tok) {
  if (!tok) return '';
  return 'sha256:' + crypto.createHash('sha256').update(tok).digest('hex').slice(0, 24);
}

// Allowed profile names: alphanumerics + . _ -. No slashes, no .. so
// --profile cannot escape the camui/relay/ root via traversal. Empty
// names and pure-dot names ('.', '..') also rejected.
const _SAFE_PROFILE_RE = /^[A-Za-z0-9._-]+$/;

function _assertSafeProfileName(name) {
  const s = String(name == null ? '' : name);
  if (!s || s === '.' || s === '..' || !_SAFE_PROFILE_RE.test(s)) {
    fail(
      `invalid --profile name ${JSON.stringify(name)}: ` +
      'must match /^[A-Za-z0-9._-]+$/ (no slashes, no traversal)',
    );
  }
  return s;
}

function _relayProfileDir(name) {
  return path.join(os.homedir(), '.cam', 'camui', 'relay',
                   _assertSafeProfileName(name));
}

function _relayProfileFile(name) {
  return path.join(_relayProfileDir(name), 'profile.json');
}

function _loadOrCreateProfile(name) {
  const dir = _relayProfileDir(name);
  const file = _relayProfileFile(name);
  // Always (re-)tighten dir to 0700 — mkdirSync's `mode` is ignored
  // when the dir already exists, and an older install may have left
  // it group/world-readable. Same defensive pattern is then applied
  // to the file after read/write.
  try { fs.mkdirSync(dir, { recursive: true, mode: 0o700 }); } catch (_) {}
  try { fs.chmodSync(dir, 0o700); } catch (_) {}
  let profile = {};
  if (fs.existsSync(file)) {
    try { profile = JSON.parse(fs.readFileSync(file, 'utf-8')); }
    catch (_) { profile = {}; }
    // Defensive re-chmod on read so a previously world-readable
    // profile gets tightened even if we never need to write again
    // this run.
    try { fs.chmodSync(file, 0o600); } catch (_) {}
  }
  if (!profile.cam_api_token) {
    // Generate once and persist with mode 0600.
    profile.cam_api_token = crypto.randomBytes(24).toString('base64url');
    fs.writeFileSync(file, JSON.stringify(profile, null, 2), { mode: 0o600 });
    try { fs.chmodSync(file, 0o600); } catch (_) {}
  }
  return { profile, file };
}

function _resolveApiToken(flags) {
  if (flags['api-token']) {
    return { token: String(flags['api-token']), source: 'flag' };
  }
  if (process.env.CAMUI_API_TOKEN) {
    return { token: String(process.env.CAMUI_API_TOKEN), source: 'env' };
  }
  if (flags['profile']) {
    const { profile, file } = _loadOrCreateProfile(flags['profile']);
    return { token: profile.cam_api_token, source: `profile:${file}` };
  }
  return { token: null, source: 'auto-generated' };
}

async function ensureHub(flags = {}) {
  if (_hub) return _hub;
  const dataDir = defaultDataDir();
  credentialStore.configure({ safeStorage: _safeStorageFromEnv(), dataDir });
  embeddedHub.configure({ credentialStore, sshTransport });
  const { token: apiToken, source: tokenSource } = _resolveApiToken(flags);
  const res = await embeddedHub.start({ dataDir, apiToken });
  if (!res || res.ok !== true) {
    fail(`failed to start embedded Hub: ${res && res.error || 'unknown'}`, 2);
  }
  _hub     = embeddedHub;
  _hubInfo = res;
  // Stash the resolution source for diagnostics in `cmdStatus` /
  // `cmdStart`. Use the fingerprint, never the raw token.
  _hubInfo.apiTokenSource      = tokenSource;
  _hubInfo.apiTokenFingerprint = _tokenFingerprint(res.apiToken);
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


/* ─────────────── relay connector (server side) ─────────────── */

function normalizeRelayUrl(raw) {
  if (!raw) return '';
  let url = String(raw).trim().replace(/\/+$/, '');
  if (!url) return '';
  if (url.startsWith('http://')) url = 'ws://' + url.slice('http://'.length);
  else if (url.startsWith('https://')) url = 'wss://' + url.slice('https://'.length);
  if (!/^wss?:\/\//i.test(url)) url = 'ws://' + url;
  return url;
}

function getHeader(headers, name) {
  const want = String(name || '').toLowerCase();
  for (const [k, v] of Object.entries(headers || {})) {
    if (String(k).toLowerCase() === want) return String(v || '');
  }
  return '';
}

function relayAuthorized(headers) {
  const auth = getHeader(headers, 'authorization');
  return auth === `Bearer ${_hubInfo.apiToken}`;
}

function relayHttpResponse(id, status, bodyObj, headers = {}) {
  const body = typeof bodyObj === 'string' ? bodyObj : JSON.stringify(bodyObj || {});
  return {
    id,
    status,
    headers: { 'content-type': 'application/json', ...headers },
    body,
  };
}

async function relayFetch(req) {
  const method = String(req.method || 'GET').toUpperCase();
  const path_ = String(req.path || '/');
  const headers = req.headers || {};
  const body = req.body == null ? '' : String(req.body);

  if (method === 'WS') {
    return relayHttpResponse(req.id, 501, { error: 'ws_not_supported', detail: 'camui CLI relay supports REST polling; /api/ws is not implemented in this source CLI connector yet.' });
  }
  if (!path_.startsWith('/api/')) {
    return relayHttpResponse(req.id, 404, { error: 'not_found' });
  }
  if (!relayAuthorized(headers)) {
    return relayHttpResponse(req.id, 401, { error: 'unauthorized', detail: 'missing or invalid CAM API token' });
  }

  const init = {
    method,
    headers: {
      'Authorization': `Bearer ${_hubInfo.apiToken}`,
      'Content-Type': getHeader(headers, 'content-type') || 'application/json',
    },
  };
  if (!['GET', 'HEAD'].includes(method) && body) init.body = body;

  try {
    const r = await fetch(`${_hubInfo.apiUrl}${path_}`, init);
    const text = await r.text();
    return {
      id: req.id,
      status: r.status,
      headers: { 'content-type': r.headers.get('content-type') || 'application/json' },
      body: text,
    };
  } catch (e) {
    return relayHttpResponse(req.id, 502, { error: 'hub_fetch_failed', detail: e && e.message || String(e) });
  }
}

function relayUrlWithAuth(relayUrl, relayToken, sid) {
  const url = new URL(`${relayUrl}/server`);
  if (sid) url.searchParams.set('sid', sid);
  if (relayToken) url.searchParams.set('token', relayToken);
  return url.toString();
}

function startRelayConnector({ relayUrl, relayToken, json = false }) {
  const baseUrl = normalizeRelayUrl(relayUrl);
  if (!baseUrl) return { stop() {} };
  if (typeof WebSocket !== 'function') {
    fail('this Node runtime does not provide global WebSocket; use Node 20+ or install a WebSocket client', 2);
  }

  let stopped = false;
  let ws = null;
  let retryMs = 1000;
  const sid = crypto.randomUUID ? crypto.randomUUID() : crypto.randomBytes(16).toString('hex');

  function logRelay(msg) {
    if (!json) process.stderr.write(`[relay] ${msg}\n`);
  }

  function connect() {
    if (stopped) return;
    const url = relayUrlWithAuth(baseUrl, relayToken, sid);
    logRelay(`connecting ${baseUrl}`);
    ws = new WebSocket(url);

    ws.addEventListener('open', () => {
      retryMs = 1000;
      logRelay('connected');
    });

    ws.addEventListener('message', (ev) => {
      void (async () => {
        let text = '';
        if (typeof ev.data === 'string') text = ev.data;
        else if (ev.data instanceof ArrayBuffer) text = Buffer.from(ev.data).toString('utf8');
        else if (Buffer.isBuffer(ev.data)) text = ev.data.toString('utf8');
        else text = String(ev.data || '');

        let req = null;
        try { req = JSON.parse(text); }
        catch (_) { return; }
        const resp = await relayFetch(req);
        try {
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(resp));
        } catch (e) {
          logRelay(`send response failed: ${e && e.message || e}`);
        }
      })();
    });

    ws.addEventListener('close', (ev) => {
      if (stopped) return;
      const reason = ev && ev.reason ? ` (${ev.reason})` : '';
      logRelay(`disconnected${reason}; reconnecting in ${Math.round(retryMs / 1000)}s`);
      const wait = retryMs;
      retryMs = Math.min(retryMs * 2, 60000);
      const t = setTimeout(connect, wait);
      if (typeof t.unref === 'function') t.unref();
    });

    ws.addEventListener('error', (ev) => {
      const err = ev && (ev.error || ev.message) || 'connection error';
      logRelay(String(err));
    });
  }

  connect();
  return {
    stop() {
      stopped = true;
      try { if (ws) ws.close(); } catch {}
    },
    sid,
    url: baseUrl,
  };
}

/* ─────────────── commands ─────────────── */

async function cmdStatus(flags) {
  await ensureHub(flags);
  const out = {
    runtime:  'embedded',
    apiUrl:   _hubInfo.apiUrl,
    dataDir:  defaultDataDir(),
    profile:  _hubInfo.state || null,
  };
  emit(out, !!flags.json);
}

async function cmdStart(flags) {
  await ensureHub(flags);
  const relayUrl = normalizeRelayUrl(flags['relay-url'] || '');
  const relay = relayUrl ? startRelayConnector({ relayUrl, relayToken: flags['relay-token'] || '', json: !!flags.json }) : null;
  const out = {
    started:  true,
    apiUrl:   _hubInfo.apiUrl,
    dataDir:  defaultDataDir(),
    mode:     'foreground',
    relayUrl: relayUrl || '',
    relaySid: relay && relay.sid || '',
  };
  // Always surface the (sha256-truncated) fingerprint + source so
  // diagnostics can confirm which token the Hub adopted without ever
  // leaking the raw secret. --show-token still opts into the raw value.
  out.apiTokenSource      = _hubInfo.apiTokenSource;
  out.apiTokenFingerprint = _hubInfo.apiTokenFingerprint;
  if (flags['show-token']) out.apiToken = _hubInfo.apiToken;
  emit(out, !!flags.json);
  // The embedded Hub is owned by THIS process: when this CLI exits,
  // the Hub dies with it. There is intentionally NO cross-process
  // `camui stop` in the source CLI — see the help text for why.
  // Press Ctrl+C to stop.
  if (!flags.json) {
    process.stderr.write(
      '\nEmbedded Hub is running in the foreground.\n' +
      (relayUrl ? `Relay connector: ${relayUrl}\n` : '') +
      `CAM API token source: ${_hubInfo.apiTokenSource} (fingerprint ${_hubInfo.apiTokenFingerprint})\n` +
      (flags['show-token']
        ? `CAM API token: ${_hubInfo.apiToken}\n`
        : 'CAM API token is profile-managed; the relay injects it on /api/* forwarding.\n'
          + 'Use --show-token to print the raw token (debugging only).\n') +
      'Press Ctrl+C to stop. (No daemon mode in source CLI.)\n',
    );
  }
  // Stop cleanly on SIGINT so the loopback socket is freed and any
  // pending store writes flush before we exit.
  process.on('SIGINT', async () => {
    try { relay && relay.stop && relay.stop(); } catch {}
    try { await embeddedHub.stop(); } catch {}
    process.exit(0);
  });
  await new Promise(() => {});  // wait for SIGINT
}
async function cmdNodeList(flags) {
  await ensureHub(flags);
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
  await ensureHub(flags);
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
  await ensureHub(flags);
  const r = await hubFetch(`/api/contexts/${encodeURIComponent(name)}/sync`, opts);
  if (r.status !== 200) fail(`sync HTTP ${r.status}`, 2);
  emit(r.data, !!flags.json);
}

async function cmdAgentList(flags) {
  await ensureHub(flags);
  const r = await hubFetch('/api/agents');
  if (r.status !== 200) fail(`/api/agents: HTTP ${r.status}`, 2);
  emit({ agents: r.data.agents || [] }, !!flags.json);
}

async function cmdLogs(flags) {
  await ensureHub(flags);
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
