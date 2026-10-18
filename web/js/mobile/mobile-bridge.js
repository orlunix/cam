/** Android CamBridge shims (Direct Hub + file picker).
 *
 * Pending-callback state lives on window.__camBridgeShared so reinstalling the
 * shim cannot orphan in-flight native responses. Terminal bytes use /term
 * WebSocket frames and never cross this string callback bridge.
 */

const _sh = (() => {
  const fresh = () => ({
    dhSeq: 0,
    dhPending: {},
    filesPending: {},
  });
  if (typeof window === 'undefined') return fresh();
  window.__camBridgeShared = window.__camBridgeShared || fresh();
  return window.__camBridgeShared;
})();

// Native bridge calls only settle via the native callback; if the callback
// is lost (or an SSH connect stalls), reject after a timeout instead of
// hanging forever. A late native response for a timed-out id is dropped
// harmlessly (the pending entry is already gone).
const HUB_OP_TIMEOUT_MS = 60000;      // start/stop/restart/check/logs/getProfile
const HUB_REQUEST_TIMEOUT_MS = 90000; // hub API requests (SSH-backed, serialized)

function _armTimeout(pendingMap, id, ms, label) {
  return setTimeout(() => {
    const p = pendingMap[id];
    if (!p) return;
    delete pendingMap[id];
    if (p.cleanup) p.cleanup();
    console.warn(`[mobile-bridge] ${label} timed out after ${ms}ms (id=${id})`);
    p.reject(new Error(`${label} timed out — no response from native side`));
  }, ms);
}

function nativeBridge() {
  const b = typeof window !== 'undefined' ? window.CamBridge : null;
  if (!b || typeof b.directHub_start !== 'function') return null;
  return b;
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

function invokeDirectHubRequest(method, path, body, token, signal) {
  return new Promise((resolve, reject) => {
    const bridge = nativeBridge();
    if (!bridge || typeof bridge.directHub_request !== 'function') {
      reject(new Error('Embedded Hub API bridge unavailable'));
      return;
    }
    if (signal?.aborted) {
      const err = new Error('Request aborted');
      err.name = 'AbortError';
      reject(err);
      return;
    }
    const id = 'dh' + (++_sh.dhSeq);
    let onAbort = null;
    _sh.dhPending[id] = {
      resolve: (res) => {
        if (res && res.ok) resolve(res.data);
        else reject(hubErr(res));
      },
      reject,
      cleanup: () => {
        if (signal && onAbort) signal.removeEventListener('abort', onAbort);
      },
    };
    if (signal) {
      onAbort = () => {
        const pending = _sh.dhPending[id];
        if (!pending) return;
        clearTimeout(pending.timer);
        delete _sh.dhPending[id];
        pending.cleanup();
        const err = new Error('Request aborted');
        err.name = 'AbortError';
        reject(err);
      };
      signal.addEventListener('abort', onAbort, { once: true });
    }
    _sh.dhPending[id].timer = _armTimeout(
      _sh.dhPending, id, HUB_REQUEST_TIMEOUT_MS, `hub ${method} ${path}`);
    try {
      const bodyJson = body != null ? JSON.stringify(body) : '';
      bridge.directHub_request(id, method, path || '/', bodyJson, token || '');
    } catch (err) {
      clearTimeout(_sh.dhPending[id] && _sh.dhPending[id].timer);
      const pending = _sh.dhPending[id];
      delete _sh.dhPending[id];
      if (pending?.cleanup) pending.cleanup();
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
    request: (method, path, body, token, signal) => invokeDirectHubRequest(method, path, body, token, signal),
  };
}

export function installMobileCamBridgeShim() {
  if (typeof window === 'undefined') return false;

  window.__camDirectHubCb = (id, ok, json) => {
    const p = _sh.dhPending[id];
    clearTimeout(p && p.timer);
    delete _sh.dhPending[id];
    if (!p) return;
    if (p.cleanup) p.cleanup();
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

  window.__camFilesCb = (id, ok, json) => {
    const p = _sh.filesPending[id];
    clearTimeout(p && p.timer);
    delete _sh.filesPending[id];
    if (!p) return;
    let data = null;
    try { data = typeof json === 'string' ? JSON.parse(json) : json; } catch { data = null; }
    if (ok) p.resolve(data || { ok: true });
    else p.reject(new Error((data && data.detail) || 'file operation failed'));
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
    // Export helper used by nodes-mode: share text via the Android share sheet.
    if (typeof window.CamBridge.files_saveText === 'function'
        && typeof window.CamBridge.files.saveText !== 'function') {
      try {
        window.CamBridge.files.saveText = (opts) => new Promise((resolve, reject) => {
          const id = 'fs' + (++_sh.dhSeq);
          _sh.filesPending[id] = { resolve, reject };
          _sh.filesPending[id].timer = _armTimeout(
            _sh.filesPending, id, 30000, 'files_saveText');
          try {
            window.CamBridge.files_saveText(id, JSON.stringify(opts || {}));
          } catch (err) {
            clearTimeout(_sh.filesPending[id] && _sh.filesPending[id].timer);
            delete _sh.filesPending[id];
            reject(err);
          }
        });
      } catch {}
    }
    // Import helper used by nodes-mode Browse…: SAF pick → app storage path.
    if (typeof window.CamBridge.files_pickFile === 'function'
        && typeof window.CamBridge.files.pickFile !== 'function') {
      try {
        window.CamBridge.files.pickFile = (opts) => new Promise((resolve, reject) => {
          const id = 'fs' + (++_sh.dhSeq);
          _sh.filesPending[id] = { resolve, reject };
          _sh.filesPending[id].timer = _armTimeout(
            _sh.filesPending, id, 120000, 'files_pickFile');
          try {
            window.CamBridge.files_pickFile(id, JSON.stringify(opts || {}));
          } catch (err) {
            clearTimeout(_sh.filesPending[id] && _sh.filesPending[id].timer);
            delete _sh.filesPending[id];
            reject(err);
          }
        });
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
