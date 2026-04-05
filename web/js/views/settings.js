import { api, state } from '../app.js';

// --- Profile helpers ---

const PROFILES_KEY = 'cam_profiles';
const ACTIVE_PROFILE_KEY = 'cam_active_profile';

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

function currentConfigFromStorage() {
  return {
    serverUrl: localStorage.getItem('cam_server_url') || '',
    token: localStorage.getItem('cam_token') || '',
    relayUrl: localStorage.getItem('cam_relay_url') || '',
    relayToken: localStorage.getItem('cam_relay_token') || '',
  };
}

function applyConfig(cfg) {
  localStorage.setItem('cam_server_url', cfg.serverUrl || '');
  localStorage.setItem('cam_token', cfg.token || '');
  localStorage.setItem('cam_relay_url', cfg.relayUrl || '');
  localStorage.setItem('cam_relay_token', cfg.relayToken || '');
}

// --- Main render ---

export function renderSettings(container) {
  const mode = state.get('connectionMode');
  const profiles = loadProfiles();
  const activeProfile = getActiveProfileName();

  container.innerHTML = `
    <div class="page-header">
      <h2>Settings</h2>
    </div>

    <div class="connection-status">
      <span class="conn-dot ${mode}"></span>
      <span class="conn-text">
        ${mode === 'direct' ? 'Connected directly' :
          mode === 'relay' ? 'Connected via relay' :
          'Disconnected'}
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
      <div class="form-section-label">Direct Connection</div>
      <div class="form-group">
        <label for="server-url">Server URL</label>
        <input type="url" id="server-url" class="form-input"
          value="${localStorage.getItem('cam_server_url') || ''}"
          placeholder="http://localhost:8420">
      </div>
      <div class="form-group">
        <label for="api-token">API Token</label>
        <input type="password" id="api-token" class="form-input"
          value="${localStorage.getItem('cam_token') || ''}"
          placeholder="Bearer token">
      </div>

      <div class="section-divider"></div>

      <div class="form-section-label">Relay Connection</div>
      <div class="form-group">
        <label for="relay-url">Relay URL</label>
        <input type="url" id="relay-url" class="form-input"
          value="${localStorage.getItem('cam_relay_url') || ''}"
          placeholder="ws://relay:8443">
      </div>
      <div class="form-group">
        <label for="relay-token">Relay Token</label>
        <input type="password" id="relay-token" class="form-input"
          value="${localStorage.getItem('cam_relay_token') || ''}"
          placeholder="Shared secret">
      </div>

      <div class="form-actions">
        <button type="submit" class="btn-primary btn-full">Save & Connect</button>
        <div style="display:flex;gap:8px;width:100%">
          <button type="button" class="btn-secondary btn-full" id="test-btn" style="flex:1">Test</button>
          <button type="button" class="btn-secondary btn-full" id="save-profile-btn" style="flex:1">Save as Profile</button>
        </div>
      </div>
    </form>

    <div class="section-divider"></div>
    <div class="form-section-label">App</div>
    <div class="form-group">
      <div class="about-text" style="margin-bottom:8px;">CAM v0.1.0 &mdash; Installed: ${(window.CamBridge && window.CamBridge.getAppVersion) ? window.CamBridge.getAppVersion() : (document.querySelector('meta[name="cam-version"]')?.content || 'unknown')}</div>
      <button type="button" class="btn-secondary btn-full" id="update-btn">Update app</button>
      ${(window.CamBridge && window.CamBridge.restartApp) ? '<button type="button" class="btn-secondary btn-full" id="reload-btn" style="margin-top:8px;">Reload app</button>' : ''}
    </div>
  `;

  // --- Profile actions ---

  container.querySelectorAll('.profile-load').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.name;
      const profile = profiles.find(p => p.name === name);
      if (!profile) return;

      applyConfig(profile);
      setActiveProfileName(name);
      api.configure(profile);

      let newMode;
      try {
        newMode = await api.connect();
      } catch {
        newMode = 'disconnected';
      }
      state.set('connectionMode', newMode);

      if (newMode !== 'disconnected') {
        state.toast(`Switched to "${name}" (${newMode})`, 'success');
        try {
          const [agentsResp, ctxResp] = await Promise.all([
            api.listAgents({ limit: 50 }),
            api.listContexts(),
          ]);
          state.set('agents', agentsResp.agents || []);
          state.set('contexts', ctxResp.contexts || []);
        } catch {}
        if (newMode === 'relay') api.requestRelayEventStream();
      } else {
        state.toast(`Switched to "${name}" but connection failed`, 'error');
      }

      renderSettings(container);
    });
  });

  container.querySelectorAll('.profile-delete').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name;
      if (!confirm(`Delete profile "${name}"?`)) return;
      const updated = profiles.filter(p => p.name !== name);
      saveProfiles(updated);
      if (getActiveProfileName() === name) setActiveProfileName('');
      state.toast('Profile deleted', 'success');
      renderSettings(container);
    });
  });

  // --- Save as Profile ---

  container.querySelector('#save-profile-btn').addEventListener('click', () => {
    const cfg = readFormConfig(container);
    const summary = profileSummary(cfg);

    // Suggest name from relay/server URL
    let suggested = '';
    try {
      const url = cfg.relayUrl || cfg.serverUrl;
      if (url) suggested = new URL(url).hostname;
    } catch {}

    const name = prompt('Profile name:', suggested);
    if (!name || !name.trim()) return;

    const trimmed = name.trim();
    const existing = loadProfiles();
    const idx = existing.findIndex(p => p.name === trimmed);
    const entry = { name: trimmed, ...cfg };

    if (idx >= 0) {
      existing[idx] = entry;
    } else {
      existing.push(entry);
    }

    // Keep max 10 profiles
    while (existing.length > 10) existing.shift();

    saveProfiles(existing);
    setActiveProfileName(trimmed);
    state.toast(`Profile "${trimmed}" saved`, 'success');
    renderSettings(container);
  });

  // --- Save & Connect ---

  container.querySelector('#settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const cfg = readFormConfig(container);

    applyConfig(cfg);
    api.configure(cfg);

    // Auto-update active profile if one matches
    const existing = loadProfiles();
    const active = getActiveProfileName();
    if (active) {
      const idx = existing.findIndex(p => p.name === active);
      if (idx >= 0) {
        existing[idx] = { name: active, ...cfg };
        saveProfiles(existing);
      }
    }

    let newMode;
    try {
      newMode = await api.connect();
    } catch (err) {
      state.toast(`Connect error: ${err.message}`, 'error');
      newMode = 'disconnected';
    }
    state.set('connectionMode', newMode);

    if (newMode !== 'disconnected') {
      state.toast(`Connected (${newMode})`, 'success');
      try {
        const [agentsResp, ctxResp] = await Promise.all([
          api.listAgents({ limit: 50 }),
          api.listContexts(),
        ]);
        state.set('agents', agentsResp.agents || []);
        state.set('contexts', ctxResp.contexts || []);
      } catch (err) {
        state.toast(`Data load error: ${err.message}`, 'error');
      }
      if (newMode === 'relay') api.requestRelayEventStream();
    } else {
      state.toast('Connection failed — check URL and token', 'error');
    }

    renderSettings(container);
  });

  // --- Update app ---

  container.querySelector('#update-btn').addEventListener('click', async () => {
    const btn = container.querySelector('#update-btn');
    btn.disabled = true;
    btn.textContent = 'Checking...';

    const hasBridge = window.CamBridge && window.CamBridge.installApk;

    if (hasBridge) {
      try {
        const info = await api.request('GET', '/api/system/apk/info');
        const installedVer = window.CamBridge.getAppVersion();
        if (info.version === installedVer) {
          state.toast(`Already up to date (${installedVer})`, 'success');
          btn.disabled = false;
          btn.textContent = 'Update app';
          return;
        }
        btn.textContent = `Downloading ${info.version}...`;
        const result = await api.request('GET', '/api/system/apk/download');
        if (!result.data) {
          state.toast('Download failed: no data', 'error');
          btn.disabled = false;
          btn.textContent = 'Update app';
          return;
        }
        btn.textContent = 'Installing...';
        const ok = window.CamBridge.installApk(result.data);
        if (ok) {
          state.toast(`Installing ${result.version}...`, 'success');
        } else {
          state.toast('Install failed', 'error');
        }
      } catch (err) {
        state.toast(`Update failed: ${err.message}`, 'error');
      }
      btn.disabled = false;
      btn.textContent = 'Update app';
    } else {
      const serverUrl = localStorage.getItem('cam_server_url') || '';
      let baseUrl = serverUrl || (location.protocol !== 'file:' ? location.origin : '');
      if (baseUrl) {
        try { const r = await navigator.serviceWorker.getRegistrations(); await Promise.all(r.map(x => x.unregister())); } catch {}
        try { const k = await caches.keys(); await Promise.all(k.map(x => caches.delete(x))); } catch {}
        state.toast('Reloading...', 'success');
        setTimeout(() => { location.href = baseUrl + '/?_=' + Date.now(); }, 500);
      } else {
        btn.disabled = false;
        btn.textContent = 'Update app';
        state.toast('No server URL configured', 'error');
      }
    }
  });

  // --- Reload app (Android only) ---
  const reloadBtn = container.querySelector('#reload-btn');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', () => {
      // Save current route so the app returns to the same screen after restart
      localStorage.setItem('cam_reload_route', location.hash || '#/');
      state.toast('Restarting...', 'success');
      setTimeout(() => window.CamBridge.restartApp(), 300);
    });
  }

  // --- Test Connection ---

  container.querySelector('#test-btn').addEventListener('click', async () => {
    const serverUrl = container.querySelector('#server-url').value.trim();
    const token = container.querySelector('#api-token').value.trim();

    if (!serverUrl) {
      state.toast('Enter a server URL first', 'error');
      return;
    }

    try {
      const headers = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;
      const r = await fetch(`${serverUrl}/api/contexts`, {
        headers,
        signal: AbortSignal.timeout(10000),
      });
      if (r.ok) {
        state.toast('Server OK — token valid', 'success');
      } else if (r.status === 401) {
        state.toast('Server reachable but token rejected', 'error');
      } else {
        state.toast(`Server returned ${r.status}`, 'error');
      }
    } catch (e) {
      state.toast(`Connection failed: ${e.message}`, 'error');
    }
  });
}

// --- Helpers ---

function readFormConfig(container) {
  return {
    serverUrl: container.querySelector('#server-url').value.trim(),
    token: container.querySelector('#api-token').value.trim(),
    relayUrl: container.querySelector('#relay-url').value.trim(),
    relayToken: container.querySelector('#relay-token').value.trim(),
  };
}

function profileSummary(cfg) {
  const parts = [];
  if (cfg.relayUrl) {
    try { parts.push('relay: ' + new URL(cfg.relayUrl).host); } catch { parts.push('relay'); }
  }
  if (cfg.serverUrl) {
    try { parts.push('server: ' + new URL(cfg.serverUrl).host); } catch { parts.push('server'); }
  }
  return parts.join(' | ') || 'empty';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}
