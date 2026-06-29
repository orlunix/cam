/**
 * Relay Settings — frozen v2.2.0 implementation.
 * Do not add Direct mode logic here; extend via settings.js + settings-direct.js.
 */
import { api, state } from './app.js';
import { resetDirectSessionForRelay } from './direct-session.js';

const PROFILES_KEY = 'cam_profiles_v2';
const ACTIVE_PROFILE_KEY = 'cam_active_profile_v2';
const PROFILE_KIND_KEY = 'cam_profile_kind';

function loadProfiles() {
  try {
    return JSON.parse(localStorage.getItem(PROFILES_KEY)) || [];
  } catch { return []; }
}

function saveProfiles(profiles) {
  localStorage.setItem(PROFILES_KEY, JSON.stringify(profiles));
}

function getActiveProfileName() {
  return localStorage.getItem(ACTIVE_PROFILE_KEY) || '';
}

function setActiveProfileName(name) {
  localStorage.setItem(ACTIVE_PROFILE_KEY, name);
}

function applyRelayConfig(cfg) {
  localStorage.setItem('cam_relay_url', cfg.relayUrl || '');
  localStorage.setItem('cam_relay_token', cfg.relayToken || '');
  localStorage.setItem('cam_server_url', '');
  localStorage.setItem('cam_token', '');
  localStorage.setItem(PROFILE_KIND_KEY, 'relay');
}

function readFormConfig(container) {
  return {
    relayUrl: container.querySelector('#relay-url').value.trim(),
    relayToken: container.querySelector('#relay-token').value.trim(),
  };
}

function profileSummary(cfg) {
  if (!cfg.relayUrl) return 'empty';
  try { return 'relay: ' + new URL(cfg.relayUrl).host; } catch { return 'relay'; }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}

async function reloadHubData() {
  const [agentsResp, ctxResp, healthResp] = await Promise.all([
    api.listAgents({ limit: 50, refresh: true }),
    api.listContexts(),
    api.health().catch(() => null),
  ]);
  state.set('agents', agentsResp.agents || []);
  state.set('contexts', ctxResp.contexts || []);
  if (healthResp?.adapters) state.set('adapters', healthResp.adapters);
}

export async function connectRelay(cfg) {
  api.configure({ serverUrl: '', token: '', relayUrl: cfg.relayUrl, relayToken: cfg.relayToken });
  const mode = await api.connect();
  state.set('connectionMode', mode);
  if (mode === 'relay') {
    try { api._requestRelayEventStream(); } catch {}
    try {
      const rs = await api.relayStatus();
      if (rs && !rs.server_connected) {
        state.toast('Relay OK but camui start source is offline', 'warning', 8000);
      }
    } catch {}
    await reloadHubData();
  }
  return mode;
}

export function readRelayConfig() {
  return {
    relayUrl: localStorage.getItem('cam_relay_url') || '',
    relayToken: localStorage.getItem('cam_relay_token') || '',
  };
}

export function hasRelayConfig(cfg) {
  return !!(cfg.relayUrl && cfg.relayToken);
}

/** Render Relay settings into `container` (Relay-only UI, unchanged from v2.2.0). */
export function renderRelaySettings(container) {
  const mode = state.get('connectionMode');
  const profiles = loadProfiles();
  const activeProfile = getActiveProfileName();

  container.innerHTML = `
    <div class="page-header">
      <h2>Settings</h2>
    </div>

    <div class="relay-hint">
      <strong>CamUI V2</strong> connects via Relay to a workstation running
      <code>camui start --profile … --relay-url … --relay-token …</code>.
      The phone stores only Relay URL and Relay token.
    </div>

    <div class="connection-status">
      <span class="conn-dot ${mode}"></span>
      <span class="conn-text">
        ${mode === 'relay' ? 'Connected via relay' : 'Disconnected'}
      </span>
    </div>

    ${profiles.length > 0 ? `
    <div class="form-section-label">Saved Profiles</div>
    <div class="profile-list">
      ${profiles.map(p => `
        <div class="profile-item${p.name === activeProfile ? ' active' : ''}" data-name="${esc(p.name)}">
          <div class="profile-item-left">
            <span class="profile-name">${esc(p.name)}</span>
            <span class="profile-detail">${esc(profileSummary(p))}</span>
          </div>
          <div class="profile-item-right">
            <button class="btn-sm btn-secondary profile-load" data-name="${esc(p.name)}">Use</button>
            <button class="btn-sm btn-danger profile-delete" data-name="${esc(p.name)}">&times;</button>
          </div>
        </div>
      `).join('')}
    </div>
    <div class="section-divider"></div>
    ` : ''}

    <form id="settings-form" class="form">
      <div class="form-section-label">Relay Connection</div>
      <div class="form-group">
        <label for="relay-url">Relay URL</label>
        <input type="url" id="relay-url" class="form-input"
          value="${esc(localStorage.getItem('cam_relay_url') || '')}"
          placeholder="https://relay.example.com or ws://host:8443" autocomplete="off">
      </div>
      <div class="form-group">
        <label for="relay-token">Relay Token</label>
        <input type="password" id="relay-token" class="form-input"
          value="${esc(localStorage.getItem('cam_relay_token') || '')}"
          placeholder="Shared secret" autocomplete="off">
      </div>

      <div class="form-actions">
        <button type="submit" class="btn-primary btn-full">Save &amp; Connect</button>
        <div style="display:flex;gap:8px;width:100%">
          <button type="button" class="btn-secondary btn-full" id="test-btn" style="flex:1">Test</button>
          <button type="button" class="btn-secondary btn-full" id="save-profile-btn" style="flex:1">Save Profile</button>
        </div>
      </div>
    </form>

    <div class="section-divider"></div>
    <div class="form-section-label">App</div>
    <div class="form-group">
      <div class="about-text" style="margin-bottom:8px;">CamUI V2 &mdash; APK ${(window.CamBridge && window.CamBridge.getAppVersion) ? 'v' + window.CamBridge.getAppVersion() : (document.querySelector('meta[name="cam-version"]')?.content || 'unknown')}</div>
    </div>
  `;

  container.querySelectorAll('.profile-load').forEach(btn => {
    btn.addEventListener('click', async () => {
      const profile = profiles.find(p => p.name === btn.dataset.name);
      if (!profile) return;
      applyRelayConfig(profile);
      setActiveProfileName(profile.name);
      try {
        const newMode = await connectRelay(profile);
        if (newMode === 'relay') {
          await resetDirectSessionForRelay();
        }
        state.toast(newMode === 'relay' ? `Using "${profile.name}"` : 'Connection failed', newMode === 'relay' ? 'success' : 'error');
      } catch (e) {
        state.toast(e.message || 'Connection failed', 'error');
      }
      container.dispatchEvent(new CustomEvent('cam-relay-settings-changed', { bubbles: true }));
    });
  });

  container.querySelectorAll('.profile-delete').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name;
      if (!confirm(`Delete profile "${name}"?`)) return;
      saveProfiles(profiles.filter(p => p.name !== name));
      if (getActiveProfileName() === name) setActiveProfileName('');
      state.toast('Profile deleted', 'success');
      container.dispatchEvent(new CustomEvent('cam-relay-settings-changed', { bubbles: true }));
    });
  });

  container.querySelector('#save-profile-btn').addEventListener('click', () => {
    const cfg = readFormConfig(container);
    if (!cfg.relayUrl || !cfg.relayToken) {
      state.toast('Enter Relay URL and token first', 'error');
      return;
    }
    let suggested = '';
    try { suggested = new URL(cfg.relayUrl).hostname; } catch {}
    const name = prompt('Profile name:', suggested);
    if (!name || !name.trim()) return;
    const trimmed = name.trim();
    const existing = loadProfiles();
    const idx = existing.findIndex(p => p.name === trimmed);
    const entry = { name: trimmed, ...cfg };
    if (idx >= 0) existing[idx] = entry;
    else existing.push(entry);
    while (existing.length > 10) existing.shift();
    saveProfiles(existing);
    setActiveProfileName(trimmed);
    state.toast(`Profile "${trimmed}" saved`, 'success');
    container.dispatchEvent(new CustomEvent('cam-relay-settings-changed', { bubbles: true }));
  });

  container.querySelector('#settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const cfg = readFormConfig(container);
    if (!cfg.relayUrl || !cfg.relayToken) {
      state.toast('Relay URL and token are required', 'error');
      return;
    }
    applyRelayConfig(cfg);
    const active = getActiveProfileName();
    if (active) {
      const existing = loadProfiles();
      const idx = existing.findIndex(p => p.name === active);
      if (idx >= 0) {
        existing[idx] = { name: active, ...cfg };
        saveProfiles(existing);
      }
    }
    try {
      const newMode = await connectRelay(cfg);
      if (newMode === 'relay') {
        await resetDirectSessionForRelay();
      }
      state.toast(newMode === 'relay' ? 'Connected via relay' : 'Connection failed', newMode === 'relay' ? 'success' : 'error');
    } catch (err) {
      state.set('connectionMode', 'disconnected');
      state.toast(`Connect error: ${err.message}`, 'error');
    }
    container.dispatchEvent(new CustomEvent('cam-relay-settings-changed', { bubbles: true }));
  });

  container.querySelector('#test-btn').addEventListener('click', async () => {
    const relayUrl = container.querySelector('#relay-url').value.trim();
    if (!relayUrl) {
      state.toast('Enter Relay URL first', 'error');
      return;
    }
    const base = relayUrl.replace(/\/$/, '');
    try {
      const r = await fetch(`${base}/api/system/health`, { signal: AbortSignal.timeout(12000) });
      if (r.ok) state.toast('Relay HTTP reachable', 'success');
      else state.toast(`Relay returned HTTP ${r.status}`, 'error');
    } catch (e) {
      state.toast(`Relay test failed: ${e.message}`, 'error');
    }
  });
}
