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
import { mountAgentConsole } from './agent-console.js?v=0.64.1';
import { mountSettingsMode } from './settings-mode.js?v=0.64.0';
import { mountStartAgentMode } from './start-agent-mode.js?v=0.64.0';
import { mountNodesMode } from './nodes-mode.js?v=0.64.0';
import { mountSkillsMode } from './skills-mode.js?v=0.64.0';
import { mountTodosMode } from './todos-mode.js?v=0.64.0';

const POLL_INTERVAL_MS = 5000;
const AGENT_SNAPSHOT_SYNC_EVERY_TICKS = 6; // ~30s at POLL_INTERVAL_MS.
const PROFILE_KIND_KEY = 'cam_profile_kind';
// `start`/`nodes` are real left-nav workspace modes like
// `agents`/`settings`. `nodes` shows hub-provided controllers/nodes
// (CAM-DESK-NODEUI-010..017) and is not a connection mode.
const MODES = ['agents', 'settings', 'start', 'nodes', 'skills', 'todos'];
const DEFAULT_MODE = 'agents';

function readConfig() {
  let profileKind = '';
  try {
    const stored = localStorage.getItem(PROFILE_KIND_KEY);
    if (stored === 'direct' || stored === 'relay') profileKind = stored;
  } catch {}
  return {
    serverUrl: localStorage.getItem('cam_server_url') || '',
    token: localStorage.getItem('cam_token') || '',
    relayUrl: localStorage.getItem('cam_relay_url') || '',
    relayToken: localStorage.getItem('cam_relay_token') || '',
    profileKind,
  };
}

function saveConfig(cfg) {
  localStorage.setItem('cam_server_url', cfg.serverUrl || '');
  localStorage.setItem('cam_token', cfg.token || '');
  localStorage.setItem('cam_relay_url', cfg.relayUrl || '');
  localStorage.setItem('cam_relay_token', cfg.relayToken || '');
}

function resolveConnectionConfig(cfg) {
  let kind = cfg.profileKind;
  if (kind !== 'direct' && kind !== 'relay') {
    // Backward-compatible inference for profiles saved before
    // cam_profile_kind existed. Relay wins when both sets are present
    // because Settings also opens the Relay tab in that case.
    if (cfg.relayUrl && cfg.relayToken) kind = 'relay';
    else if (cfg.serverUrl && cfg.token) kind = 'direct';
    else kind = 'unset';
  }

  if (kind === 'relay') {
    return {
      profileKind: 'relay',
      serverUrl: '',
      token: cfg.token || '',
      relayUrl: cfg.relayUrl || '',
      relayToken: cfg.relayToken || '',
    };
  }

  if (kind === 'direct') {
    return {
      profileKind: 'direct',
      serverUrl: cfg.serverUrl || '',
      token: cfg.token || '',
      relayUrl: '',
      relayToken: '',
    };
  }

  return {
    profileKind: 'unset',
    serverUrl: '',
    token: '',
    relayUrl: '',
    relayToken: '',
  };
}

function hasConnectionConfig(cfg) {
  // Relay's CAM API token is source/profile-managed and injected by the
  // relay on /api/* forwarding. The Desktop client only needs the relay
  // endpoint and relay shared secret.
  if (cfg.profileKind === 'relay') return !!(cfg.relayUrl && cfg.relayToken);
  if (cfg.profileKind === 'direct') return !!(cfg.serverUrl && cfg.token);
  return false;
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

async function loadSelectedAgent(agentId) {
  if (!agentId) return;
  try {
    const resp = await api.getAgent(agentId);
    const nextAgent = resp && (resp.agent || resp);
    if (!nextAgent || !nextAgent.id) return;
    const agents = Array.isArray(state.get('agents')) ? state.get('agents') : [];
    const found = agents.some((a) => a && a.id === nextAgent.id);
    state.set('agents', found
      ? agents.map((a) => (a && a.id === nextAgent.id ? { ...a, ...nextAgent } : a))
      : [nextAgent, ...agents]);
  } catch (e) {
    if (api.mode !== 'disconnected') console.warn('getAgent failed:', e);
  }
}

function shouldPrioritizeSelectedRelayAgent() {
  return api.mode === 'relay' &&
    state.get('mode') === 'agents' &&
    !!state.get('selectedAgentId');
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

let snapshotSyncInFlight = false;

function endpointKeyForContext(ctx) {
  const m = (ctx && ctx.machine) || {};
  const type = m.type || 'local';
  if (type !== 'ssh') return '';
  const host = String(m.host || '').trim();
  const user = String(m.user || '').trim();
  const port = Number(m.port || 22) || 22;
  if (!host || !user) return '';
  return `${type}|${user}|${host}|${port}`;
}

function representativeSyncContexts(contexts) {
  const reps = new Map();
  for (const ctx of Array.isArray(contexts) ? contexts : []) {
    const m = (ctx && ctx.machine) || {};
    if (m.type !== 'ssh') continue;
    const auth = m.auth_method || (m.key_file ? 'key' : 'agent');
    // Password one-shot contexts cannot be refreshed silently. Remembered
    // password contexts are fine because the Hub decrypts in main only.
    if (auth === 'password' && !m.credential_saved) continue;
    const key = endpointKeyForContext(ctx);
    if (key && !reps.has(key)) reps.set(key, ctx);
  }
  return [...reps.values()];
}

async function syncAgentSnapshotsFromContexts(reason = 'poll') {
  if (api.mode !== 'direct') return;
  if (snapshotSyncInFlight) return;
  const contexts = representativeSyncContexts(state.get('contexts') || []);
  if (contexts.length === 0) return;

  snapshotSyncInFlight = true;
  try {
    let attempted = 0;
    for (const ctx of contexts) {
      try {
        await api.syncContext(ctx.name);
        attempted++;
      } catch (e) {
        console.warn(`background sync ${ctx.name} failed:`, e);
      }
    }
    if (attempted > 0) await loadAgents();
  } catch (e) {
    console.warn(`background agent snapshot sync failed (${reason}):`, e);
  } finally {
    snapshotSyncInFlight = false;
  }
}

async function connect() {
  const cfg = resolveConnectionConfig(readConfig());
  if (!hasConnectionConfig(cfg)) {
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
    if (mode === 'direct') void syncAgentSnapshotsFromContexts('connect');
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

// Modes that survive a page reload.
const PERSISTENT_MODES = new Set(['agents', 'settings', 'start', 'nodes', 'skills', 'todos']);

function setMode(next) {
  if (!MODES.includes(next)) next = DEFAULT_MODE;
  const cur = state.get('mode');
  if (cur === next) return;
  state.set('mode', next);
  try {
    if (PERSISTENT_MODES.has(next)) {
      localStorage.setItem('cam_desktop_mode', next);
    }
  } catch {}
}

function applyModeToDom(mode) {
  // Mode nav button pressed state.
  document.querySelectorAll('.mode-nav-btn[data-mode]').forEach(btn => {
    const active = btn.dataset.mode === mode;
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  // Main mode panels — `agents` (default) shows the output/composer;
  // `start`/`nodes`/`settings` swap that out without unmounting it, so
  // composer textarea, scroll position, and output-mode state are preserved.
  document.querySelectorAll('.mode-panel').forEach(panel => {
    const active = panel.dataset.mode === mode;
    if (active) panel.removeAttribute('hidden');
    else panel.setAttribute('hidden', '');
  });
  // Sidebar contextual content. The agent list stays visible only in
  // Agents mode.
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

/* ────────── Direct auto-start ──────────
 *
 * CAM-DESK-DIRECT-010..013: Direct is the default mode and runs the
 * embedded CAM Hub from Electron main with no host runtime. On a
 * cold launch with no saved profile, autoStartConnection() starts
 * the Hub via CamBridge.directHub.start(), persists the generated
 * loopback URL + token into the shared Direct localStorage keys,
 * then calls connect(). If a saved profile exists, that profile is
 * honored. A saved Direct profile that fails to connect against a
 * loopback URL gets one auto-restart attempt (the Hub may have been
 * stopped by a previous Settings → Stop). */

function bridgeDirectHubLocal() {
  const b = typeof window !== 'undefined' ? window.CamBridge : null;
  return (b && b.directHub) || null;
}

function isLoopbackUrl(u) {
  try {
    const h = new URL(u).hostname;
    return h === '127.0.0.1' || h === 'localhost' || h === '::1';
  } catch { return false; }
}

async function startEmbeddedHubAndPersist() {
  const bridge = bridgeDirectHubLocal();
  if (!bridge) return false;
  let res;
  try { res = await bridge.start(); }
  catch (e) { console.warn('directHub.start failed:', e); return false; }
  if (!res || res.ok !== true || !res.apiUrl || !res.apiToken) return false;
  saveConfig({
    serverUrl:  res.apiUrl,
    token:      res.apiToken,
    relayUrl:   '',
    relayToken: '',
  });
  try { localStorage.setItem('cam_profile_kind', 'direct'); } catch {}
  return true;
}

async function autoStartConnection() {
  const raw = readConfig();
  const cfg = resolveConnectionConfig(raw);

  // Relay profile present — honor it; do NOT auto-start Direct on top.
  if (cfg.profileKind === 'relay' && hasConnectionConfig(cfg)) {
    await connect();
    return;
  }

  // Direct profile present — try it, with one auto-restart attempt if
  // the Hub turns out to be stopped and we own the bridge + the URL
  // is loopback. Guards against infinite loops by trying start at
  // most once per launch.
  if (cfg.profileKind === 'direct' && hasConnectionConfig(cfg)) {
    const mode = await connect();
    if (mode !== 'disconnected') return;
    if (bridgeDirectHubLocal() && isLoopbackUrl(cfg.serverUrl)) {
      if (await startEmbeddedHubAndPersist()) {
        await connect();
      }
    }
    return;
  }

  // No profile at all. If we have the Electron bridge, start the
  // embedded Hub by default. Otherwise (browser/dev mode) drop to
  // Settings so the user can configure Relay manually.
  if (bridgeDirectHubLocal()) {
    if (await startEmbeddedHubAndPersist()) {
      await connect();
      return;
    }
    // Embedded start failed (e.g. all loopback ports taken). Fall
    // through to Settings so the user can see the Direct panel and
    // act, rather than spinning silently.
  }
  state.set('connectionMode', 'disconnected');
  updateConnectionDot('disconnected');
  setMode('settings');
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

  mountShell({ api, state, showToast, connect: autoStartConnection });
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
  mountNodesMode({
    api, state, showToast, setMode,
    loadContextsAndAdapters, loadAgents,
    connect: autoStartConnection,
  });
  mountSkillsMode({ api, state, showToast });
  mountTodosMode({ api, state, showToast });

  // First connection attempt.
  //
  // CAM-DESK-DIRECT-010..013: Direct mode (embedded CAM Hub) is the
  // default and must start automatically when no Relay profile and
  // no Direct profile are present. The user should never have to
  // open Settings just to get a working app — Direct on an empty
  // embedded Hub with contexts=[] and agents=[] is a valid running
  // state.
  await autoStartConnection();

  // Agent list refreshes every POLL_INTERVAL_MS. Contexts / adapters
  // change much less frequently (only when the operator creates a
  // context or restarts the server), so refresh them on a slower
  // cadence to keep the dropdowns in Start mode fresh without spamming
  // the server. In Relay mode, when an agent is selected, prioritize the
  // visible detail pane: avoid expensive global list/context refreshes that
  // can queue behind the same relay/source connection and make output look
  // stuck. Direct mode keeps the existing full refresh behavior.
  let pollTick = 0;
  setInterval(async () => {
    if (state.get('connectionMode') === 'disconnected') {
      const cfg = resolveConnectionConfig(readConfig());
      if (hasConnectionConfig(cfg)) await connect();
      return;
    }

    pollTick = (pollTick + 1) % AGENT_SNAPSHOT_SYNC_EVERY_TICKS;

    if (shouldPrioritizeSelectedRelayAgent()) {
      await loadSelectedAgent(state.get('selectedAgentId'));
      return;
    }

    if (pollTick === 0) {
      // Every ~30s: refresh contexts/adapters, then refresh one
      // representative SSH context per endpoint so agent updated_at
      // stays current without duplicating sibling contexts.
      await Promise.all([loadAgents(), loadContextsAndAdapters()]);
      void syncAgentSnapshotsFromContexts('poll');
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
