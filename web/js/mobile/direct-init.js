/**
 * Direct mode bootstrap — additive only; Relay startup is unchanged in app.js.
 */
import { readRelayConfig, hasRelayConfig } from './settings-relay.js';
import { connectMobileDirect } from './settings-direct.js';
import { isDirectEnabled } from './direct-session.js';
import { bridgeDirectHub } from '../shared/direct-settings.js';

export async function maybeInitDirectMode(updateConnectionDot) {
  const relay = readRelayConfig();
  if (hasRelayConfig(relay) && !isDirectEnabled()) return;

  if (!isDirectEnabled()) return;

  const hub = bridgeDirectHub();
  if (hub) {
    try {
      const check = await hub.check();
      if (check && check.summary !== 'running') {
        await hub.start();
      }
    } catch {}
  }

  const cfg = {
    serverUrl: localStorage.getItem('cam_server_url') || '',
    token: localStorage.getItem('cam_token') || '',
  };
  if (!cfg.serverUrl || !cfg.token) return;

  const mode = await connectMobileDirect();
  if (typeof updateConnectionDot === 'function') updateConnectionDot(mode);
}
