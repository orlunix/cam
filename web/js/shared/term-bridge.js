/**
 * Terminal bridge — same shape as Desktop CamBridge.term (preload.cjs).
 * Desktop/Electron: window.CamBridge.term (real SSH PTY attach)
 * Android Direct: binary PTY stream over the loopback /term WebSocket
 * Relay: capture poll + sendInput over Hub REST APIs.
 */

import {
  getHubCapabilities,
  terminalSupported,
  terminalWebSocketSupported,
  agentOpsSupported,
  isMobileEmbeddedHub,
} from './hub-capabilities.js';

const CAPTURE_POLL_MS = 2000;
const CAPTURE_LINES = 200;

function nativeTermBridge() {
  if (typeof window === 'undefined') return null;
  const b = window.CamBridge && window.CamBridge.term;
  return b || null;
}

function trimTrailingBlank(text) {
  return String(text || '').replace(/\n([ \t]*\n)*[ \t]*$/, '\n');
}

function emitData(handlers, sessionId, chunk) {
  if (!chunk) return;
  for (const h of handlers.data) {
    try { h({ sessionId, data: chunk }); } catch { /* noop */ }
  }
}

function emitStatus(handlers, sessionId, kind, extra = {}) {
  for (const h of handlers.status) {
    try { h({ sessionId, kind, ...extra }); } catch { /* noop */ }
  }
}

function bridgeError(msg, fallback = 'terminal_failed') {
  return new Error((msg && (msg.detail || msg.error)) || fallback);
}

/** Client-only terminal: polls /output, sends keystrokes via /input. */
export function createRelayTermBridge(api) {
  const handlers = { data: new Set(), status: new Set() };
  const sessions = new Map();
  const byAgent = new Map();
  let seq = 0;

  function dropSession(sessionId) {
    const ent = sessions.get(sessionId);
    if (!ent) return;
    if (ent.timer) clearInterval(ent.timer);
    sessions.delete(sessionId);
    if (ent.agentId) byAgent.delete(ent.agentId);
  }

  async function pollOnce(ent) {
    if (!ent || ent.inflight || !api) return;
    ent.inflight = true;
    try {
      const data = await api.agentOutput(ent.agentId, CAPTURE_LINES, ent.hash);
      if (data && data.hash) ent.hash = data.hash;
      if (data && !data.unchanged && data.output != null) {
        const next = trimTrailingBlank(data.output);
        if (next !== ent.lastText) {
          let chunk;
          if (ent.lastText && next.startsWith(ent.lastText)) {
            chunk = next.slice(ent.lastText.length);
          } else if (ent.lastText) {
            chunk = `\x1b[2J\x1b[H${next}`;
          } else {
            chunk = next;
          }
          ent.lastText = next;
          emitData(handlers, ent.sessionId, chunk);
        }
      }
      ent.errors = 0;
    } catch {
      ent.errors = (ent.errors || 0) + 1;
      if (ent.errors >= 3) ent.hash = null;
    } finally {
      ent.inflight = false;
    }
  }

  function startPolling(ent) {
    if (ent.timer) return;
    void pollOnce(ent);
    ent.timer = setInterval(() => { void pollOnce(ent); }, CAPTURE_POLL_MS);
  }

  return {
    async open(payload = {}) {
      const agentId = String(payload && payload.agentId || '');
      if (!agentId) return { ok: false, error: 'invalid_args', detail: 'agentId is required' };
      if (!api || typeof api.agentOutput !== 'function') {
        return { ok: false, error: 'unavailable', detail: 'agentOutput not supported' };
      }

      const existingSid = byAgent.get(agentId);
      if (existingSid && sessions.has(existingSid)) {
        startPolling(sessions.get(existingSid));
        return { ok: true, sessionId: existingSid, reused: true };
      }

      const sessionId = `cap${++seq}-${Date.now().toString(36)}`;
      const ent = { sessionId, agentId, hash: null, lastText: '', timer: null, inflight: false, errors: 0 };
      sessions.set(sessionId, ent);
      byAgent.set(agentId, sessionId);
      startPolling(ent);
      return { ok: true, sessionId, reused: false };
    },

    async input(payload = {}) {
      const ent = sessions.get(String(payload && payload.sessionId || ''));
      if (!ent || typeof api.sendInput !== 'function') return { ok: false, error: 'not_found' };
      const data = String(payload.data == null ? '' : payload.data);
      if (!data) return { ok: true };
      try {
        await api.sendInput(ent.agentId, data, false);
        return { ok: true };
      } catch (e) {
        return { ok: false, error: 'input_failed', detail: e && e.message || String(e) };
      }
    },

    async resize() { return { ok: true, ignored: true }; },

    async close(payload = {}) {
      const sid = String(payload && payload.sessionId || '');
      const ent = sessions.get(sid);
      if (!ent) return { ok: false, error: 'not_found' };
      dropSession(sid);
      emitStatus(handlers, sid, 'closed');
      return { ok: true };
    },

    async closeAll() {
      for (const sid of [...sessions.keys()]) {
        dropSession(sid);
        emitStatus(handlers, sid, 'closed');
      }
    },

    onData(cb) {
      handlers.data.add(cb);
      return () => handlers.data.delete(cb);
    },
    onStatus(cb) {
      handlers.status.add(cb);
      return () => handlers.status.delete(cb);
    },
  };
}

/** Android Direct PTY: one loopback WebSocket per terminal, binary in both directions. */
export function createDirectWebSocketTermBridge(api) {
  const handlers = { data: new Set(), status: new Set() };
  const byAgent = new Map();
  const pending = new Map();
  const encoder = new TextEncoder();
  let requestSeq = 0;

  const wsUrl = `${api.serverUrl.replace(/^http/, 'ws')}/term?token=${encodeURIComponent(api.token)}`;

  function rejectPending(ent, err) {
    for (const [id, req] of [...pending.entries()]) {
      if (req.ent !== ent) continue;
      clearTimeout(req.timer);
      pending.delete(id);
      req.reject(err);
    }
  }

  function cleanup(ent, closeSocket = true) {
    if (!ent || ent.cleaned) return;
    ent.cleaned = true;
    byAgent.delete(ent.agentId);
    rejectPending(ent, new Error('Terminal connection closed'));
    if (closeSocket) {
      try { ent.ws.close(); } catch { /* noop */ }
    }
  }

  function sendRequest(ent, action, payload = {}, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      if (ent.cleaned || !ent.ws || ent.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('Terminal WebSocket is not connected'));
        return;
      }
      const requestId = `tr${++requestSeq}`;
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`Terminal ${action} timed out`));
      }, timeoutMs);
      pending.set(requestId, { ent, action, resolve, reject, timer });
      try {
        ent.ws.send(JSON.stringify({ ...payload, action, requestId }));
      } catch (e) {
        clearTimeout(timer);
        pending.delete(requestId);
        reject(e);
      }
    });
  }

  function emitBinary(ent, bytes) {
    const chunk = ent.decoder.decode(bytes, { stream: true });
    if (!chunk) return;
    if (ent.sessionId) emitData(handlers, ent.sessionId, chunk);
    else ent.earlyData.push(chunk);
  }

  function handleResult(ent, msg) {
    const req = pending.get(msg.requestId);
    if (!req) return;
    clearTimeout(req.timer);
    pending.delete(msg.requestId);
    if (!msg.ok) {
      req.reject(bridgeError(msg, `Terminal ${req.action} failed`));
      return;
    }
    if (req.action === 'open') {
      ent.sessionId = String(msg.sessionId || '');
      for (const chunk of ent.earlyData.splice(0)) emitData(handlers, ent.sessionId, chunk);
      req.resolve({ ok: true, sessionId: ent.sessionId, reused: !!msg.reused });
    } else {
      req.resolve(msg);
    }
  }

  function handleStatus(ent, msg) {
    const sid = String(msg.sessionId || ent.sessionId || '');
    if (msg.kind === 'closed') {
      if (sid) emitStatus(handlers, sid, 'closed', msg);
      cleanup(ent, false);
      return;
    }
    if (sid) emitStatus(handlers, sid, msg.kind || 'error', msg);
  }

  function attachHandlers(ent) {
    ent.ws.onmessage = (evt) => {
      if (ent.cleaned) return;
      if (typeof evt.data === 'string') {
        let msg;
        try { msg = JSON.parse(evt.data); } catch { return; }
        if (msg.type === 'result') handleResult(ent, msg);
        else if (msg.type === 'status') handleStatus(ent, msg);
        return;
      }
      ent.binaryChain = ent.binaryChain.then(async () => {
        let bytes;
        if (evt.data instanceof ArrayBuffer) bytes = new Uint8Array(evt.data);
        else if (ArrayBuffer.isView(evt.data)) {
          bytes = new Uint8Array(evt.data.buffer, evt.data.byteOffset, evt.data.byteLength);
        } else if (evt.data && typeof evt.data.arrayBuffer === 'function') {
          bytes = new Uint8Array(await evt.data.arrayBuffer());
        }
        if (bytes) emitBinary(ent, bytes);
      }).catch(() => cleanup(ent));
    };
    ent.ws.onclose = () => {
      if (ent.cleaned) return;
      if (ent.sessionId) emitStatus(handlers, ent.sessionId, 'closed');
      cleanup(ent, false);
    };
    ent.ws.onerror = () => {
      if (!ent.sessionId) rejectPending(ent, new Error('Terminal WebSocket connection failed'));
    };
  }

  return {
    open(payload = {}) {
      const agentId = String(payload.agentId || '');
      if (!agentId) return Promise.resolve({ ok: false, error: 'invalid_args', detail: 'agentId is required' });
      const existing = byAgent.get(agentId);
      if (existing) {
        if (existing.openPromise) return existing.openPromise;
        if (existing.sessionId && existing.ws.readyState === WebSocket.OPEN) {
          return Promise.resolve({ ok: true, sessionId: existing.sessionId, reused: true });
        }
        cleanup(existing);
      }

      const ent = {
        agentId,
        ws: new WebSocket(wsUrl),
        sessionId: '',
        decoder: new TextDecoder('utf-8'),
        earlyData: [],
        binaryChain: Promise.resolve(),
        openPromise: null,
        cleaned: false,
      };
      ent.ws.binaryType = 'arraybuffer';
      byAgent.set(agentId, ent);
      attachHandlers(ent);
      ent.openPromise = new Promise((resolve, reject) => {
        ent.ws.onopen = () => {
          sendRequest(ent, 'open', payload, 60000).then(resolve, reject);
        };
      }).catch((e) => {
        cleanup(ent);
        throw e;
      });
      return ent.openPromise;
    },

    async input(payload = {}) {
      const ent = [...byAgent.values()].find((item) => item.sessionId === payload.sessionId);
      if (!ent || !ent.sessionId || ent.ws.readyState !== WebSocket.OPEN) {
        return { ok: false, error: 'not_found', detail: 'terminal session not found' };
      }
      const data = String(payload.data == null ? '' : payload.data);
      if (!data) return { ok: true };
      try {
        ent.ws.send(encoder.encode(data));
        return { ok: true };
      } catch (e) {
        return { ok: false, error: 'input_failed', detail: e && e.message || String(e) };
      }
    },

    async resize(payload = {}) {
      const ent = [...byAgent.values()].find((item) => item.sessionId === payload.sessionId);
      if (!ent) return { ok: false, error: 'not_found' };
      return sendRequest(ent, 'resize', payload);
    },

    async copymode(payload = {}) {
      const ent = [...byAgent.values()].find((item) => item.sessionId === payload.sessionId);
      if (!ent) return { ok: false, error: 'not_found' };
      return sendRequest(ent, 'copymode', { mode: payload.action || '' });
    },

    async close(payload = {}) {
      const ent = [...byAgent.values()].find((item) => item.sessionId === payload.sessionId);
      if (!ent) return { ok: true };
      try {
        const res = await sendRequest(ent, 'close', {});
        cleanup(ent);
        return res;
      } catch (e) {
        cleanup(ent);
        return { ok: false, error: 'close_failed', detail: e && e.message || String(e) };
      }
    },

    async closeAll() {
      for (const ent of [...byAgent.values()]) cleanup(ent);
    },

    onData(cb) {
      handlers.data.add(cb);
      return () => handlers.data.delete(cb);
    },
    onStatus(cb) {
      handlers.status.add(cb);
      return () => handlers.status.delete(cb);
    },
  };
}

const relayBridges = new WeakMap();
const directWsBridges = new WeakMap();

/** Native PTY (Electron), Android WebSocket PTY, or capture-based relay bridge. */
export function getTermBridge(api) {
  const native = nativeTermBridge();
  if (native) return native;
  if (!api || api.mode === 'disconnected') return null;
  const caps = getHubCapabilities();
  if (isMobileEmbeddedHub(caps)) {
    if (!terminalWebSocketSupported(caps)) return null;
    let bridge = directWsBridges.get(api);
    if (!bridge) {
      bridge = createDirectWebSocketTermBridge(api);
      directWsBridges.set(api, bridge);
    }
    return bridge;
  }
  if (caps && !agentOpsSupported(caps)) return null;
  if (api.mode === 'relay' || api.mode === 'direct') {
    let bridge = relayBridges.get(api);
    if (!bridge) {
      bridge = createRelayTermBridge(api);
      relayBridges.set(api, bridge);
    }
    return bridge;
  }
  return null;
}

export async function closeTermBridges(api) {
  const jobs = [];
  const direct = directWsBridges.get(api);
  const relay = relayBridges.get(api);
  if (direct) jobs.push(direct.closeAll());
  if (relay) jobs.push(relay.closeAll());
  directWsBridges.delete(api);
  relayBridges.delete(api);
  await Promise.allSettled(jobs);
}

export function canUseTerminalMode(api) {
  if (nativeTermBridge()) return true;
  if (!api || api.mode === 'disconnected') return false;
  const caps = getHubCapabilities();
  if (isMobileEmbeddedHub(caps)) return terminalWebSocketSupported(caps);
  if (caps && !terminalSupported(caps)) return false;
  if (api.mode === 'relay' || api.mode === 'direct') return agentOpsSupported(caps);
  return false;
}
