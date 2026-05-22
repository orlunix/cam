/**
 * CAM Desktop — Electron main process.
 *
 * Loads the WebUI-derived desktop entry (web/desktop.html) into a
 * BrowserWindow with strict renderer isolation. The renderer is a thin
 * HTTP/WS client of an existing CAM endpoint (direct or relay).
 *
 * Phase 2A adds a narrow backend-readiness bridge that detects whether
 * a usable local CAM environment exists (cam CLI, Python, WSL on Windows)
 * and optionally starts an already-installed local backend. It never
 * installs or upgrades anything and never accepts user-provided command
 * strings — only fixed argv built from system-trusted data.
 */

'use strict';

const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('node:path');
const url = require('node:url');
const { execFile, spawn } = require('node:child_process');
const crypto = require('node:crypto');

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

/* ─────────────── Backend readiness (Phase 2A) ─────────────── */

const DEFAULT_PORT = 8420;
const DEFAULT_SERVER_URL = `http://127.0.0.1:${DEFAULT_PORT}`;
const DEFAULT_HEALTH_URL = `${DEFAULT_SERVER_URL}/api/system/health`;

// Distro names accepted from `wsl -l -q`. Restrictive on purpose; if the
// real distro name doesn't match, we skip it rather than execute it.
const SAFE_DISTRO_RE = /^[A-Za-z0-9._-]+$/;

// wsl.exe emits UTF-16 LE with a leading BOM. Strip stray NUL / BOM
// characters after decoding. Constructed via RegExp() so the source file
// can stay free of literal NUL bytes.
const WSL_NOISE_RE = new RegExp('[\\u0000\\uFEFF]', 'g');

function execFileSafe(cmd, args, opts = {}) {
  const { timeoutMs = 4000, encoding = 'utf8' } = opts;
  const emptyOut = encoding === 'buffer' ? Buffer.alloc(0) : '';
  return new Promise((resolve) => {
    let done = false;
    const finish = (payload) => {
      if (done) return;
      done = true;
      resolve(payload);
    };
    let child;
    try {
      child = execFile(
        cmd,
        args,
        { timeout: timeoutMs, encoding, windowsHide: true },
        (err, stdout, stderr) => finish({ err, stdout: stdout ?? emptyOut, stderr: stderr ?? '' }),
      );
    } catch (e) {
      finish({ err: e, stdout: emptyOut, stderr: '' });
      return;
    }
    child.on('error', (err) => {
      finish({ err, stdout: emptyOut, stderr: err.message });
    });
  });
}

/**
 * Probe the default local health endpoint and verify the responder is
 * actually a CAM server, not a random listener on port 8420.
 *
 * Returns:
 *   { running: true,  cam: true,  status, version? }   — confirmed CAM
 *   { running: false, portOccupied: true, status }      — something else listening
 *   { running: false, portOccupied: false, error }      — nothing listening
 */
async function probeLocalHealth(timeoutMs = 1500) {
  let resp;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    resp = await fetch(DEFAULT_HEALTH_URL, { signal: ctrl.signal });
    clearTimeout(t);
  } catch (e) {
    return { running: false, portOccupied: false, error: e?.message || String(e) };
  }
  if (!resp.ok) {
    return { running: false, portOccupied: true, status: resp.status };
  }
  let body;
  try { body = await resp.json(); }
  catch { return { running: false, portOccupied: true, status: resp.status }; }
  // CAM's HealthResponse schema includes version + adapters + agents_running.
  // Require at least version and one of the other two to avoid false positives.
  const isCam =
    body && typeof body === 'object' &&
    typeof body.version === 'string' &&
    (Array.isArray(body.adapters) || typeof body.agents_running === 'number');
  if (!isCam) {
    return { running: false, portOccupied: true, status: resp.status };
  }
  return { running: true, cam: true, status: resp.status, version: body.version };
}

function emptyReadiness(extra = {}) {
  return {
    platform: process.platform,
    hasWsl: false,
    wslDistros: [],
    selectedDistro: null,
    hasPython: false,
    hasCam: false,
    localServerRunning: false,
    localPortOccupiedByOther: false,
    suggestedCommand: '',
    message: '',
    ...extra,
  };
}

/**
 * Linux / macOS detection.
 *
 * Both probes run under `bash -lc` so login-shell PATH (e.g.
 * `~/.local/bin`) is loaded — Electron launched from a GUI menu otherwise
 * does not see those entries.
 */
async function detectLinuxMac() {
  const py = await execFileSafe('bash', ['-lc', 'command -v python3 || command -v python'], { timeoutMs: 3000 });
  const hasPython = !py.err && (py.stdout || '').trim().length > 0;

  const cam = await execFileSafe('bash', ['-lc', 'command -v cam'], { timeoutMs: 3000 });
  const hasCam = !cam.err && (cam.stdout || '').trim().length > 0;

  const health = await probeLocalHealth();

  let suggestedCommand = '';
  let message = '';
  if (!hasCam) {
    suggestedCommand = `pip install --user "cam[server]" && cam serve --port ${DEFAULT_PORT}`;
    message = 'CAM CLI not detected on local PATH. Install CAM with the server extras, then click Check again.';
  } else if (health.portOccupied) {
    message =
      `Port ${DEFAULT_PORT} is in use but the responder is not a CAM server. ` +
      'Stop that process before starting CAM here.';
  } else if (!health.running) {
    suggestedCommand = `cam serve --port ${DEFAULT_PORT}`;
    message = 'CAM is installed but no local server is listening. You can start it from here.';
  } else {
    message =
      `CAM ${health.version ? 'v' + health.version + ' ' : ''}is listening at ` +
      `127.0.0.1:${DEFAULT_PORT}. If you need a token, supply it below or click ` +
      'Start backend to relaunch with a fresh one (stop the existing server first).';
  }

  return emptyReadiness({
    hasPython,
    hasCam,
    localServerRunning: !!health.running,
    localPortOccupiedByOther: !!health.portOccupied,
    suggestedCommand,
    message,
  });
}

function decodeWslText(buf) {
  if (!Buffer.isBuffer(buf)) return '';
  return buf.toString('utf16le').replace(WSL_NOISE_RE, '');
}

/**
 * Windows detection. Probes WSL via `wsl.exe` only; native-Windows Python
 * is not in scope for Phase 2A.
 */
async function detectWindows() {
  const status = await execFileSafe('wsl.exe', ['--status'], { timeoutMs: 3000, encoding: 'buffer' });
  const statusText = decodeWslText(status.stdout);
  const hasWsl = !status.err && /WSL|Default|Version/i.test(statusText);

  let wslDistros = [];
  let selectedDistro = null;
  if (hasWsl) {
    const list = await execFileSafe('wsl.exe', ['-l', '-q'], { timeoutMs: 3000, encoding: 'buffer' });
    const listText = decodeWslText(list.stdout);
    wslDistros = listText
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter((s) => s && SAFE_DISTRO_RE.test(s));
    selectedDistro =
      wslDistros.find((d) => !/^docker-desktop/i.test(d)) || wslDistros[0] || null;
  }

  let hasPython = false;
  let hasCam = false;
  if (selectedDistro) {
    const py = await execFileSafe(
      'wsl.exe',
      ['-d', selectedDistro, '--', 'bash', '-lc', 'command -v python3 || command -v python'],
      { timeoutMs: 5000 },
    );
    hasPython = !py.err && (py.stdout || '').trim().length > 0;

    const cam = await execFileSafe(
      'wsl.exe',
      ['-d', selectedDistro, '--', 'bash', '-lc', 'command -v cam'],
      { timeoutMs: 5000 },
    );
    hasCam = !cam.err && (cam.stdout || '').trim().length > 0;
  }

  const health = await probeLocalHealth();

  let suggestedCommand = '';
  let message = '';
  if (!hasWsl) {
    message = 'WSL is not available. Full Windows setup is deferred to Phase 2C.';
  } else if (!selectedDistro) {
    message = 'WSL is available but no usable distro was detected. Phase 2C will help install one.';
  } else if (!hasCam) {
    suggestedCommand = `wsl -d ${selectedDistro} -- pip install --user "cam[server]"`;
    message =
      `WSL detected (distro: ${selectedDistro}), but CAM is not installed inside it. ` +
      'Install CAM with the server extras in WSL, then click Check again. ' +
      '(Full WSL bootstrap is deferred to Phase 2B.)';
  } else if (health.portOccupied) {
    message =
      `Port ${DEFAULT_PORT} on 127.0.0.1 is in use but the responder is not a CAM server. ` +
      'Stop that process before starting CAM here.';
  } else if (!health.running) {
    suggestedCommand = `wsl -d ${selectedDistro} -- cam serve --port ${DEFAULT_PORT}`;
    message =
      `CAM is installed in WSL (${selectedDistro}) but no local server is listening. ` +
      'You can start it from here.';
  } else {
    message =
      `CAM ${health.version ? 'v' + health.version + ' ' : ''}is listening at ` +
      `127.0.0.1:${DEFAULT_PORT} (via WSL ${selectedDistro}). If you need a ` +
      'token, supply it below or click Start backend to relaunch with a fresh ' +
      'one (stop the existing server first).';
  }

  return emptyReadiness({
    hasWsl,
    wslDistros,
    selectedDistro,
    hasPython,
    hasCam,
    localServerRunning: !!health.running,
    localPortOccupiedByOther: !!health.portOccupied,
    suggestedCommand,
    message,
  });
}

async function checkBackendReadiness() {
  try {
    if (process.platform === 'win32') return await detectWindows();
    return await detectLinuxMac();
  } catch (e) {
    return emptyReadiness({
      message: `Readiness check failed: ${e?.message || String(e)}`,
    });
  }
}

/**
 * Generate an opaque token for `cam serve --token`. URL-safe base64 of 24
 * random bytes; no '=', '+', or '/' characters → safe to embed in a
 * `bash -lc` argv string without quoting.
 */
function generateToken() {
  return crypto.randomBytes(24).toString('base64url');
}

/**
 * Start an already-installed local backend with a freshly-generated token.
 *
 * Linux/macOS: spawn `bash -lc "cam serve --port 8420 --token <tok>"` detached.
 * Windows:     spawn `wsl.exe -d <distro> -- bash -lc "..."` detached.
 *
 * Polls the validated CAM health endpoint until success or timeout.
 * Returns `{ ok, url?, token?, message }`. The token is returned to the
 * renderer so it can be persisted in localStorage and used by CamApi —
 * stdout is never parsed.
 */
async function startLocalBackend() {
  try {
    const r = await checkBackendReadiness();
    if (r.localServerRunning) {
      // A CAM server is already up, but we cannot know its token from
      // outside the process. Tell the renderer instead of silently
      // claiming success without credentials.
      return {
        ok: false,
        url: DEFAULT_SERVER_URL,
        message:
          'A CAM server is already listening on 127.0.0.1:' + DEFAULT_PORT + '. ' +
          'Enter its existing token in the Direct connection fields below, ' +
          'or stop that server and click Start again to relaunch with a fresh token.',
      };
    }
    if (r.localPortOccupiedByOther) {
      return {
        ok: false,
        message:
          `Port ${DEFAULT_PORT} is in use by a non-CAM service. ` +
          'Free the port (or stop that service) and try Start again.',
      };
    }
    if (!r.hasCam) {
      return {
        ok: false,
        message:
          process.platform === 'win32'
            ? 'CAM is not installed inside any WSL distro. Install CAM with the server extras first (Phase 2B will handle this).'
            : 'CAM CLI is not on PATH. Install CAM with the server extras first.',
      };
    }

    const token = generateToken();
    const serveCmd = `cam serve --port ${DEFAULT_PORT} --token ${token}`;
    let proc;
    if (process.platform === 'win32') {
      if (!r.selectedDistro || !SAFE_DISTRO_RE.test(r.selectedDistro)) {
        return { ok: false, message: 'No usable WSL distro to host the backend.' };
      }
      proc = spawn(
        'wsl.exe',
        ['-d', r.selectedDistro, '--', 'bash', '-lc', serveCmd],
        { detached: true, stdio: 'ignore', windowsHide: true },
      );
    } else {
      proc = spawn('bash', ['-lc', serveCmd], {
        detached: true,
        stdio: 'ignore',
      });
    }
    proc.on('error', () => { /* surfaced via health probe outcome */ });
    proc.unref();

    // Poll the validated CAM health probe for up to ~12s.
    const deadline = Date.now() + 12000;
    while (Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 500));
      const h = await probeLocalHealth(800);
      if (h.running) {
        return {
          ok: true,
          url: DEFAULT_SERVER_URL,
          token,
          message: 'Backend started with a fresh token.',
        };
      }
    }
    return {
      ok: false,
      message:
        'Launched the backend process but no CAM health response within 12 seconds. ' +
        'Open a terminal and run the suggested command to see the error.',
    };
  } catch (e) {
    return { ok: false, message: `Start failed: ${e?.message || String(e)}` };
  }
}

/* ─────────────── App lifecycle ─────────────── */

app.whenReady().then(() => {
  ipcMain.on('cam:restart', () => {
    app.relaunch();
    app.exit(0);
  });

  ipcMain.handle('cam:checkBackendReadiness', () => checkBackendReadiness());
  ipcMain.handle('cam:startLocalBackend', () => startLocalBackend());

  createMainWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
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
