/**
 * Terminal bridge — same shape as Desktop CamBridge.term (preload.cjs).
 * Desktop/Electron: window.CamBridge.term (real SSH PTY attach)
 * Android Direct: window.__camNativeTerm (JS cannot extend Java CamBridge)
 * Relay: capture poll + sendInput over Hub REST APIs.
 */

import {
  getHubCapabilities,
  terminalSupported,
  agentOpsSupported,
  isMobileEmbeddedHub,
} from './hub-capabilities.js';

const CAPTURE_POLL_MS = 2000;
const CAPTURE_LINES = 200;

function ensureNativeTermBridge() {
  if (typeof window === 'undefined') return null;
  if (window.__camNativeTerm) return window.__camNativeTerm;
  if (window.CamBridge && typeof window.CamBridge.term_open === 'function') {
    if (typeof window.__camInstallBridge === 'function') window.__camInstallBridge();
  }
  if (window.__camNativeTerm) return window.__camNativeTerm;
  const b = window.CamBridge && window.CamBridge.term;
  return b || null;
}

function nativeTermBridge() {
  return ensureNativeTermBridge();
}

function electronBridge() {
  return nativeTermBridge();
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

/**
 * Client-only terminal: polls /output, sends keystrokes via /input.
 * Same CamBridge.term API so terminal-mount.js works unchanged.
 */
export function createRelayTermBridge(api) {
  const handlers = { data: new Set(), status: new Set() };
  const sessions = new Map(); // sessionId -> entry
  const byAgent = new Map(); // agentId -> sessionId
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
      if (!agentId) {
        return { ok: false, error: 'invalid_args', detail: 'agentId is required' };
      }
      if (!api || typeof api.agentOutput !== 'function') {
        return { ok: false, error: 'unavailable', detail: 'agentOutput not supported' };
      }

      const existingSid = byAgent.get(agentId);
      if (existingSid && sessions.has(existingSid)) {
        startPolling(sessions.get(existingSid));
        return { ok: true, sessionId: existingSid, reused: true };
      }

      const sessionId = `cap${++seq}-${Date.now().toString(36)}`;
      const ent = {
        sessionId,
        agentId,
        hash: null,
        lastText: '',
        timer: null,
        inflight: false,
        errors: 0,
      };
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

    async resize() {
      return { ok: true, ignored: true };
    },

    async close(payload = {}) {
      const sid = String(payload && payload.sessionId || '');
      const ent = sessions.get(sid);
      if (!ent) return { ok: false, error: 'not_found' };
      dropSession(sid);
      emitStatus(handlers, sid, 'closed');
      return { ok: true };
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

/** Native PTY (Electron/Android) or capture-based relay bridge. */
export function getTermBridge(api) {
  const native = nativeTermBridge();
  if (native) return native;
  if (!api || api.mode === 'disconnected') return null;
  const caps = getHubCapabilities();
  if (isMobileEmbeddedHub(caps)) return null;
  if (caps && !agentOpsSupported(caps)) return null;
  if (api.mode === 'relay' || api.mode === 'direct') {
    return createRelayTermBridge(api);
  }
  return null;
}

export function canUseTerminalMode(api) {
  if (nativeTermBridge()) return true;
  if (typeof window !== 'undefined' && window.CamBridge && typeof window.CamBridge.term_open === 'function') {
    return true;
  }
  if (!api || api.mode === 'disconnected') return false;
  const caps = getHubCapabilities();
  if (isMobileEmbeddedHub(caps)) return false;
  if (caps && !terminalSupported(caps)) return false;
  if (api.mode === 'relay' || api.mode === 'direct') {
    return agentOpsSupported(caps);
  }
  return false;
}
