/**
 * Desktop Start Agent mode (CAM-DESK-RUN-010..015).
 *
 * Reuses CamApi.startAgent (POST /api/agents). On success we refresh
 * the agent list, select the new agent, and switch to Agents mode so
 * the user can immediately see output (CAM-DESK-RUN-013). On failure
 * we leave the form contents alone, re-enable the submit, and surface
 * an actionable error inline + via toast (CAM-DESK-RUN-014).
 *
 * Context is OPTIONAL: a "(none — start without context)" entry is
 * the first option in the Context dropdown. When selected, a Path
 * field appears (no Node select — the agent runs on the local node)
 * and the request sends node="local" + path inline (no context record
 * is created, per A2).
 *
 * The API picker (Advanced (API) section) lists custom LLM API profiles
 * from the SELECTED context/node via GET /api/api-models?context=… or
 * ?node=…. Each profile is a clickable row; clicking fills --api. The
 * section is hidden for tools without --api support (3.3). "Use
 * official/login API" sends --no-default-api; "API token" sends
 * --api-token.
 */

const DEFAULT_TOOLS = ['claude', 'codex', 'cursor', 'aider'];

const NONE_CONTEXT_VALUE = '__none__';

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
  const pathEl = panel.querySelector('#start-path');
  const apiSectionEl = panel.querySelector('#start-api-section');
  const apiInputEl = panel.querySelector('#start-api');
  const apiListBtn = panel.querySelector('#start-api-list-btn');
  const apiListEl = panel.querySelector('#start-api-list');
  const apiStatusEl = panel.querySelector('#start-api-status');
  const apiHintEl = panel.querySelector('#start-api-hint');
  const noDefaultApiEl = panel.querySelector('#start-no-default-api');
  const apiTokenEl = panel.querySelector('#start-api-token');
  const promptEl = panel.querySelector('#start-prompt');
  const autoconfirmEl = panel.querySelector('#start-autoconfirm');
  const autoexitEl = panel.querySelector('#start-autoexit');
  const nameEl = panel.querySelector('#start-name');
  const timeoutEl = panel.querySelector('#start-timeout');
  const retryEl = panel.querySelector('#start-retry');
  const submitBtn = panel.querySelector('#start-submit');
  const statusEl = panel.querySelector('#start-status');
  const disconnectedEl = panel.querySelector('#start-disconnected');

  // Cached api-models response (models + defaults + toolSupport + source).
  let apiModelsCache = null;
  // Currently selected API profile name (from clicking a list row).
  let selectedApi = '';

  // Static fallback for tool --api support, used before models are
  // fetched so unsupported tools (cursor/aider) hide the section from
  // the start rather than flashing visible-then-hidden. Mirrors the
  // hub's API_TOOL_SUPPORT map.
  const TOOL_API_SUPPORT_STATIC = { claude: true, codex: true, cursor: false, aider: false };

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
    // First entry: "(none — start without context)" → runs on the local
    // node with an inline path, no context record (A2).
    const none = `<option value="${NONE_CONTEXT_VALUE}">(none — start without context)</option>`;
    ctxSel.innerHTML = none + contexts.map(c =>
      `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}${c.path ? ' — ' + escapeHtml(c.path) : ''}</option>`,
    ).join('');
    if (cur) {
      const stillPresent = cur === NONE_CONTEXT_VALUE ||
        (state.get('contexts') || []).some(c => c.name === cur);
      if (stillPresent) ctxSel.value = cur;
    }
  }

  /** Default Path to /home/<user> for the selected context's machine,
   *  or /home/$USER (best-effort) for the local/none case. Only
   *  pre-fills when the field is empty. */
  function syncDefaultPath() {
    const val = ctxSel.value;
    if (val && val !== NONE_CONTEXT_VALUE) {
      // A real context is selected — Path field is hidden, no need.
      if (!pathEl.value) pathEl.value = '';
      return;
    }
    // none / local: default to /home/<user> if we can infer one.
    if (pathEl.value) return;
    const ctxs = state.get('contexts') || [];
    const localCtx = ctxs.find(c => {
      const m = (c && c.machine) || {};
      return (m.type || 'local') !== 'ssh';
    });
    const user = (localCtx && localCtx.machine && localCtx.machine.user) ||
      (typeof window !== 'undefined' && window.camLocalUser) || '';
    pathEl.value = user ? `/home/${user}` : '/home/hren';
  }

  /** Show the Path field when no real context is selected (none or
   *  empty). A real context hides it (the context already carries a
   *  path). */
  function toggleNodeFields() {
    const val = ctxSel.value;
    const hasCtx = val && val !== NONE_CONTEXT_VALUE;
    if (nodeFieldsEl) nodeFieldsEl.hidden = !!hasCtx;
    if (!hasCtx) syncDefaultPath();
  }

  /** The node key to pass to the hub for the API picker / start. For a
   *  real context, we pass nothing (the hub resolves the context). For
   *  none/empty, we pass "local". */
  function currentNodeKey() {
    const val = ctxSel.value;
    if (val && val !== NONE_CONTEXT_VALUE) return '';
    return 'local';
  }

  /** Hide the API section for tools without --api support (3.3). The
   *  list + status stay empty until "List custom LLM APIs" is clicked. */
  function applyApiSupport() {
    const tool = toolSel.value;
    const support = (apiModelsCache && apiModelsCache.toolSupport) || TOOL_API_SUPPORT_STATIC;
    const supported = support[tool] !== false;
    if (apiSectionEl) apiSectionEl.hidden = !supported;
    if (apiListBtn) apiListBtn.disabled = !supported;
    if (apiInputEl) apiInputEl.disabled = !supported;
    if (apiHintEl && supported) {
      apiHintEl.innerHTML = 'Lists profiles on the selected context/node via <code>camc --json api list --all</code>. Click a result to fill <code>--api</code>.';
    } else if (apiHintEl) {
      apiHintEl.textContent = `Tool "${tool}" does not support --api selection.`;
    }
  }

  /** Build hints for getApiModels — context name OR node key. */
  function apiModelsHints() {
    const ctx = ctxSel.value;
    if (ctx && ctx !== NONE_CONTEXT_VALUE) return { context: ctx };
    return { node: 'local' };
  }

  /** Render the cached models as a clickable list of rows. Each row:
   *    name (left) · model path (middle, muted) · provider·status (right)
   *  The per-tool default row gets a `default` badge. Clicking a row
   *  sets selectedApi and fills the input. */
  function renderApiList(tool) {
    const models = (apiModelsCache && apiModelsCache.models) || [];
    const defaults = (apiModelsCache && apiModelsCache.defaults) || [];
    const def = defaults.find(d => d && d.tool === tool && d.api && d.mode === 'api');
    const defName = def ? def.api : '';
    if (!models.length) {
      if (apiListEl) apiListEl.hidden = true;
      return;
    }
    if (!apiListEl) return;
    apiListEl.hidden = false;
    apiListEl.innerHTML = models.map(r => {
      const isDef = defName && r.name === defName;
      const sel = selectedApi && r.name === selectedApi;
      const status = r.enabled === false ? 'disabled' : 'enabled';
      const cls = `api-row${sel ? ' is-selected' : ''}${r.enabled === false ? ' is-disabled' : ''}`;
      const badge = isDef ? '<span class="api-default-badge">default</span>' : '';
      return `<li class="${cls}" data-name="${escapeHtml(r.name)}" role="button" tabindex="0">
        <span class="api-row-name">${escapeHtml(r.name)}${badge}</span>
        <span class="api-row-model">${escapeHtml(r.model || '')}</span>
        <span class="api-row-meta">${escapeHtml(r.provider || '')} · ${status}</span>
      </li>`;
    }).join('');
    // Wire clicks.
    apiListEl.querySelectorAll('.api-row').forEach(row => {
      const handler = () => {
        const name = row.getAttribute('data-name');
        if (!name) return;
        selectedApi = name;
        if (apiInputEl) apiInputEl.value = name;
        apiListEl.querySelectorAll('.api-row').forEach(r => r.classList.remove('is-selected'));
        row.classList.add('is-selected');
        // Clear --no-default-api: an explicit --api overrides it.
        if (noDefaultApiEl && noDefaultApiEl.checked) noDefaultApiEl.checked = false;
      };
      row.addEventListener('click', handler);
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); }
      });
    });
  }

  /** Render the status line under the list:
   *    Loaded N custom LLM API profile(s) from <ctx> on <endpoint> (M enabled).
   *    Current default for <tool>: <model>. */
  function renderApiStatus(tool) {
    if (!apiStatusEl) return;
    const src = apiModelsCache && apiModelsCache.source;
    if (!src || !src.label) { apiStatusEl.hidden = true; return; }
    const models = (apiModelsCache && apiModelsCache.models) || [];
    const defaults = (apiModelsCache && apiModelsCache.defaults) || [];
    const def = defaults.find(d => d && d.tool === tool && d.api && d.mode === 'api');
    const total = models.length;
    const enabled = models.filter(m => m && m.enabled !== false).length;
    const where = src.label === 'local' ? 'the local node' : src.label;
    let line = `Loaded ${total} custom LLM API profile(s) from ${escapeHtml(where)} (${enabled} enabled).`;
    if (def && def.api) line += ` Current default for ${escapeHtml(tool)}: ${escapeHtml(def.api)}.`;
    if (src.error) line += ` Error: ${escapeHtml(src.detail || src.error)}`;
    apiStatusEl.innerHTML = line;
    apiStatusEl.hidden = false;
  }

  async function listApiModels() {
    if (!apiListBtn) return;
    const orig = apiListBtn.textContent;
    apiListBtn.disabled = true;
    apiListBtn.textContent = 'Listing…';
    selectedApi = '';
    if (apiInputEl) apiInputEl.value = '';
    try {
      const hints = apiModelsHints();
      apiModelsCache = await api.getApiModels(hints);
      renderApiList(toolSel.value);
      renderApiStatus(toolSel.value);
      applyApiSupport();
      setStatus('API models loaded.', 'is-ok');
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(`List models failed: ${msg}`, 'is-error');
      showToast(`List models failed: ${msg}`, 'error', 5000);
    } finally {
      apiListBtn.textContent = orig;
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
    const ctxVal = ctxSel.value;
    if (ctxVal && ctxVal !== NONE_CONTEXT_VALUE) {
      body.context = ctxVal;
    } else {
      body.node = 'local';
      body.path = pathEl.value.trim();
    }
    // API model: only when the tool supports it and a model is picked.
    const support = (apiModelsCache && apiModelsCache.toolSupport) || TOOL_API_SUPPORT_STATIC;
    if (support[body.tool] !== false) {
      const apiVal = (apiInputEl && apiInputEl.value.trim()) || selectedApi;
      if (apiVal) body.api = apiVal;
      if (noDefaultApiEl && noDefaultApiEl.checked) body.no_default_api = true;
      const tok = (apiTokenEl && apiTokenEl.value.trim()) || '';
      if (tok) body.api_token = tok;
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
    const ctxVal = ctxSel.value;
    if (!ctxVal || ctxVal === NONE_CONTEXT_VALUE) {
      const path = pathEl.value.trim();
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
  ctxSel.addEventListener('change', () => {
    toggleNodeFields();
    // The API cache was for the previous target; clear it so the user
    // re-lists on the new context/node.
    apiModelsCache = null;
    selectedApi = '';
    if (apiInputEl) apiInputEl.value = '';
    if (apiListEl) { apiListEl.hidden = true; apiListEl.innerHTML = ''; }
    if (apiStatusEl) apiStatusEl.hidden = true;
    applyApiSupport();
  });
  toolSel.addEventListener('change', applyApiSupport);
  if (apiListBtn) apiListBtn.addEventListener('click', listApiModels);
  // --no-default-api clears an explicit --api selection.
  if (noDefaultApiEl) {
    noDefaultApiEl.addEventListener('change', () => {
      if (noDefaultApiEl.checked) {
        selectedApi = '';
        if (apiInputEl) apiInputEl.value = '';
        if (apiListEl) apiListEl.querySelectorAll('.api-row').forEach(r => r.classList.remove('is-selected'));
      }
    });
  }

  // Refresh option lists whenever Start becomes the active mode (so
  // newly-added contexts or detected adapters show up without a full
  // page reload).
  let prevMode = state.get('mode');
  let prevConn = state.get('connectionMode');
  let prevCtxs = state.get('contexts');
  let prevAdapters = state.get('adapters');
  refreshToolOptions();
  refreshContextOptions();
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
        toggleNodeFields();
        applyApiSupport();
        applyConnectionState();
        setStatus('');
      }
    }
    if (c !== prevConn) { prevConn = c; applyConnectionState(); }
    if (ctxs !== prevCtxs) { prevCtxs = ctxs; refreshContextOptions(); toggleNodeFields(); }
    if (adapters !== prevAdapters) { prevAdapters = adapters; refreshToolOptions(); applyApiSupport(); }
  });
}
