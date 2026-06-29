/**
 * Direct Settings — mobile workflow: Enable → add Nodes → Connect.
 */
import { api, state, navigate } from './app.js';
import { ensureMobileCamBridgeShim } from './mobile-bridge.js?v=2.3.39';
import { bridgeDirectHub } from '../shared/direct-settings.js';
import {
  refreshHubCapabilities,
  setHubCapabilities,
} from '../shared/hub-capabilities.js';
import {
  isDirectEnabled,
  setDirectEnabled,
  disconnectRelayForDirect,
  resetDirectSessionForRelay,
} from './direct-session.js';
import { filterAgentsOnEnabledHosts } from '../shared/node-host-meta.js';

const PROFILE_KIND_KEY = 'cam_profile_kind';

export function readDirectConfig() {
  return {
    serverUrl: localStorage.getItem('cam_server_url') || '',
    token: localStorage.getItem('cam_token') || '',
    relayUrl: '',
    relayToken: '',
  };
}

export function saveDirectConfig(cfg) {
  if (cfg.serverUrl) localStorage.setItem('cam_server_url', cfg.serverUrl);
  else localStorage.removeItem('cam_server_url');
  if (cfg.token) localStorage.setItem('cam_token', cfg.token);
  else localStorage.removeItem('cam_token');
}

function setProfileKindDirect() {
  try { localStorage.setItem(PROFILE_KIND_KEY, 'direct'); } catch {}
}

export async function connectMobileDirect() {
  try {
    await healDirectHub();
  } catch {
    return 'disconnected';
  }
  const cfg = readDirectConfig();
  if (!cfg.serverUrl || !cfg.token) return 'disconnected';

  api.configure({
    serverUrl: cfg.serverUrl,
    token: cfg.token,
    relayUrl: '',
    relayToken: '',
  });
  setProfileKindDirect();

  try {
    const mode = await api.connect();
    state.set('connectionMode', mode);
    if (mode === 'direct') {
      ensureMobileCamBridgeShim();
      try {
        const cfg = readDirectConfig();
        const caps = await refreshHubCapabilities(api, cfg.token);
        state.set('hubCapabilities', caps);
        const [agentsResp, ctxResp] = await Promise.all([
          api.listAgents(),
          api.listContexts(),
        ]);
        state.set('agents', filterAgentsOnEnabledHosts(agentsResp.agents || []));
        state.set('contexts', ctxResp.contexts || []);
      } catch {
        state.set('agents', []);
        state.set('contexts', []);
      }
    }
    return mode;
  } catch {
    state.set('connectionMode', 'disconnected');
    return 'disconnected';
  }
}

async function countDirectNodes() {
  const cfg = readDirectConfig();
  if (!cfg.serverUrl || !cfg.token) return 0;
  const hub = bridgeDirectHub();
  if (hub && typeof hub.request === 'function') {
    try {
      const data = await hub.request('GET', '/api/contexts', null, cfg.token);
      return (data && data.contexts || []).length;
    } catch {
      return 0;
    }
  }
  try {
    const resp = await fetch(`${cfg.serverUrl.replace(/\/$/, '')}/api/contexts`, {
      headers: { Authorization: `Bearer ${cfg.token}` },
      signal: AbortSignal.timeout(8000),
    });
    if (!resp.ok) return 0;
    const data = await resp.json();
    return (data.contexts || []).length;
  } catch {
    return 0;
  }
}

async function applyHubProfile(apiUrl, apiToken) {
  saveDirectConfig({
    serverUrl: apiUrl,
    token: apiToken,
    relayUrl: '',
    relayToken: '',
  });
  api.configure({
    serverUrl: apiUrl,
    token: apiToken,
    relayUrl: '',
    relayToken: '',
  });
}

async function probeHubProfile(cfg) {
  if (!cfg.serverUrl || !cfg.token) return false;
  const hub = bridgeDirectHub();
  if (hub && typeof hub.request === 'function') {
    try {
      await hub.request('GET', '/api/contexts', null, cfg.token);
      return true;
    } catch {
      return false;
    }
  }
  try {
    const resp = await fetch(`${cfg.serverUrl.replace(/\/$/, '')}/api/contexts`, {
      headers: { Authorization: `Bearer ${cfg.token}` },
      signal: AbortSignal.timeout(4000),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

async function startDirectHub() {
  const hub = bridgeDirectHub();
  if (!hub) throw new Error('Embedded Hub is not available on this device');
  const res = await hub.start();
  if (!res || res.ok !== true || !res.apiUrl || !res.apiToken) {
    throw new Error((res && res.error) || 'Failed to start embedded Hub');
  }
  await applyHubProfile(res.apiUrl, res.apiToken);
  try {
    const caps = await refreshHubCapabilities(api, res.apiToken);
    state.set('hubCapabilities', caps);
  } catch {
    setHubCapabilities(null);
  }
  return res;
}

/** Restart or refresh local Hub URL/token when fetch fails (stale port after app resume). */
export async function healDirectHub() {
  if (!isDirectEnabled()) return false;
  const hub = bridgeDirectHub();
  if (!hub) throw new Error('Embedded Hub is not available on this device');

  const cfg = readDirectConfig();
  if (await probeHubProfile(cfg)) {
    try {
      const caps = await refreshHubCapabilities(api, cfg.token);
      state.set('hubCapabilities', caps);
    } catch {}
    return true;
  }

  const res = await hub.start();
  if (!res || res.ok !== true || !res.apiUrl || !res.apiToken) {
    throw new Error((res && res.error) || 'Failed to start embedded Hub');
  }
  await applyHubProfile(res.apiUrl, res.apiToken);
  if (await probeHubProfile(readDirectConfig())) {
    try {
      const caps = await refreshHubCapabilities(api, readDirectConfig().token);
      state.set('hubCapabilities', caps);
    } catch {}
    return true;
  }

  if (typeof hub.restart === 'function') {
    const restarted = await hub.restart();
    if (restarted && restarted.ok === true && restarted.apiUrl && restarted.apiToken) {
      await applyHubProfile(restarted.apiUrl, restarted.apiToken);
    }
  }
  return await probeHubProfile(readDirectConfig());
}

/** Ensure local Hub is up for Save Node — does NOT call Connect. */
export async function ensureHubForSave() {
  if (!isDirectEnabled()) {
    throw new Error('Enable Direct in Settings first.');
  }
  const ok = await healDirectHub();
  if (!ok) {
    throw new Error('Local Hub could not start. Settings → Direct → Enable.');
  }
  const cfg = readDirectConfig();
  if (!cfg.serverUrl || !cfg.token) {
    throw new Error('Local Hub profile missing. Settings → Direct → Enable.');
  }
  api.configure({
    serverUrl: cfg.serverUrl,
    token: cfg.token,
    relayUrl: '',
    relayToken: '',
  });
  return cfg;
}

/** Save a node to the phone Hub only — no Connect session required. */
export async function hubCreateContext(body) {
  const cfg = await ensureHubForSave();
  const hub = bridgeDirectHub();
  if (hub && typeof hub.request === 'function') {
    return hub.request('POST', '/api/contexts', body, cfg.token);
  }
  return api.createContext(body);
}

export async function hubUpdateContext(name, body) {
  const cfg = await ensureHubForSave();
  const hub = bridgeDirectHub();
  const path = `/api/contexts/${encodeURIComponent(name)}`;
  if (hub && typeof hub.request === 'function') {
    return hub.request('PUT', path, body, cfg.token);
  }
  return api.updateContext(name, body);
}

export async function hubDeleteContext(name) {
  const cfg = await ensureHubForSave();
  const hub = bridgeDirectHub();
  const path = `/api/contexts/${encodeURIComponent(name)}`;
  if (hub && typeof hub.request === 'function') {
    return hub.request('DELETE', path, null, cfg.token);
  }
  return api.deleteContext(name);
}

/** Refresh node list from Hub without establishing Connect. */
export async function loadContextsFromHub(state) {
  const cfg = await ensureHubForSave();
  const hub = bridgeDirectHub();
  let contexts = [];
  if (hub && typeof hub.request === 'function') {
    const data = await hub.request('GET', '/api/contexts', null, cfg.token);
    contexts = (data && data.contexts) || [];
  } else {
    const resp = await api.listContexts();
    contexts = resp.contexts || [];
  }
  if (state) state.set('contexts', contexts);
  return contexts;
}

/** Render Direct settings panel into `container`. */
export function renderDirectSettings(container) {
  let nodeCount = 0;
  let hubSummary = 'stopped';

  container.innerHTML = `
    <div class="page-header"><h2>Settings — Direct</h2></div>
    <div class="relay-hint" style="margin:0 16px 12px">
      <strong>Direct</strong> runs a CAM Hub on this phone.
      <strong>Nodes → Save Node</strong> registers SSH hosts locally.
      <strong>Connect</strong> (here) is separate — use it when you want agents.
    </div>
    <div class="connection-status">
      <span class="conn-dot disconnected" id="direct-conn-dot"></span>
      <span class="conn-text" id="direct-conn-text">Disconnected</span>
    </div>
    <div class="settings-section mobile-direct-panel">
      <div class="direct-state-row">
        <span class="direct-state-badge state-stopped" id="direct-mobile-badge">stopped</span>
        <span class="direct-state-detail" id="direct-mobile-detail">
          Tap <strong>Enable</strong> to turn on Direct on this device.
        </span>
      </div>
      <div class="direct-target" id="direct-mobile-target"></div>
      <div class="direct-actions">
        <button type="button" id="direct-enable-btn" class="btn-primary btn-sm">Enable</button>
        <button type="button" id="direct-connect-btn" class="btn-secondary btn-sm" disabled>Connect</button>
        <button type="button" id="direct-disable-btn" class="btn-secondary btn-sm">Disable</button>
        <button type="button" id="direct-nodes-btn" class="btn-secondary btn-sm" disabled>Go to Nodes</button>
      </div>
      <div class="direct-hint" id="direct-mobile-hint" aria-live="polite"></div>
      <div class="settings-status" id="direct-mobile-status" aria-live="polite"></div>
      <details class="settings-section mobile-ssh-debug">
        <summary class="collapse-summary">SSH debug logs</summary>
        <p class="direct-hint" style="margin:8px 0">
          Reproduce the issue (Sync Host or open an agent), then Refresh and Copy to send for diagnosis.
          Passwords are never logged — only <code>auth=password cred=saved|missing</code>.
        </p>
        <pre class="diag-logs-pre" id="direct-ssh-logs">(tap Refresh)</pre>
        <div class="direct-actions" style="margin-top:8px">
          <button type="button" id="direct-ssh-logs-refresh" class="btn-secondary btn-sm">Refresh logs</button>
          <button type="button" id="direct-ssh-logs-copy" class="btn-secondary btn-sm">Copy logs</button>
        </div>
      </details>
    </div>
  `;

  const badgeEl = container.querySelector('#direct-mobile-badge');
  const detailEl = container.querySelector('#direct-mobile-detail');
  const targetEl = container.querySelector('#direct-mobile-target');
  const hintEl = container.querySelector('#direct-mobile-hint');
  const statusEl = container.querySelector('#direct-mobile-status');
  const enableBtn = container.querySelector('#direct-enable-btn');
  const connectBtn = container.querySelector('#direct-connect-btn');
  const disableBtn = container.querySelector('#direct-disable-btn');
  const nodesBtn = container.querySelector('#direct-nodes-btn');
  const connDot = container.querySelector('#direct-conn-dot');
  const connText = container.querySelector('#direct-conn-text');
  const sshLogsEl = container.querySelector('#direct-ssh-logs');
  const sshLogsRefresh = container.querySelector('#direct-ssh-logs-refresh');
  const sshLogsCopy = container.querySelector('#direct-ssh-logs-copy');

  function formatHubLogs(out) {
    const lines = (out && out.server) || [];
    const hdr = `=== CamUI SSH debug (${lines.length} lines) ===\n`;
    const body = lines.map((l) => {
      let ts = '??:??:??.???';
      if (l.ts) {
        try {
          const d = new Date(l.ts);
          ts = d.toISOString().slice(11, 23);
        } catch { /* keep default */ }
      }
      return `${ts} [${l.kind || '?'}] ${l.text || ''}`;
    }).join('\n');
    return hdr + body;
  }

  async function refreshSshLogs() {
    if (!sshLogsEl) return;
    const hub = bridgeDirectHub();
    if (!hub || typeof hub.logs !== 'function') {
      sshLogsEl.textContent = '(Enable Direct first — Hub logs need embedded Hub)';
      return;
    }
    sshLogsEl.textContent = 'Loading…';
    try {
      const out = await hub.logs();
      sshLogsEl.textContent = formatHubLogs(out);
    } catch (err) {
      sshLogsEl.textContent = `(logs unavailable: ${err?.message || err})`;
    }
  }

  async function copySshLogs() {
    const text = sshLogsEl?.textContent || '';
    if (!text || text.startsWith('(')) {
      setStatus('Refresh logs first.', false);
      return;
    }
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      setStatus('Logs copied — paste into chat or email.', true);
      state.toast('SSH logs copied', 'success');
    } catch (err) {
      setStatus(`Copy failed: ${err?.message || err}`, false);
    }
  }

  sshLogsRefresh?.addEventListener('click', () => { void refreshSshLogs(); });
  sshLogsCopy?.addEventListener('click', () => { void copySshLogs(); });

  function setStatus(msg, ok) {
    if (!statusEl) return;
    statusEl.textContent = msg || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (ok === true) statusEl.classList.add('is-ok');
    else if (ok === false) statusEl.classList.add('is-error');
  }

  function setHint(msg, cls) {
    if (!hintEl) return;
    hintEl.textContent = msg || '';
    hintEl.classList.remove('is-error', 'is-ok');
    if (cls) hintEl.classList.add(cls);
  }

  function paintChrome() {
    const enabled = isDirectEnabled();
    const mode = state.get('connectionMode');
    const connected = mode === 'direct';
    const cfg = readDirectConfig();
    const hasProfile = !!(cfg.serverUrl && cfg.token);

    if (badgeEl) {
      let badge = 'stopped';
      if (connected) badge = 'running';
      else if (enabled) badge = 'starting';
      badgeEl.className = 'direct-state-badge state-' + badge;
      badgeEl.textContent = connected ? 'connected' : enabled ? 'enabled' : 'stopped';
    }

    if (detailEl) {
      if (connected) {
        detailEl.textContent = 'Connected to the local Hub. Manage hosts on Nodes.';
      } else if (enabled && nodeCount === 0) {
        detailEl.innerHTML = 'Direct is <strong>enabled</strong>. Save hosts on <strong>Nodes</strong> (no Connect needed).';
      } else if (enabled && nodeCount > 0) {
        detailEl.textContent = `${nodeCount} host(s) saved locally. Tap Connect when ready for agents.`;
      } else {
        detailEl.innerHTML = 'Tap <strong>Enable</strong> to turn on Direct on this device.';
      }
    }

    if (targetEl) {
      if (hasProfile) {
        try {
          const u = new URL(cfg.serverUrl);
          targetEl.innerHTML = `Local Hub: <code>${u.host}</code> · nodes: ${nodeCount}`;
        } catch {
          targetEl.textContent = `Local Hub profile saved · nodes: ${nodeCount}`;
        }
      } else if (enabled) {
        targetEl.textContent = 'Starting local Hub…';
      } else {
        targetEl.textContent = 'No Direct profile yet.';
      }
    }

    if (connDot) connDot.className = 'conn-dot ' + (connected ? 'direct' : 'disconnected');
    if (connText) {
      connText.textContent = connected
        ? 'Connected (direct — local Hub)'
        : enabled ? 'Enabled — not connected' : 'Disconnected';
    }

    if (enableBtn) {
      enableBtn.disabled = enabled;
      enableBtn.textContent = enabled ? 'Enabled' : 'Enable';
    }
    if (connectBtn) {
      connectBtn.disabled = !(enabled && hasProfile && !connected);
      connectBtn.textContent = connected ? 'Connected' : 'Connect';
    }
    if (nodesBtn) nodesBtn.disabled = !enabled;
    if (disableBtn) disableBtn.disabled = !enabled && !connected;
  }

  async function refresh() {
    if (isDirectEnabled()) {
      const hub = bridgeDirectHub();
      if (hub) {
        try {
          const chk = await hub.check();
          hubSummary = (chk && chk.summary) || hubSummary;
        } catch {}
      }
      nodeCount = await countDirectNodes();
    } else {
      nodeCount = 0;
    }
    paintChrome();
  }

  enableBtn?.addEventListener('click', async () => {
    if (isDirectEnabled()) return;
    setStatus('Enabling Direct…');
    setHint('');
    try {
      disconnectRelayForDirect();
      await startDirectHub();
      setDirectEnabled(true);
      setProfileKindDirect();
      nodeCount = await countDirectNodes();
      setStatus('Direct enabled. Save hosts on Nodes — Connect is optional.', true);
      setHint('Nodes → Add Host → Save Node. Connect here only when you need agents.', 'is-ok');
      state.toast('Direct enabled', 'success');
      paintChrome();
    } catch (err) {
      setDirectEnabled(false);
      setStatus(err.message || 'Enable failed', false);
      paintChrome();
    }
  });

  connectBtn?.addEventListener('click', async () => {
    if (!isDirectEnabled()) {
      setStatus('Enable Direct first.', false);
      return;
    }
    setStatus('Connecting…');
    try {
      const mode = await connectMobileDirect();
      if (mode === 'direct') {
        setStatus('Connected via Direct.', true);
        setHint('Agents and Nodes now use the local Hub.', 'is-ok');
        state.toast('Connected (direct)', 'success');
      } else {
        setStatus('Connect failed — is the local Hub running?', false);
      }
    } catch (err) {
      setStatus(err.message || 'Connect failed', false);
    }
    paintChrome();
  });

  disableBtn?.addEventListener('click', async () => {
    setStatus('Disabling Direct…');
    const hub = bridgeDirectHub();
    if (hub) {
      try { await hub.stop(); } catch {}
    }
    setDirectEnabled(false);
    saveDirectConfig({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
    try { localStorage.removeItem(PROFILE_KIND_KEY); } catch {}
    api.disconnect();
    state.set('connectionMode', 'disconnected');
    state.set('agents', []);
    state.set('contexts', []);
    nodeCount = 0;
    setStatus('Direct disabled. Relay tab is unchanged.', null);
    setHint('');
    state.toast('Direct disabled', 'success');
    paintChrome();
  });

  nodesBtn?.addEventListener('click', () => {
    navigate('/machines');
  });

  void refresh();
  const poll = setInterval(() => {
    if (!container.isConnected) {
      clearInterval(poll);
      return;
    }
    void refresh();
  }, 4000);

  return () => clearInterval(poll);
}

/** Called when Relay Save & Connect succeeds — see direct-session.js */
