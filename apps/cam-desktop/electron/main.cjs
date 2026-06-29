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

const { app, BrowserWindow, ipcMain, shell, dialog, safeStorage, Menu, clipboard } = require('electron');
const fs   = require('node:fs');
const path = require('node:path');
const url  = require('node:url');
const http = require('node:http');
const https = require('node:https');

const embeddedHub     = require('./embedded-hub.cjs');
const credentialStore = require('./credential-store.cjs');
const sshTransport    = require('./ssh-transport.cjs');

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

async function _repairRemoteTerminalSize(opts, agentId, cols, rows) {
  if (!opts || !agentId) return;
  try {
    await sshTransport.execRemote({
      ...opts,
      command: _terminalRepairCommand(agentId, cols, rows),
      timeout_ms: 8000,
    });
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

function _dropSession(sessionId) {
  const ent = _terminals.get(sessionId);
  if (!ent) return;
  _terminals.delete(sessionId);
  try { ent.dispose && ent.dispose(); } catch { /* noop */ }
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
    try { existingEnt && existingEnt.resize && existingEnt.resize(cols, rows); } catch (_) {}
    if (existingEnt && existingEnt.opts) {
      void _repairRemoteTerminalSize(existingEnt.opts, agentId, cols, rows);
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

  await _repairRemoteTerminalSize(resolved.opts, agentId, cols, rows);

  const ch = await sshTransport.openTerminalChannel(
    { ...resolved.opts, command },
    {
      cols, rows,
      onData: (buf) => {
        if (sender.isDestroyed()) { _dropSession(sessionId); return; }
        try { sender.send('term:data', { sessionId, data: buf.toString('utf8') }); }
        catch { /* sender torn down */ }
      },
      onClose: ({ code, signal }) => {
        if (!sender.isDestroyed()) {
          try { sender.send('term:status', { sessionId, kind: 'closed', code, signal }); }
          catch { /* noop */ }
        }
        _terminals.delete(sessionId);
      },
    }
  );

  if (!ch.ok) {
    return { ok: false, error: ch.error, detail: ch.detail };
  }
  _terminals.set(sessionId, {
    dispose: ch.dispose,
    write:   ch.write,
    resize:  ch.resize,
    contentsId: sender.id,
    agentId,
    opts: resolved.opts,
  });
  return { ok: true, sessionId };
}

function termInput(event, payload = {}) {
  const sid = String(payload && payload.sessionId || '');
  const ent = _terminals.get(sid);
  if (!ent || ent.contentsId !== event.sender.id) return { ok: false, error: 'not_found' };
  const data = String(payload.data == null ? '' : payload.data);
  ent.write(data);
  return { ok: true };
}

function termResize(event, payload = {}) {
  const sid = String(payload && payload.sessionId || '');
  const ent = _terminals.get(sid);
  if (!ent || ent.contentsId !== event.sender.id) return { ok: false, error: 'not_found' };
  const size = _terminalResizeSize(payload);
  if (!size) return { ok: true, ignored: true, reason: 'invalid_terminal_size' };
  ent.resize(size.cols, size.rows);
  return { ok: true };
}

function termClose(event, payload = {}) {
  const sid = String(payload && payload.sessionId || '');
  const ent = _terminals.get(sid);
  if (!ent || ent.contentsId !== event.sender.id) return { ok: false, error: 'not_found' };
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
  ipcMain.handle('term:input',  (event, p) => termInput(event, p));
  ipcMain.handle('term:resize', (event, p) => termResize(event, p));
  ipcMain.handle('term:close',  (event, p) => termClose(event, p));

  createMainWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
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
