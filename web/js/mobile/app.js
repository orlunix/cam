/**
 * CamUI Mobile V2 — Relay entry (v2.2.0) + additive Direct mode hooks.
 */
import { api } from '../api.js?v=2.3.84';
import { state } from '../state.js?v=2.3.84';
import { renderDashboard } from './views/dashboard.js?v=2.3.84';
import { renderAgentDetail } from './views/agent-detail.js?v=2.3.84';
import { renderStartAgent } from './views/start-agent.js?v=2.3.84';
import { renderSettings } from './settings.js?v=2.3.84';
import { readRelayConfig, hasRelayConfig } from './settings-relay.js?v=2.3.84';
import { renderFileBrowser } from './views/file-browser.js?v=2.3.84';
import { renderNodes } from './nodes-page.js?v=2.3.84';
import { renderSkills } from './skills.js?v=2.3.84';
import { renderTodos } from './todos.js?v=2.3.84';
import { renderAgentSettings } from './agent-settings.js?v=2.3.84';
import { maybeInitDirectMode } from './direct-init.js?v=2.3.84';
import { installMobileCamBridgeShim } from './mobile-bridge.js?v=2.3.84';
import { refreshHubCapabilities, setHubCapabilities, HUB_CAP_FULL } from '../shared/hub-capabilities.js?v=2.3.84';
import { loadAgentFilters } from '../shared/agent-filters.js?v=2.3.84';
import { applyMobileAppearance } from '../shared/mobile-appearance.js?v=2.3.84';
import { filterAgentsOnEnabledHosts } from '../shared/node-host-meta.js?v=2.3.84';

const PROFILE_KIND_KEY = 'cam_profile_kind';

const routes = [
  { pattern: /^\/$/, view: renderDashboard, nav: '/' },
  { pattern: /^\/agent\/([^/]+)\/settings(?:\/([^/]+))?$/, view: renderAgentSettings, nav: null },
  { pattern: /^\/agent\/([^/]+)$/, view: renderAgentDetail, nav: null },
  { pattern: /^\/context\/([^/]+)\/files(.*)$/, view: renderFileBrowser, nav: null },
  { pattern: /^\/start$/, view: renderStartAgent, nav: '/start' },
  { pattern: /^\/skills$/, view: renderSkills, nav: '/skills' },
  { pattern: /^\/todos$/, view: renderTodos, nav: '/todos' },
  { pattern: /^\/machines$/, view: renderNodes, nav: '/machines' },
  { pattern: /^\/settings$/, view: renderSettings, nav: '/settings' },
];

let currentCleanup = null;

/** Clear inline heights after WebView resume (tablet drift fix). */
function forceViewportReflow() {
  const vp = 'width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover';
  const meta = document.querySelector('meta[name=viewport]');
  if (!meta) return;
  meta.setAttribute('content', vp + ',maximum-scale=1.01');
  void document.documentElement.offsetWidth;
  meta.setAttribute('content', vp);
}

function resetMobileLayout() {
  const vp = 'width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover';
  const meta = document.querySelector('meta[name=viewport]');
  if (meta) meta.setAttribute('content', vp);
  const app = document.getElementById('app');
  if (app) {
    app.style.height = '';
    app.style.width = '';
    app.style.transform = '';
    app.style.marginTop = '';
  }
  document.documentElement.style.height = '';
  document.documentElement.style.width = '';
  document.body.style.height = '';
  document.body.style.width = '';
  document.body.style.marginBottom = '';
  window.scrollTo(0, 0);
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  forceViewportReflow();
  window.dispatchEvent(new Event('resize'));
}

function scheduleLayoutResets() {
  resetMobileLayout();
  requestAnimationFrame(() => {
    resetMobileLayout();
    requestAnimationFrame(resetMobileLayout);
  });
  [50, 150, 350, 800].forEach((ms) => setTimeout(resetMobileLayout, ms));
}

function notifyNativeReady() {
  installMobileCamBridgeShim();
  scheduleLayoutResets();
  try {
    if (window.CamBridge && window.CamBridge.notifyAppReady) {
      window.CamBridge.notifyAppReady();
    }
  } catch {}
}

window.__camResetLayout = resetMobileLayout;

function getPath() {
  const raw = location.hash.slice(1) || '/';
  const q = raw.indexOf('?');
  return q >= 0 ? raw.slice(0, q) : raw;
}

function getPathSearch() {
  const raw = location.hash.slice(1) || '/';
  const q = raw.indexOf('?');
  return q >= 0 ? raw.slice(q) : '';
}

function hideLoading() {
  const el = document.getElementById('loading-indicator');
  if (el) el.style.display = 'none';
}

function route() {
  hideLoading();
  const path = getPath();
  const search = getPathSearch();
  const content = document.getElementById('content');
  if (currentCleanup) { currentCleanup(); currentCleanup = null; }

  for (const r of routes) {
    const m = path.match(r.pattern);
    if (m) {
      content.innerHTML = '';
      const args = m.slice(1);
      if (r.view === renderAgentDetail) args.push(search);
      currentCleanup = r.view(content, ...args) || null;
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

async function init() {
  installMobileCamBridgeShim();
  scheduleLayoutResets();
  applyMobileAppearance();
  state.set('filters', loadAgentFilters());

  const headerMenu = document.getElementById('header-menu');
  if (headerMenu) {
    headerMenu.querySelectorAll('.header-menu-item').forEach(item => {
      item.addEventListener('click', () => headerMenu.classList.add('hidden'));
    });
  }

  document.addEventListener('touchmove', e => {
    if (e.touches.length > 1) e.preventDefault();
  }, { passive: false });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') scheduleLayoutResets();
  });
  window.addEventListener('pageshow', e => {
    if (e.persisted) scheduleLayoutResets();
  });

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

  try {
    const cfg = readRelayConfig();
    if (hasRelayConfig(cfg)) {
      try { localStorage.setItem(PROFILE_KIND_KEY, 'relay'); } catch {}
    }

    if (!hasRelayConfig(cfg)) {
      api.configure({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
      _loadFromCache();
      state.set('connectionMode', 'disconnected');
      updateConnectionDot('disconnected');
      await maybeInitDirectMode(updateConnectionDot);
      if (state.get('connectionMode') === 'disconnected') {
        location.hash = '#/settings';
      }
      route();
      startReconnectLoop();
      return;
    }

    api.configure({ serverUrl: '', token: '', ...cfg });
    _loadFromCache();
    await _connectRelay(cfg);
    route();
    startReconnectLoop();
  } finally {
    notifyNativeReady();
  }
}

function startReconnectLoop() {
  setInterval(async () => {
    const c = readRelayConfig();
    if (state.get('connectionMode') === 'disconnected') {
      if (hasRelayConfig(c)) {
        await _connectRelay(c);
        return;
      }
      await maybeInitDirectMode(updateConnectionDot);
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
        const caps = await refreshHubCapabilities(api);
        state.set('hubCapabilities', caps);
      } catch {
        setHubCapabilities(HUB_CAP_FULL);
        state.set('hubCapabilities', HUB_CAP_FULL);
      }
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
    state.set('agents', filterAgentsOnEnabledHosts(agentsResp.agents || []));
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
  const cls = mode === 'direct' ? 'direct' : mode === 'relay' ? 'relay' : 'disconnected';
  dot.className = 'conn-dot ' + cls;
  dot.title = mode === 'direct' ? 'Direct' : mode === 'relay' ? 'Relay' : 'Disconnected';
  const label = document.getElementById('conn-label');
  if (label) {
    if (mode === 'direct') label.textContent = 'direct';
    else if (mode === 'relay') label.textContent = 'relay';
    else label.textContent = '';
  }
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
