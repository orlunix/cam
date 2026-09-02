'use strict';

/* cam-container smoke + contract tests: boots the real server (embedded hub,
 * assistant host, term WS) on an ephemeral port with a temp data dir.
 *
 *  - boot smoke: static UI + shim injection, hub proxy auth, traversal guard
 *  - shim surface: cam-web-shim.js implements the whole CamBridge surface
 *    that preload.cjs exposes (string guards, ext-nav style)
 *  - AES safeStorage: roundtrip + wrong-key failure
 *  - assistant over HTTP: configure → start → send → poll done (mock LLM),
 *    exercising the plain-Node child spawn path
 *  - WS rail: token gate + term frame dispatch (open on a bogus agent must
 *    fail cleanly, not hang)
 *
 * Run:  node apps/cam-container/test/server.test.cjs
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const assert = require('assert');

const SERVER_DIR = path.join(__dirname, '..');
const SHIM = path.join(SERVER_DIR, 'lib', 'cam-web-shim.js');
const PRELOAD = path.join(SERVER_DIR, '..', 'cam-desktop', 'electron', 'preload.cjs');

let n = 0;
function ok(cond, msg, extra) { assert(cond, msg + (extra ? ' :: ' + extra : '')); n++; }

async function waitFor(fn, label, timeoutMs = 15000) {
  const t0 = Date.now();
  for (;;) {
    const r = await fn();
    if (r) return r;
    if (Date.now() - t0 > timeoutMs) throw new Error('timeout waiting for ' + label);
    await new Promise((r) => setTimeout(r, 100));
  }
}

/* ── shim surface parity (static, no server needed) ── */
(function shimSurface() {
  const shim = fs.readFileSync(SHIM, 'utf8');
  const preload = fs.readFileSync(PRELOAD, 'utf8');
  const need = [
    'getPlatform', 'getAppVersion', 'getSystemUser', 'openExternal',
    'restartApp', 'resetApp', 'diagLog', 'diagTail',
  ];
  for (const m of need) {
    ok(preload.includes(m) && shim.includes(m), `shim implements CamBridge.${m}`);
  }
  for (const m of ['check', 'start', 'stop', 'restart', 'logs', 'getProfile']) {
    ok(shim.includes('directHub'), `shim has directHub`);
    break;
  }
  for (const m of ['pickPrivateKey', 'pickAttachment', 'pickFile', 'saveText', 'saveFile', 'readClipboardText', 'readClipboardAttachments']) {
    ok(shim.includes(m), `shim implements files.${m}`);
  }
  for (const m of ['status', 'configure', 'models', 'start', 'stop', 'send', 'poll', 'reset', 'threads', 'newChat', 'openThread', 'deleteThread', 'onEvent']) {
    ok(new RegExp(`\\b${m}\\s*\\(`).test(shim), `shim implements assistant.${m}`);
  }
  for (const m of ['open', 'ready', 'input', 'resize', 'close', 'listWindows', 'selectWindow', 'createWindow', 'copyMode', 'cancelCopyMode', 'onData', 'onStatus']) {
    ok(new RegExp(`\\b${m}\\s*\\(`).test(shim), `shim implements term.${m}`);
  }
  ok(shim.includes("ch: 'term'"), 'shim term frames carry the term channel tag');
})();

/* ── AES safeStorage shim ── */
(async function aesRoundtrip() {
  const { createAesSafeStorage } = await import(path.join(SERVER_DIR, 'lib', 'aes-safestorage.mjs'));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cam-aes-'));
  const ss = createAesSafeStorage({ dataDir: tmp, env: {} });
  ok(ss.isEncryptionAvailable() === true, 'AES shim reports available');
  const blob = ss.encryptString('s3cret-值');
  ok(Buffer.isBuffer(blob) && blob.length > 29, 'encrypt returns a buffer');
  ok(ss.decryptString(blob) === 's3cret-值', 'roundtrip decrypts');
  const ss2 = createAesSafeStorage({ dataDir: tmp, env: {} }); // same key file
  ok(ss2.decryptString(blob) === 's3cret-值', 'key file persists across instances');
  const tmp2 = fs.mkdtempSync(path.join(os.tmpdir(), 'cam-aes-'));
  const ssOther = createAesSafeStorage({ dataDir: tmp2, env: {} });
  let threw = false;
  try { ssOther.decryptString(blob); } catch (_) { threw = true; }
  ok(threw, 'wrong key fails to decrypt');
  const ssEnv = createAesSafeStorage({ dataDir: tmp2, env: { CAM_SECRET_KEY: 'a'.repeat(64) } });
  const b2 = ssEnv.encryptString('x');
  ok(ssEnv.decryptString(b2) === 'x', 'env hex key works');
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.rmSync(tmp2, { recursive: true, force: true });
})().then(runServerSuite).catch((e) => { console.error(e); process.exit(1); });

/* ── mock LLM (plain-text answer path only) ── */
function makeMockLlm() {
  return http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/v1/models') {
      if (req.headers.authorization !== 'Bearer good-token') {
        res.writeHead(401, { 'content-type': 'application/json' });
        res.end('{}');
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ object: 'list', data: [{ id: 'mock-1', object: 'model' }] }));
      return;
    }
    if (req.method === 'POST' && req.url === '/v1/chat/completions') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        const parsed = JSON.parse(body);
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        const chunk = (delta, finish) =>
          `data: ${JSON.stringify({ id: 'x', object: 'chat.completion.chunk', created: 0, model: parsed.model, choices: [{ index: 0, delta, finish_reason: finish || null }] })}\n\n`;
        res.write(chunk({ role: 'assistant', content: 'WebUI ' }));
        res.write(chunk({ content: 'answer.' }));
        res.write(chunk({}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      });
      return;
    }
    res.writeHead(404); res.end('{}');
  });
}

async function runServerSuite() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cam-container-test-'));
  const mockLlm = makeMockLlm();
  await new Promise((r) => mockLlm.listen(0, '127.0.0.1', r));
  const mockUrl = `http://127.0.0.1:${mockLlm.address().port}/v1`;

  const { start } = await import(path.join(SERVER_DIR, 'server.mjs'));
  const TOKEN = 'test-token-' + Date.now();
  const inst = await start({
    env: { CAM_DATA_DIR: tmp, CAM_API_TOKEN: TOKEN },
    port: 0,
    bind: '127.0.0.1',
  });
  const base = `http://127.0.0.1:${inst.port}`;
  const auth = { authorization: `Bearer ${TOKEN}` };

  const get = (p, headers) => fetch(base + p, { headers });
  const post = (p, body, headers) => fetch(base + p, { method: 'POST', headers: { 'content-type': 'application/json', ...(headers || {}) }, body: JSON.stringify(body || {}) });

  try {
    // ── boot smoke ──
    let r = await get('/');
    ok(r.status === 200, 'GET / is 200');
    const html = await r.text();
    ok(html.includes('/cam-web-shim.js'), 'desktop.html served with shim injected');
    ok(/frame-src\s+'self'/.test(html), 'CSP frame-src gains \'self\' via serve-time injection');
    ok(html.includes('js/desktop/app.js'), 'desktop.html asset references intact');

    r = await get('/cam-web-shim.js');
    ok(r.status === 200 && (r.headers.get('content-type') || '').includes('javascript'), 'shim served as JS');

    r = await get('/api/system/health');
    ok(r.status === 200, 'hub health via proxy (unauthenticated by design)');
    const health = await r.json();
    ok(health.capabilities && health.capabilities.agent_terminal === true, 'hub capabilities present');

    r = await get('/api/agents');
    ok(r.status === 401, 'hub API rejects missing token');
    r = await get('/api/agents', auth);
    ok(r.status === 200, 'hub API accepts the server token');
    ok(Array.isArray((await r.json()).agents), 'agents list shape');

    r = await get('/../etc/passwd').catch(() => null);
    ok(!r || r.status === 403 || r.status === 404, 'static traversal blocked');
    r = await get('/%2e%2e/%2e%2e/etc/passwd');
    ok(r.status === 403 || r.status === 404, 'encoded traversal blocked');

    // ── assistant over HTTP ──
    r = await post('/api/assistant/status');
    ok(r.status === 401, 'assistant endpoint requires auth');
    r = await post('/api/assistant/status', {}, auth);
    let st = await r.json();
    ok(r.status === 200 && st.ok === true, 'assistant status ok', JSON.stringify(st));

    r = await post('/api/assistant/configure', { apiUrl: mockUrl, model: 'mock-1', token: 'good-token' }, auth);
    const cfg = await r.json();
    ok(cfg.ok === true, 'assistant configure against mock LLM', JSON.stringify(cfg));
    r = await post('/api/assistant/start', {}, auth);
    ok((await r.json()).ok === true, 'assistant start (plain-Node child spawn)');
    await waitFor(async () => {
      const s = await (await post('/api/assistant/status', {}, auth)).json();
      return s.status === 'idle' ? s : null;
    }, 'assistant idle after configure');

    r = await post('/api/assistant/send', { text: 'hello webui' }, auth);
    ok((await r.json()).ok === true, 'assistant send accepted');
    const done = await waitFor(async () => {
      const p = await (await post('/api/assistant/poll', { since: 0 }, auth)).json();
      const d = (p.events || []).find((e) => e.type === 'done');
      return d || null;
    }, 'assistant done event');
    ok(/WebUI answer\./.test(done.text), 'mock answer streamed through', done.text);

    // ── WS rail ──
    const WebSocket = require(path.join(SERVER_DIR, 'node_modules', 'ws'));
    const badWs = new WebSocket(`ws://127.0.0.1:${inst.port}/ws?token=wrong`);
    const badClosed = await new Promise((resolve) => {
      badWs.on('open', () => resolve(false));
      badWs.on('close', () => resolve(true));
      badWs.on('error', () => resolve(true));
      setTimeout(() => resolve(false), 3000);
    });
    ok(badClosed, 'WS rejects a wrong token');

    const ws = new WebSocket(`ws://127.0.0.1:${inst.port}/ws?token=${TOKEN}`);
    await new Promise((resolve, reject) => { ws.on('open', resolve); ws.on('error', reject); });
    const termReply = await new Promise((resolve) => {
      ws.on('message', (raw) => {
        const f = JSON.parse(raw.toString());
        if (f.id === 1) resolve(f);
      });
      ws.send(JSON.stringify({ id: 1, ch: 'term', op: 'open', agentId: 'no-such-agent', cols: 80, rows: 24 }));
    });
    ok(termReply.ok === false && !!termReply.error, 'term open on a bogus agent fails cleanly', JSON.stringify(termReply));
    ws.close();

    console.log(`${n} passed, 0 failed`);
  } catch (e) {
    console.error(`FAILED after ${n} passes:`, e);
    process.exitCode = 1;
  } finally {
    try { inst.termChannel.closeAll(); } catch (_) {}
    try { inst.server.close(); } catch (_) {}
    try { await inst.boot.embeddedHub.stop(); } catch (_) {}
    try { inst.boot.assistantHost.stop(); } catch (_) {}
    mockLlm.close();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}
