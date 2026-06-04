/**
 * CAM Desktop — Electron preload.
 *
 * Exposes only the narrow CamBridge surface to the renderer. No CAM
 * product behavior lives here; agent operations go through CamApi
 * (web/js/api.js) and reach the existing CAM HTTP/WS endpoints.
 */

'use strict';

const { contextBridge, ipcRenderer, shell } = require('electron');

function isHttpUrl(target) {
  return typeof target === 'string' && /^https?:\/\//i.test(target);
}

contextBridge.exposeInMainWorld('CamBridge', {
  getPlatform() {
    return process.platform;
  },
  getAppVersion() {
    // Renderer-safe version string from preload's process.versions namespace.
    return process.versions?.electron || '';
  },
  openExternal(target) {
    if (isHttpUrl(target)) shell.openExternal(target);
  },
  restartApp(_route) {
    // The renderer is responsible for persisting route state (e.g. via
    // localStorage) before calling this — preload runs in a separate
    // context and cannot touch the renderer's localStorage.
    ipcRenderer.send('cam:restart');
  },

  // Direct Hub lifecycle (CAM-DESK-DIRECT-010..019,
  // CAM-DESK-HUB-010..012). The Direct Hub is an embedded Node HTTP
  // server inside Electron main — see
  // apps/cam-desktop/electron/embedded-hub.cjs. There is no host
  // `cam` CLI, Python, WSL, or shell involved. All methods are
  // argument-free; main owns every port/token decision. The renderer
  // cannot specify a binary, a path, an environment, or a shell
  // string — there is no shell.
  //
  // check()    → snapshot of platform / runtime / api port state +
  //              ownership + summary (stopped|running|
  //              port-conflict|error).
  // start()    → starts the embedded Hub on 127.0.0.1:8420 (or first
  //              free port in 8420..8429) with a freshly generated
  //              API token; returns { ok, apiUrl, apiToken, state }.
  //              The renderer persists apiUrl + apiToken into the
  //              existing Direct localStorage keys (cam_server_url
  //              + cam_token), sets cam_profile_kind=direct, and
  //              calls CamApi.connect. The embedded Hub then serves
  //              the contexts/agents tables it owns.
  // stop()     → closes only the embedded Hub we started.
  // restart()  → stop() + start().
  // logs()     → rolling event buffer for Diagnostics (start/stop,
  //              request errors). No shell output — there is no shell.
  // getProfile() → token-free view of current port/PID/state.
  //
  // IPC handler names on the main-process side are kept as `local:*`
  // for internal compatibility with the preload contract. Renderer-
  // facing code must use this `directHub` surface; the older
  // `localBackend` and Phase 2A `checkBackendReadiness` /
  // `startLocalBackend` names are retired.
  directHub: {
    check() {
      return ipcRenderer.invoke('local:check');
    },
    start() {
      return ipcRenderer.invoke('local:start');
    },
    stop() {
      return ipcRenderer.invoke('local:stop');
    },
    restart() {
      return ipcRenderer.invoke('local:restart');
    },
    logs() {
      return ipcRenderer.invoke('local:logs', {});
    },
    getProfile() {
      return ipcRenderer.invoke('local:getProfile');
    },
  },

  // Narrow file-picker surface for the Nodes "Add Host" SSH key
  // field (CAM-DESK-DIRECT-017). Parameter-free by design: the
  // renderer cannot dictate the dialog title, filters, default path,
  // or any other surface — main owns those. Returns
  // `{ path: <selected file path> | null }`. No shell, no exec.
  files: {
    pickPrivateKey() {
      return ipcRenderer.invoke('files:pickPrivateKey');
    },
  },

  // Terminal mode (CAM-DESK-TERM-001..005). The renderer xterm.js
  // never opens its own SSH — every channel goes through main, which
  // resolves the agent's owning context, decrypts the remembered
  // credential, and opens an ssh2 PTY channel via the existing
  // connection pool. The renderer only ever sees the opaque sessionId
  // and the UTF-8 byte stream.
  //
  //   open({agentId, cols, rows}) → {ok, sessionId} | {ok:false, error, detail}
  //   input({sessionId, data})    → {ok}
  //   resize({sessionId, cols, rows}) → {ok}
  //   close({sessionId})          → {ok}
  //   onData(cb)                  → unsubscribe(); cb({sessionId, data:string})
  //   onStatus(cb)                → unsubscribe(); cb({sessionId, kind, code, signal})
  term: {
    open(payload)   { return ipcRenderer.invoke('term:open',   payload || {}); },
    input(payload)  { return ipcRenderer.invoke('term:input',  payload || {}); },
    resize(payload) { return ipcRenderer.invoke('term:resize', payload || {}); },
    close(payload)  { return ipcRenderer.invoke('term:close',  payload || {}); },
    onData(cb) {
      const listener = (_evt, msg) => { try { cb && cb(msg); } catch { /* noop */ } };
      ipcRenderer.on('term:data', listener);
      return () => ipcRenderer.removeListener('term:data', listener);
    },
    onStatus(cb) {
      const listener = (_evt, msg) => { try { cb && cb(msg); } catch { /* noop */ } };
      ipcRenderer.on('term:status', listener);
      return () => ipcRenderer.removeListener('term:status', listener);
    },
  },
});
