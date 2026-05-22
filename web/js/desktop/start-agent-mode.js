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
  const promptEl = panel.querySelector('#start-prompt');
  const autoconfirmEl = panel.querySelector('#start-autoconfirm');
  const autoexitEl = panel.querySelector('#start-autoexit');
  const nameEl = panel.querySelector('#start-name');
  const timeoutEl = panel.querySelector('#start-timeout');
  const retryEl = panel.querySelector('#start-retry');
  const submitBtn = panel.querySelector('#start-submit');
  const statusEl = panel.querySelector('#start-status');
  const disconnectedEl = panel.querySelector('#start-disconnected');

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

  function readForm() {
    const body = {
      tool: toolSel.value,
      context: ctxSel.value,
      prompt: (promptEl.value || ' '),
      auto_confirm: autoconfirmEl.checked,
      auto_exit: autoexitEl.checked,
      retry: parseInt(retryEl.value, 10) || 0,
    };
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
    if (!ctxSel.value) {
      setStatus('Pick a context.', 'is-error');
      return;
    }
    const body = readForm();
    submitBtn.disabled = true;
    const origLabel = submitBtn.textContent;
    submitBtn.textContent = 'Starting…';
    setStatus('Starting…');
    let agent = null;
    try {
      agent = await api.startAgent(body);
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
    if (agent && agent.id) {
      state.set('selectedAgentId', agent.id);
    }
    submitBtn.disabled = false;
    submitBtn.textContent = origLabel;
    // CAM-DESK-RUN-013: jump back to Agents so user sees output immediately.
    setMode('agents');
  });

  // Refresh option lists whenever Start becomes the active mode (so
  // newly-added contexts or detected adapters show up without a full
  // page reload).
  let prevMode = state.get('mode');
  let prevConn = state.get('connectionMode');
  let prevCtxs = state.get('contexts');
  let prevAdapters = state.get('adapters');
  refreshToolOptions();
  refreshContextOptions();
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
        applyConnectionState();
        setStatus('');
      }
    }
    if (c !== prevConn) { prevConn = c; applyConnectionState(); }
    if (ctxs !== prevCtxs) { prevCtxs = ctxs; refreshContextOptions(); }
    if (adapters !== prevAdapters) { prevAdapters = adapters; refreshToolOptions(); }
  });
}
