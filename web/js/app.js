import { api } from './api.js?v=46';
import { state } from './state.js?v=46';
import { renderDashboard } from './views/dashboard.js?v=46';
import { renderAgentDetail } from './views/agent-detail.js?v=46';
import { renderStartAgent } from './views/start-agent.js?v=46';
import { renderContexts } from './views/contexts.js?v=46';
import { renderSettings } from './views/settings.js?v=46';
import { renderFileBrowser } from './views/file-browser.js?v=46';

// --- Router ---

const routes = [
  { pattern: /^\/$/,               view: renderDashboard,   nav: '/' },
  { pattern: /^\/agent\/(.+)$/,    view: renderAgentDetail,  nav: null },
  { pattern: /^\/context\/([^/]+)\/files(.*)$/,  view: renderFileBrowser,  nav: null },
  { pattern: /^\/start$/,          view: renderStartAgent,   nav: '/start' },
  { pattern: /^\/contexts$/,       view: renderContexts,     nav: '/contexts' },
  { pattern: /^\/settings$/,       view: renderSettings,     nav: '/settings' },
];

let currentCleanup = null;

function getPath() {
  return location.hash.slice(1) || '/';
}

function route() {
  const path = getPath();
  const content = document.getElementById('content');

  // Cleanup previous view
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

  // Fallback: dashboard
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

// --- Init ---

async function init() {
  // Header menu: close on nav item click (toggle + outside-close handled by inline scripts in HTML)
  const headerMenu = document.getElementById('header-menu');
  if (headerMenu) {
    headerMenu.querySelectorAll('.header-menu-item').forEach(item => {
      item.addEventListener('click', () => headerMenu.classList.add('hidden'));
    });
  }

  // Prevent pinch-zoom on mobile (viewport meta alone is unreliable on some Android WebViews)
  document.addEventListener('touchmove', e => {
    if (e.touches.length > 1) e.preventDefault();
  }, { passive: false });

  // Wire state listener and router FIRST — UI must render immediately
  api.onEvent(handleEvent);
  state.subscribe(() => {
    updateConnectionDot(state.get('connectionMode'));
    updateToast();
  });
  window.addEventListener('hashchange', route);
  route();

  // Service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  // Load config from localStorage
  const cfg = {
    serverUrl: localStorage.getItem('cam_server_url') || '',
    token: localStorage.getItem('cam_token') || '',
    relayUrl: localStorage.getItem('cam_relay_url') || '',
    relayToken: localStorage.getItem('cam_relay_token') || '',
  };

  // Auto-detect server URL if not configured (same origin for direct mode)
  if (!cfg.serverUrl) {
    cfg.serverUrl = location.origin;
  }

  api.configure(cfg);

  // Show cached data immediately so the UI is never empty on load.
  // Network fetch will replace it in the background.
  _loadFromCache();

  // Connect (tries direct first, then relay)
  await _connectWithRetry(cfg);

  // Periodic refresh — also retries connection if disconnected
  setInterval(() => {
    if (state.get('connectionMode') === 'disconnected') {
      _connectWithRetry(cfg);
    } else {
      refreshAgents();
    }
  }, 10000);
}

async function _connectWithRetry(cfg) {
  try {
    const mode = await api.connect();
    state.set('connectionMode', mode);
    updateConnectionDot(mode);

    if (mode !== 'disconnected') {
      await loadData();
      if (mode === 'relay') api._requestRelayEventStream();
    } else if (!cfg.token && !cfg.relayToken) {
      location.hash = '#/settings';
    }
  } catch (e) {
    console.error('Connect error:', e);
    state.set('connectionMode', 'disconnected');
    updateConnectionDot('disconnected');
  }
}

function _loadFromCache() {
  try {
    const DISPLAY_TTL = 1_800_000; // 30 min — stale but better than blank
    const agentsRaw = localStorage.getItem('cam_cache:/api/agents?limit=50');
    if (agentsRaw) {
      const { data, ts } = JSON.parse(agentsRaw);
      if (Date.now() - ts < DISPLAY_TTL && data?.agents) {
        state.set('agents', data.agents);
      }
    }
    const ctxRaw = localStorage.getItem('cam_cache:/api/contexts');
    if (ctxRaw) {
      const { data, ts } = JSON.parse(ctxRaw);
      if (Date.now() - ts < DISPLAY_TTL && data?.contexts) {
        state.set('contexts', data.contexts);
      }
    }
  } catch {}
}

async function loadData() {
  try {
    const [agentsResp, contextsResp, healthResp] = await Promise.all([
      api.listAgents({ limit: 50 }),
      api.listContexts(),
      api.health().catch(() => null),
    ]);
    state.set('agents', agentsResp.agents || []);
    state.set('contexts', contextsResp.contexts || []);
    if (healthResp?.adapters) state.set('adapters', healthResp.adapters);
    if (agentsResp._cached || contextsResp._cached) {
      const ago = Math.round((Date.now() - (agentsResp._cachedAt || contextsResp._cachedAt)) / 1000);
      state.toast(`Showing cached data (${ago}s ago)`, 'warning', 5000);
    }
  } catch (e) {
    console.error('Failed to load data:', e);
  }
}

let _idlePollCount = 0;
async function refreshAgents() {
  if (state.get('connectionMode') === 'disconnected') return;
  // When no agents are active, poll less frequently (every 3rd tick = ~30s)
  // to save bandwidth while still discovering new agents from CLI.
  const current = state.get('agents') || [];
  const hasActive = current.length === 0 || current.some(a => ['running', 'starting', 'pending'].includes(a.status));
  if (!hasActive) {
    _idlePollCount++;
    if (_idlePollCount % 3 !== 0) return;
  } else {
    _idlePollCount = 0;
  }
  try {
    // Only fetch running agents — completed/failed agents don't change.
    // This reduces payload from ~39 agents to ~2-3 through the relay tunnel.
    const resp = await api.listAgents({ status: 'running', limit: 50 });
    const running = resp.agents || [];
    const current = state.get('agents') || [];

    // Merge: update running agents in place, add new ones
    const map = new Map(current.map(a => [a.id, a]));
    for (const a of running) map.set(a.id, a);

    // If an agent was running in our state but isn't in the running list,
    // it likely completed — do a full refresh to catch the status change.
    const stale = current.some(a =>
      ['running', 'starting', 'pending'].includes(a.status) &&
      !running.find(r => r.id === a.id)
    );
    if (stale) {
      const full = await api.listAgents({ limit: 50 });
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
  dot.className = 'conn-dot ' + mode;
  dot.title = mode === 'direct' ? 'Direct connection'
    : mode === 'relay' ? 'Via relay'
    : 'Disconnected';
  const label = document.getElementById('conn-label');
  if (label) {
    label.textContent = mode === 'direct' ? 'connected'
      : mode === 'relay' ? 'relay'
      : '';
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

// Expose for views
export { api, state };

// Expose on window for WebView injection (Android app)
window.__camApi = api;
window.__camState = state;

init();
