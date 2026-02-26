import { api, state, navigate } from '../app.js';

const STATUS_ICONS = {
  running: '<span class="dot running"></span>',
  completed: '<span class="dot completed"></span>',
  failed: '<span class="dot failed"></span>',
  timeout: '<span class="dot timeout"></span>',
  killed: '<span class="dot killed"></span>',
  pending: '<span class="dot pending"></span>',
  starting: '<span class="dot starting"></span>',
  retrying: '<span class="dot retrying"></span>',
};

function statusIcon(status) {
  return STATUS_ICONS[status] || '<span class="dot"></span>';
}

function timeSince(dateStr) {
  if (!dateStr) return '';
  const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function renderStats(agents) {
  const running = agents.filter(a => a.status === 'running').length;
  const completed = agents.filter(a => a.status === 'completed').length;
  const failed = agents.filter(a => ['failed', 'timeout', 'killed'].includes(a.status)).length;
  return `
    <div class="stats-bar">
      <div class="stat"><span class="stat-num running-text">${running}</span> running</div>
      <div class="stat"><span class="stat-num completed-text">${completed}</span> done</div>
      <div class="stat"><span class="stat-num failed-text">${failed}</span> failed</div>
    </div>
  `;
}

const TERMINAL = ['completed', 'failed', 'timeout', 'killed'];

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

export function renderDashboard(container) {
  let editingId = null;

  // Static structure: stats + filters + list container + edit form
  container.innerHTML = `
    <div id="dash-stats"></div>
    <div class="filter-bar">
      <select id="filter-context" class="filter-select">
        <option value="">All contexts</option>
      </select>
      <select id="filter-status" class="filter-select">
        <option value="">All status</option>
        <option value="running">Running</option>
        <option value="completed">Completed</option>
        <option value="failed">Failed</option>
        <option value="killed">Killed</option>
      </select>
      <select id="filter-tool" class="filter-select">
        <option value="">All tools</option>
      </select>
    </div>
    <div id="agent-list-container"></div>

    <div class="section-divider" id="edit-divider" style="display:none"></div>
    <div id="edit-section" style="display:none">
      <div class="page-header">
        <h3 id="edit-title">Edit Agent</h3>
      </div>
      <form id="agent-edit-form" class="form">
        <div class="form-group">
          <label for="agent-edit-name">Name</label>
          <input type="text" id="agent-edit-name" class="form-input" required placeholder="agent label">
        </div>
        <div class="form-group form-toggle">
          <label for="agent-edit-autoconfirm">Auto Confirm</label>
          <input type="checkbox" id="agent-edit-autoconfirm">
        </div>
        <div class="form-actions" style="flex-direction:row">
          <button type="submit" class="btn-primary" id="agent-edit-submit" style="flex:1">Save Changes</button>
          <button type="button" class="btn-secondary" id="agent-edit-cancel">Cancel</button>
        </div>
      </form>
    </div>
  `;

  // Filter elements (static, not rebuilt on refresh)
  const statusSel = container.querySelector('#filter-status');
  const toolSel = container.querySelector('#filter-tool');
  const contextSel = container.querySelector('#filter-context');
  const filters = state.get('filters');
  if (filters.status) statusSel.value = filters.status;
  if (filters.tool) toolSel.value = filters.tool;
  if (filters.context) contextSel.value = filters.context;

  statusSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), status: statusSel.value });
  });
  toolSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), tool: toolSel.value });
  });
  contextSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), context: contextSel.value });
  });

  function renderList() {
    const agents = state.get('agents') || [];
    const filters = state.get('filters');

    let filtered = agents;
    if (filters.status) filtered = filtered.filter(a => a.status === filters.status);
    if (filters.tool) filtered = filtered.filter(a => a.tool === filters.tool);
    if (filters.context) filtered = filtered.filter(a => a.context_name === filters.context);

    // Sort: running first, then by started_at desc
    filtered.sort((a, b) => {
      if (a.status === 'running' && b.status !== 'running') return -1;
      if (b.status === 'running' && a.status !== 'running') return 1;
      return new Date(b.started_at || 0) - new Date(a.started_at || 0);
    });

    // Stats
    container.querySelector('#dash-stats').innerHTML = renderStats(agents);

    // Update tool/context filter options (preserve selection)
    const tools = [...new Set(agents.map(a => a.tool))];
    const curTool = toolSel.value;
    toolSel.innerHTML = `<option value="">All tools</option>${tools.map(t => `<option value="${t}">${t}</option>`).join('')}`;
    toolSel.value = curTool;

    const contexts = [...new Set(agents.map(a => a.context_name).filter(Boolean))].sort();
    const curCtx = contextSel.value;
    contextSel.innerHTML = `<option value="">All contexts</option>${contexts.map(c => `<option value="${c}">${c}</option>`).join('')}`;
    contextSel.value = curCtx;

    // Agent list — incremental DOM update to avoid full rebuild flicker
    const listEl = container.querySelector('#agent-list-container');
    const existingList = listEl.querySelector('.agent-list');
    const existingIds = existingList
      ? [...existingList.querySelectorAll('.agent-card')].map(el => el.dataset.id)
      : [];
    const filteredIds = filtered.map(a => a.id);

    // Check if we can do an incremental update (same set of agent IDs in same order)
    const canPatch = existingList
      && existingIds.length === filteredIds.length
      && existingIds.every((id, i) => id === filteredIds[i]);

    if (canPatch) {
      // In-place update: patch badge, meta, and time for each card
      filtered.forEach(agent => {
        const card = existingList.querySelector(`.agent-card[data-id="${agent.id}"]`);
        if (!card) return;
        const badge = card.querySelector('.badge');
        if (badge && badge.textContent !== agent.status) {
          badge.textContent = agent.status;
          badge.className = `badge badge-${agent.status}`;
        }
        const dot = card.querySelector('.dot');
        if (dot && dot.className !== `dot ${agent.status}`) {
          dot.className = `dot ${agent.status}`;
        }
        const timeEl = card.querySelector('.agent-time');
        if (timeEl) timeEl.textContent = timeSince(agent.started_at);
        const nameEl = card.querySelector('.agent-name');
        const name = agent.task_name || agent.id.slice(0, 8);
        if (nameEl && nameEl.textContent !== name) nameEl.textContent = name;
        card.classList.toggle('editing', agent.id === editingId);
      });
    } else {
      // Full rebuild when agent count/order changed
      listEl.innerHTML = `
        <div class="agent-list">
          ${filtered.length === 0
            ? '<div class="empty-state">No agents yet. Tap + to start one.</div>'
            : filtered.map(agent => {
                const prompt = (agent.prompt || '').slice(0, 80);
                const time = timeSince(agent.started_at);
                const canDelete = TERMINAL.includes(agent.status);
                const isEditing = agent.id === editingId;
                return `
                <div class="agent-card${isEditing ? ' editing' : ''}" data-id="${agent.id}">
                  <div class="agent-card-header">
                    ${statusIcon(agent.status)}
                    <span class="agent-name">${escapeHtml(agent.task_name || agent.id.slice(0, 8))}</span>
                    <span class="badge badge-${agent.status}">${agent.status}</span>
                    <button class="btn-sm btn-secondary edit-agent" data-id="${agent.id}" title="Edit">Edit</button>
                    ${canDelete ? `<button class="btn-delete-card" data-delete-id="${agent.id}" title="Delete">&times;</button>` : ''}
                  </div>
                  <div class="agent-card-meta">
                    <span class="agent-tool">${agent.tool}</span>
                    ${agent.context_name ? `<span class="agent-ctx">${agent.context_name}</span>` : ''}
                    <span class="agent-time">${time}</span>
                  </div>
                  ${prompt ? `<div class="agent-card-prompt">${escapeHtml(prompt)}</div>` : ''}
                </div>`;
              }).join('')}
        </div>
      `;

      // Wire event listeners only on full rebuild
      listEl.querySelectorAll('.agent-card').forEach(el => {
        el.addEventListener('click', () => navigate(`/agent/${el.dataset.id}`));
      });

      listEl.querySelectorAll('.edit-agent').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const agent = agents.find(a => a.id === btn.dataset.id);
          if (agent) fillFormForEdit(agent);
        });
      });

      listEl.querySelectorAll('.btn-delete-card').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('Delete this agent from history?')) return;
          try {
            await api.deleteAgentHistory(btn.dataset.deleteId);
            state.toast('Agent deleted', 'success');
            const resp = await api.listAgents({ limit: 50 });
            state.set('agents', resp.agents || []);
          } catch (err) { state.toast(err.message, 'error'); }
        });
      });
    }
  }

  function fillFormForEdit(agent) {
    editingId = agent.id;
    const nameInput = container.querySelector('#agent-edit-name');
    nameInput.value = agent.task_name || '';
    container.querySelector('#agent-edit-autoconfirm').checked = !!agent.auto_confirm;
    container.querySelector('#edit-section').style.display = '';
    container.querySelector('#edit-divider').style.display = '';
    container.querySelector('#edit-title').textContent = `Edit: ${agent.task_name || agent.id.slice(0, 8)}`;
    renderList(); // update card highlight
    nameInput.focus(); // trigger keyboard
    setTimeout(() => nameInput.scrollIntoView({ behavior: 'smooth', block: 'center' }), 400);
  }

  function resetForm() {
    editingId = null;
    container.querySelector('#agent-edit-form').reset();
    container.querySelector('#edit-section').style.display = 'none';
    container.querySelector('#edit-divider').style.display = 'none';
    renderList(); // remove card highlight
  }

  // Initial render
  renderList();

  // Only re-render list when agents/filters change (preserve form state)
  let prevAgents = state.get('agents');
  let prevFilters = state.get('filters');
  const unsub = state.subscribe(() => {
    const curAgents = state.get('agents');
    const curFilters = state.get('filters');
    if (curAgents !== prevAgents || curFilters !== prevFilters) {
      prevAgents = curAgents;
      prevFilters = curFilters;
      renderList();
    }
  });

  // Cancel button
  container.querySelector('#agent-edit-cancel').addEventListener('click', resetForm);

  // Submit handler
  container.querySelector('#agent-edit-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!editingId) return;
    const name = container.querySelector('#agent-edit-name').value.trim();
    if (!name) return;
    const autoConfirm = container.querySelector('#agent-edit-autoconfirm').checked;
    try {
      await api.updateAgent(editingId, { name, auto_confirm: autoConfirm });
      state.toast('Agent updated', 'success');
      resetForm();
      const resp = await api.listAgents({ limit: 50 });
      state.set('agents', resp.agents || []);
    } catch (err) { state.toast(err.message, 'error'); }
  });

  return () => { unsub(); };
}
