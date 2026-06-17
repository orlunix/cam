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
 *   - mints an opaque session id, keeps the channel handle in a
 *     per-window Map, and pipes data events to the originating
 *     WebContents as 'term:data' / 'term:status'.
 *   - accepts input/resize/close via 'term:input' / 'term:resize' /
 *     'term:close'. Closing the channel does NOT kill the underlying
 *     agent — `camc attach` is a tmux attach to the agent session.
 *
 * Secrets stay in main only. The renderer sees session id + bytes. */
const _terminals = new Map();   // sessionId → { dispose, contentsId, agentId }
let _termSeq = 0;

function _sessionFor(contentsId) {
  for (const [sid, ent] of _terminals) {
    if (ent && ent.contentsId === contentsId) return [sid, ent];
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
  const cols = Math.max(2, Math.min(500, Number(payload.cols) || 80));
  const rows = Math.max(2, Math.min(500, Number(payload.rows) || 24));
  if (!agentId) return { ok: false, error: 'invalid_args', detail: 'agentId is required' };

  // Single attach per renderer at a time: opening a new one closes the
  // previous (the user is switching agents). Renderer can still
  // explicitly term:close before opening.
  const [prevSid] = _sessionFor(event.sender.id);
  if (prevSid) _dropSession(prevSid);

  const resolved = await embeddedHub.getAttachConnectOpts(agentId);
  if (!resolved.ok) {
    return { ok: false, error: resolved.error, detail: resolved.detail };
  }

  // `~/.cam/camc attach <agent>` is the attach contract. The embedded
  // Hub has already prepared that exact remote path before returning.
  const command = resolved.command || `~/.cam/camc attach ${agentId}`;
  const sender = event.sender;
  const sessionId = `t${++_termSeq}-${Date.now().toString(36)}`;

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
  const cols = Math.max(2, Math.min(500, Number(payload.cols) || 80));
  const rows = Math.max(2, Math.min(500, Number(payload.rows) || 24));
  ent.resize(cols, rows);
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
    filters: [
      { name: 'All files', extensions: ['*'] },
    ],
  });
  if (r.canceled || !r.filePaths || r.filePaths.length === 0) {
    return { path: null };
  }
  return { path: r.filePaths[0] };
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
