/* cam-container — CAM WebUI. One public HTTP port:
 *
 *   /, /desktop.html        web/desktop.html with /cam-web-shim.js injected
 *   /cam-web-shim.js        browser CamBridge (replaces the Electron preload)
 *   /css/* /js/* /…         static web/ assets
 *   /api/assistant/<m>      assistant-host methods (bearer-authed)
 *   /api/server/*           server-local ops (diag tail, probe, upload, reset)
 *   /api/*, /ext/*          reverse-proxied to the embedded hub (loopback)
 *   WS /ws?token=           term channel + assistant:event push rail
 *
 * The hub stays loopback-bound inside the container; this layer owns the
 * public socket and nothing else. See README.md for deployment.
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';
import { bootstrap } from './lib/bootstrap.mjs';
import { createTermChannel } from './lib/term-ws.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const WEB_ROOT = path.join(REPO_ROOT, 'web');

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
  '.woff': 'font/woff', '.map': 'application/json', '.webmanifest': 'application/manifest+json',
  '.yaml': 'text/yaml', '.py': 'text/plain',
};

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

function readBody(req, cap = 64 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let n = 0;
    req.on('data', (c) => {
      n += c.length;
      if (n > cap) { reject(new Error('body_too_large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

export async function start(opts = {}) {
  const env = opts.env || process.env;
  const boot = await bootstrap({ env, onAssistantEvent: (ev) => wsBroadcast({ push: 'assistant:event', ev }) });
  const { apiToken, hubPort, dataDir, log } = boot;

  const authed = (req) => {
    const h = req.headers.authorization || '';
    return h === `Bearer ${apiToken}`;
  };

  /* ── assistant HTTP surface: POST /api/assistant/<method> ── */
  // Method names mirror the preload bridge (assistant.configure → host
  // .setConfig — the host ALSO exports a lifecycle `configure`, so a bare
  // name passthrough would call the wrong function).
  const ASSISTANT_METHODS = {
    status: 'status', configure: 'setConfig', models: 'listModels',
    start: 'start', stop: 'stop', send: 'send', poll: 'poll',
    reset: 'reset', threads: 'threads', newChat: 'newChat',
    openThread: 'openThread', deleteThread: 'deleteThread',
  };
  async function handleAssistant(req, res, method) {
    const fn = ASSISTANT_METHODS[method] && boot.assistantHost[ASSISTANT_METHODS[method]];
    if (typeof fn !== 'function') return sendJson(res, 404, { ok: false, error: 'unknown_method', detail: method });
    let payload = {};
    try { payload = JSON.parse((await readBody(req)).toString('utf8') || '{}'); }
    catch (e) { return sendJson(res, 400, { ok: false, error: 'bad_json', detail: String(e.message || e) }); }
    try {
      const r = await fn.call(boot.assistantHost, payload);
      sendJson(res, 200, r == null ? { ok: true } : r);
    } catch (e) {
      sendJson(res, 500, { ok: false, error: 'assistant_error', detail: String((e && e.message) || e) });
    }
  }

  /* ── server-local ops: /api/server/* ── */
  async function handleServerOp(req, res, op, urlObj) {
    if (op === 'diag' && req.method === 'GET') {
      const lines = Math.max(1, Math.min(2000, Number(urlObj.searchParams.get('lines')) || 200));
      let text = '';
      try { text = fs.readFileSync(boot.logFile, 'utf8'); } catch (_) {}
      return sendJson(res, 200, { ok: true, text: text.split('\n').slice(-lines).join('\n') });
    }
    if (op === 'diag' && req.method === 'POST') {
      const body = (await readBody(req)).toString('utf8');
      try { log(`[renderer] ${JSON.parse(body).line || ''}`); } catch (_) {}
      return sendJson(res, 200, { ok: true });
    }
    if (op === 'probe' && req.method === 'POST') {
      let url, timeoutMs;
      try { ({ url, timeoutMs } = JSON.parse((await readBody(req)).toString('utf8') || '{}')); } catch (_) {}
      if (!/^https?:\/\//.test(String(url || ''))) return sendJson(res, 400, { ok: false, error: 'invalid_url' });
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), Math.max(1000, Math.min(30000, Number(timeoutMs) || 10000)));
      const t0 = Date.now();
      try {
        const r = await fetch(url, { signal: ctrl.signal });
        clearTimeout(timer);
        return sendJson(res, 200, { ok: true, status: r.status, ms: Date.now() - t0 });
      } catch (e) {
        clearTimeout(timer);
        return sendJson(res, 200, { ok: false, error: String((e && e.message) || e), ms: Date.now() - t0 });
      }
    }
    if (op === 'upload' && req.method === 'POST') {
      // Browser file-pick replacement: raw body + ?name=, lands in
      // <dataDir>/uploads/; the hub flows that take a server-side path
      // (extension install, ssh key, attachments) consume it directly.
      const name = path.basename(String(urlObj.searchParams.get('name') || 'upload.bin'));
      const dir = path.join(dataDir, 'uploads');
      fs.mkdirSync(dir, { recursive: true });
      const dest = path.join(dir, `${Date.now()}-${name}`);
      fs.writeFileSync(dest, await readBody(req));
      return sendJson(res, 200, { ok: true, path: dest, name });
    }
    if (op === 'reset' && req.method === 'POST') {
      // Desktop app:reset semantics: drop every term session + SSH pool.
      termChannel.closeAll();
      try { boot.sshTransport.closeAll(); } catch (_) {}
      log('[app:reset] terminals disposed, SSH pools dropped');
      return sendJson(res, 200, { ok: true });
    }
    return sendJson(res, 404, { ok: false, error: 'unknown_op', detail: op });
  }

  /* ── reverse proxy: /api/* and /ext/* → loopback hub ── */
  function proxyToHub(req, res) {
    const preq = http.request({
      hostname: '127.0.0.1', port: hubPort,
      path: req.url, method: req.method,
      headers: { ...req.headers, host: `127.0.0.1:${hubPort}` },
    }, (pres) => {
      res.writeHead(pres.statusCode || 502, pres.headers);
      pres.pipe(res);
    });
    preq.on('error', (e) => {
      log(`hub proxy error: ${e && e.message}`);
      try { sendJson(res, 502, { ok: false, error: 'hub_unreachable' }); } catch (_) {}
    });
    preq.setTimeout(0); // hub routes include long sync calls; no proxy-side cap
    req.pipe(preq);
  }

  /* ── static: web/, with the shim injected into desktop.html ── */
  function serveStatic(req, res, pathname) {
    let rel = decodeURIComponent(pathname);
    if (rel === '/' || rel === '') rel = '/desktop.html';
    const file = path.normalize(path.join(WEB_ROOT, rel));
    if (!file.startsWith(WEB_ROOT + path.sep) && file !== WEB_ROOT) return sendJson(res, 403, { error: 'forbidden' });
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) return sendJson(res, 404, { error: 'not_found', detail: rel });
    const ext = path.extname(file).toLowerCase();
    const type = MIME[ext] || 'application/octet-stream';
    if (path.basename(file) === 'desktop.html') {
      // Two serve-time transforms; the file on disk stays byte-identical
      // to what Electron loads:
      // 1. frame-src gains 'self' — the browser page is http(s)://, so
      //    same-origin extension iframes require it (under Electron's
      //    file:// origin it would be a no-op).
      // 2. The browser CamBridge shim right before </head> — a classic
      //    script, so it runs before any deferred module (app.js).
      let html = fs.readFileSync(file, 'utf8');
      html = html.replace(/(content="[^"]*frame-src\s+)([^";"]+)/, (m, pre, hosts) =>
        hosts.includes("'self'") ? m : pre + "'self' " + hosts);
      html = html.replace('</head>', '  <script src="/cam-web-shim.js"></script>\n</head>');
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(html);
      return;
    }
    res.writeHead(200, { 'content-type': type });
    fs.createReadStream(file).pipe(res);
  }

  const server = http.createServer(async (req, res) => {
    const u = new URL(req.url, 'http://x');
    const p = u.pathname;
    try {
      if (p === '/cam-web-shim.js') {
        res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8', 'cache-control': 'no-cache' });
        fs.createReadStream(path.join(HERE, 'lib', 'cam-web-shim.js')).pipe(res);
        return;
      }
      if (p.startsWith('/api/assistant/')) {
        if (!authed(req)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
        await handleAssistant(req, res, p.slice('/api/assistant/'.length));
        return;
      }
      if (p.startsWith('/api/server/')) {
        if (!authed(req)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
        await handleServerOp(req, res, p.slice('/api/server/'.length), u);
        return;
      }
      if (p.startsWith('/api/') || p.startsWith('/ext/')) { proxyToHub(req, res); return; }
      serveStatic(req, res, p);
    } catch (e) {
      log(`route error ${p}: ${e && e.message}`);
      try { sendJson(res, 500, { ok: false, error: 'internal_error' }); } catch (_) {}
    }
  });

  /* ── WS /ws: token-checked; hosts the term channel + assistant push ── */
  const wss = new WebSocketServer({ noServer: true });
  const wsClients = new Set();
  function wsBroadcast(msg) {
    const s = JSON.stringify(msg);
    for (const c of wsClients) { try { c.send(s); } catch (_) {} }
  }
  server.on('upgrade', (req, socket, head) => {
    const u = new URL(req.url, 'http://x');
    if (u.pathname !== '/ws' || u.searchParams.get('token') !== apiToken) {
      socket.destroy();
      return;
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      wsClients.add(ws);
      ws.on('close', () => wsClients.delete(ws));
      termChannel.attach(ws); // term sessions die with their connection
      ws.on('message', (raw) => {
        let frame;
        try { frame = JSON.parse(raw.toString()); } catch (_) { return; }
        if (!frame || typeof frame !== 'object' || frame.id == null) return;
        if (frame.ch === 'term') {
          termChannel.handle(ws, frame)
            .then((r) => { try { ws.send(JSON.stringify({ id: frame.id, ...r })); } catch (_) {} })
            .catch((e) => { try { ws.send(JSON.stringify({ id: frame.id, ok: false, error: 'term_error', detail: String((e && e.message) || e) })); } catch (_) {} });
        }
      });
    });
  });

  const termChannel = createTermChannel({
    embeddedHub: boot.embeddedHub,
    sshTransport: boot.sshTransport,
    log,
  });

  const port = Number(opts.port != null ? opts.port : env.CAM_PORT) || 0;
  const bind = opts.bind || env.CAM_BIND || '0.0.0.0';
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, bind, resolve);
  });
  const actualPort = server.address().port;
  log(`cam-container listening on http://${bind}:${actualPort} (data: ${dataDir})`);
  return { server, boot, port: actualPort, apiToken, termChannel };
}

/* Direct-run entry (tests import start() instead). */
const _isMain = (() => {
  try { return process.argv[1] && import.meta.url === new URL(`file://${path.resolve(process.argv[1])}`).href; }
  catch (_) { return false; }
})();
if (_isMain) {
  start({ env: process.env, port: Number(process.env.CAM_PORT) || 8420 })
    .then(({ port, apiToken }) => {
      console.log(`CAM WebUI: http://0.0.0.0:${port}/?token=${apiToken}`);
    })
    .catch((e) => {
      console.error('fatal:', e);
      process.exit(1);
    });
}
