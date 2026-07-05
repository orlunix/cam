/**
 * Desktop Start Agent mode (CAM-DESK-RUN-010..015).
 *
 * Reuses CamApi.startAgent (POST /api/agents). On success we refresh
 * the agent list, select the new agent, and switch to Agents mode so
 * the user can immediately see output (CAM-DESK-RUN-013). On failure
 * we leave the form contents alone, re-enable the submit, and surface
 * an actionable error inline + via toast (CAM-DESK-RUN-014).
 */

const DEFAULT_TOOLS = ['claude', 'codex', 'cursor', 'aider'];

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

export function mountStartAgentMode({ api, state, showToast, setMode, loadAgents }) {
  const panel = document.getElementById('mode-start');
  if (!panel) return;

  const form = panel.querySelector('#start-form');
  const toolSel = panel.querySelector('#start-tool');
  const ctxSel = panel.querySelector('#start-context');
  const nodeFieldsEl = panel.querySelector('#start-node-fields');
  const nodeSel = panel.querySelector('#start-node');
  const pathEl = panel.querySelector('#start-path');
  const apiSectionEl = panel.querySelector('#start-api-section');
  const apiSel = panel.querySelector('#start-api');
  const apiListBtn = panel.querySelector('#start-api-list-btn');
  const apiHintEl = panel.querySelector('#start-api-hint');
  const promptEl = panel.querySelector('#start-prompt');
  const autoconfirmEl = panel.querySelector('#start-autoconfirm');
  const autoexitEl = panel.querySelector('#start-autoexit');
  const nameEl = panel.querySelector('#start-name');
  const timeoutEl = panel.querySelector('#start-timeout');
  const retryEl = panel.querySelector('#start-retry');
  const submitBtn = panel.querySelector('#start-submit');
  const statusEl = panel.querySelector('#start-status');
  const disconnectedEl = panel.querySelector('#start-disconnected');

  // Cached api-models response (models + defaults + toolSupport).
  let apiModelsCache = null;

  function setStatus(text, cls = '') {
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function isConnected() {
    return (state.get('connectionMode') || 'disconnected') !== 'disconnected';
  }

  function applyConnectionState() {
    const connected = isConnected();
    submitBtn.disabled = !connected;
    if (disconnectedEl) {
      if (connected) disconnectedEl.setAttribute('hidden', '');
      else disconnectedEl.removeAttribute('hidden');
    }
  }

  function refreshToolOptions() {
    const adapters = state.get('adapters') || DEFAULT_TOOLS;
    const tools = adapters.filter(a => a && a !== 'generic');
    const cur = toolSel.value;
    toolSel.innerHTML = tools.map(
      t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`,
    ).join('');
    if (cur && tools.includes(cur)) toolSel.value = cur;
  }

  function refreshContextOptions() {
    const contexts = state.get('contexts') || [];
    const cur = ctxSel.value;
    ctxSel.innerHTML =
      '<option value="">Select context…</option>' +
      contexts.map(c =>
        `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}${c.path ? ' — ' + escapeHtml(c.path) : ''}</option>`,
      ).join('');
    if (cur) {
      const stillPresent = (state.get('contexts') || []).some(c => c.name === cur);
      if (stillPresent) ctxSel.value = cur;
    }
  }

  /** Build the node list from the unique machines backing contexts.
   *  Keys: "local" or "user@host:port" (matches hostKeyForMachine +
   *  the hub's _resolveStartTarget). The local anchor context (if any)
   *  contributes a "local" option. */
  function refreshNodeOptions() {
    const contexts = state.get('contexts') || [];
    const seen = new Map(); // key -> label
    for (const c of contexts) {
      const m = (c && c.machine) || {};
      const host = m.host || 'local';
      const isSSH = !!(m.type === 'ssh' || (host && host !== 'local'));
      if (!isSSH) { seen.set('local', 'local'); continue; }
      const port = m.port || 22;
      const key = `${m.user || ''}@${host}:${port}`;
      seen.set(key, key);
    }
    const cur = nodeSel.value;
    nodeSel.innerHTML = [...seen.entries()].map(
      ([key, label]) => `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`,
    ).join('');
    if (cur && seen.has(cur)) nodeSel.value = cur;
    else if (seen.size === 1) nodeSel.value = seen.keys().next().value;
    // Default path to /home/<user> for the selected node.
    syncDefaultPath();
  }

  /** Pre-fill #start-path with /home/<user> when empty (matches the
   *  Add Context default). Local → /home/$USER or cwd fallback. */
  function syncDefaultPath() {
    const key = nodeSel.value;
    if (!key || key === 'local') {
      if (!pathEl.value) pathEl.value = '';
      return;
    }
    const m = /^([^@]+)@/.exec(key);
    const user = m ? m[1] : '';
    if (!pathEl.value && user) pathEl.value = `/home/${user}`;
  }

  /** Show node+path fields when no context is selected. */
  function toggleNodeFields() {
    const hasCtx = !!ctxSel.value;
    if (nodeFieldsEl) nodeFieldsEl.hidden = hasCtx;
    if (!hasCtx) syncDefaultPath();
  }

  /** Grey the API section for tools without --api support (3.3). The
   *  select stays disabled until models are loaded (List models). */
  function applyApiSupport() {
    const tool = toolSel.value;
    const support = apiModelsCache && apiModelsCache.toolSupport || {};
    const supported = support[tool] !== false;
    const hasModels = !!(apiModelsCache && Array.isArray(apiModelsCache.models) && apiModelsCache.models.length);
    if (apiSectionEl) apiSectionEl.classList.toggle('is-disabled', !supported);
    if (apiListBtn) apiListBtn.disabled = !supported;
    if (apiSel) apiSel.disabled = !supported || !hasModels;
    if (apiHintEl) {
      apiHintEl.textContent = supported
        ? 'Click "List models" to populate; the per-tool default is marked.'
        : `Tool "${tool}" does not support --api selection (uses login/OAuth).`;
    }
  }

  /** Populate #start-api from cached models + mark the per-tool default. */
  function renderApiOptions(tool) {
    const models = (apiModelsCache && apiModelsCache.models) || [];
    const defaults = (apiModelsCache && apiModelsCache.defaults) || [];
    const enabled = models.filter(r => r && r.enabled !== false);
    const def = defaults.find(d => d && d.tool === tool && d.api && d.mode === 'api');
    const defName = def ? def.api : '';
    apiSel.innerHTML =
      '<option value="">(use login default)</option>' +
      enabled.map(r => {
        const isDef = defName && r.name === defName;
        const label = isDef ? `${r.name} (default)` : r.name;
        return `<option value="${escapeHtml(r.name)}">${escapeHtml(label)}</option>`;
      }).join('');
    if (defName) apiSel.value = defName;
  }

  async function listApiModels() {
    if (!apiListBtn) return;
    const orig = apiListBtn.textContent;
    apiListBtn.disabled = true;
    apiListBtn.textContent = 'Listing…';
    try {
      apiModelsCache = await api.getApiModels();
      renderApiOptions(toolSel.value);
      applyApiSupport();
      setStatus('API models loaded.', 'is-ok');
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(`List models failed: ${msg}`, 'is-error');
      showToast(`List models failed: ${msg}`, 'error', 5000);
    } finally {
      apiListBtn.textContent = orig;
      // Re-apply support state (may have been overridden by disabled tool).
      applyApiSupport();
    }
  }

  function readForm() {
    const body = {
      tool: toolSel.value,
      prompt: (promptEl.value || ' '),
      auto_confirm: autoconfirmEl.checked,
      auto_exit: autoexitEl.checked,
      retry: parseInt(retryEl.value, 10) || 0,
    };
    if (ctxSel.value) {
      body.context = ctxSel.value;
    } else {
      body.node = nodeSel.value;
      body.path = pathEl.value.trim();
    }
    // API model: only when the tool supports it and a model is picked.
    const support = apiModelsCache && apiModelsCache.toolSupport || {};
    if (apiSel && apiSel.value && support[body.tool] !== false) {
      body.api = apiSel.value;
    }
    const t = (timeoutEl.value || '').trim();
    if (t) body.timeout = t;
    const n = (nameEl.value || '').trim();
    if (n) body.name = n;
    return body;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!isConnected()) {
      setStatus('Connect to a CAM endpoint in Settings first.', 'is-error');
      return;
    }
    // Require EITHER a context OR (node + path).
    if (!ctxSel.value) {
      const node = nodeSel.value;
      const path = pathEl.value.trim();
      if (!node) { setStatus('Pick a context, or choose a node.', 'is-error'); return; }
      if (!path) { setStatus('Pick a context, or provide a path.', 'is-error'); return; }
    }
    const body = readForm();
    submitBtn.disabled = true;
    const origLabel = submitBtn.textContent;
    submitBtn.textContent = 'Starting…';
    setStatus('Starting…');
    let agent = null;
    try {
      const res = await api.startAgent(body);
      // Hub returns { agent, agentId }; older Relay may return the agent directly.
      agent = res && res.agent ? res.agent : res;
    } catch (err) {
      // CAM-DESK-RUN-014: keep form contents intact; only re-enable.
      submitBtn.disabled = false;
      submitBtn.textContent = origLabel;
      const msg = err?.message || String(err);
      setStatus(`Start failed: ${msg}`, 'is-error');
      showToast(`Start failed: ${msg}`, 'error', 5000);
      return;
    }
    setStatus('Agent started.', 'is-ok');
    showToast('Agent started', 'success');
    try { await loadAgents(); } catch {}
    const newId = agent && (agent.id || agent.agentId);
    if (newId) state.set('selectedAgentId', newId);
    submitBtn.disabled = false;
    submitBtn.textContent = origLabel;
    // CAM-DESK-RUN-013: jump back to Agents so user sees output immediately.
    setMode('agents');
  });

  // Wire context/tool/api interactions.
  ctxSel.addEventListener('change', toggleNodeFields);
  nodeSel.addEventListener('change', syncDefaultPath);
  toolSel.addEventListener('change', applyApiSupport);
  if (apiListBtn) apiListBtn.addEventListener('click', listApiModels);

  // Refresh option lists whenever Start becomes the active mode (so
  // newly-added contexts or detected adapters show up without a full
  // page reload).
  let prevMode = state.get('mode');
  let prevConn = state.get('connectionMode');
  let prevCtxs = state.get('contexts');
  let prevAdapters = state.get('adapters');
  refreshToolOptions();
  refreshContextOptions();
  refreshNodeOptions();
  toggleNodeFields();
  applyApiSupport();
  applyConnectionState();
  state.subscribe(() => {
    const m = state.get('mode');
    const c = state.get('connectionMode');
    const ctxs = state.get('contexts');
    const adapters = state.get('adapters');
    if (m !== prevMode) {
      prevMode = m;
      if (m === 'start') {
        refreshToolOptions();
        refreshContextOptions();
        refreshNodeOptions();
        toggleNodeFields();
        applyApiSupport();
        applyConnectionState();
        setStatus('');
      }
    }
    if (c !== prevConn) { prevConn = c; applyConnectionState(); }
    if (ctxs !== prevCtxs) { prevCtxs = ctxs; refreshContextOptions(); refreshNodeOptions(); toggleNodeFields(); }
    if (adapters !== prevAdapters) { prevAdapters = adapters; refreshToolOptions(); applyApiSupport(); }
  });
}
