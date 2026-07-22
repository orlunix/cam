/**
 * CAM Desktop — Electron main process (CAM-DESK-DIRECT-010..019,
 * CAM-DESK-HUB-010..012).
 *
 * Loads the WebUI-derived desktop entry (web/desktop.html) into a
 * BrowserWindow with strict renderer isolation. The renderer is a thin
 * HTTP/WS client of either the embedded Direct Hub (CAM-DESK-DIRECT-011)
 * or an external Hub reached through a Relay (CAM-DESK-REMOTE-012).
 *
 * The Direct path is implemented entirely in Node — see
 * `embedded-hub.cjs`. There is no dependency on a host `cam` CLI,
 * Python, WSL, or any shell.
 *
 * Renderer-facing surface lives in `preload.cjs` as
 * `CamBridge.directHub.*`; the IPC handler names below are kept as
 * `local:*` for backwards compatibility with the preload contract.
 */

'use strict';

const { app, BrowserWindow, ipcMain, shell, dialog, safeStorage, Menu, clipboard, powerMonitor } = require('electron');
const fs   = require('node:fs');
const path = require('node:path');
const url  = require('node:url');
const http = require('node:http');
const https = require('node:https');

const embeddedHub     = require('./embedded-hub.cjs');
const credentialStore = require('./credential-store.cjs');
const sshTransport    = require('./ssh-transport.cjs');
const { tmuxMetadataForAgent, selectOnlyClient, selectNewClient, parseClientState, parseWindowRows, tmuxCommand } = require('./tmux-controls.cjs');

// An SSH PTY can become a tmux client a little after the attach command
// starts (notably on PDX). Probe at the renderer's 2s control-refresh cadence
// only while the selected terminal is waiting, never as a global monitor.
const TMUX_CLIENT_RECOVERY_MAX_ATTEMPTS = 10;
const TMUX_CLIENT_RECOVERY_RETRY_DELAY_MS = 30000;

function isSshHandshakeLoss(err) {
  const msg = String(err && (err.message || err) || '');
  const stack = String(err && err.stack || '');
  return /Connection lost before handshake/i.test(msg) && /(node_modules[\\/]ssh2|ssh2)/i.test(stack);
}

process.on('uncaughtException', (err) => {
  if (isSshHandshakeLoss(err)) {
    console.warn('[cam-desktop] swallowed ssh2 handshake loss:', err && err.message || err);
    return;
  }
  throw err;
});

process.on('unhandledRejection', (reason) => {
  if (isSshHandshakeLoss(reason)) {
    console.warn('[cam-desktop] swallowed ssh2 handshake rejection:', reason && reason.message || reason);
    return;
  }
});

/** Locate the bundled web root.
 *
 * - Dev: apps/cam-desktop/electron/main.cjs → ../../../web
 * - Packaged (electron-builder extraResources): process.resourcesPath/web
 */
function resolveWebRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'web');
  }
  return path.resolve(__dirname, '..', '..', '..', 'web');
}


function netProbe(target, timeoutMs = 8000) {
  return new Promise((resolve) => {
    let u;
    try { u = new URL(String(target || '')); }
    catch (e) { resolve({ ok: false, error: 'invalid_url', detail: e && e.message || String(e) }); return; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') {
      resolve({ ok: false, error: 'unsupported_protocol', detail: u.protocol });
      return;
    }
    const lib = u.protocol === 'https:' ? https : http;
    const started = Date.now();
    const req = lib.request(u, { method: 'GET', timeout: timeoutMs }, (res) => {
      let bytes = 0;
      res.on('data', (chunk) => { bytes += chunk ? chunk.length : 0; });
      res.on('end', () => resolve({
        ok: res.statusCode >= 200 && res.statusCode < 400,
        status: res.statusCode,
        statusText: res.statusMessage || '',
        bytes,
        ms: Date.now() - started,
      }));
    });
    req.on('timeout', () => {
      try { req.destroy(new Error('probe timeout')); } catch {}
    });
    req.on('error', (e) => resolve({
      ok: false,
      error: e && (e.code || e.name) || 'probe_failed',
      detail: e && e.message || String(e),
      ms: Date.now() - started,
    }));
    req.end();
  });
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 720,
    minHeight: 480,
    backgroundColor: '#111111',
    title: 'CAM Desktop',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  const webRoot = resolveWebRoot();
  const indexPath = path.join(webRoot, 'desktop.html');
  const fileUrl = url.pathToFileURL(indexPath).toString();
  win.loadURL(fileUrl);

  // Open http(s) links in the user's default browser.
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//i.test(target)) {
      shell.openExternal(target);
    }
    return { action: 'deny' };
  });

  // Refuse renderer navigation away from the bundled file.
  win.webContents.on('will-navigate', (event, target) => {
    if (target !== fileUrl) {
      event.preventDefault();
      if (/^https?:\/\//i.test(target)) shell.openExternal(target);
    }
  });

  // Electron does not provide a browser-style context menu by
  // default. Add a narrow native text menu so selected output can be
  // copied with right-click, while editable fields keep normal actions.
  win.webContents.on('context-menu', (_event, params) => {
    const template = [];
    const selected = String(params.selectionText || '');
    if (selected) {
      template.push({
        label: 'Copy',
        accelerator: 'CmdOrCtrl+C',
        click: () => clipboard.writeText(selected),
      });
    }
    if (params.isEditable) {
      if (template.length) template.push({ type: 'separator' });
      template.push(
        { role: 'cut', enabled: !!selected },
        { role: 'copy', enabled: !!selected },
        { role: 'paste' },
        { type: 'separator' },
        { role: 'selectAll' },
      );
    }
    if (!template.length) return;
    Menu.buildFromTemplate(template).popup({ window: win });
  });

  return win;
}

/* ─────────────── Direct Hub IPC bindings ───────────────
 *
 * The renderer-facing names are `CamBridge.directHub.{check,start,
 * stop,restart,logs,getProfile}` (see preload.cjs). The IPC channel
 * names stay `local:*` because the preload contract already uses
 * them; renaming the channels here would be a no-op surface change
 * and break older preload bundles in flight. The actual work
 * delegates straight to `embedded-hub.cjs`.
 */

function userDataDir() {
  return app.getPath('userData');
}

// One-time wiring: configure the credential store with Electron's
// safeStorage backend, and inject the store into the embedded Hub so
// POST /api/contexts can persist remembered secrets without seeing
// the raw bytes in the JSON store. Idempotent — safe to call from
// the start of each Hub lifecycle action.
function _ensureBackendsConfigured() {
  credentialStore.configure({ safeStorage, dataDir: userDataDir() });
  embeddedHub.configure({ credentialStore, sshTransport });
}

async function localCheck() {
  _ensureBackendsConfigured();
  return embeddedHub.check({ dataDir: userDataDir() });
}

async function localStart() {
  _ensureBackendsConfigured();
  return embeddedHub.start({ dataDir: userDataDir() });
}

async function localStop() {
  return embeddedHub.stop();
}

async function localRestart() {
  _ensureBackendsConfigured();
  return embeddedHub.restart({ dataDir: userDataDir() });
}

function localLogs() {
  return embeddedHub.getLogs();
}

function localGetProfile() {
  return embeddedHub.getProfile();
}

/* ─────────────── File picker (CAM-DESK-DIRECT-017) ───────────────
 * Narrow Electron-main file picker for the Nodes "Add Host" → SSH
 * private-key field. The renderer never passes a path or a command;
 * it just receives the user's selection (or null on cancel). The
 * picker is parameter-less by contract — there are no renderer-
 * controllable filters, default paths, or titles. */
/* ─────────────── Terminal mode (CAM-DESK-TERM-001..005) ───────────────
 * The renderer's xterm.js asks main to open an interactive `~/.cam/camc
 * attach <agent-id>` channel against the agent's owning node. Main:
 *   - resolves the agent → context → SSH opts (incl. decrypted secret)
 *     through embedded-hub.getAttachConnectOpts() — no shell, no system
 *     `ssh`, no child_process.
 *   - opens an ssh2 exec channel WITH a PTY via the existing connection
 *     pool (sshTransport.openTerminalChannel). The same pool serves
 *     execRemote / writeRemoteFile so a sync-warm endpoint costs zero
 *     extra handshakes.
 *   - mints an opaque session id, keeps channel handles in a per-window
 *     Map, and pipes data events to the originating WebContents as
 *     'term:data' / 'term:status'. Multiple agent sessions may stay
 *     open for fast renderer-side switching.
 *   - accepts input/resize/close via 'term:input' / 'term:resize' /
 *     'term:close'. Closing the channel does NOT kill the underlying
 *     agent — `camc attach` is a tmux attach to the agent session.
 *
 * Secrets stay in main only. The renderer sees session id + bytes. */
const _terminals = new Map();   // sessionId → { dispose, contentsId, agentId }
const _tmuxDiscoveryTails = new Map();
let _termSeq = 0;
const TERM_MIN_COLS = 40;
const TERM_MIN_ROWS = 4;

function _terminalOpenSize(payload = {}) {
  const rawCols = Number(payload.cols);
  const rawRows = Number(payload.rows);
  return {
    cols: Math.max(TERM_MIN_COLS, Math.min(500, Number.isFinite(rawCols) && rawCols >= TERM_MIN_COLS ? rawCols : 80)),
    rows: Math.max(TERM_MIN_ROWS, Math.min(500, Number.isFinite(rawRows) && rawRows >= TERM_MIN_ROWS ? rawRows : 24)),
  };
}

function _terminalResizeSize(payload = {}) {
  const rawCols = Number(payload.cols);
  const rawRows = Number(payload.rows);
  if (!Number.isFinite(rawCols) || !Number.isFinite(rawRows)) return null;
  if (rawCols < TERM_MIN_COLS || rawRows < TERM_MIN_ROWS) return null;
  return {
    cols: Math.max(TERM_MIN_COLS, Math.min(500, rawCols)),
    rows: Math.max(TERM_MIN_ROWS, Math.min(500, rawRows)),
  };
}

function _shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function _terminalRepairCommand(agentId, cols, rows) {
  const safeCols = Math.max(TERM_MIN_COLS, Math.min(500, Math.floor(Number(cols) || 80)));
  const safeRows = Math.max(TERM_MIN_ROWS, Math.min(500, Math.floor(Number(rows) || 24)));
  const py = String.raw`import json
import os
import subprocess
import sys

agent_id = sys.argv[1]
cols = max(40, min(500, int(sys.argv[2])))
rows = max(4, min(500, int(sys.argv[3])))
camc = os.path.expanduser("~/.cam/camc")

try:
    raw = subprocess.check_output(
        [camc, "--json", "status", agent_id],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    data = json.loads(raw or "{}")
except Exception:
    sys.exit(0)

if isinstance(data, list):
    data = data[0] if data else {}
if not isinstance(data, dict):
    sys.exit(0)

socket = data.get("tmux_socket") or data.get("socket")
session = data.get("tmux_session") or data.get("session") or data.get("tmux_name")
if not socket or not session:
    sys.exit(0)

try:
    out = subprocess.check_output(
        ["tmux", "-S", str(socket), "list-clients", "-F", "#{client_name}\t#{client_width}\t#{client_height}"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=3,
    )
except Exception:
    out = ""

for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) != 3:
        continue
    name, width, height = parts
    try:
        if int(width) < 40 or int(height) < 4:
            subprocess.run(
                ["tmux", "-S", str(socket), "detach-client", "-t", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            # Evidence for the recurring "window shrank to ~10 cols"
            # mystery: record exactly WHICH client was clamping the
            # window (its name/tty betrays the source device).
            print(f"REPAIR_DETACHED tiny-client name={name} size={width}x{height}")
    except Exception:
        pass

# tmux 2.7 (main PDX environment) has no resize-window command. Use a
# short-lived control-mode client and set its size; tmux then propagates that
# size to the session/window. This also works on newer tmux versions.
try:
    proc = subprocess.Popen(
        ["tmux", "-S", str(socket), "-C", "attach-session", "-t", str(session)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        proc.communicate(f"refresh-client -C {cols},{rows}\n", timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.stdin.write("detach-client\n")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.communicate(timeout=2)
        except Exception:
            proc.kill()
except Exception:
    pass
`;
  return `python3 - ${_shellQuote(agentId)} ${_shellQuote(String(safeCols))} ${_shellQuote(String(safeRows))} <<'PY'\n${py}\nPY`;
}

/** Append one line to the user-visible diagnostics log (userData).
 * Packaged apps have no stdout, so evidence like REPAIR_DETACHED would
 * otherwise evaporate — this file is where we keep it. */
function _diagLog(line) {
  try {
    const f = path.join(app.getPath('userData'), 'cam-desktop.log');
    fs.appendFileSync(f, `${new Date().toISOString()} ${line}\n`);
  } catch (_) {}
}

async function _repairRemoteTerminalSize(opts, agentId, cols, rows) {
  if (!opts || !agentId) return;
  try {
    const res = await sshTransport.execRemote({
      ...opts,
      command: _terminalRepairCommand(agentId, cols, rows),
      timeout_ms: 8000,
    });
    // Evidence for the recurring "window shrank to ~10 cols" mystery:
    // the repair prints REPAIR_DETACHED lines naming the client it
    // kicked — log them so the culprit is identifiable afterwards.
    const out = (res && res.ok && res.stdout) || '';
    for (const line of String(out).split('\n')) {
      if (line.startsWith('REPAIR_DETACHED')) {
        console.warn(`[terminal-repair] ${agentId}: ${line}`);
        _diagLog(`[terminal-repair] ${agentId}: ${line}`);
      }
    }
  } catch (_) {
    // Best-effort guard: attach must still proceed if old camc/tmux cannot report metadata.
  }
}

function _sessionForAgent(contentsId, agentId) {
  for (const [sid, ent] of _terminals) {
    if (ent && ent.contentsId === contentsId && ent.agentId === agentId) return [sid, ent];
  }
  return [null, null];
}

// Per-endpoint cache of remote tmux binary probes. Older agent records
// carry no tmux_bin; rather than hardcoding /bin/tmux (absent on e.g.
// Homebrew macOS), ask the remote once per endpoint and cache the
// answer — '' means "no tmux found", cached too so we probe at most
// once per endpoint per app run.
const _tmuxBinProbes = new Map();  // endpointKey -> Promise<string>

function _probeRemoteTmuxBin(opts) {
  const key = `${opts.host}|${opts.user}|${opts.port || 22}`;
  if (_tmuxBinProbes.has(key)) return _tmuxBinProbes.get(key);
  const p = (async () => {
    try {
      const res = await sshTransport.execRemote({
        ...opts,
        command: "/bin/sh -c 'command -v /bin/tmux || command -v tmux'",
        timeout_ms: 15000,
      });
      const first = res && res.ok ? String(res.stdout || '').split('\n')[0].trim() : '';
      return /^\/[^\s]+$/.test(first) ? first : '';
    } catch (_) { return ''; }
  })();
  _tmuxBinProbes.set(key, p);
  return p;
}

function _dropSession(sessionId) {
  const ent = _terminals.get(sessionId);
  if (!ent) return;
  _terminals.delete(sessionId);
  try { ent.dispose && ent.dispose(); } catch { /* noop */ }
}

function _beginTmuxDiscovery(sessionKey) {
  const previous = _tmuxDiscoveryTails.get(sessionKey) || Promise.resolve();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const tail = previous.then(() => gate);
  _tmuxDiscoveryTails.set(sessionKey, tail);
  return previous.then(() => () => {
    release();
    if (_tmuxDiscoveryTails.get(sessionKey) === tail) _tmuxDiscoveryTails.delete(sessionKey);
  });
}

async function _tmuxExec(ent, args) {
  if (!ent?.tmux || !ent?.opts) return { ok: false, error: 'tmux_unavailable', detail: 'tmux metadata unavailable' };
  let command;
  try { command = tmuxCommand(ent.tmux, args); }
  catch (e) { return { ok: false, error: 'tmux_unavailable', detail: e.message }; }
  const res = await sshTransport.execRemote({ ...ent.opts, command, timeout_ms: 15000 });
  // Evidence for "tmux controls slow/timing out": log the transport
  // timing breakdown on failure so we can see whether the time went to
  // connect (handshake) or to the op itself, and whether the pool was warm.
  if (!res?.ok) {
    const t = res && res.timings ? res.timings : {};
    console.warn(`[tmux-exec] ${ent.agentId} ${args[0]} failed: ${res && res.error} | pooled=${t.pooled} connect=${t.connect_ms}ms op=${t.op_ms}ms total=${t.total_ms}ms | ${String(res && res.detail || '').slice(0, 160)}`);
  }
  return res;
}

function _tmuxFailure(stage, result, ent) {
  const baseDetail = String(result?.detail || result?.error || 'tmux control operation failed');
  const lastProbeError = String(ent?.tmuxLastProbeError || '');
  return {
    ok: false,
    stage,
    error: result?.error || 'tmux_unavailable',
    detail: lastProbeError && !baseDetail.includes(lastProbeError)
      ? `${baseDetail}; last client probe: ${lastProbeError}`
      : baseDetail,
    diagnostics: {
      session: String(ent?.tmux?.session || ''),
      socket: String(ent?.tmux?.socket || ''),
      clientTty: String(ent?.tmuxClientTty || ''),
      initialPending: !!ent?.tmuxInitialDiscoveryPending,
      recoveryAttempts: Number(ent?.tmuxClientRecoveryAttempts || 0),
      beforeClientCount: ent?.tmuxBeforeClients instanceof Set ? ent.tmuxBeforeClients.size : null,
      lastProbeError: lastProbeError,
      lastProbeMs: Number(ent?.tmuxLastProbeMs || 0),
    },
  };
}

async function _tmuxClientSet(opts, tmux, diagnosticsTarget = null) {
  const started = Date.now();
  const record = (error = '') => {
    if (!diagnosticsTarget || typeof diagnosticsTarget !== 'object') return;
    diagnosticsTarget.tmuxLastProbeError = String(error || '');
    diagnosticsTarget.tmuxLastProbeMs = Date.now() - started;
  };
  let command;
  try { command = tmuxCommand(tmux, ['list-clients', '-t', tmux.session, '-F', '#{client_tty}']); }
  catch (e) { record(e?.message || e); return null; }
  const result = await sshTransport.execRemote({ ...opts, command, timeout_ms: 15000 });
  if (!result?.ok) {
    const t = result && result.timings ? result.timings : {};
    console.warn(`[tmux-probe] list-clients failed: ${result && result.error} | pooled=${t.pooled} connect=${t.connect_ms}ms op=${t.op_ms}ms total=${t.total_ms}ms`);
    record(`${result?.error || 'probe_failed'}: ${result?.detail || 'no detail'}`);
    return null;
  }
  record('');
  return new Set(String(result.stdout || '').split(/\r?\n/).filter((tty) => /^\/dev\/pts\/\d+$/.test(tty)));
}

async function _discoverTmuxClient(sessionId, beforeClients) {
  const ent = _terminals.get(sessionId);
  if (!ent?.tmux) return;
  try {
    for (let attempt = 0; attempt < 15; attempt++) {
      const afterClients = await _tmuxClientSet(ent.opts, ent.tmux, ent);
      const tty = afterClients && (beforeClients
        ? selectNewClient(beforeClients, afterClients)
        : selectOnlyClient(afterClients));
      if (tty) {
        ent.tmuxClientTty = tty;
        ent.tmuxControlError = '';
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    ent.tmuxControlError = 'Could not identify this tmux client';
  } finally {
    ent.tmuxInitialDiscoveryPending = false;
  }
}

async function _retryTmuxClientDiscovery(ent) {
  if (!ent?.tmux || ent.tmuxClientTty || ent.tmuxInitialDiscoveryPending
      || ent.tmuxClientRecoveryInFlight || Date.now() < (ent.tmuxClientRecoveryNextAt || 0)) {
    return false;
  }
  ent.tmuxClientRecoveryInFlight = true;
  ent.tmuxClientRecoveryAttempts += 1;
  try {
    const afterClients = await _tmuxClientSet(ent.opts, ent.tmux, ent);
    const tty = afterClients && (ent.tmuxBeforeClients
      ? selectNewClient(ent.tmuxBeforeClients, afterClients)
      : selectOnlyClient(afterClients));
    if (tty) {
      ent.tmuxClientTty = tty;
      ent.tmuxControlError = '';
      return true;
    }
    if (ent.tmuxClientRecoveryAttempts >= TMUX_CLIENT_RECOVERY_MAX_ATTEMPTS) {
      ent.tmuxClientRecoveryAttempts = 0;
      ent.tmuxClientRecoveryNextAt = Date.now() + TMUX_CLIENT_RECOVERY_RETRY_DELAY_MS;
      ent.tmuxControlError = 'Waiting to retry tmux client discovery';
      return false;
    }
    ent.tmuxControlError = 'Waiting for tmux client discovery';
    return false;
  } finally {
    ent.tmuxClientRecoveryInFlight = false;
  }
}

function _ownedTerminal(event, payload = {}) {
  const sid = String(payload && payload.sessionId || '');
  const ent = _terminals.get(sid);
  return (ent && ent.contentsId === event.sender.id) ? ent : null;
}

// Window listing and pane state depend only on the session, not on the
// attached client's tty: the display-message probe targets the session.
// A missing or undiscovered tty must not fail the tab strip — client
// discovery is kicked in the background here and only gates switch-client
// (see _ensureTmuxClientTty).
async function _tmuxClientState(ent) {
  if (!ent?.tmux) return _tmuxFailure('client_discovery', {
    error: 'tmux_unavailable',
    detail: 'tmux metadata unavailable',
  }, ent);
  if (!ent.tmux.bin) return _tmuxFailure('client_discovery', {
    error: 'tmux_unavailable',
    detail: 'no tmux binary recorded or discoverable on this endpoint',
  }, ent);
  if (!ent.tmuxClientTty) void _retryTmuxClientDiscovery(ent).catch(() => {});
  // Target the session (its current window's active pane) instead of the
  // client tty: `display-message -p -c <client>` is rejected as a usage
  // error by tmux < 3.3 (hlren runs 3.2a — the probe failed 100% there,
  // which kept the tab strip permanently hidden). `-p -t <session>` is
  // the oldest portable form and gives the same answer for our
  // single-client attach.
  const result = await _tmuxExec(ent, ['display-message', '-p', '-t', ent.tmux.session, '#{window_index}:#{pane_id}:#{pane_in_mode}']);
  if (!result?.ok) return _tmuxFailure('client_state', result, ent);
  const state = parseClientState(result.stdout);
  return state ? { ok: true, ...state } : _tmuxFailure('client_state', { error: 'tmux_parse_failed', detail: 'invalid client state' }, ent);
}

// switch-client is the one operation that genuinely needs the attached
// client's tty. Unlike window listing, it must wait for discovery and
// report a clear failure when the client still cannot be identified.
async function _ensureTmuxClientTty(ent) {
  if (!ent?.tmuxClientTty) await _retryTmuxClientDiscovery(ent);
  if (!ent?.tmuxClientTty) return _tmuxFailure('client_discovery', {
    error: 'tmux_unavailable',
    detail: ent?.tmuxControlError || 'tmux client discovery pending',
  }, ent);
  return { ok: true };
}

async function termListWindows(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  const state = await _tmuxClientState(ent);
  if (!state.ok) return state;
  const result = await _tmuxExec(ent, ['list-windows', '-t', ent.tmux.session, '-F', '#{window_index}:#{window_name}']);
  if (!result?.ok) return _tmuxFailure('list_windows', result, ent);
  const windows = parseWindowRows(result.stdout).map((window) => ({ ...window, active: window.index === state.activeIndex }));
  return { ok: true, windows, activeIndex: state.activeIndex, paneId: state.paneId, copyMode: state.copyMode };
}

async function termEnterCopyMode(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  const state = await _tmuxClientState(ent);
  if (!state.ok) return state;
  if (state.copyMode) return { ok: true, copyMode: true, paneId: state.paneId };
  const result = await _tmuxExec(ent, ['copy-mode', '-u', '-t', state.paneId]);
  return result?.ok
    ? { ok: true, copyMode: true, paneId: state.paneId }
    : _tmuxFailure('copy_mode_enter', result, ent);
}

async function termCancelCopyMode(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  const state = await _tmuxClientState(ent);
  if (!state.ok) return state;
  if (!state.copyMode) return { ok: true, copyMode: false, paneId: state.paneId };
  const result = await _tmuxExec(ent, ['send-keys', '-X', '-t', state.paneId, 'cancel']);
  return result?.ok
    ? { ok: true, copyMode: false, paneId: state.paneId }
    : _tmuxFailure('copy_mode_cancel', result, ent);
}

async function termSelectWindow(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  const index = Number(payload && payload.index);
  if (!ent) return { ok: false, error: 'not_found' };
  if (!Number.isInteger(index) || index < 0 || index > 9999) return { ok: false, error: 'invalid_args', detail: 'window index is invalid' };
  const listed = await termListWindows(event, payload);
  if (!listed.ok) return listed;
  if (!listed.windows.some((window) => window.index === index)) return { ok: false, error: 'not_found', detail: 'window not found' };
  const client = await _ensureTmuxClientTty(ent);
  if (!client.ok) return client;
  const result = await _tmuxExec(ent, ['switch-client', '-c', ent.tmuxClientTty, '-t', `${ent.tmux.session}:${index}`]);
  return result?.ok ? termListWindows(event, payload) : result;
}

async function termCreateWindow(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  const client = await _ensureTmuxClientTty(ent);
  if (!client.ok) return client;
  const created = await _tmuxExec(ent, ['new-window', '-t', ent.tmux.session, '-P', '-F', '#{window_index}']);
  const index = Number(String(created?.stdout || '').trim());
  if (!created?.ok || !Number.isInteger(index) || index < 0 || index > 9999) return created?.ok ? { ok: false, error: 'tmux_parse_failed', detail: 'new window index missing' } : created;
  const selected = await _tmuxExec(ent, ['switch-client', '-c', ent.tmuxClientTty, '-t', `${ent.tmux.session}:${index}`]);
  return selected?.ok ? termListWindows(event, payload) : selected;
}

async function termOpen(event, payload = {}) {
  _ensureBackendsConfigured();
  const agentId = String(payload && payload.agentId || '');
  const { cols, rows } = _terminalOpenSize(payload);
  if (!agentId) return { ok: false, error: 'invalid_args', detail: 'agentId is required' };

  // Multiple terminal sessions may stay warm per renderer. Reopening the
  // same agent returns the existing channel so renderer-side fast switch can
  // show the cached xterm buffer without reconnecting.
  const [existingSid, existingEnt] = _sessionForAgent(event.sender.id, agentId);
  if (existingSid) {
    const sizeChanged = !!(existingEnt && (existingEnt._appliedCols !== cols || existingEnt._appliedRows !== rows));
    if (sizeChanged) {
      try {
        // SSH: skip the setWindow when the size is unchanged (redundant
        // resizes trigger a full tmux redraw for no benefit).
        existingEnt.resize && existingEnt.resize(cols, rows);
        existingEnt._appliedCols = cols;
        existingEnt._appliedRows = rows;
      } catch (_) {}
      // Same rule for the remote repair: a same-size reuse must not touch
      // the network — refresh-client -C forces a full tmux redraw.
      if (existingEnt.opts) void _repairRemoteTerminalSize(existingEnt.opts, agentId, cols, rows);
    }
    return { ok: true, sessionId: existingSid, reused: true };
  }

  const resolved = await embeddedHub.getAttachConnectOpts(agentId);
  if (!resolved.ok) {
    return { ok: false, error: resolved.error, detail: resolved.detail };
  }

  // `~/.cam/camc attach <agent>` is the attach contract. The embedded
  // Hub has already prepared that exact remote path before returning.
  const command = resolved.command || `~/.cam/camc attach ${agentId}`;
  const sender = event.sender;
  const sessionId = `t${++_termSeq}-${Date.now().toString(36)}`;
  const tmux = tmuxMetadataForAgent(resolved.agent);
  if (tmux && !tmux.bin) {
    // Older agent records carry no tmux_bin. Probe the remote once per
    // endpoint instead of guessing /bin/tmux; a failed probe leaves the
    // controls degraded exactly as before (strip hidden, terminal fine).
    tmux.bin = await _probeRemoteTmuxBin(resolved.opts);
  }
  if (tmux && !tmux.bin) {
    // No recorded or discoverable tmux binary on this endpoint.
    console.warn(`[tmux-probe] no tmux binary found on ${resolved.opts.host}; window controls disabled`);
  }
  const releaseTmuxDiscovery = tmux && tmux.bin ? await _beginTmuxDiscovery(`${resolved.opts.host}|${tmux.socket}`) : null;
  const initialTmuxProbe = {};

  // Attach fast path: the client-baseline probe runs in PARALLEL with
  // the channel open instead of blocking it, and the remote terminal-
  // size repair is deferred until after the channel is live. The attach
  // critical path is only getAttachConnectOpts + channel open — one
  // warm-pool round trip each. Window-control discovery keeps working
  // because the baseline probe almost always lands before our own
  // client registers; when it loses the race, discovery falls back to
  // selectOnlyClient exactly as before.
  const beforeClientsP = tmux && tmux.bin
    ? _tmuxClientSet(resolved.opts, tmux, initialTmuxProbe)
    : Promise.resolve(null);

  const tOpen0 = Date.now();
  const utf8 = _utf8Stream();
  const hooks = {
    cols, rows,
    onData: (buf) => {
      if (sender.isDestroyed()) { _dropSession(sessionId); return; }
      _termGateSend(sessionId, sender, utf8.decode(buf));
    },
    onClose: ({ code, signal }) => {
      const tail = utf8.flush();
      if (tail) _termGateSend(sessionId, sender, tail);
      _termGateClose(sessionId);
      if (!sender.isDestroyed()) {
        try { sender.send('term:status', { sessionId, kind: 'closed', code, signal }); }
        catch { /* noop */ }
      }
      _terminals.delete(sessionId);
    },
  };
  // Connect-class failures on the primary (context) endpoint retry once
  // with the agent's own machine fields (stale-context case — see
  // embedded-hub getAttachConnectOpts). Auth is included: machine_user
  // may differ from the context user.
  const ATTACH_FALLBACK_ERRORS = new Set(['connect_timeout', 'connect_refused', 'dns_failure', 'connect_lost', 'auth_failed']);
  let ch = await sshTransport.openTerminalChannel({ ...resolved.opts, command }, hooks);
  if (!ch.ok && resolved.fallbackOpts && ATTACH_FALLBACK_ERRORS.has(ch.error)) {
    console.warn(`[cam-desktop] attach via context host ${resolved.opts.host} failed (${ch.error}); retrying via agent machine fields (${resolved.fallbackOpts.host})`);
    _diagLog(`[attach] context-host ${resolved.opts.host} failed (${ch.error}); retry via machine fields ${resolved.fallbackOpts.host}`);
    ch = await sshTransport.openTerminalChannel({ ...resolved.fallbackOpts, command }, hooks);
  }
  const openMs = Date.now() - tOpen0;

  if (!ch.ok) {
    if (releaseTmuxDiscovery) releaseTmuxDiscovery();
    return { ok: false, error: ch.error, detail: ch.detail };
  }
  const beforeClients = await beforeClientsP;
  _terminals.set(sessionId, {
    dispose: ch.dispose,
    write:   ch.write,
    resize:  ch.resize,
    contentsId: sender.id,
    agentId,
    _appliedCols: cols,
    _appliedRows: rows,
    opts: resolved.opts,
    tmux,
    tmuxClientTty: '',
    tmuxControlError: tmux ? '' : 'tmux metadata unavailable',
    tmuxBeforeClients: beforeClients,
    tmuxInitialDiscoveryPending: !!tmux,
    tmuxClientRecoveryAttempts: 0,
    tmuxClientRecoveryNextAt: 0,
    tmuxClientRecoveryInFlight: false,
    tmuxLastProbeError: initialTmuxProbe.tmuxLastProbeError || '',
    tmuxLastProbeMs: initialTmuxProbe.tmuxLastProbeMs || 0,
  });
  // Post-open housekeeping: size repair + window-control discovery are
  // background work — they must never add to the attach latency.
  void _repairRemoteTerminalSize(resolved.opts, agentId, cols, rows);
  if (releaseTmuxDiscovery) void _discoverTmuxClient(sessionId, beforeClients).finally(releaseTmuxDiscovery);
  _termGateOpen(sessionId, sender);
  return { ok: true, sessionId, timings: { open_ms: openMs } };
}

function termInput(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  const data = String(payload.data == null ? '' : payload.data);
  ent.write(data);
  return { ok: true };
}

/** Per-channel streaming UTF-8 decoder. A multibyte character split
 *  across transport chunks must not decode to '�' — decode with
 *  {stream: true} so trailing partial bytes carry into the next chunk,
 *  and flush the remainder when the channel closes. (Tabby's
 *  UTF8SplitterMiddleware equivalent.) */
function _utf8Stream() {
  const dec = new TextDecoder('utf-8');
  return {
    decode: (buf) => dec.decode(buf, { stream: true }),
    flush: () => dec.decode(),
  };
}

/* ── Ready-gate (Tabby initialDataBuffer equivalent) ────────────────
 * tmux repaints the whole pane instantly on attach; bytes arriving
 * before the renderer's xterm has opened and fitted garble the first
 * screen. Every session's data is buffered until the renderer signals
 * term:ready, with a hard cap (drop-oldest) and a fallback flush so a
 * stuck renderer can never wedge the channel. */
const TERM_READY_FALLBACK_MS = 2000;
const TERM_READY_BUFFER_CAP = 256 * 1024;
const _termReady = new Map(); // sessionId -> gate

function _rawTermSend(sessionId, sender, data) {
  if (!sender || sender.isDestroyed()) return;
  try { sender.send('term:data', { sessionId, data }); } catch { /* torn down */ }
}

function _termGateOpen(sessionId, sender) {
  const gate = { ready: false, chunks: [], bytes: 0, sender, timer: null };
  gate.timer = setTimeout(() => {
    gate.ready = true; // renderer never signalled — flush anyway
    _termGateFlush(sessionId, gate);
  }, TERM_READY_FALLBACK_MS);
  if (gate.timer.unref) gate.timer.unref();
  _termReady.set(sessionId, gate);
}

function _termGateSend(sessionId, sender, data) {
  const gate = _termReady.get(sessionId);
  if (!gate || gate.ready) return _rawTermSend(sessionId, sender, data);
  gate.sender = sender;
  gate.chunks.push(data);
  gate.bytes += data.length;
  while (gate.bytes > TERM_READY_BUFFER_CAP && gate.chunks.length > 1) {
    gate.bytes -= gate.chunks.shift().length;
  }
}

function _termGateFlush(sessionId, gate) {
  if (gate.timer) { clearTimeout(gate.timer); gate.timer = null; }
  for (const chunk of gate.chunks) _rawTermSend(sessionId, gate.sender, chunk);
  gate.chunks = [];
  gate.bytes = 0;
}

function _termGateClose(sessionId) {
  const gate = _termReady.get(sessionId);
  if (!gate) return;
  gate.ready = true;
  _termGateFlush(sessionId, gate);
  _termReady.delete(sessionId);
}

function termReady(event, payload = {}) {
  const sid = String(payload && payload.sessionId || '');
  const gate = _termReady.get(sid);
  if (!gate) return { ok: true, ignored: true };
  gate.ready = true;
  _termGateFlush(sid, gate);
  return { ok: true };
}

async function termResize(event, payload = {}) {
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  const size = _terminalResizeSize(payload);
  if (!size) return { ok: true, ignored: true, reason: 'invalid_terminal_size' };
  // Skip the setWindow round trip when the size is unchanged — every
  // resize makes tmux redraw the pane, so redundant resizes add visible
  // churn on tab switches without changing anything.
  if (ent._appliedCols !== size.cols || ent._appliedRows !== size.rows) {
    ent.resize(size.cols, size.rows);
    ent._appliedCols = size.cols;
    ent._appliedRows = size.rows;
  }
  return { ok: true };
}

function termClose(event, payload = {}) {
  const sid = String(payload && payload.sessionId || '');
  const ent = _ownedTerminal(event, payload);
  if (!ent) return { ok: false, error: 'not_found' };
  _dropSession(sid);
  return { ok: true };
}

async function filesPickPrivateKey() {
  const wins = BrowserWindow.getAllWindows();
  const owner = wins.length > 0 ? wins[0] : null;
  const r = await dialog.showOpenDialog(owner || undefined, {
    title: 'Select SSH private key file',
    properties: ['openFile', 'showHiddenFiles'],
  });
  if (r.canceled || !r.filePaths || r.filePaths.length === 0) {
    return { path: null };
  }
  return { path: r.filePaths[0] };
}

function filesReadClipboardText() {
  try {
    return { ok: true, text: clipboard.readText() || '' };
  } catch (e) {
    return { ok: false, error: 'clipboard_text_failed', detail: e && e.message || String(e) };
  }
}

async function filesPickAttachment() {
  const wins = BrowserWindow.getAllWindows();
  const owner = wins.length > 0 ? wins[0] : null;
  const r = await dialog.showOpenDialog(owner || undefined, {
    title: 'Attach file',
    properties: ['openFile'],
  });
  if (r.canceled || !r.filePaths || r.filePaths.length === 0) {
    return { ok: false, canceled: true };
  }
  const selected = r.filePaths[0];
  try {
    const st = fs.statSync(selected);
    if (!st.isFile()) return { ok: false, error: 'not_file' };
    const maxBytes = 50 * 1024 * 1024;
    if (st.size > maxBytes) return { ok: false, error: 'too_large', size: st.size, maxBytes };
    const buf = fs.readFileSync(selected);
    return {
      ok: true,
      filename: path.basename(selected),
      size: buf.length,
      data: buf.toString('base64'),
    };
  } catch (e) {
    return { ok: false, error: 'read_failed', detail: e && e.message || String(e) };
  }
}

function _attachmentFromPath(selected) {
  try {
    const st = fs.statSync(selected);
    if (!st.isFile()) return { ok: false, error: 'not_file', path: selected };
    const maxBytes = 50 * 1024 * 1024;
    if (st.size > maxBytes) return { ok: false, error: 'too_large', size: st.size, maxBytes, path: selected };
    const buf = fs.readFileSync(selected);
    return {
      ok: true,
      filename: path.basename(selected),
      size: buf.length,
      data: buf.toString('base64'),
    };
  } catch (e) {
    return { ok: false, error: 'read_failed', detail: e && e.message || String(e), path: selected };
  }
}

function _clipboardFilePaths() {
  const formats = process.platform === 'win32' ? ['FileNameW', 'FileName'] : ['text/uri-list'];
  for (const fmt of formats) {
    try {
      const b = clipboard.readBuffer(fmt);
      if (!b || !b.length) continue;
      if (fmt === 'FileNameW') {
        return b.toString('utf16le').split('\u0000').map(v => v.trim()).filter(Boolean);
      }
      if (fmt === 'FileName') {
        return b.toString('utf8').split('\u0000').map(v => v.trim()).filter(Boolean);
      }
      const txt = b.toString('utf8');
      return txt.split(/\r?\n/).map(v => v.trim()).filter(v => v && !v.startsWith('#'))
        .map(v => v.startsWith('file://') ? decodeURIComponent(v.replace(/^file:\/\//, '')) : v);
    } catch (_) {}
  }
  return [];
}

async function filesReadClipboardAttachments() {
  const paths = _clipboardFilePaths();
  if (paths.length) {
    const files = [];
    for (const p of paths.slice(0, 8)) {
      const r = _attachmentFromPath(p);
      if (!r.ok) return r;
      files.push(r);
    }
    return { ok: true, files, source: 'files' };
  }

  try {
    const img = clipboard.readImage();
    if (img && !img.isEmpty()) {
      const buf = img.toPNG();
      const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*/, '').replace('T', '-');
      return {
        ok: true,
        source: 'image',
        files: [{ filename: `clipboard-image-${ts}.png`, size: buf.length, data: buf.toString('base64') }],
      };
    }
  } catch (e) {
    return { ok: false, error: 'clipboard_image_failed', detail: e && e.message || String(e) };
  }

  return { ok: false, error: 'empty_clipboard', detail: 'Clipboard does not contain a file or image.' };
}
/* ─────────────── App lifecycle ─────────────── */

app.whenReady().then(() => {
  ipcMain.on('cam:restart', () => {
    app.relaunch();
    app.exit(0);
  });

  // Direct Hub lifecycle (CAM-DESK-DIRECT-010..019). All handlers are
  // argument-free or take a tiny structured payload; main owns every
  // port/token decision. The renderer cannot specify a binary, a
  // path, an environment, or a shell string — there is no shell.
  ipcMain.handle('local:check',      () => localCheck());
  ipcMain.handle('local:start',      () => localStart());
  ipcMain.handle('local:stop',       () => localStop());
  ipcMain.handle('local:restart',    () => localRestart());
  ipcMain.handle('local:logs',       () => localLogs());
  ipcMain.handle('local:getProfile', () => localGetProfile());

  // Narrow file picker for the Nodes "Add Host" key-file field.
  // Argument-free; main owns the dialog config.
  ipcMain.handle('files:pickPrivateKey', () => filesPickPrivateKey());
  ipcMain.handle('files:pickAttachment',  () => filesPickAttachment());
  ipcMain.handle('files:readClipboardText', () => filesReadClipboardText());
  ipcMain.handle('files:readClipboardAttachments', () => filesReadClipboardAttachments());
  ipcMain.handle('net:probe', (_event, payload) => netProbe(payload && payload.url || '', payload && payload.timeoutMs || 8000));

  // Terminal mode (CAM-DESK-TERM-001..005). Secrets stay in main.
  ipcMain.handle('term:open',   (event, p) => termOpen(event, p));
  ipcMain.handle('term:ready',  (event, p) => termReady(event, p));
  ipcMain.handle('term:input',  (event, p) => termInput(event, p));
  ipcMain.handle('term:resize', (event, p) => termResize(event, p));
  ipcMain.handle('term:close',  (event, p) => termClose(event, p));
  ipcMain.handle('term:listWindows', (event, p) => termListWindows(event, p));
  ipcMain.handle('term:selectWindow', (event, p) => termSelectWindow(event, p));
  ipcMain.handle('term:createWindow', (event, p) => termCreateWindow(event, p));
  ipcMain.handle('term:copyMode', (event, p) => termEnterCopyMode(event, p));
  ipcMain.handle('term:cancelCopyMode', (event, p) => termCancelCopyMode(event, p));
  createMainWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// After an OS sleep the pooled SSH sockets that were idle are almost
// certainly half-dead — drop exactly those (busy connections keep their
// live terminals and detect via their own traffic). The next attach
// then reconnects fresh instead of hanging on a corpse for 45-60s.
powerMonitor.on('resume', () => {
  try {
    sshTransport.dropIdleEntries();
    console.warn('[cam-desktop] OS resume: dropped idle pooled SSH entries');
    _diagLog('[cam-desktop] OS resume: dropped idle pooled SSH entries');
  } catch (_) {}
});

// CAM-DESK-DIRECT-011 ownership cleanup: when the app exits, stop the
// embedded Hub we started. Nothing else listens on the loopback port
// we own, so this is a no-op if Start was never clicked.
app.on('before-quit', () => {
  // Dispose every open terminal channel before the embedded Hub
  // teardown so any pooled ssh2 client can flush its END/CLOSE frames
  // cleanly.
  for (const sid of [..._terminals.keys()]) _dropSession(sid);
  try {
    // best-effort, synchronous-flavored — the close callback may fire
    // after Electron exits; that's fine because the OS reclaims the
    // socket on process exit.
    embeddedHub.stop();
  } catch (_) {}
});

// Single-instance: focus existing window if user reopens the app.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const wins = BrowserWindow.getAllWindows();
    if (wins.length > 0) {
      const w = wins[0];
      if (w.isMinimized()) w.restore();
      w.focus();
    }
  });
}
