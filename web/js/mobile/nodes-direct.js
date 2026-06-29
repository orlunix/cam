/**
 * Direct Nodes — Save Node and Connect are separate.
 */
import { api, state, navigate } from './app.js';
import { mountNodesMode } from '../shared/nodes-mode.js';
import { MOBILE_NODES_HTML } from './nodes-shell.js';
import {
  connectMobileDirect,
  ensureHubForSave,
  hubCreateContext,
  hubUpdateContext,
  hubDeleteContext,
  loadContextsFromHub,
  healDirectHub,
} from './settings-direct.js';
import { isDirectEnabled } from './direct-session.js';
import {
  refreshHubCapabilities,
  directHubLimitationHint,
} from '../shared/hub-capabilities.js';

const FORM_DRAFT_KEY = 'cam_mobile_nodes_add_draft';

async function loadContextsAndAdapters() {
  if (isDirectEnabled()) {
    await loadContextsFromHub(state);
    return;
  }
  const ctxResp = await api.listContexts();
  state.set('contexts', ctxResp.contexts || []);
}

async function loadAgents() {
  if (api.mode !== 'direct') return;
  try {
    const resp = await api.listAgents();
    state.set('agents', resp.agents || []);
  } catch {
    state.set('agents', []);
  }
}

/** Connect session — only for agents/sync; not required to Save Node. */
async function ensureDirectApiReady() {
  if (!isDirectEnabled()) return false;
  try {
    const mode = await connectMobileDirect();
    return mode === 'direct';
  } catch {
    return false;
  }
}

export function renderDirectNodes(container) {
  container.innerHTML = MOBILE_NODES_HTML;
  const panel = container.querySelector('#mobile-nodes');
  if (!panel) return undefined;

  const statusEl = panel.querySelector('#nodes-status');
  void (async () => {
    if (!isDirectEnabled()) {
      if (statusEl) {
        statusEl.textContent = 'Enable Direct in Settings to save nodes on this device.';
        statusEl.classList.add('is-error');
      }
      return;
    }
    try {
      await healDirectHub();
      const cfg = {
        serverUrl: localStorage.getItem('cam_server_url') || '',
        token: localStorage.getItem('cam_token') || '',
      };
      api.configure({ serverUrl: cfg.serverUrl, token: cfg.token, relayUrl: '', relayToken: '' });
      const caps = await refreshHubCapabilities(api, cfg.token);
      state.set('hubCapabilities', caps);
      await loadContextsFromHub(state);
      const n = (state.get('contexts') || []).length;
      const hint = directHubLimitationHint(caps);
      if (statusEl) {
        statusEl.textContent = n > 0
          ? `${n} node(s) saved. Use Sync Host to pull remote agents.${hint ? ' ' + hint + '.' : ''}`
          : 'Save nodes with Add Host, then Sync Host to import agents.';
        statusEl.classList.remove('is-error');
      }
    } catch {
      if (statusEl) {
        statusEl.textContent = 'Local Hub offline — Settings → Direct → Enable, then Save Node.';
        statusEl.classList.add('is-error');
      }
    }
  })();

  let cleanup = null;
  cleanup = mountNodesMode({
    panel,
    api,
    state,
    showToast: (msg, type, duration) => state.toast(msg, type, duration),
    setMode: (mode) => {
      if (mode === 'agents') navigate('/');
    },
    loadContextsAndAdapters,
    loadAgents,
    connect: async () => {
      const ok = await ensureDirectApiReady();
      return ok ? 'direct' : 'disconnected';
    },
    isReadOnly: () => false,
    mobileForm: true,
    relaxPasswordRemember: true,
    formDraftKey: FORM_DRAFT_KEY,
    ensureHubForSave,
    hubCreateContext,
    hubUpdateContext,
    hubDeleteContext,
    formLabels: { addHost: 'Save Node', saveHost: 'Save Host', saveContext: 'Save Context' },
  });

  return () => { if (cleanup) cleanup(); };
}
