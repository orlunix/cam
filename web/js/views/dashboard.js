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

function renderAgentCard(agent) {
  const prompt = (agent.prompt || '').slice(0, 80);
  const time = timeSince(agent.started_at);
  return `
    <div class="agent-card" data-id="${agent.id}">
      <div class="agent-card-header">
        ${statusIcon(agent.status)}
        <span class="agent-name">${agent.task_name || agent.id.slice(0, 8)}</span>
        <span class="badge badge-${agent.status}">${agent.status}</span>
      </div>
      <div class="agent-card-meta">
        <span class="agent-tool">${agent.tool}</span>
        ${agent.context_name ? `<span class="agent-ctx">${agent.context_name}</span>` : ''}
        <span class="agent-time">${time}</span>
      </div>
      ${prompt ? `<div class="agent-card-prompt">${escapeHtml(prompt)}</div>` : ''}
    </div>
  `;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

export function renderDashboard(container) {
  const render = () => {
    const agents = state.get('agents') || [];
    const filters = state.get('filters');

    let filtered = agents;
    if (filters.status) filtered = filtered.filter(a => a.status === filters.status);
    if (filters.tool) filtered = filtered.filter(a => a.tool === filters.tool);

    // Sort: running first, then by started_at desc
    filtered.sort((a, b) => {
      if (a.status === 'running' && b.status !== 'running') return -1;
      if (b.status === 'running' && a.status !== 'running') return 1;
      return new Date(b.started_at || 0) - new Date(a.started_at || 0);
    });

    const tools = [...new Set(agents.map(a => a.tool))];

    container.innerHTML = `
      ${renderStats(agents)}
      <div class="filter-bar">
        <select id="filter-status" class="filter-select">
          <option value="">All status</option>
          <option value="running" ${filters.status === 'running' ? 'selected' : ''}>Running</option>
          <option value="completed" ${filters.status === 'completed' ? 'selected' : ''}>Completed</option>
          <option value="failed" ${filters.status === 'failed' ? 'selected' : ''}>Failed</option>
          <option value="killed" ${filters.status === 'killed' ? 'selected' : ''}>Killed</option>
        </select>
        <select id="filter-tool" class="filter-select">
          <option value="">All tools</option>
          ${tools.map(t => `<option value="${t}" ${filters.tool === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="agent-list">
        ${filtered.length === 0
          ? '<div class="empty-state">No agents yet. Tap + to start one.</div>'
          : filtered.map(renderAgentCard).join('')}
      </div>
    `;

    // Event handlers
    container.querySelectorAll('.agent-card').forEach(el => {
      el.addEventListener('click', () => navigate(`/agent/${el.dataset.id}`));
    });

    const statusSel = container.querySelector('#filter-status');
    const toolSel = container.querySelector('#filter-tool');
    if (statusSel) statusSel.addEventListener('change', () => {
      state.set('filters', { ...state.get('filters'), status: statusSel.value });
    });
    if (toolSel) toolSel.addEventListener('change', () => {
      state.set('filters', { ...state.get('filters'), tool: toolSel.value });
    });
  };

  render();
  const unsub = state.subscribe(render);

  // Cleanup when navigating away
  const observer = new MutationObserver(() => {
    if (!container.isConnected) { unsub(); observer.disconnect(); }
  });
  observer.observe(document.getElementById('content'), { childList: true });
}
