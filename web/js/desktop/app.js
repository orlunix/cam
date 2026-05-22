/**
 * CAM Desktop — entry module.
 *
 * Wires the WebUI CamApi and AppState against the desktop shell.
 * Phase 1 surface: workspace nav (Agents / Settings / placeholders),
 * agent list + selected-agent output + composer + quick keys, and an
 * in-pane settings mode that replaces the agent console. No router,
 * no terminal, no server-lifecycle UI.
 */

import { api } from '../api.js?v=0.64.0';
import { state } from '../state.js?v=0.64.0';
import { mountShell } from './shell.js?v=0.64.0';
import { mountAgentConsole } from './agent-console.js?v=0.64.0';
import { mountSettingsMode } from './settings-mode.js?v=0.64.0';
import { mountStartAgentMode } from './start-agent-mode.js?v=0.64.0';
import { mountAgentEditMode } from './agent-edit-mode.js?v=0.64.0';

const POLL_INTERVAL_MS = 5000;
// `edit` is a transient subview reachable only from the agent header;
// `start` is a real left-nav workspace mode like `agents`/`settings`.
const MODES = ['agents', 'settings', 'start', 'edit'];
const DEFAULT_MODE = 'agents';

function readConfig() {
  return {
    serverUrl: localStorage.getItem('cam_server_url') || '',
    token: localStorage.getItem('cam_token') || '',
    relayUrl: localStorage.getItem('cam_relay_url') || '',
    relayToken: localStorage.getItem('cam_relay_token') || '',
  };
}

function saveConfig(cfg) {
  localStorage.setItem('cam_server_url', cfg.serverUrl || '');
  localStorage.setItem('cam_token', cfg.token || '');
  localStorage.setItem('cam_relay_url', cfg.relayUrl || '');
  localStorage.setItem('cam_relay_token', cfg.relayToken || '');
}

function updateConnectionDot(mode) {
  const dot = document.getElementById('conn-status');
  const label = document.getElementById('conn-label');
  if (dot) {
    dot.className = 'conn-dot ' + mode;
    dot.title = mode;
  }
  if (label) {
    label.textContent =
      mode === 'direct' ? 'connected (direct)' :
      mode === 'relay' ? 'connected (relay)' :
      mode === 'checking' ? 'checking…' :
      'disconnected';
  }
}

function renderToast() {
  const toast = state.get('toast');
  let el = document.getElementById('toast');
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

function showToast(message, type = 'info', duration = 3000) {
  state.toast(message, type, duration);
}

async function loadAgents() {
  try {
    const resp = await api.listAgents({ limit: 100 });
    state.set('agents', resp.agents || []);
  } catch (e) {
    if (api.mode !== 'disconnected') console.warn('listAgents failed:', e);
  }
}

/**
 * Load contexts + health (adapters) into AppState. Mirrors the mobile
 * web/js/app.js loadData() pattern so the desktop Start mode has a
 * populated tool/context dropdown. Failures are non-fatal — agent list
 * and output keep working even if /api/contexts or /api/system/health
 * 5xxes.
 */
async function loadContextsAndAdapters() {
  try {
    const [ctxResp, healthResp] = await Promise.all([
      api.listContexts().catch(() => null),
      api.health().catch(() => null),
    ]);
    if (ctxResp && Array.isArray(ctxResp.contexts)) {
      state.set('contexts', ctxResp.contexts);
    }
    if (healthResp && Array.isArray(healthResp.adapters)) {
      state.set('adapters', healthResp.adapters);
    }
  } catch (e) {
    if (api.mode !== 'disconnected') {
      console.warn('loadContextsAndAdapters failed:', e);
    }
  }
}

async function connect() {
  const cfg = readConfig();
  if (!cfg.serverUrl && !cfg.relayUrl) {
    state.set('connectionMode', 'disconnected');
    updateConnectionDot('disconnected');
    return 'disconnected';
  }
  api.configure(cfg);
  state.set('connectionMode', 'checking');
  updateConnectionDot('checking');
  let mode = 'disconnected';
  try {
    mode = await api.connect();
  } catch (e) {
    mode = 'disconnected';
    showToast(`Connection error: ${e.message}`, 'error', 5000);
  }
  state.set('connectionMode', mode);
  updateConnectionDot(mode);
  if (mode !== 'disconnected') {
    await Promise.all([loadAgents(), loadContextsAndAdapters()]);
    if (mode === 'relay') {
      try { api._requestRelayEventStream(); } catch {}
    }
  }
  return mode;
}

function handleEvent(event) {
  if (!event || !event.type) return;
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

/* ────────── Mode host ────────── */

// Modes that survive a page reload. `edit` is transient — it lives on
// state but never lands in localStorage, so a refresh while editing
// drops back to Agents with the agent list visible and a nav-button
// highlighted, rather than stranding the user on an Edit form with no
// nav indicator.
const PERSISTENT_MODES = new Set(['agents', 'settings', 'start']);

function setMode(next) {
  if (!MODES.includes(next)) next = DEFAULT_MODE;
  const cur = state.get('mode');
  if (cur === next) return;
  state.set('mode', next);
  try {
    if (PERSISTENT_MODES.has(next)) {
      localStorage.setItem('cam_desktop_mode', next);
    }
    // Else: leave the previous persisted mode in place — that mode is
    // where the user "came from" and is where reload should land.
  } catch {}
}

function applyModeToDom(mode) {
  // Mode nav button pressed state. `edit` is not in the nav — only
  // reachable from the agent header — so no button highlights for it.
  document.querySelectorAll('.mode-nav-btn[data-mode]').forEach(btn => {
    const active = btn.dataset.mode === mode;
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  // Main mode panels — `agents` (default) shows the output/composer;
  // `start` and `edit` swap that out without unmounting it, so the
  // composer textarea, scroll position, and output-mode state are
  // preserved (CAM-DESK-EDIT-014).
  document.querySelectorAll('.mode-panel').forEach(panel => {
    const active = panel.dataset.mode === mode;
    if (active) panel.removeAttribute('hidden');
    else panel.setAttribute('hidden', '');
  });
  // Sidebar contextual content. The agent list stays visible only in
  // Agents mode — switching to Edit removes the list so a user cannot
  // change selection mid-edit (CAM-DESK-EDIT-013 keeps the edit target
  // single-valued via state.editAgentId).
  const agentsSide = document.getElementById('sidebar-agents-content');
  const settingsSide = document.getElementById('sidebar-settings-content');
  if (agentsSide) agentsSide.toggleAttribute('hidden', mode !== 'agents');
  if (settingsSide) settingsSide.toggleAttribute('hidden', mode !== 'settings');
}

function wireModeNav() {
  document.querySelectorAll('.mode-nav-btn[data-mode]').forEach(btn => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
  });
}

/* ────────── Init ────────── */

async function init() {
  // Restore last mode (best effort). Only PERSISTENT_MODES are allowed
  // on cold start — a stale `edit` value from any older client (or a
  // hand-edited localStorage) is coerced to the default so the user
  // never lands on the Edit form without a nav highlight or sidebar list.
  let initialMode = DEFAULT_MODE;
  try {
    const saved = localStorage.getItem('cam_desktop_mode');
    if (saved && PERSISTENT_MODES.has(saved)) initialMode = saved;
    else if (saved) {
      // Stale value left over from an earlier build that persisted edit.
      localStorage.removeItem('cam_desktop_mode');
    }
  } catch {}
  state.set('mode', initialMode);

  // State -> DOM
  let prevMode = state.get('mode');
  state.subscribe(() => {
    renderToast();
    const m = state.get('mode');
    if (m !== prevMode) {
      prevMode = m;
      applyModeToDom(m);
    }
  });
  applyModeToDom(initialMode);

  wireModeNav();
  api.onEvent(handleEvent);

  mountShell({ api, state, showToast });
  mountAgentConsole({ api, state, showToast, setMode });
  mountSettingsMode({
    api,
    state,
    showToast,
    readConfig,
    saveConfig,
    connect,
  });
  mountStartAgentMode({ api, state, showToast, setMode, loadAgents });
  mountAgentEditMode({ api, state, showToast, setMode, loadAgents });

  // First connection attempt — if nothing configured, jump to Settings.
  const initialCfg = readConfig();
  if (!initialCfg.serverUrl && !initialCfg.relayUrl) {
    state.set('connectionMode', 'disconnected');
    updateConnectionDot('disconnected');
    setMode('settings');
  } else {
    await connect();
  }

  // Agent list refreshes every POLL_INTERVAL_MS. Contexts / adapters
  // change much less frequently (only when the operator creates a
  // context or restarts the server), so refresh them on a slower
  // cadence to keep the dropdowns in Start mode fresh without spamming
  // the server.
  let pollTick = 0;
  setInterval(async () => {
    if (state.get('connectionMode') === 'disconnected') {
      const cfg = readConfig();
      if (cfg.serverUrl || cfg.relayUrl) await connect();
      return;
    }
    pollTick = (pollTick + 1) % 6;
    if (pollTick === 0) {
      // Every 6th tick (~30s @ 5s POLL_INTERVAL): refresh contexts and
      // adapters in addition to the agent list.
      await Promise.all([loadAgents(), loadContextsAndAdapters()]);
    } else {
      await loadAgents();
    }
  }, POLL_INTERVAL_MS);
}

/* Debug handles — guarded behind a developer opt-in to avoid shipping
   global references in default builds. To enable in DevTools:
     localStorage.setItem('cam_desktop_debug', '1') && location.reload()
*/
try {
  if (localStorage.getItem('cam_desktop_debug') === '1') {
    window.__camApi = api;
    window.__camState = state;
  }
} catch {}

init();
