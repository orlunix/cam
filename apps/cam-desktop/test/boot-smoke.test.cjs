'use strict';

/* Boot smoke test (CAM-DESK-SMOKE-001): launches the real Electron app
 * with an isolated userData dir and asserts the boot chain that the
 * 2026-07-22 preload-sandbox regression broke:
 *
 *   1. the preload script loads (no "Unable to load preload script"),
 *   2. CamBridge is exposed with the expected surface,
 *   3. the embedded hub actually starts (local proxy connects).
 *
 * Runs the app ~15s; excluded from the fast suites — invoke via
 * `npm run test:smoke`. Requires a desktop session (opens a window).
 */

const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const CDP_PORT = 9223;
const BOOT_TIMEOUT_MS = 30000;
const LISTEN_MS = 8000;

let pass = 0;
let fail = 0;
function ok(name, condition, detail = '') {
  if (condition) { pass++; console.log('ok ' + name); }
  else { fail++; console.error('FAIL ' + name + (detail ? ' — ' + detail : '')); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForPage() {
  for (let i = 0; i < BOOT_TIMEOUT_MS / 500; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
      const list = await r.json();
      const page = list.find((t) => t.type === 'page' && /desktop\.html/.test(t.url));
      if (page) return page;
    } catch (_) {}
    await sleep(500);
  }
  return null;
}

async function main() {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'cam-smoke-'));
  // require('electron') resolves to the executable path when loaded
  // from plain Node (not inside Electron).
  const electronBin = require('electron');
  const child = spawn(String(electronBin), ['.', `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${userData}`], {
    cwd: ROOT,
    stdio: 'ignore',
  });
  const cleanup = () => {
    try { child.kill('SIGTERM'); } catch (_) {}
    try { fs.rmSync(userData, { recursive: true, force: true }); } catch (_) {}
  };
  process.on('exit', cleanup);

  const pageErrors = [];
  try {
    const page = await waitForPage();
    ok('app exposes a desktop.html CDP target', !!page);
    if (!page) return;

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    let id = 0;
    const pending = new Map();
    ws.addEventListener('message', (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails;
        pageErrors.push((d.exception && (d.exception.description || d.exception.value)) || d.text);
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        pageErrors.push(m.params.args.map((a) => a.value ?? a.description ?? '').join(' '));
      }
    });
    const send = (method, params = {}) => new Promise((res) => {
      const mid = ++id;
      pending.set(mid, res);
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
    const evaluate = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      return r && r.result ? r.result.value : undefined;
    };
    await send('Runtime.enable');
    await sleep(LISTEN_MS);

    const preloadFailed = pageErrors.some((e) => /Unable to load preload/i.test(String(e)));
    ok('preload script loads (no sandbox require failure)', !preloadFailed,
      pageErrors.filter((e) => /preload/i.test(String(e))).join(' | ').slice(0, 300));

    const bridge = await evaluate(
      `typeof CamBridge !== 'undefined' && typeof CamBridge.directHub !== 'undefined' && typeof CamBridge.getSystemUser === 'function'`
    );
    ok('CamBridge surface is exposed (directHub + getSystemUser)', bridge === true, 'got ' + bridge);

    const started = await evaluate(`CamBridge.directHub.start().then(r => !!(r && r.ok)).catch(() => false)`);
    ok('embedded hub starts via directHub.start()', started === true, 'got ' + started);
  } finally {
    cleanup();
  }
}

main()
  .catch((e) => { fail++; console.error('FAIL harness error — ' + (e && e.message)); })
  .finally(() => {
    console.log('\n' + pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
  });
