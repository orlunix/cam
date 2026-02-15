import { api } from './api.js?v=10';
import { state } from './state.js?v=10';
import { renderDashboard } from './views/dashboard.js?v=10';
import { renderAgentDetail } from './views/agent-detail.js?v=10';
import { renderStartAgent } from './views/start-agent.js?v=10';
import { renderContexts } from './views/contexts.js?v=10';
import { renderSettings } from './views/settings.js?v=10';

// --- Router ---

const routes = [
  { pattern: /^\/$/,               view: renderDashboard,   nav: '/' },
  { pattern: /^\/agent\/(.+)$/,    view: renderAgentDetail,  nav: null },
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
  document.querySelectorAll('#bottom-nav .nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.route === activePath);
  });
}

export function navigate(path) {
  location.hash = path;
}

// --- Init ---

async function init() {
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

  // Connect (tries direct first, then relay)
  const mode = await api.connect();
  state.set('connectionMode', mode);
  updateConnectionDot(mode);

  if (mode !== 'disconnected') {
    await loadData();
    if (mode === 'relay') api._requestRelayEventStream();
  } else if (!cfg.token && !cfg.relayToken) {
    // Nothing configured — send user to Settings
    location.hash = '#/settings';
  }

  // Wire events
  api.onEvent(handleEvent);

  // State changes re-render current view
  state.subscribe(() => {
    updateConnectionDot(state.get('connectionMode'));
    updateToast();
  });

  // Router
  window.addEventListener('hashchange', route);
  route();

  // Periodic refresh
  setInterval(refreshAgents, 10000);

  // Service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
}

async function loadData() {
  try {
    const [agentsResp, contextsResp] = await Promise.all([
      api.listAgents({ limit: 50 }),
      api.listContexts(),
    ]);
    state.set('agents', agentsResp.agents || []);
    state.set('contexts', contextsResp.contexts || []);
  } catch (e) {
    console.error('Failed to load data:', e);
  }
}

async function refreshAgents() {
  if (state.get('connectionMode') === 'disconnected') return;
  try {
    const resp = await api.listAgents({ limit: 50 });
    state.set('agents', resp.agents || []);
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

init();
