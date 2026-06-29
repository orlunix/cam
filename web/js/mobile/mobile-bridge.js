/** Android CamBridge shims (Direct Hub + file picker). */

let _dhSeq = 0;
const _dhPending = {};
let _termSeq = 0;
const _termPending = {};
const _termDataHandlers = new Set();
const _termStatusHandlers = new Set();

function invokeTerm(method, payload = {}) {
  return new Promise((resolve, reject) => {
    const bridge = nativeBridge();
    if (!bridge || typeof bridge['term_' + method] !== 'function') {
      reject(new Error('Terminal bridge unavailable'));
      return;
    }
    const id = 'tm' + (++_termSeq);
    _termPending[id] = { resolve, reject };
    try {
      bridge['term_' + method](id, JSON.stringify(payload || {}));
    } catch (err) {
      delete _termPending[id];
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
    onData(cb) {
      _termDataHandlers.add(cb);
      return () => _termDataHandlers.delete(cb);
    },
    onStatus(cb) {
      _termStatusHandlers.add(cb);
      return () => _termStatusHandlers.delete(cb);
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
    const id = 'dh' + (++_dhSeq);
    _dhPending[id] = { resolve, reject };
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
          delete _dhPending[id];
          reject(new Error('Unknown Hub method: ' + method));
          return;
      }
    } catch (err) {
      delete _dhPending[id];
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
    const id = 'dh' + (++_dhSeq);
    _dhPending[id] = {
      resolve: (res) => {
        if (res && res.ok) resolve(res.data);
        else reject(hubErr(res));
      },
      reject,
    };
    try {
      const bodyJson = body != null ? JSON.stringify(body) : '';
      bridge.directHub_request(id, method, path || '/', bodyJson, token || '');
    } catch (err) {
      delete _dhPending[id];
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
    const p = _termPending[id];
    delete _termPending[id];
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
      for (const h of _termDataHandlers) {
        try { h(msg); } catch { /* noop */ }
      }
    } else if (kind === 'status') {
      for (const h of _termStatusHandlers) {
        try { h(msg); } catch { /* noop */ }
      }
    }
  };

  window.__camDirectHubCb = (id, ok, json) => {
    const p = _dhPending[id];
    delete _dhPending[id];
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
