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

  // Phase 2A — backend readiness. Both methods are argument-less; the
  // main process executes only fixed argv built from system-trusted data
  // (e.g. wsl.exe -l -q). The renderer cannot pass a command to run.
  checkBackendReadiness() {
    return ipcRenderer.invoke('cam:checkBackendReadiness');
  },
  startLocalBackend() {
    return ipcRenderer.invoke('cam:startLocalBackend');
  },
});
