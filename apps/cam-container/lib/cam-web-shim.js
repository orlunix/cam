/* cam-web-shim.js — the browser replacement for the Electron preload
 * (apps/cam-desktop/electron/preload.cjs). Served by cam-container at
 * /cam-web-shim.js and injected into desktop.html, so the renderer's
 * business code runs unchanged: window.CamBridge exists, just backed by
 * fetch + WebSocket instead of ipcRenderer.
 *
 * Token bootstrap: open the UI as /?token=<CAM_API_TOKEN> once — the shim
 * stores it in localStorage and cleans the URL. Without a token the app
 * falls to its Settings page (its own manual serverUrl+token path).
 */
(function () {
  'use strict';

  /* ── token bootstrap ── */
  try {
    const u = new URL(location.href);
    const t = u.searchParams.get('token');
    if (t) {
      localStorage.setItem('cam_token', t);
      localStorage.setItem('cam_server_url', location.origin);
      localStorage.setItem('cam_profile_kind', 'direct');
      u.searchParams.delete('token');
      history.replaceState(null, '', u.pathname + u.search + u.hash);
    }
  } catch (_) {}

  const token = () => { try { return localStorage.getItem('cam_token') || ''; } catch (_) { return ''; } };
  const hasToken = () => !!token();

  async function api(path, opts = {}) {
    const r = await fetch(location.origin + path, {
      method: opts.method || 'GET',
      headers: { authorization: `Bearer ${token()}`, ...(opts.headers || {}) },
      body: opts.body,
    });
    const data = await r.json().catch(() => ({}));
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

  /* ── WS rail (term frames + assistant push) with reconnect ── */
  let ws = null;
  let wsReady = null;      // Promise resolving on open
  let seq = 0;
  const pending = new Map();  // id → {resolve, reject}
  const pushHandlers = { 'term:data': [], 'term:status': [], 'assistant:event': [] };

  function wsConnect() {
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return wsReady;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token())}`);
    ws = sock;
    wsReady = new Promise((resolve, reject) => {
      sock.onopen = () => resolve();
      sock.onerror = () => reject(new Error('ws_failed'));
    });
    sock.onmessage = (m) => {
      let frame;
      try { frame = JSON.parse(m.data); } catch (_) { return; }
      if (!frame || typeof frame !== 'object') return;
      if (frame.push) {
        for (const cb of (pushHandlers[frame.push] || [])) { try { cb(frame); } catch (_) {} }
        return;
      }
      if (frame.id != null && pending.has(frame.id)) {
        const p = pending.get(frame.id);
        pending.delete(frame.id);
        p.resolve(frame);
      }
    };
    sock.onclose = () => {
      for (const [, p] of pending) p.resolve({ ok: false, error: 'ws_closed' });
      pending.clear();
      ws = null;
      setTimeout(wsConnect, 1500); // quiet reconnect; term sessions die server-side on close
    };
    return wsReady;
  }

  async function wsCall(op, payload) {
    await wsConnect();
    if (!ws || ws.readyState !== 1) return { ok: false, error: 'ws_unavailable' };
    const id = ++seq;
    return new Promise((resolve) => {
      pending.set(id, { resolve });
      ws.send(JSON.stringify({ id, ch: 'term', ...payload, op }));
    });
  }

  function onPush(kind, cb) {
    if (typeof cb !== 'function') return;
    pushHandlers[kind].push(cb);
    wsConnect();
  }

  /* ── browser file/clipboard replacements ── */
  function pickFileInput(opts = {}) {
    return new Promise((resolve) => {
      const el = document.createElement('input');
      el.type = 'file';
      el.style.display = 'none';
      if (opts.directory) el.setAttribute('webkitdirectory', '');
      document.body.appendChild(el);
      el.onchange = () => { const f = el.files && el.files[0]; el.remove(); resolve(f || null); };
      // No reliable cancel event; a blur-without-change leaves the promise
      // pending — same as the user dismissing a native dialog mid-flow in
      // the desktop (the caller just never continues).
      el.click();
    });
  }

  async function uploadFile(file) {
    const r = await fetch(`${location.origin}/api/server/upload?name=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token()}`, 'content-type': 'application/octet-stream' },
      body: file,
    });
    return r.json().catch(() => ({ ok: false, error: 'upload_failed' }));
  }

  function downloadBlob(name, blob) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  const b64encode = (buf) => {
    let s = '';
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  };

  /* ── CamBridge surface (mirrors preload.cjs) ── */
  window.CamBridge = {
    getPlatform() { return 'web'; },
    getAppVersion() { return 'webui'; },
    getSystemUser() { return ''; }, // no OS account concept server-side
    openExternal(url) {
      if (/^https?:\/\//i.test(String(url || ''))) window.open(url, '_blank', 'noopener');
    },
    restartApp() { location.reload(); },
    async resetApp() {
      const r = await post('/api/server/reset');
      try { if (r && r.ok) location.reload(); } catch (_) {}
      return r;
    },
    diagLog(line) { void post('/api/server/diag', { line: String(line || '') }); },
    async diagTail(lines) { return api(`/api/server/diag?lines=${Number(lines) || 200}`); },

    directHub: {
      async check() { return { ok: true, platform: 'web', summary: 'cam-container' }; },
      async start() {
        if (!hasToken()) return { ok: false, error: 'token_required', detail: 'open with ?token=<CAM_API_TOKEN>' };
        return { ok: true, apiUrl: location.origin, apiToken: token() };
      },
      async stop() { return { ok: true, note: 'server hub stays up (shared infra)' }; },
      async restart() { return post('/api/server/reset'); },
      async logs() { return api('/api/server/diag?lines=200'); },
      async getProfile() { return { ok: true, kind: 'server' }; },
    },

    files: {
      async pickPrivateKey() {
        const f = await pickFileInput();
        if (!f) return { path: null };
        const up = await uploadFile(f);
        return up.ok ? { path: up.path } : { path: null, error: up.error || 'upload_failed' };
      },
      async pickFile(opts = {}) {
        if (opts && opts.directory) {
          // Browsers cannot hand the server a local folder path; the
          // WebUI flow is .tar.gz upload (the hub accepts both).
          return { ok: false, error: 'directory_unsupported', detail: 'WebUI: pack the extension as .tar.gz and pick that file instead' };
        }
        const f = await pickFileInput();
        if (!f) return { ok: false, canceled: true, path: null };
        const up = await uploadFile(f);
        return up.ok ? { ok: true, path: up.path } : { ok: false, error: up.error || 'upload_failed', path: null };
      },
      async pickAttachment() {
        const f = await pickFileInput();
        if (!f) return { ok: false, canceled: true };
        if (f.size > 50 * 1024 * 1024) return { ok: false, error: 'too_large', size: f.size, maxBytes: 50 * 1024 * 1024 };
        const buf = await f.arrayBuffer();
        return { ok: true, filename: f.name, size: f.size, data: b64encode(buf) };
      },
      async saveText(opts = {}) {
        downloadBlob(String(opts.defaultName || 'export.txt'), new Blob([String(opts.content || '')], { type: 'text/plain' }));
        return { ok: true, path: String(opts.defaultName || 'export.txt') };
      },
      async saveFile(opts = {}) {
        const bin = atob(String(opts.contentBase64 || ''));
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        downloadBlob(String(opts.defaultName || 'download'), new Blob([bytes]));
        return { ok: true, path: String(opts.defaultName || 'download'), bytes: bytes.length };
      },
      async readClipboardText() {
        try { return { ok: true, text: await navigator.clipboard.readText() }; }
        catch (e) { return { ok: false, error: 'clipboard_text_failed', detail: String((e && e.message) || e) }; }
      },
      async readClipboardAttachments() {
        try {
          const items = await navigator.clipboard.read();
          for (const item of items) {
            const type = item.types.find((t) => t.startsWith('image/'));
            if (type) {
              const blob = await item.getType(type);
              const buf = await blob.arrayBuffer();
              const ts = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*/, '').replace('T', '-');
              return { ok: true, source: 'image', files: [{ filename: `clipboard-image-${ts}.png`, size: buf.byteLength, data: b64encode(buf) }] };
            }
          }
          return { ok: false, error: 'empty_clipboard', detail: 'Clipboard does not contain an image (file-paste is desktop-only).' };
        } catch (e) {
          return { ok: false, error: 'clipboard_image_failed', detail: String((e && e.message) || e) };
        }
      },
    },

    net: {
      async probe(url, timeoutMs) { return post('/api/server/probe', { url, timeoutMs }); },
    },

    assistant: {
      status()          { return post('/api/assistant/status'); },
      configure(p)      { return post('/api/assistant/configure', p); },
      models(p)         { return post('/api/assistant/models', p); },
      start()           { return post('/api/assistant/start'); },
      stop()            { return post('/api/assistant/stop'); },
      send(p)           { return post('/api/assistant/send', p); },
      poll(p)           { return post('/api/assistant/poll', p); },
      reset()           { return post('/api/assistant/reset'); },
      threads()         { return post('/api/assistant/threads'); },
      newChat()         { return post('/api/assistant/newChat'); },
      openThread(id)    { return post('/api/assistant/openThread', { id }); },
      deleteThread(id)  { return post('/api/assistant/deleteThread', { id }); },
      onEvent(cb)       { onPush('assistant:event', (f) => cb(f.ev)); },
    },

    term: {
      open(p)              { return wsCall('open', p); },
      ready(p)             { return wsCall('ready', p); },
      input(p)             { return wsCall('input', p); },
      resize(p)            { return wsCall('resize', p); },
      close(p)             { return wsCall('close', p); },
      listWindows(p)       { return wsCall('listWindows', p); },
      selectWindow(p)      { return wsCall('selectWindow', p); },
      createWindow(p)      { return wsCall('createWindow', p); },
      copyMode(p)          { return wsCall('copyMode', p); },
      cancelCopyMode(p)    { return wsCall('cancelCopyMode', p); },
      onData(cb)           { onPush('term:data', (f) => cb(f)); },
      onStatus(cb)         { onPush('term:status', (f) => cb(f)); },
    },
  };

  // Establish the WS rail eagerly when we have a token (assistant push).
  if (hasToken()) wsConnect();
})();
