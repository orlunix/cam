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

const { app, BrowserWindow, ipcMain, shell, dialog, safeStorage } = require('electron');
const path = require('node:path');
const url  = require('node:url');

const embeddedHub     = require('./embedded-hub.cjs');
const credentialStore = require('./credential-store.cjs');
const sshTransport    = require('./ssh-transport.cjs');

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
