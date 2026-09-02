'use strict';

/* AssistantHost — the one app-side surface for the CAM Desktop
 * assistant (design: docs/desktop/assistant-design.md).
 *
 * Owns the assistant child process: the bundled pi agent
 * (extensions/vendor/cam-assist/dist/cam-assist.js) spawned via
 * `ELECTRON_RUN_AS_NODE=1 <own binary>` — the app binary is the Node
 * runtime, so there is no download and no per-platform matrix.
 *
 * Config split:
 *   - apiUrl + model  → <userData>/ext-data/assistant/config.json
 *     (plaintext, boring)
 *   - LLM token       → credential store (safeStorage, encrypted at
 *     rest), ref "assistant:llm-token" — never written plaintext
 *
 * The renderer (assistant extension view) talks to this host through
 * the scoped assistant:* IPC family; output reaches the view as a
 * polled event log ({seq, ...child-event} ring buffer). The hub HTTP
 * server is NOT involved — it never executes local commands.
 */

const fs   = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const TOKEN_REF   = 'assistant:llm-token';
const MAX_EVENTS  = 5000;         // in-memory ring buffer cap
const EVENTS_KEEP = 2000;         // lines kept when the log is (re)written
const EVENTS_MAX_BYTES = 10485760; // runtime rewrite threshold (10 MB)
const MODELS_TIMEOUT_MS = 15000;
// Out-of-the-box endpoint: an empty URL/model field (or a bare Tab in
// the Settings form) means "use the NVIDIA inference API".
const DEFAULT_API_URL = 'https://inference-api.nvidia.com/v1';
const DEFAULT_MODEL   = 'nvidia/moonshotai/kimi-k3';

const state = {
  dataDir: null,
  credentialStore: null,
  logger: null,
  onEvent: null,       // push hook (main → renderer broadcast)
  child: null,
  status: 'stopped',   // stopped | starting | idle | running | error
  lastError: '',
  config: { apiUrl: '', model: '' },
  hub: null,           // { url, token } — current hub pair, rotates on hub restart
  events: [],          // ring buffer of { seq, ...event }
  seq: 0,
  threadId: null,      // active thread (threads/<id>.jsonl)
  eventsLoaded: false, // events.jsonl rehydrated from disk already
  eventsFileBytes: 0,
};

/* One-shot waiter for the child's 'configured' ack. Used by setConfig so
 * callers cannot send until the active thread has been loaded. */
let _configureResolve = null;
let _configureReject = null;
let _configureTimer = null;

function _startConfigureWait(timeoutMs = 15000) {
  // A new wait supersedes any pending one — SETTLE the old waiter first;
  // nulling it without rejecting would leave that IPC invoke (a concurrent
  // Save) hanging at "validating…" forever.
  if (_configureReject) {
    const r = _configureReject;
    _clearConfigureWait();
    r(new Error('superseded by a newer configure'));
  } else {
    _clearConfigureWait();
  }
  return new Promise((resolve, reject) => {
    _configureResolve = resolve;
    _configureReject = reject;
    _configureTimer = setTimeout(() => {
      _clearConfigureWait();
      reject(new Error('configure timeout'));
    }, timeoutMs);
  });
}

function _resolveConfigureWait() {
  if (_configureResolve) {
    const r = _configureResolve;
    _clearConfigureWait();
    r();
  }
}

function _rejectConfigureWait(reason) {
  if (_configureReject) {
    const r = _configureReject;
    _clearConfigureWait();
    r(reason);
  }
}

function _clearConfigureWait() {
  if (_configureTimer) { clearTimeout(_configureTimer); _configureTimer = null; }
  _configureResolve = null;
  _configureReject = null;
}

function _log(text) {
  try { state.logger && state.logger(String(text)); } catch (_) {}
}

/* MAS compliance (Apple 2.5.2 / App Sandbox): the Mac App Store binary
 * must not execute downloaded/user-supplied code and must not spawn
 * arbitrary shell commands. Electron sets process.mas on MAS builds.
 * state._forceMas is the test hook. */
function _isMas() {
  return !!process.mas || state._forceMas === true;
}

/* Per-extension data home: <userData>/ext-data/assistant/. Lives OUTSIDE
 * the package dir, so reinstalling/updating/shadowing the extension keeps
 * config + transcript; only an explicit Remove of the whole extension
 * cascades here (hub DELETE /api/extensions/<name>). */
function _extDataDir() {
  return path.join(state.dataDir || '', 'ext-data', 'assistant');
}

function _cfgPath() {
  return path.join(_extDataDir(), 'config.json');
}

function _loadConfig() {
  let raw = null;
  let fromLegacyFile = false;
  try {
    raw = fs.readFileSync(_cfgPath(), 'utf8');
  } catch (_) {
    // Legacy location (<0.2.24): <userData>/assistant.json — migrate it
    // into the ext-data dir so the token/config story is uniform.
    const legacy = path.join(state.dataDir || '', 'assistant.json');
    try { raw = fs.readFileSync(legacy, 'utf8'); fromLegacyFile = true; }
    catch (_) { return; }
  }
  try {
    const parsed = JSON.parse(raw);
    let dirty = fromLegacyFile;
    if (parsed && typeof parsed === 'object') {
      state.config = {
        apiUrl: String(parsed.apiUrl || ''),
        model:  String(parsed.model || ''),
      };
      // 0.2.36 cleanup: the cam-pi shell bridge is gone (the child runs
      // local shell directly) — strip a legacy bridge pair on first boot.
      if (parsed.shellUrl) {
        dirty = true;
        try { state.credentialStore && state.credentialStore.remove('assistant:shell-token'); } catch (_) {}
        _log('dropped legacy shell-bridge config (direct local shell now)');
      }
    }
    if (fromLegacyFile) {
      try { fs.unlinkSync(path.join(state.dataDir || '', 'assistant.json')); } catch (_) {}
      _log('migrated assistant.json → ext-data/assistant/config.json');
    }
    if (dirty) _saveConfig();
  } catch (_) { /* corrupt → defaults */ }
}

function _saveConfig() {
  try {
    fs.mkdirSync(_extDataDir(), { recursive: true });
    fs.writeFileSync(_cfgPath(), JSON.stringify(state.config, null, 1), { mode: 0o600 });
    return true;
  } catch (e) {
    _log(`config save failed: ${e && e.message}`);
    return false;
  }
}

/* Threads (0.2.25): each chat thread is a JSONL event log under
 * ext-data/assistant/threads/<id>.jsonl; threads.json holds the index
 * ({active, threads:[{id,title,created_at,updated_at}]}). The pre-threads
 * flat events.jsonl migrates into the first thread on boot. */
function _threadsDir() {
  return path.join(_extDataDir(), 'threads');
}

function _threadsIndexPath() {
  return path.join(_extDataDir(), 'threads.json');
}

function _readThreadsIndex() {
  try {
    const p = JSON.parse(fs.readFileSync(_threadsIndexPath(), 'utf8'));
    if (p && Array.isArray(p.threads)) return p;
  } catch (_) { /* absent/corrupt */ }
  return null;
}

function _writeThreadsIndex(idx) {
  try {
    fs.mkdirSync(_extDataDir(), { recursive: true });
    fs.writeFileSync(_threadsIndexPath(), JSON.stringify(idx, null, 1), { mode: 0o600 });
  } catch (_) { /* best-effort */ }
}

function _threadTitleFromFile(file) {
  try {
    const raw = fs.readFileSync(file, 'utf8');
    for (const line of raw.split('\n')) {
      if (!line) continue;
      try {
        const e = JSON.parse(line);
        if (e && e.type === 'user' && e.text) {
          return String(e.text).replace(/\s+/g, ' ').slice(0, 48);
        }
      } catch (_) { /* skip */ }
    }
  } catch (_) { /* absent */ }
  return '';
}

function _ensureThread() {
  if (state.threadId || !state.dataDir) return;
  let idx = _readThreadsIndex();
  if (!idx) {
    const id = 't' + Date.now().toString(36);
    idx = { active: id, threads: [{ id, title: '', created_at: new Date().toISOString(), updated_at: new Date().toISOString() }] };
    try {
      fs.mkdirSync(_threadsDir(), { recursive: true });
      // Migrate the pre-threads flat log, if any.
      const legacy = path.join(_extDataDir(), 'events.jsonl');
      if (fs.existsSync(legacy)) {
        const dst = path.join(_threadsDir(), id + '.jsonl');
        fs.renameSync(legacy, dst);
        idx.threads[0].title = _threadTitleFromFile(dst);
      }
    } catch (_) { /* best-effort */ }
    _writeThreadsIndex(idx);
  }
  state.threadId = idx.active;
}

/* Transcript persistence (design: "leaving the page and coming back must
 * not clear the chat"). Every DURABLE event lands in the active thread's
 * JSONL; on thread switch / host boot the whole file rehydrates the ring
 * buffer, capped to MAX_EVENTS in memory, and the view rebuilds by polling
 * from seq 0. */
function _eventsPath() {
  return path.join(_threadsDir(), `${state.threadId || 'pending'}.jsonl`);
}

function _touchThread(kind, text) {
  // Index maintenance on meaningful events only (user/done) — never on
  // high-frequency deltas.
  if (kind !== 'user' && kind !== 'done') return;
  const idx = _readThreadsIndex();
  if (!idx) return;
  const t = idx.threads.find((x) => x.id === state.threadId);
  if (!t) return;
  if (!t.title && kind === 'user' && text) {
    t.title = String(text).replace(/\s+/g, ' ').slice(0, 48);
  }
  t.updated_at = new Date().toISOString();
  _writeThreadsIndex(idx);
}

/* Streaming intermediates are live-view only: they ride the in-memory
 * ring buffer and the push rail but NEVER touch disk. A single reply can
 * stream hundreds of cumulative snapshots — persisted, they flooded the
 * thread files (~97% of lines), burned the EVENTS_KEEP/MAX_EVENTS caps
 * within days, and truncated the visible history. The 'done' event
 * already carries the final text, so replay loses nothing but the typing
 * animation. */
const EPHEMERAL_EVENTS = new Set(['delta', 'thinking']);

function _persistEvent(ev) {
  if (!state.dataDir || !state.threadId) return;
  try {
    _touchThread(ev.type, ev.text);
    if (EPHEMERAL_EVENTS.has(ev.type)) return; // live-only — never on disk
    const line = JSON.stringify(ev) + '\n';
    fs.mkdirSync(_threadsDir(), { recursive: true });
    fs.appendFileSync(_eventsPath(), line);
    state.eventsFileBytes += Buffer.byteLength(line);
    if (state.eventsFileBytes > EVENTS_MAX_BYTES) {
      const keep = state.events
        .filter((e) => !EPHEMERAL_EVENTS.has(e.type))
        .slice(-EVENTS_KEEP)
        .map((e) => JSON.stringify(e) + '\n').join('');
      fs.writeFileSync(_eventsPath(), keep, { mode: 0o600 });
      state.eventsFileBytes = Buffer.byteLength(keep);
    }
  } catch (_) { /* best-effort — never break the chat over disk hiccups */ }
}

function _loadEvents() {
  if (state.eventsLoaded || !state.dataDir) return;
  state.eventsLoaded = true;
  let raw;
  try { raw = fs.readFileSync(_eventsPath(), 'utf8'); } catch (_) { return; }
  // Load the whole thread file so switching threads shows the full history.
  // Streaming lines (delta/thinking) written by older builds are skipped —
  // 'done' carries the final text — so legacy files replay their FULL
  // durable history instead of truncating at the ring-buffer cap.
  const lines = raw.split('\n').filter(Boolean);
  const evs = [];
  let maxSeq = 0;
  for (const l of lines) {
    try {
      const e = JSON.parse(l);
      if (e && typeof e.seq === 'number') {
        if (e.seq > maxSeq) maxSeq = e.seq;
        if (!EPHEMERAL_EVENTS.has(e.type)) evs.push(e);
      }
    } catch (_) { /* skip corrupt lines */ }
  }
  if (evs.length) {
    state.events = evs.slice(-MAX_EVENTS);
    state.eventsFileBytes = Buffer.byteLength(lines.join('\n') + '\n');
  }
  if (maxSeq) state.seq = maxSeq;
}

function _threadMessages() {
  const msgs = [];
  for (const e of state.events) {
    if (e.type === 'user') msgs.push({ role: 'user', text: e.text });
    else if (e.type === 'done' && e.text) msgs.push({ role: 'assistant', text: e.text });
  }
  return msgs;
}

function _loadChildThread() {
  if (!state.child || !state.threadId) return false;
  const msgs = _threadMessages();
  _write({ type: 'load', threadId: state.threadId, messages: msgs });
  _log(`load thread ${state.threadId} → child (${msgs.length} context msgs)`);
  return true;
}

/* Version-compare for shadow resolution (kept self-contained so this
 * module never has to locate the registry): dotted-numeric, missing or
 * non-numeric sorts LOW. Same rule as registry.compareVersions. */
function _cmpVer(a, b) {
  const pa = String(a || '').split('.'), pb = String(b || '').split('.');
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const xa = /^\d+$/.test(pa[i] || '') ? Number(pa[i]) : -1;
    const xb = /^\d+$/.test(pb[i] || '') ? Number(pb[i]) : -1;
    if (xa !== xb) return xa - xb;
  }
  return 0;
}

function _pkgVersion(dir) {
  try {
    const m = /^version:\s*(\S+)\s*$/m.exec(fs.readFileSync(path.join(dir, 'manifest.yaml'), 'utf8'));
    return m ? m[1] : '';
  } catch (_) { return ''; }
}

function _builtinPkgDir(name) {
  const rel = path.join('packages', name);
  try {
    const dev = path.join(path.resolve(__dirname, '..', '..', '..', 'extensions'), rel);
    if (fs.existsSync(dev)) return dev;
  } catch (_) {}
  return path.join(process.resourcesPath || '', 'extensions', rel);
}

/* The user-installed copy of <name> wins ONLY when its manifest version
 * is strictly newer than the built-in's — a tie or an older copy goes to
 * the built-in, so reinstalling/upgrading the app repairs stale shadows
 * (registry.resolvePackageDir implements the same rule for listings and
 * file serving). tar.gz ext updates keep working by bumping the version. */
function _userPkgWins(name) {
  try {
    const userDir = path.join(state.dataDir || '', 'extensions', name);
    if (!fs.existsSync(userDir)) return false;
    return _cmpVer(_pkgVersion(userDir), _pkgVersion(_builtinPkgDir(name))) > 0;
  } catch (_) { return false; }
}

function _assistPath() {
  // Bundle shadowing (0.2.26, version-gated 0.2.30): a NEWER user-installed
  // assistant package may carry its own cam-assist.js — the ext tar.gz
  // then ships view AND agent logic together, and assistant iterations
  // stop needing an MSI. The file comes from the ext; the PRIVILEGE
  // (spawn, token, disk) stays here in the main process either way.
  // MAS builds never load the user copy (Apple 2.5.2: no executable
  // code from outside the signed app bundle).
  if (!_isMas() && _userPkgWins('assistant')) {
    try {
      const userCopy = path.join(state.dataDir || '', 'extensions', 'assistant', 'cam-assist.js');
      if (fs.existsSync(userCopy)) return userCopy;
    } catch (_) {}
  }  // Dev: <repo>/extensions/vendor/cam-assist/dist/cam-assist.js
  // Packaged: <resources>/extensions/vendor/cam-assist/dist/cam-assist.js
  // (the whole extensions/ dir ships via extraResources).
  const rel = path.join('vendor', 'cam-assist', 'dist', 'cam-assist.js');
  const repoExt = path.resolve(__dirname, '..', '..', '..', 'extensions');
  try {
    const devPath = path.join(repoExt, rel);
    if (fs.existsSync(devPath)) return devPath;
  } catch (_) {}
  return path.join(process.resourcesPath || '', 'extensions', rel);
}

function _pushEvent(ev) {
  const withSeq = { seq: ++state.seq, ts: Date.now(), ...ev };
  state.events.push(withSeq);
  if (state.events.length > MAX_EVENTS) {
    state.events.splice(0, state.events.length - MAX_EVENTS);
  }
  _persistEvent(withSeq);
  // Event timeline in cam-desktop.log (debug rail for "the answer only
  // appears on the next send" class of issues — the emit-side timestamps
  // discriminate child-stall from delivery-stall from view-stall).
  if (state.logger) {
    const desc = (ev.type === 'delta' || ev.type === 'thinking') ? ` len=${(ev.text || '').length}`
      : ev.type === 'tool' ? ` ${ev.name || ''} ${ev.phase || ''}${ev.summary ? ' ' + ev.summary : ''}`
      : ev.type === 'status' ? ` ${ev.state}`
      : (ev.type === 'user' || ev.type === 'done') ? ` len=${(ev.text || '').length}${ev.errorMessage ? ' err' : ''}`
      : '';
    _log(`ev#${withSeq.seq} ${ev.type}${desc}`);
  }
  // Push rail (main → renderer → view): the view renders instantly
  // instead of waiting for its next poll tick.
  try { state.onEvent && state.onEvent(withSeq); } catch (_) {}
}

/* ─────────────── LLM endpoint validation / model list ───────────────
 * Plain OpenAI-compatible GET {apiUrl}/models. Used by setConfig (save
 * = validate) and by the Settings tab's model dropdown. */

async function fetchModels(apiUrl, token) {
  const base = String(apiUrl || '').trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(base)) {
    return { ok: false, error: 'invalid_url', detail: 'base URL must start with http:// or https://' };
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), MODELS_TIMEOUT_MS);
  try {
    const res = await fetch(`${base}/models`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: ctrl.signal,
    });
    if (res.status === 401 || res.status === 403) {
      return { ok: false, error: 'auth_failed', detail: `endpoint answered HTTP ${res.status} — check the token` };
    }
    if (!res.ok) {
      return { ok: false, error: `http_${res.status}`, detail: `endpoint answered HTTP ${res.status}` };
    }
    const data = await res.json();
    const list = Array.isArray(data && data.data) ? data.data : [];
    const models = list.map((m) => (m && m.id) || '').filter(Boolean).sort();
    return { ok: true, models };
  } catch (e) {
    const aborted = e && (e.name === 'AbortError');
    return { ok: false, error: aborted ? 'timeout' : 'network', detail: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}

/* ─────────────── child lifecycle ─────────────── */

function _onChildLine(line) {
  let msg;
  try { msg = JSON.parse(line); } catch (_) { return; }
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'ready') {
    _log(`child ready (cam-assist v${msg.version || '?'})`);
    // (Re)apply the stored config on every boot, then the current hub
    // pair (the child only enables its `cam` tool once this arrives).
    const token = state.credentialStore ? state.credentialStore.get(TOKEN_REF) : null;
    if (state.config.apiUrl && state.config.model && token) {
      _write({ type: 'configure', apiUrl: state.config.apiUrl, apiKey: token, model: state.config.model });
      if (state.hub) _write({ type: 'hub', url: state.hub.url, token: state.hub.token });
    } else {
      state.status = 'idle'; // reachable but unconfigured — sends will error cleanly
      _pushEvent({ type: 'status', state: 'idle' });
      return;
    }
    return;
  }
  if (msg.type === 'hub_call') {
    // The audit rail (design: "write access is full; safety =
    // auditability") — every assistant-originated hub call lands in
    // cam-desktop.log. Not pushed to the view's event log: the view
    // already renders the generic tool start/end lines.
    _log(`${msg.method} ${msg.path} → ${msg.status}`);
    return;
  }
  if (msg.type === 'shell_call') {
    // Same audit rail for the opt-in local shell tool.
    _log(`$ ${msg.command}`);
    return;
  }
  if (msg.type === 'configured') {
    state.status = 'idle';
    _pushEvent({ type: 'status', state: 'idle' });
    // The child starts with a blank session on every configure; replay the
    // active thread's history so the assistant remembers across restarts.
    _loadChildThread();
    _resolveConfigureWait();
    return;
  }
  if (msg.type === 'status' && (msg.state === 'running' || msg.state === 'idle')) {
    state.status = msg.state;
  }
  if (msg.type === 'error') {
    state.lastError = `${msg.error || 'error'}${msg.detail ? ': ' + msg.detail : ''}`;
    _log(`child error: ${state.lastError}`);
    _rejectConfigureWait(new Error(state.lastError));
  }
  _pushEvent(msg);
}

function _onChildExit(code, signal) {
  _rejectConfigureWait(new Error(`assistant process exited (code=${code})`));
  _log(`child exited code=${code} signal=${signal}`);
  state.child = null;
  if (state.status !== 'stopped') {
    state.status = state.status === 'running' || state.status === 'starting' ? 'error' : 'stopped';
    if (state.status === 'error') state.lastError = `assistant process exited (code=${code})`;
  }
  _pushEvent({ type: 'status', state: state.status });
}

function _write(msg) {
  try {
    state.child && state.child.stdin.write(JSON.stringify(msg) + '\n');
    return true;
  } catch (e) {
    _log(`child stdin write failed: ${e && e.message}`);
    return false;
  }
}

function start() {
  if (state.child) return { ok: true, status: state.status };
  const bundle = _assistPath();
  if (!fs.existsSync(bundle)) {
    state.status = 'error';
    state.lastError = 'assistant bundle missing';
    return { ok: false, error: 'bundle_missing', detail: bundle };
  }
  _log(`bundle: ${bundle}`);
  state.status = 'starting';
  state.lastError = '';
  let child;
  try {
    child = spawn(process.execPath, [bundle], {
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch (e) {
    state.status = 'error';
    state.lastError = String((e && e.message) || e);
    return { ok: false, error: 'spawn_failed', detail: state.lastError };
  }
  state.child = child;
  let buf = '';
  child.stdout.on('data', (c) => {
    buf += c;
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx); buf = buf.slice(idx + 1);
      if (line.trim()) _onChildLine(line.trim());
    }
  });
  let errBuf = '';
  child.stderr.on('data', (c) => {
    errBuf += c;
    if (errBuf.length > 4000) errBuf = errBuf.slice(-4000);
    _log(`child stderr: ${String(c).slice(0, 300)}`);
  });
  child.on('close', (code, signal) => _onChildExit(code, signal));
  child.on('error', (e) => {
    _log(`child spawn error: ${e && e.message}`);
    state.status = 'error';
    state.lastError = String((e && e.message) || e);
    _pushEvent({ type: 'status', state: 'error' });
  });
  _log('child spawned');
  return { ok: true, status: state.status };
}

function stop() {
  if (!state.child) { state.status = 'stopped'; return { ok: true }; }
  state.status = 'stopped';
  try { state.child.kill(); } catch (_) {}
  state.child = null;
  _pushEvent({ type: 'status', state: 'stopped' });
  _log('child stopped');
  return { ok: true };
}

/* ─────────────── public API (IPC handlers) ─────────────── */

function configure({ dataDir, credentialStore, logger, onEvent } = {}) {
  if (dataDir) state.dataDir = dataDir;
  if (credentialStore) state.credentialStore = credentialStore;
  if (logger) state.logger = logger;
  if (onEvent) state.onEvent = onEvent;
  if (state.dataDir) { _loadConfig(); _ensureThread(); _loadEvents(); }
}

function status() {
  const hasToken = !!(state.credentialStore && state.credentialStore.metadata(TOKEN_REF));
  return {
    ok: true,
    status: state.status,
    apiUrl: state.config.apiUrl,
    model: state.config.model,
    hasToken,
    configured: !!(state.config.apiUrl && state.config.model && hasToken),
    lastError: state.lastError,
    seq: state.seq,
    defaults: { apiUrl: DEFAULT_API_URL, model: DEFAULT_MODEL },
  };
}

/** Resolve form values against the built-in defaults: an empty base URL
 *  or model field means the NVIDIA inference API default. */
function _resolveConfig(payload = {}) {
  return {
    apiUrl: (String(payload.apiUrl || '').trim().replace(/\/+$/, '')) || DEFAULT_API_URL,
    model:  String(payload.model || '').trim() || DEFAULT_MODEL,
  };
}

/** Save = validate: the endpoint must answer GET /models with the token
 *  before anything is persisted. `token` empty-string keeps the stored
 *  one; `token: null` clears it. */
async function setConfig(payload = {}) {
  const { apiUrl, model } = _resolveConfig(payload);
  let token;
  if (typeof payload.token === 'string' && payload.token) token = payload.token;
  else token = state.credentialStore ? state.credentialStore.get(TOKEN_REF) : null;
  if (!token) return { ok: false, error: 'missing_token', detail: 'an API token is required' };

  const probe = await fetchModels(apiUrl, token);
  if (!probe.ok) return probe;
  if (!probe.models.includes(model)) {
    return { ok: false, error: 'model_not_listed', detail: `"${model}" is not in the endpoint's model list`, models: probe.models };
  }

  state.config = { apiUrl, model };
  if (!_saveConfig()) return { ok: false, error: 'save_failed' };
  if (typeof payload.token === 'string' && payload.token && state.credentialStore) {
    const put = state.credentialStore.put(TOKEN_REF, 'llm-token', payload.token);
    if (!put.ok) return { ok: false, error: put.error || 'token_save_failed', detail: put.detail };
  } else if (payload.token === null && state.credentialStore) {
    state.credentialStore.remove(TOKEN_REF);
  }
  _log(`config saved: ${apiUrl} model=${model}`);
  // Live child: re-apply without a respawn. Wait for the 'configured' ack
  // so the active thread is loaded before any send can race ahead.
  if (state.child) {
    _write({ type: 'configure', apiUrl, apiKey: token, model });
    try { await _startConfigureWait(); }
    catch (e) {
      _log(`configure wait failed: ${e && e.message}`);
      return { ok: false, error: 'configure_failed', detail: e && e.message };
    }
  }
  return { ok: true, models: probe.models };
}

/** Model list for the Settings dropdown. Accepts optional form-value
 *  overrides so the user can probe an endpoint before saving it; an
 *  empty URL probes the default endpoint. */
async function listModels(payload = {}) {
  const apiUrl = String(payload.apiUrl || '').trim() || state.config.apiUrl || DEFAULT_API_URL;
  const formToken = typeof payload.token === 'string' && payload.token ? payload.token : null;
  const token = formToken || (state.credentialStore ? state.credentialStore.get(TOKEN_REF) : null);
  return fetchModels(apiUrl, token);
}

function send(payload = {}) {
  const text = String(payload.text || '');
  if (!text.trim()) return { ok: false, error: 'empty_message' };
  if (!state.child) return { ok: false, error: 'not_running' };
  _log(`send → child len=${text.trim().length} status=${state.status}`);
  if (!_write({ type: 'send', text })) return { ok: false, error: 'write_failed' };
  return { ok: true };
}

function poll(payload = {}) {
  const since = Number(payload.since) || 0;
  let events;
  if (since >= state.seq) events = [];
  else if (since === 0) {
    // First poll after mount / thread switch: give the view the whole
    // thread so history is not truncated to the last 200 events.
    events = state.events.slice();
  } else if (since < state.events[0].seq - 1) {
    // Cursor fell behind the ring buffer: replay what we still hold.
    events = state.events.slice(-200);
  } else {
    events = state.events.filter((e) => e.seq > since);
  }
  return { ok: true, status: state.status, events, seq: state.seq };
}

function reset() {
  state.events = [];
  state.seq = 0;
  state.eventsFileBytes = 0;
  try { fs.unlinkSync(_eventsPath()); } catch (_) { /* absent */ }
  if (state.child) _write({ type: 'reset' });
  _log('reset (active thread cleared)');
  _pushEvent({ type: 'status', state: state.status });
  return { ok: true };
}

/* ─────────────── threads ─────────────── */

function threads() {
  _ensureThread();
  const idx = _readThreadsIndex() || { active: state.threadId, threads: [] };
  return { ok: true, threads: idx.threads, active: state.threadId };
}

function newChat() {
  if (state.status === 'running') return { ok: false, error: 'busy', detail: 'wait for the current reply (or stop it)' };
  _ensureThread();
  // Reuse the active thread while it has no user messages yet.
  if (!state.events.some((e) => e.type === 'user')) {
    return { ok: true, active: state.threadId, reused: true };
  }
  const idx = _readThreadsIndex() || { active: state.threadId, threads: [] };
  const id = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  const now = new Date().toISOString();
  idx.threads.push({ id, title: '', created_at: now, updated_at: now });
  idx.active = id;
  _writeThreadsIndex(idx);
  state.threadId = id;
  state.events = [];
  state.seq = 0;
  state.eventsFileBytes = 0;
  // Tell the child to start a fresh session bound to this new thread id,
  // so the old thread's context does not leak into the new chat.
  _loadChildThread();
  _log(`new chat thread ${id}`);
  _pushEvent({ type: 'status', state: state.status });
  return { ok: true, active: id };
}

/** Switch the active thread: its events become the live log (the view
 *  replays from seq 0), and the child's transcript is seeded with the
 *  thread's user/assistant messages so the conversation continues with
 *  context. */
function openThread(payload = {}) {
  if (state.status === 'running') return { ok: false, error: 'busy', detail: 'wait for the current reply (or stop it)' };
  const id = String(payload.id || '');
  const idx = _readThreadsIndex();
  const meta = idx && idx.threads.find((t) => t.id === id);
  if (!meta) return { ok: false, error: 'unknown_thread', detail: id };
  idx.active = id;
  _writeThreadsIndex(idx);
  state.threadId = id;
  state.events = [];
  state.seq = 0;
  state.eventsFileBytes = 0;
  state.eventsLoaded = false;
  _loadEvents();
  _loadChildThread();
  _log(`opened thread ${id} (${state.events.length} events)`);
  return { ok: true, active: id, seq: state.seq };
}

function deleteThread(payload = {}) {
  if (state.status === 'running') return { ok: false, error: 'busy', detail: 'wait for the current reply (or stop it)' };
  const id = String(payload.id || '');
  const idx = _readThreadsIndex();
  if (!idx || !idx.threads.some((t) => t.id === id)) return { ok: false, error: 'unknown_thread', detail: id };
  idx.threads = idx.threads.filter((t) => t.id !== id);
  try { fs.unlinkSync(path.join(_threadsDir(), `${id}.jsonl`)); } catch (_) { /* absent */ }
  _log(`deleted thread ${id}`);
  if (state.threadId === id) {
    if (idx.threads.length) {
      _writeThreadsIndex(idx);
      return openThread({ id: idx.threads[idx.threads.length - 1].id });
    }
    // None left: start a fresh one (events are already "the deleted
    // thread's" — newChat sees user events and mints a new id).
    _writeThreadsIndex(idx);
    const r = newChat();
    return { ok: true, active: r.active };
  }
  _writeThreadsIndex(idx);
  return { ok: true, active: state.threadId };
}

/** Called by main whenever the embedded hub comes up (start/restart —
 * the token rotates each time) or goes down (null). Re-injected into a
 * live child immediately; a stopped child gets it on next boot. */
function setHub(pair) {
  state.hub = (pair && pair.url && pair.token) ? { url: String(pair.url), token: String(pair.token) } : null;
  if (state.child) _write({ type: 'hub', url: state.hub && state.hub.url, token: state.hub && state.hub.token });
}

/** Test hook: drop all in-memory state (child included). */
function _resetForTests() {
  stop();
  state.status = 'stopped';
  state.lastError = '';
  state.config = { apiUrl: '', model: '' };
  state.hub = null;
  state.events = [];
  state.seq = 0;
  state.onEvent = null;
  state.eventsLoaded = false;
  state.eventsFileBytes = 0;
  state.threadId = null;
  state._forceMas = false;
}

/** Test hook: simulate a MAS build (process.mas is read-only). */
function _setMasForTests(on) {
  state._forceMas = !!on;
}

module.exports = {
  configure,
  status,
  setConfig,
  listModels,
  start,
  stop,
  send,
  poll,
  reset,
  threads,
  newChat,
  openThread,
  deleteThread,
  setHub,
  fetchModels,
  DEFAULT_API_URL,
  DEFAULT_MODEL,
  _resolveConfig,
  _resetForTests,
  _setMasForTests,
};
