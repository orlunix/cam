/**
 * Desktop sidebar/list — renders the agent list, status/tool filters,
 * and selection. Selection writes `selectedAgentId` to AppState; the
 * agent console listens for changes.
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function timeSince(dateStr) {
  if (!dateStr) return '';
  const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (s < 0) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function mountShell({ state }) {
  const listWrap = document.getElementById('agent-list-wrap');
  const statusSel = document.getElementById('filter-status');
  const toolSel = document.getElementById('filter-tool');

  // Restore filter state if AppState already had a value from prior view.
  const filters = state.get('filters') || { status: '', tool: '' };
  if (filters.status) statusSel.value = filters.status;
  if (filters.tool) toolSel.value = filters.tool;

  statusSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), status: statusSel.value });
  });
  toolSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), tool: toolSel.value });
  });

  function applyFilters(agents) {
    const f = state.get('filters') || {};
    let out = agents.slice();
    if (f.status) out = out.filter(a => a.status === f.status);
    if (f.tool) out = out.filter(a => a.tool === f.tool);
    // Sort: running first, then by started_at desc.
    out.sort((a, b) => {
      const aRun = a.status === 'running' ? 0 : 1;
      const bRun = b.status === 'running' ? 0 : 1;
      if (aRun !== bRun) return aRun - bRun;
      return new Date(b.started_at || 0) - new Date(a.started_at || 0);
    });
    return out;
  }

  function refreshToolFilterOptions(agents) {
    const tools = [...new Set(agents.map(a => a.tool).filter(Boolean))];
    const cur = toolSel.value;
    toolSel.innerHTML =
      '<option value="">All tools</option>' +
      tools.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
    if (tools.includes(cur)) toolSel.value = cur;
  }

  function render() {
    const agents = state.get('agents') || [];
    const selectedId = state.get('selectedAgentId');
    refreshToolFilterOptions(agents);
    const filtered = applyFilters(agents);

    if (filtered.length === 0) {
      listWrap.innerHTML = `
        <div class="empty-state">
          ${agents.length === 0 ? 'No agents found.' : 'No agents match the current filter.'}
        </div>`;
      return;
    }

    const html = filtered.map(agent => {
      const name = escapeHtml(agent.task_name || agent.id?.slice(0, 8) || '');
      const tool = escapeHtml(agent.tool || '');
      const ctx = agent.context_name ? ' · ' + escapeHtml(agent.context_name) : '';
      const status = escapeHtml(agent.status || 'unknown');
      const host = agent.machine_host ? ' · ' + escapeHtml(agent.machine_host.split('.')[0]) : '';
      const t = timeSince(agent.started_at);
      const sel = agent.id === selectedId ? ' selected' : '';
      return `
        <div class="desktop-agent-row${sel}" data-id="${escapeHtml(agent.id)}">
          <span class="dot ${escapeHtml(agent.status || '')}"></span>
          <div class="row-body">
            <div class="row-name">${name}</div>
            <div class="row-meta">${tool}${ctx}${host} · ${status}</div>
          </div>
          <div class="row-time">${t}</div>
        </div>`;
    }).join('');

    listWrap.innerHTML = html;
    listWrap.querySelectorAll('.desktop-agent-row').forEach(el => {
      el.addEventListener('click', () => {
        state.set('selectedAgentId', el.dataset.id);
      });
    });
  }

  // Initial render
  render();

  // Re-render when agents, filters, or selection change
  let prevAgents = state.get('agents');
  let prevFilters = state.get('filters');
  let prevSelected = state.get('selectedAgentId');
  state.subscribe(() => {
    const a = state.get('agents');
    const f = state.get('filters');
    const s = state.get('selectedAgentId');
    if (a !== prevAgents || f !== prevFilters || s !== prevSelected) {
      prevAgents = a;
      prevFilters = f;
      prevSelected = s;
      render();
    }
  });
}
