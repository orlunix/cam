/** Android CamBridge shims (Direct Hub + file picker).
 *
 * Pending-callback state lives on window.__camBridgeShared, NOT in module
 * instance state: mobile.html loads app.js with a ?v= stamp while views
 * import it bare, so this module can be evaluated as two instances, and the
 * shim is reinstalled on every agent-detail entry / onPageFinished. With
 * instance-private state, a reinstall orphaned in-flight callbacks — the
 * promise never settled and the UI wedged in the "Connecting via SSH…"
 * dead state until a manual page refresh.
 */

const _sh = (() => {
  const fresh = () => ({
    dhSeq: 0,
    termSeq: 0,
    dhPending: {},
    termPending: {},
    termDataHandlers: new Set(),
    termStatusHandlers: new Set(),
  });
  if (typeof window === 'undefined') return fresh();
  window.__camBridgeShared = window.__camBridgeShared || fresh();
  return window.__camBridgeShared;
})();

// Native bridge calls only settle via the native callback; if the callback
// is lost (or an SSH connect stalls), reject after a timeout instead of
// hanging forever. A late native response for a timed-out id is dropped
// harmlessly (the pending entry is already gone).
const TERM_TIMEOUT_MS = { open: 60000, input: 10000, resize: 10000, close: 15000, copymode: 30000 };
const HUB_OP_TIMEOUT_MS = 60000;      // start/stop/restart/check/logs/getProfile
const HUB_REQUEST_TIMEOUT_MS = 90000; // hub API requests (SSH-backed, serialized)

function _armTimeout(pendingMap, id, ms, label) {
  return setTimeout(() => {
    const p = pendingMap[id];
    if (!p) return;
    delete pendingMap[id];
    console.warn(`[mobile-bridge] ${label} timed out after ${ms}ms (id=${id})`);
    p.reject(new Error(`${label} timed out — no response from native side`));
  }, ms);
}

function invokeTerm(method, payload = {}) {
  return new Promise((resolve, reject) => {
    const bridge = nativeBridge();
    if (!bridge || typeof bridge['term_' + method] !== 'function') {
      reject(new Error('Terminal bridge unavailable'));
      return;
    }
    const id = 'tm' + (++_sh.termSeq);
    _sh.termPending[id] = { resolve, reject };
    _sh.termPending[id].timer = _armTimeout(
      _sh.termPending, id, TERM_TIMEOUT_MS[method] || 30000, `term_${method}`);
    try {
      bridge['term_' + method](id, JSON.stringify(payload || {}));
    } catch (err) {
      clearTimeout(_sh.termPending[id] && _sh.termPending[id].timer);
      delete _sh.termPending[id];
      reject(err);
    }
  });
}

function buildTermBridge() {
  return {
    open(payload) { return invokeTerm('open', payload); },
    input(payload) { return invokeTerm('input', payload); },
    resize(payload) { return invokeTerm('resize', payload); },
    close(payload) { return invokeTerm('close', payload); },
    copymode(payload) { return invokeTerm('copymode', payload); },
    onData(cb) {
      _sh.termDataHandlers.add(cb);
      return () => _sh.termDataHandlers.delete(cb);
    },
    onStatus(cb) {
      _sh.termStatusHandlers.add(cb);
      return () => _sh.termStatusHandlers.delete(cb);
    },
  };
}

function nativeBridge() {
  const b = typeof window !== 'undefined' ? window.CamBridge : null;
  if (!b || typeof b.directHub_start !== 'function') return null;
  return b;
}

function nativeTermHost() {
  const b = typeof window !== 'undefined' ? window.CamBridge : null;
  if (!b || typeof b.term_open !== 'function') return null;
  return b;
}

export function hasNativeTermApi() {
  return !!nativeTermHost();
}

/** Android WebView cannot attach properties to the Java CamBridge object — keep term here. */
function nativeTermBridge() {
  if (typeof window !== 'undefined' && window.__camNativeTerm) return window.__camNativeTerm;
  const b = typeof window !== 'undefined' && window.CamBridge && window.CamBridge.term;
  return b || null;
}

function invokeDirectHub(method) {
  return new Promise((resolve, reject) => {
    const bridge = nativeBridge();
    if (!bridge) {
      reject(new Error('Embedded Hub bridge unavailable'));
      return;
    }
    const id = 'dh' + (++_sh.dhSeq);
    _sh.dhPending[id] = { resolve, reject };
    _sh.dhPending[id].timer = _armTimeout(
      _sh.dhPending, id, HUB_OP_TIMEOUT_MS, `directHub_${method}`);
    try {
      // Must call on the injected object — never extract the method reference.
      switch (method) {
        case 'check': bridge.directHub_check(id); break;
        case 'start': bridge.directHub_start(id); break;
        case 'stop': bridge.directHub_stop(id); break;
        case 'restart': bridge.directHub_restart(id); break;
        case 'logs': bridge.directHub_logs(id); break;
        case 'getProfile': bridge.directHub_getProfile(id); break;
        default:
          clearTimeout(_sh.dhPending[id].timer);
          delete _sh.dhPending[id];
          reject(new Error('Unknown Hub method: ' + method));
          return;
      }
    } catch (err) {
      clearTimeout(_sh.dhPending[id] && _sh.dhPending[id].timer);
      delete _sh.dhPending[id];
      reject(err);
    }
  });
}

function hubErrorFromPayload(data) {
  if (!data) return 'Hub operation failed';
  if (typeof data === 'string') return data;
  if (data.detail) return String(data.detail);
  if (data.error) return String(data.error);
  if (data.data && typeof data.data === 'object') {
    if (data.data.detail) return String(data.data.detail);
    if (data.data.error) return String(data.data.error);
  }
  if (data.status) return `Hub HTTP ${data.status}`;
  return 'Hub operation failed';
}

function hubErr(data) {
  const err = new Error(hubErrorFromPayload(data));
  if (data && data.status) err.status = data.status;
  return err;
}

function invokeDirectHubRequest(method, path, body, token) {
  return new Promise((resolve, reject) => {
    const bridge = nativeBridge();
    if (!bridge || typeof bridge.directHub_request !== 'function') {
      reject(new Error('Embedded Hub API bridge unavailable'));
      return;
    }
    const id = 'dh' + (++_sh.dhSeq);
    _sh.dhPending[id] = {
      resolve: (res) => {
        if (res && res.ok) resolve(res.data);
        else reject(hubErr(res));
      },
      reject,
    };
    _sh.dhPending[id].timer = _armTimeout(
      _sh.dhPending, id, HUB_REQUEST_TIMEOUT_MS, `hub ${method} ${path}`);
    try {
      const bodyJson = body != null ? JSON.stringify(body) : '';
      bridge.directHub_request(id, method, path || '/', bodyJson, token || '');
    } catch (err) {
      clearTimeout(_sh.dhPending[id] && _sh.dhPending[id].timer);
      delete _sh.dhPending[id];
      reject(err);
    }
  });
}

function buildDirectHubApi() {
  return {
    check: () => invokeDirectHub('check'),
    start: () => invokeDirectHub('start'),
    stop: () => invokeDirectHub('stop'),
    restart: () => invokeDirectHub('restart'),
    logs: () => invokeDirectHub('logs'),
    getProfile: () => invokeDirectHub('getProfile'),
    request: (method, path, body, token) => invokeDirectHubRequest(method, path, body, token),
  };
}

export function installMobileCamBridgeShim() {
  if (typeof window === 'undefined') return false;

  window.__camTermCb = (id, json) => {
    const p = _sh.termPending[id];
    clearTimeout(p && p.timer);
    delete _sh.termPending[id];
    if (!p) return;
    let data = null;
    if (json != null && json !== 'null') {
      try { data = typeof json === 'string' ? JSON.parse(json) : json; } catch { data = null; }
    }
    if (data && data.ok === false) {
      p.reject(new Error(data.detail || data.error || 'terminal failed'));
    } else {
      p.resolve(data);
    }
  };

  window.__camTermEvent = (kind, json) => {
    let msg = null;
    try { msg = typeof json === 'string' ? JSON.parse(json) : json; } catch { return; }
    if (!msg) return;
    if (kind === 'data') {
      for (const h of _sh.termDataHandlers) {
        try { h(msg); } catch { /* noop */ }
      }
    } else if (kind === 'status') {
      for (const h of _sh.termStatusHandlers) {
        try { h(msg); } catch { /* noop */ }
      }
    }
  };

  window.__camDirectHubCb = (id, ok, json) => {
    const p = _sh.dhPending[id];
    clearTimeout(p && p.timer);
    delete _sh.dhPending[id];
    if (!p) return;
    let data = null;
    if (json != null && json !== 'null') {
      if (typeof json === 'string') {
        try { data = JSON.parse(json); } catch { data = null; }
      } else if (typeof json === 'object') {
        data = json;
      }
    }
    if (ok) p.resolve(data);
    else p.reject(hubErr(data));
  };

  window.__camOnKeyPicked = (json) => {
    let data = json;
    if (typeof json === 'string') {
      try { data = JSON.parse(json); } catch { data = null; }
    }
    if (window.__camPickKeyResolve) {
      window.__camPickKeyResolve(data);
      window.__camPickKeyResolve = null;
      window.__camPickKeyReject = null;
    }
  };

  window.__camOnKeyPickError = (message) => {
    if (window.__camPickKeyReject) {
      window.__camPickKeyReject(new Error(message || 'File picker failed'));
      window.__camPickKeyResolve = null;
      window.__camPickKeyReject = null;
    }
  };

  let ok = false;
  const termHost = nativeTermHost();
  if (termHost) {
    const termApi = buildTermBridge();
    window.__camNativeTerm = termApi;
    try { window.CamBridge.term = termApi; } catch {}
    ok = true;
  }

  const bridge = nativeBridge();
  if (bridge) {
    const api = buildDirectHubApi();
    window.__camDirectHub = api;
    try { bridge.directHub = api; } catch {}
    if (!window.CamBridge.files) {
      try {
        window.CamBridge.files = {
          pickPrivateKey: () => pickPrivateKeyFromAndroid(),
        };
      } catch {}
    }
    ok = true;
  }
  return ok;
}

/** Re-bind after page load (safe to call multiple times). */
export function ensureMobileCamBridgeShim() {
  return installMobileCamBridgeShim();
}

if (typeof window !== 'undefined') {
  window.__camInstallBridge = () => installMobileCamBridgeShim();
}

export function pickPrivateKeyFromAndroid() {
  return new Promise((resolve, reject) => {
    const bridge = nativeBridge();
    if (!bridge || typeof bridge.pickPrivateKey !== 'function') {
      reject(new Error('Private key picker is not available on this device'));
      return;
    }
    window.__camPickKeyResolve = resolve;
    window.__camPickKeyReject = reject;
    try {
      bridge.pickPrivateKey();
    } catch (err) {
      window.__camPickKeyResolve = null;
      window.__camPickKeyReject = null;
      reject(err);
    }
  });
}

/** Native in-process Hub API when WebView fetch to 127.0.0.1 is blocked. */
export function nativeHubRequest(method, path, body, token) {
  const hub = window.__camDirectHub;
  if (hub && typeof hub.request === 'function') {
    return hub.request(method, path, body, token);
  }
  return null;
}
