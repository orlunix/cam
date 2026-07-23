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
 * the first option in the Context dropdown. When selected, a Node
 * <select> + Path field appears so the user picks WHICH node (any
 * registered SSH host) plus a path. The request sends node=<key> +
 * path inline (no context record is created, per A2). Local sessions
 * were retired 2026-07-17 — the local node is never offered, and the
 * hub refuses a local target with `local_unsupported` plus SSH-node
 * guidance.
 *
 * The API picker (Advanced (API) section) lists custom LLM API profiles
 * from the SELECTED context/node via GET /api/api-models?context=… or
 * ?node=…. Each profile is a clickable row; clicking fills --api. The
 * section is hidden for tools without --api support (3.3). "Use
 * official/login API" sends --no-default-api; "API token" sends
 * --api-token.
 */

import { hostKeyForMachine } from '../shared/node-host-meta.js';

const DEFAULT_TOOLS = ['claude', 'codex', 'cursor', 'others'];
const HIDDEN_TOOLS = new Set(['generic', 'aider']);

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
  const customToolFieldEl = panel.querySelector('#start-custom-tool-field');
  const customToolEl = panel.querySelector('#start-custom-tool');
  const nodeFieldsEl = panel.querySelector('#start-node-fields');
  const nodeSel = panel.querySelector('#start-node');
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
  const autoexitEl = panel.querySelector('#start-autoexit');
  const autoconfirmEl = panel.querySelector('#start-autoconfirm');
  const timeoutEl = panel.querySelector('#start-timeout');
  const retryEl = panel.querySelector('#start-retry');
  const nameEl = panel.querySelector('#start-name');
  const submitBtn = panel.querySelector('#start-submit');
  const statusEl = panel.querySelector('#start-status');
  const disconnectedEl = panel.querySelector('#start-disconnected');

  // Cached api-models response (models + defaults + toolSupport + source).
  let apiModelsCache = null;
  // Currently selected API profile name (from clicking a list row).
  let selectedApi = '';

  // Static fallback for tool --api support, used before models are
  // fetched so unsupported tools hide the section from the start rather
  // than flashing visible-then-hidden. Mirrors the hub's API_TOOL_SUPPORT map.
  const TOOL_API_SUPPORT_STATIC = { claude: true, codex: true, cursor: false, others: false };

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

  function toolOptions() {
    const adapters = state.get('adapters');
    const base = Array.isArray(adapters) && adapters.length ? adapters : DEFAULT_TOOLS;
    const tools = [];
    for (const item of [...base, 'others']) {
      const tool = String(item == null ? '' : item).trim();
      if (!tool || HIDDEN_TOOLS.has(tool)) continue;
      if (!tools.includes(tool)) tools.push(tool);
    }
    return tools.length ? tools : DEFAULT_TOOLS;
  }

  function refreshToolOptions() {
    const tools = toolOptions();
    const cur = toolSel.value;
    toolSel.innerHTML = tools.map(
      t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`,
    ).join('');
    if (cur && tools.includes(cur)) toolSel.value = cur;
    toggleCustomTool();
  }

  function selectedToolCommand() {
    if (toolSel.value !== 'others') return toolSel.value;
    return customToolEl ? (customToolEl.value || '').trim() : '';
  }

  function toggleCustomTool() {
    const custom = toolSel.value === 'others';
    if (customToolFieldEl) customToolFieldEl.hidden = !custom;
    if (customToolEl) {
      customToolEl.disabled = !custom;
      customToolEl.required = custom;
    }
  }

  function refreshContextOptions() {
    const contexts = state.get('contexts') || [];
    const cur = ctxSel.value;
    // First entry: "(none — start without context)" → runs inline on
    // the selected node with a path, no context record (A2).
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

  /** Populate the Node <select> from the unique host machines known
   *  to the hub (every context's machine), keyed by the shared
   *  hostKeyForMachine so the value matches what the hub /api/agents
   *  and /api/api-models routes resolve. Local sessions are
   *  unsupported, so local-type contexts (legacy store rows) are
   *  skipped. Preserves the current selection when the underlying
   *  contexts list changes. */
  function refreshNodeOptions() {
    if (!nodeSel) return;
    const contexts = state.get('contexts') || [];
    const seen = new Map(); // key → label
    for (const c of contexts) {
      const m = (c && c.machine) || {};
      const key = hostKeyForMachine({
        type: m.type,
        host: m.host || 'local',
        user: m.user || '',
        port: m.port,
      });
      if (key === 'local') continue; // local sessions unsupported
      if (!seen.has(key)) {
        const label = `${m.user || ''}@${m.host || ''}${m.port ? ':' + m.port : ''}`.replace(/^@/, '');
        seen.set(key, label);
      }
    }
    const cur = nodeSel.value;
    nodeSel.innerHTML = Array.from(seen.entries()).map(
      ([k, label]) => `<option value="${escapeHtml(k)}">${escapeHtml(label)}</option>`,
    ).join('');
    // Preserve selection if still present; otherwise the browser keeps
    // the first option (empty when no SSH node is registered yet).
    if (cur && seen.has(cur)) nodeSel.value = cur;
  }

  /** Default Path to /home/<user> for the selected node's machine.
   *  Only pre-fills when
   *  the field is empty OR the current value was auto-filled by us
   *  (dataset.autofill === '1'); a user-typed path is preserved across
   *  node changes so switching nodes does not clobber an explicit
   *  path. We tag the field with dataset.autofill='1' whenever WE set
   *  it, and clear that flag on any user input so we know to stop
   *  touching it. */
  function syncDefaultPath() {
    const val = ctxSel.value;
    if (val && val !== NONE_CONTEXT_VALUE) {
      // A real context is selected — Path field is hidden, no need.
      if (!pathEl.value) pathEl.value = '';
      return;
    }
    // none / inline: infer the user from the selected node, else $USER.
    // Re-default ONLY when the field is empty or the current value was
    // auto-filled by us. A user-typed path (dataset.autofill cleared)
    // is left alone.
    const autofilled = pathEl.dataset.autofill === '1';
    if (pathEl.value && !autofilled) return;
    const key = (nodeSel && nodeSel.value) || 'local';
    const ctxs = state.get('contexts') || [];
    const matched = ctxs.find(c => {
      const m = (c && c.machine) || {};
      return hostKeyForMachine({
        type: m.type,
        host: m.host || 'local',
        user: m.user || '',
        port: m.port,
      }) === key;
    });
    const user = (matched && matched.machine && matched.machine.user) ||
      (key === 'local' && typeof window !== 'undefined' && window.camLocalUser) || '';
    pathEl.value = user ? `/home/${user}` : '/home/hren';
    pathEl.dataset.autofill = '1';
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
   *  none/empty, we pass the selected Node <select> value. With no
   *  registered SSH node the select is empty and the 'local' fallback
   *  routes to the hub's `local_unsupported` refusal, which carries
   *  the SSH-node guidance. */
  function currentNodeKey() {
    const val = ctxSel.value;
    if (val && val !== NONE_CONTEXT_VALUE) return '';
    return (nodeSel && nodeSel.value) || 'local';
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
    return { node: (nodeSel && nodeSel.value) || 'local' };
  }

  function selectToolDefaultApi(tool) {
    if (noDefaultApiEl && noDefaultApiEl.checked) return;
    const defaults = (apiModelsCache && apiModelsCache.defaults) || [];
    const def = defaults.find(d => d && d.tool === tool && d.api && d.mode === 'api');
    selectedApi = def ? def.api : '';
    if (apiInputEl) apiInputEl.value = selectedApi;
  }

  /** Render the cached models as a clickable list of rows. Each row:
   *    name (left, with default★ + selected✓ marks)
   *    model path (middle, muted)
   *    provider · ●enabled/○disabled (right)
   *  The per-tool default row gets a ★default badge; the row the user
   *  clicked gets a ✓selected mark + highlight. Disabled profiles are
   *  dimmed so it's clear which are usable. */
  function renderApiList(tool) {
    const models = (apiModelsCache && apiModelsCache.models) || [];
    const defaults = (apiModelsCache && apiModelsCache.defaults) || [];
    const def = defaults.find(d => d && d.tool === tool && d.api && d.mode === 'api');
    const defName = def ? def.api : '';
    if (!models.length) {
      // Show WHY the list is empty when the hub reported an error,
      // instead of a silent blank. The hub sets source.error when the
      // target is unsupported (local sessions were retired — the
      // refusal carries the SSH-node guidance) or a remote camc call
      // failed.
      const src = apiModelsCache && apiModelsCache.source;
      if (apiListEl) {
        if (src && src.error && src.detail) {
          apiListEl.hidden = false;
          apiListEl.innerHTML = `<li class="api-row api-row-empty" style="padding:10px 12px;color:var(--text-muted,#888);cursor:default">No profiles loaded: ${escapeHtml(src.detail)}</li>`;
        } else {
          apiListEl.hidden = true;
        }
      }
      return;
    }
    if (!apiListEl) return;
    apiListEl.hidden = false;
    const header = `<li class="api-row-list-header" aria-hidden="true">
      <span>Profile · marks</span>
      <span>Model</span>
      <span>Provider · status</span>
    </li>`;
    apiListEl.innerHTML = header + models.map(r => {
      const isDef = defName && r.name === defName;
      const sel = selectedApi && r.name === selectedApi;
      const disabled = r.enabled === false;
      const dot = disabled ? '○' : '●';
      const status = disabled ? 'disabled' : 'enabled';
      const cls = `api-row${sel ? ' is-selected' : ''}${disabled ? ' is-disabled' : ''}`;
      const marks =
        (isDef ? '<span class="api-mark api-mark-default" title="hub default for this tool">★</span>' : '<span class="api-mark api-mark-none"></span>') +
        (sel ? '<span class="api-mark api-mark-selected" title="currently selected">✓</span>' : '<span class="api-mark api-mark-none"></span>');
      const badge = isDef ? '<span class="api-default-badge">default</span>' : '';
      return `<li class="${cls}" data-name="${escapeHtml(r.name)}" role="button" tabindex="0">
        <span class="api-row-name">${escapeHtml(r.name)}${badge}${marks}</span>
        <span class="api-row-model">${escapeHtml(r.model || '')}</span>
        <span class="api-row-meta"><span class="api-dot ${disabled ? 'is-off' : 'is-on'}">${dot}</span> ${escapeHtml(r.provider || '')} · ${status}</span>
      </li>`;
    }).join('');
    // Wire clicks. Disabled rows are not clickable.
    apiListEl.querySelectorAll('.api-row').forEach(row => {
      if (row.classList.contains('is-disabled')) return;
      const handler = () => {
        const name = row.getAttribute('data-name');
        if (!name) return;
        selectedApi = name;
        if (apiInputEl) apiInputEl.value = name;
        apiListEl.querySelectorAll('.api-row').forEach(r => r.classList.remove('is-selected'));
        row.classList.add('is-selected');
        // Re-render so the ✓selected mark moves to the new row.
        renderApiList(tool);
        renderApiStatus(tool);
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
   *    Default for <tool>: <model>. Selected: <model>.
   *  When --no-default-api is checked, show "Selected: official/login API"
   *  instead of a profile name. */
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
    if (def && def.api) line += ` Default for ${escapeHtml(tool)}: ${escapeHtml(def.api)}.`;
    const noDefault = noDefaultApiEl && noDefaultApiEl.checked;
    if (noDefault) {
      line += ` Selected: official/login API (--no-default-api).`;
    } else if (selectedApi) {
      line += ` Selected: ${escapeHtml(selectedApi)}.`;
    }
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
      selectToolDefaultApi(toolSel.value);
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
      tool: selectedToolCommand(),
      prompt: (promptEl.value || ' '),
      auto_exit: autoexitEl.checked,
      // CAM-DESK-RUN-011: auto_confirm + timeout + retry are part of the
      // required form surface. They are always sent so the hub can record
      // them on the agent record. In Relay mode (cam serve) they are
      // enforced server-side; in Direct mode (camc run) camc has no
      // --auto-confirm/--timeout/--retry flag, so the hub accepts them,
      // records them, and surfaces a direct_limitations note (see
      // _resolveStartTarget / POST /api/agents). camc forces
      // auto_confirm=true on its own, so the toggle is a no-op there.
      auto_confirm: autoconfirmEl ? autoconfirmEl.checked : true,
      retry: retryEl ? (parseInt(retryEl.value, 10) || 0) : 0,
    };
    if (timeoutEl) {
      const t = (timeoutEl.value || '').trim();
      if (t) body.timeout = t;
    }
    const ctxVal = ctxSel.value;
    if (ctxVal && ctxVal !== NONE_CONTEXT_VALUE) {
      body.context = ctxVal;
    } else {
      body.node = (nodeSel && nodeSel.value) || 'local';
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
    if (!selectedToolCommand()) {
      setStatus('Enter a tool command for Others.', 'is-error');
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
  // Node select → re-default the Path for the newly-selected node and
  // clear the API cache (different node = different profiles).
  if (nodeSel) {
    nodeSel.addEventListener('change', () => {
      syncDefaultPath();
      apiModelsCache = null;
      selectedApi = '';
      if (apiInputEl) apiInputEl.value = '';
      if (apiListEl) { apiListEl.hidden = true; apiListEl.innerHTML = ''; }
      if (apiStatusEl) apiStatusEl.hidden = true;
      applyApiSupport();
    });
  }
  // Path input: clear the auto-fill flag as soon as the user edits the
  // field by hand, so a later node change does NOT overwrite their
  // explicit path (syncDefaultPath only re-defaults when autofill='1').
  if (pathEl) {
    pathEl.addEventListener('input', () => {
      if (pathEl.dataset.autofill === '1') pathEl.dataset.autofill = '0';
    });
  }
  toolSel.addEventListener('change', () => {
    toggleCustomTool();
    applyApiSupport();
    // Different tool → different default / selected marks. Re-render
    // the list (if cached) so the ★default moves to the new tool's row.
    if (apiModelsCache) {
      selectToolDefaultApi(toolSel.value);
      renderApiList(toolSel.value);
      renderApiStatus(toolSel.value);
    }
  });
  if (apiListBtn) apiListBtn.addEventListener('click', listApiModels);
  // --no-default-api clears an explicit --api selection + re-renders
  // status so "Selected: official/login API" reflects the toggle.
  if (noDefaultApiEl) {
    noDefaultApiEl.addEventListener('change', () => {
      if (noDefaultApiEl.checked) {
        selectedApi = '';
        if (apiInputEl) apiInputEl.value = '';
        if (apiListEl) apiListEl.querySelectorAll('.api-row').forEach(r => r.classList.remove('is-selected'));
      }
      if (apiModelsCache) {
        if (!noDefaultApiEl.checked) selectToolDefaultApi(toolSel.value);
        renderApiList(toolSel.value);
        renderApiStatus(toolSel.value);
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
  toggleCustomTool();
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
