/**
 * Desktop Agent Edit subview (CAM-DESK-EDIT-010..015).
 *
 * Triggered from the agent header in Agents mode; swaps the main pane
 * to a form that edits `name` and `auto_confirm` for the selected
 * agent. The Agents panel and its composer textarea stay mounted with
 * `[hidden]` — so unsent composer text, scroll position, and the
 * Plain/Rich output mode survive the round-trip (CAM-DESK-EDIT-014).
 *
 * Save → CamApi.updateAgent (PATCH /api/agents/{id}); on success the
 * list is refreshed and the same agent stays selected (CAM-DESK-EDIT-013).
 * Cancel → returns to Agents mode without an API call.
 * Error → keeps the form open and re-enables controls
 *         (CAM-DESK-EDIT-015).
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

export function mountAgentEditMode({ api, state, showToast, setMode, loadAgents }) {
  const panel = document.getElementById('mode-edit');
  if (!panel) return;

  const form = panel.querySelector('#edit-form');
  const nameEl = panel.querySelector('#edit-name');
  const autoconfirmEl = panel.querySelector('#edit-autoconfirm');
  const saveBtn = panel.querySelector('#edit-save');
  const cancelBtn = panel.querySelector('#edit-cancel');
  const statusEl = panel.querySelector('#edit-status');
  const subjectEl = panel.querySelector('#edit-subject');

  function setStatus(text, cls = '') {
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function targetAgent() {
    const id = state.get('editAgentId') || state.get('selectedAgentId');
    if (!id) return null;
    return (state.get('agents') || []).find(a => a.id === id) || null;
  }

  function loadForm() {
    const agent = targetAgent();
    if (!agent) {
      subjectEl.textContent = 'No agent selected to edit.';
      nameEl.value = '';
      autoconfirmEl.checked = false;
      saveBtn.disabled = true;
      return;
    }
    saveBtn.disabled = false;
    subjectEl.innerHTML =
      'Editing <strong>' +
      escapeHtml(agent.task_name || agent.id.slice(0, 8)) +
      '</strong> &middot; ' + escapeHtml(agent.tool || '') +
      ' &middot; ' + escapeHtml(agent.status || '');
    nameEl.value = agent.task_name || '';
    autoconfirmEl.checked = !!agent.auto_confirm;
    setStatus('');
  }

  cancelBtn.addEventListener('click', () => {
    // CAM-DESK-EDIT-014: cancel just flips mode back — the Agents
    // panel was hidden, not torn down, so output mode, scroll
    // position, and composer text persist unchanged.
    setMode('agents');
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const agent = targetAgent();
    if (!agent) return;
    const name = (nameEl.value || '').trim();
    if (!name) {
      setStatus('Name cannot be empty.', 'is-error');
      return;
    }
    const body = {
      name,
      auto_confirm: !!autoconfirmEl.checked,
    };
    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    const origLabel = saveBtn.textContent;
    saveBtn.textContent = 'Saving…';
    setStatus('Saving…');
    try {
      await api.updateAgent(agent.id, body);
    } catch (err) {
      // CAM-DESK-EDIT-015: keep the form visible with controls re-enabled.
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
      saveBtn.textContent = origLabel;
      const msg = err?.message || String(err);
      setStatus(`Save failed: ${msg}`, 'is-error');
      showToast(`Save failed: ${msg}`, 'error', 5000);
      return;
    }
    setStatus('Saved.', 'is-ok');
    showToast('Agent updated', 'success');
    try { await loadAgents(); } catch {}
    saveBtn.disabled = false;
    cancelBtn.disabled = false;
    saveBtn.textContent = origLabel;
    // CAM-DESK-EDIT-013: keep selectedAgentId so Agents mode lands
    // back on the same agent's output.
    if (!state.get('selectedAgentId') && agent.id) {
      state.set('selectedAgentId', agent.id);
    }
    setMode('agents');
  });

  // Reload form whenever Edit becomes the active mode or the agent
  // we're editing gets new data from the poller.
  let prevMode = state.get('mode');
  let prevEditId = state.get('editAgentId');
  state.subscribe(() => {
    const m = state.get('mode');
    const e = state.get('editAgentId');
    if (m !== prevMode) {
      prevMode = m;
      if (m === 'edit') loadForm();
    }
    if (e !== prevEditId) {
      prevEditId = e;
      if (state.get('mode') === 'edit') loadForm();
    }
  });
}
