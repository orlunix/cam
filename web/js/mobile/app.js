/**
 * CamUI Mobile V2 — entry (Relay-only, camui-start Hub via relay).
 */
import { api } from '../api.js?v=2.2.0';
import { state } from '../state.js?v=2.2.0';
import { renderDashboard } from './views/dashboard.js?v=2.2.0';
import { renderAgentDetail } from './views/agent-detail.js?v=2.2.0';
import { renderStartAgent } from './views/start-agent.js?v=2.2.0';
import { renderSettings } from './settings.js?v=2.2.0';
import { renderFileBrowser } from './views/file-browser.js?v=2.2.0';
import { renderNodes } from './nodes.js?v=2.2.0';
import { renderSkills } from './skills.js?v=2.2.0';
import { renderAgentSettings } from './agent-settings.js?v=2.2.0';

const PROFILE_KIND_KEY = 'cam_profile_kind';

const routes = [
  { pattern: /^\/$/, view: renderDashboard, nav: '/' },
  { pattern: /^\/agent\/([^/]+)\/settings(?:\/([^/]+))?$/, view: renderAgentSettings, nav: null },
  { pattern: /^\/agent\/([^/]+)$/, view: renderAgentDetail, nav: null },
  { pattern: /^\/context\/([^/]+)\/files(.*)$/, view: renderFileBrowser, nav: null },
  { pattern: /^\/start$/, view: renderStartAgent, nav: '/start' },
  { pattern: /^\/skills$/, view: renderSkills, nav: '/skills' },
  { pattern: /^\/machines$/, view: renderNodes, nav: '/machines' },
  { pattern: /^\/settings$/, view: renderSettings, nav: '/settings' },
];

let currentCleanup = null;

function getPath() {
  return location.hash.slice(1) || '/';
}

function hideLoading() {
  const el = document.getElementById('loading-indicator');
  if (el) el.style.display = 'none';
}

function route() {
  hideLoading();
  const path = getPath();
  const content = document.getElementById('content');
  if (currentCleanup) { currentCleanup(); currentCleanup = null; }

  for (const r of routes) {
    const m = path.match(r.pattern);
    if (m) {
      content.innerHTML = '';
      currentCleanup = r.view(content, ...m.slice(1)) || null;
      updateNav(r.nav || path);
      return;
    }
  }

  content.innerHTML = '';
  currentCleanup = renderDashboard(content) || null;
  updateNav('/');
}

function updateNav(activePath) {
  document.querySelectorAll('#header-menu .nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.route === activePath);
  });
}

export function navigate(path) {
  location.hash = path;
}

function readRelayConfig() {
  return {
    relayUrl: localStorage.getItem('cam_relay_url') || '',
    relayToken: localStorage.getItem('cam_relay_token') || '',
  };
}

function hasRelayConfig(cfg) {
  return !!(cfg.relayUrl && cfg.relayToken);
}

async function init() {
  const headerMenu = document.getElementById('header-menu');
  if (headerMenu) {
    headerMenu.querySelectorAll('.header-menu-item').forEach(item => {
      item.addEventListener('click', () => headerMenu.classList.add('hidden'));
    });
  }

  document.addEventListener('touchmove', e => {
    if (e.touches.length > 1) e.preventDefault();
  }, { passive: false });

  const savedRoute = localStorage.getItem('cam_reload_route');
  if (savedRoute) {
    localStorage.removeItem('cam_reload_route');
    location.hash = savedRoute;
  }

  api.onEvent(handleEvent);
  state.subscribe(() => {
    updateConnectionDot(state.get('connectionMode'));
    updateToast();
  });
  window.addEventListener('hashchange', route);
  route();

  const cfg = readRelayConfig();
  try { localStorage.setItem(PROFILE_KIND_KEY, 'relay'); } catch {}

  if (!hasRelayConfig(cfg)) {
    api.configure({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
    _loadFromCache();
    state.set('connectionMode', 'disconnected');
    updateConnectionDot('disconnected');
    location.hash = '#/settings';
    return;
  }

  api.configure({ serverUrl: '', token: '', ...cfg });
  _loadFromCache();
  await _connectRelay(cfg);
  route();

  setInterval(async () => {
    const c = readRelayConfig();
    if (state.get('connectionMode') === 'disconnected') {
      if (hasRelayConfig(c)) await _connectRelay(c);
      return;
    }
    await refreshAgents();
  }, 10000);
}

async function _connectRelay(cfg) {
  if (!hasRelayConfig(cfg)) {
    state.set('connectionMode', 'disconnected');
    updateConnectionDot('disconnected');
    return;
  }
  api.configure({ serverUrl: '', token: '', relayUrl: cfg.relayUrl, relayToken: cfg.relayToken });
  try {
    const mode = await api.connect();
    state.set('connectionMode', mode);
    updateConnectionDot(mode);
    if (mode === 'relay') {
      try {
        const rs = await api.relayStatus();
        if (rs && !rs.server_connected) {
          state.toast('Relay up — camui start source offline', 'warning', 8000);
          const label = document.getElementById('conn-label');
          if (label) label.textContent = 'source offline';
        }
      } catch {}
      await loadData();
      try { api._requestRelayEventStream(); } catch {}
    } else {
      state.toast('Relay connection failed', 'error', 5000);
    }
  } catch (e) {
    console.error('Connect error:', e);
    state.set('connectionMode', 'disconnected');
    updateConnectionDot('disconnected');
  }
}

function _loadFromCache() {
  try {
    const DISPLAY_TTL = 1_800_000;
    const agentsRaw = localStorage.getItem('cam_cache:/api/agents?limit=50');
    if (agentsRaw) {
      const { data, ts } = JSON.parse(agentsRaw);
      if (Date.now() - ts < DISPLAY_TTL && data?.agents) state.set('agents', data.agents);
    }
    const ctxRaw = localStorage.getItem('cam_cache:/api/contexts');
    if (ctxRaw) {
      const { data, ts } = JSON.parse(ctxRaw);
      if (Date.now() - ts < DISPLAY_TTL && data?.contexts) state.set('contexts', data.contexts);
    }
  } catch {}
}

async function loadData() {
  try {
    const [agentsResp, contextsResp, healthResp] = await Promise.all([
      api.listAgents({ limit: 50, refresh: true }),
      api.listContexts(),
      api.health().catch(() => null),
    ]);
    state.set('agents', agentsResp.agents || []);
    state.set('contexts', contextsResp.contexts || []);
    if (healthResp?.adapters) state.set('adapters', healthResp.adapters);
  } catch (e) {
    console.error('Failed to load data:', e);
    if (api.mode === 'relay') {
      try {
        const rs = await api.relayStatus();
        if (rs && !rs.server_connected) {
          state.toast('camui start source offline', 'error', 8000);
        }
      } catch {}
    }
  }
}

let _idlePollCount = 0;
async function refreshAgents() {
  if (state.get('connectionMode') !== 'relay') return;
  const current = state.get('agents') || [];
  const hasActive = current.length === 0 || current.some(a => ['running', 'starting', 'pending'].includes(a.status));
  if (!hasActive) {
    _idlePollCount++;
    if (_idlePollCount % 3 !== 0) return;
  } else {
    _idlePollCount = 0;
  }
  try {
    const resp = await api.listAgents({ status: 'running', limit: 50, refresh: true });
    const running = resp.agents || [];
    const map = new Map(current.map(a => [a.id, a]));
    for (const a of running) map.set(a.id, a);
    const stale = current.some(a =>
      ['running', 'starting', 'pending'].includes(a.status) &&
      !running.find(r => r.id === a.id)
    );
    if (stale) {
      const full = await api.listAgents({ limit: 50, refresh: true });
      state.set('agents', full.agents || []);
      return;
    }
    state.set('agents', [...map.values()]);
  } catch {}
}

function handleEvent(event) {
  if (event.type === 'status_update') {
    state.updateAgent(event.agent_id, {
      status: event.status,
      state: event.state,
      exit_reason: event.exit_reason,
    });
  } else if (event.type === 'event' && event.event_type === 'state_change') {
    state.updateAgent(event.agent_id, {
      state: event.detail?.to || event.detail?.state,
    });
  }
}

function updateConnectionDot(mode) {
  const dot = document.getElementById('conn-status');
  if (!dot) return;
  dot.className = 'conn-dot ' + (mode === 'relay' ? 'relay' : 'disconnected');
  dot.title = mode === 'relay' ? 'Relay' : 'Disconnected';
  const label = document.getElementById('conn-label');
  if (label && mode === 'relay') label.textContent = 'relay';
  else if (label && mode !== 'relay') label.textContent = '';
}

function updateToast() {
  let el = document.getElementById('toast');
  const toast = state.get('toast');
  if (!toast) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = toast.message;
  el.className = `toast toast-${toast.type}`;
}

export { api, state };

window.__camApi = api;
window.__camState = state;
window.__camMobileV2 = true;

init();
