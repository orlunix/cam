/**
 * Direct / Relay session isolation for mobile Settings.
 */
import { api, state } from './app.js';
import { bridgeDirectHub } from '../shared/direct-settings.js';
import { saveDirectConfig } from './settings-direct.js';

const DIRECT_ENABLED_KEY = 'cam_direct_enabled';

export function isDirectEnabled() {
  try { return localStorage.getItem(DIRECT_ENABLED_KEY) === '1'; } catch { return false; }
}

export function setDirectEnabled(on) {
  try {
    if (on) localStorage.setItem(DIRECT_ENABLED_KEY, '1');
    else localStorage.removeItem(DIRECT_ENABLED_KEY);
  } catch {}
}

/** Relay connected — reset Direct UI/session to initial. */
export async function resetDirectSessionForRelay() {
  setDirectEnabled(false);
  saveDirectConfig({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
  const hub = bridgeDirectHub();
  if (hub) {
    try { await hub.stop(); } catch {}
  }
}

/** Direct enabled — disconnect active Relay client (keep saved relay URL). */
export function disconnectRelayForDirect() {
  api.disconnect();
  if (state.get('connectionMode') === 'relay') {
    state.set('connectionMode', 'disconnected');
    state.set('agents', []);
    state.set('contexts', []);
  }
}

export function useDirectNodesUi() {
  return isDirectEnabled() || state.get('connectionMode') === 'direct';
}
