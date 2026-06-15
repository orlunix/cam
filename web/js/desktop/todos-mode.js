/**
 * Todos / Notes Worklog mode.
 *
 * V0 is intentionally renderer-local: it establishes the structural
 * Markdown-backed UI and interaction model before the todocli / Hub API is
 * wired. Markdown files remain the planned source of truth; localStorage is
 * only a mock-safe cache for this UI slice.
 */

const STORAGE_KEY = 'cam_desktop_worklog_v0';
const TABS = ['inbox', 'tasks', 'notes', 'projects', 'archive'];
const DETAIL_TABS = ['preview', 'raw', 'checklist', 'history'];

const seedItems = [
  {
    id: 'todo-20260610-layout',
    type: 'task',
    title: 'Fix Nodes layout',
    status: 'open',
    project: 'camui',
    priority: 'P1',
    tags: ['ui', 'desktop'],
    updatedAt: Date.now() - 5 * 60 * 1000,
    due: 'Jun 14',
    body: 'Need align Nodes, Skills, and Todos pages on the same full-width rail. Keep the list structural and mobile-friendly.',
    checklist: [
      { id: 'api', text: 'Add API contract', done: false },
      { id: 'ui', text: 'Add outline UI', done: true },
      { id: 'win', text: 'Test Windows install', done: false },
    ],
    history: ['created from Inbox', 'moved to project camui', 'priority set to P1'],
  },
  {
    id: 'note-20260610-design',
    type: 'note',
    title: 'Todo UI design',
    status: 'open',
    project: 'camui',
    tags: ['design'],
    updatedAt: Date.now() - 60 * 60 * 1000,
    body: 'Use a Notion-like row equals page model, but keep the default view as a todo outline. Markdown remains the source of truth.',
    checklist: [
      { id: 'mobile', text: 'Avoid hover-only actions', done: true },
      { id: 'sheet', text: 'Mobile detail can become a sheet', done: false },
    ],
    history: ['captured as note', 'linked to project camui'],
  },
  {
    id: 'todo-20260610-wrapper',
    type: 'task',
    title: 'Add todocli API wrapper',
    status: 'active',
    project: 'camui',
    priority: 'P2',
    tags: ['backend'],
    updatedAt: Date.now() - 2 * 60 * 60 * 1000,
    body: 'Bundle todo.py like camc and call it with --config <workspace>/.cam/worklog. Keep Markdown files as source of truth.',
    checklist: [
      { id: 'bundle', text: 'Bundle todo.py', done: false },
      { id: 'routes', text: 'Expose context-scoped routes', done: false },
    ],
    history: ['created from implementation plan', 'status set to active'],
  },
  {
    id: 'todo-20260610-personal',
    type: 'task',
    title: 'Capture mobile interaction notes',
    status: 'open',
    project: 'personal',
    priority: 'P3',
    tags: ['mobile'],
    updatedAt: Date.now() - 24 * 60 * 60 * 1000,
    body: 'Document bottom-sheet filters and full-screen item detail behavior for the mobile version.',
    checklist: [],
    history: ['created in Inbox'],
  },
];

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
  }[ch]));
}

function readItems() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return seedItems;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.length ? parsed : seedItems;
  } catch {
    return seedItems;
  }
}

function saveItems(items) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(items)); } catch {}
}

function relativeTime(ms) {
  const delta = Math.max(0, Date.now() - Number(ms || 0));
  const mins = Math.floor(delta / 60000);
  if (mins < 1) return 'now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function itemMatchesTab(item, tab) {
  if (tab === 'tasks') return item.type === 'task' && item.status !== 'archived';
  if (tab === 'notes') return item.type === 'note' && item.status !== 'archived';
  if (tab === 'archive') return item.status === 'archived';
  return item.status !== 'archived';
}

function itemMarkdown(item) {
  const tags = (item.tags || []).map(t => `"${String(t).replace(/"/g, '\\"')}"`).join(', ');
  return `---\nid: ${item.id}\ntype: ${item.type}\ntitle: ${item.title}\nstatus: ${item.status}\nproject: ${item.project}\npriority: ${item.priority || ''}\ntags: [${tags}]\n---\n\n${item.body || ''}`;
}

function checkboxProgress(item) {
  const list = item.checklist || [];
  if (!list.length) return '';
  const done = list.filter(x => x.done).length;
  return `${done}/${list.length}`;
}

export function mountTodosMode({ state, showToast }) {
  const root = document.getElementById('mode-todos');
  if (!root) return;

  const outlineEl = document.getElementById('todos-outline');
  const detailEl = document.getElementById('todos-detail');
  const statusEl = document.getElementById('todos-status');
  const searchEl = document.getElementById('todos-search');
  const projectEl = document.getElementById('todos-project-filter');
  const statusFilterEl = document.getElementById('todos-status-filter');
  const sortEl = document.getElementById('todos-sort');
  const contextLabelEl = document.getElementById('todos-context-label');
  const storePathEl = document.getElementById('todos-store-path');

  let items = readItems();
  let activeTab = 'inbox';
  let detailTab = 'preview';
  let selectedId = items[0] && items[0].id;

  function setStatus(msg, type = 'info') {
    if (!statusEl) return;
    statusEl.textContent = msg || '';
    statusEl.className = `settings-status todos-status status-${type}`;
  }

  function persist() {
    saveItems(items);
  }

  function contextLabel() {
    const contexts = state && state.get ? (state.get('contexts') || []) : [];
    const first = contexts.find(ctx => ctx && ctx.machine && ctx.machine.type === 'ssh') || contexts[0];
    if (!first) return 'Current workspace context';
    const machine = first.machine || {};
    const host = machine.host || first.name || 'local';
    const user = machine.user ? `${machine.user}@` : '';
    return `${first.name || host} · ${user}${host}`;
  }

  function storePath() {
    const contexts = state && state.get ? (state.get('contexts') || []) : [];
    const first = contexts.find(ctx => ctx && ctx.path) || null;
    return `${first && first.path ? first.path : '/workspace'}/.cam/worklog`;
  }

  function projects() {
    return Array.from(new Set(items.map(item => item.project || 'inbox'))).sort();
  }

  function filteredItems() {
    const query = (searchEl && searchEl.value || '').trim().toLowerCase();
    const project = projectEl && projectEl.value || 'all';
    const status = statusFilterEl && statusFilterEl.value || 'open';
    const sort = sortEl && sortEl.value || 'updated';
    const rows = items.filter(item => {
      if (!itemMatchesTab(item, activeTab)) return false;
      if (project !== 'all' && item.project !== project) return false;
      if (status !== 'all' && item.status !== status) return false;
      if (!query) return true;
      return [item.title, item.body, item.project, ...(item.tags || [])]
        .join(' ')
        .toLowerCase()
        .includes(query);
    });
    rows.sort((a, b) => {
      if (sort === 'priority') return String(a.priority || 'P9').localeCompare(String(b.priority || 'P9'));
      if (sort === 'title') return String(a.title || '').localeCompare(String(b.title || ''));
      return Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
    });
    return rows;
  }

  function renderProjectOptions() {
    if (!projectEl) return;
    const cur = projectEl.value || 'all';
    projectEl.innerHTML = '<option value="all">All</option>' + projects().map(p => (
      `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`
    )).join('');
    projectEl.value = projects().includes(cur) ? cur : 'all';
  }

  function renderOutline() {
    if (!outlineEl) return;
    const rows = filteredItems();
    if (!rows.length) {
      outlineEl.innerHTML = '<div class="empty-state">No matching todos or notes.</div>';
      return;
    }
    const grouped = rows.reduce((acc, item) => {
      const key = item.project || 'inbox';
      (acc[key] = acc[key] || []).push(item);
      return acc;
    }, {});
    outlineEl.innerHTML = Object.entries(grouped).map(([project, list]) => `
      <article class="todos-project">
        <header class="todos-project-head">
          <span class="todos-project-toggle">v</span>
          <h3>${escapeHtml(project)}</h3>
          <span class="muted">${list.length} item(s)</span>
        </header>
        <div class="todos-item-list">
          ${list.map(item => `
            <button type="button" class="todos-row ${item.id === selectedId ? 'active' : ''}" data-todo-id="${escapeHtml(item.id)}">
              <span class="todos-row-kind">${item.type === 'task' ? '[ ]' : 'Note'}</span>
              <span class="todos-row-main">
                <span class="todos-row-title">${item.priority ? `<strong>${escapeHtml(item.priority)}</strong>` : ''}${escapeHtml(item.title)}</span>
                <span class="todos-row-meta">
                  <span>${escapeHtml(item.status)}</span>
                  <span>·</span>
                  <span>updated ${escapeHtml(relativeTime(item.updatedAt))}</span>
                  ${item.due ? `<span>·</span><span>due ${escapeHtml(item.due)}</span>` : ''}
                  ${checkboxProgress(item) ? `<span>·</span><span>${escapeHtml(checkboxProgress(item))} checklist</span>` : ''}
                </span>
              </span>
              <span class="todos-tags">${(item.tags || []).map(tag => `<span class="todos-tag">#${escapeHtml(tag)}</span>`).join('')}</span>
            </button>
          `).join('')}
        </div>
      </article>
    `).join('');
  }

  function renderDetail() {
    if (!detailEl) return;
    const item = items.find(row => row.id === selectedId) || filteredItems()[0];
    if (!item) {
      detailEl.innerHTML = '<div class="empty-state">Select an item to view details.</div>';
      return;
    }
    selectedId = item.id;
    const detailBody = detailTab === 'raw'
      ? `<pre>${escapeHtml(itemMarkdown(item))}</pre>`
      : detailTab === 'checklist'
        ? `<ul class="todos-checklist">${(item.checklist || []).length ? item.checklist.map(x => `<li>${x.done ? '[x]' : '[ ]'} ${escapeHtml(x.text)}</li>`).join('') : '<li>No checklist items yet.</li>'}</ul>`
        : detailTab === 'history'
          ? `<ul class="todos-history">${(item.history || []).map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul>`
          : `<p>${escapeHtml(item.body || '')}</p>`;
    detailEl.innerHTML = `
      <header class="todos-detail-head">
        <div>
          <h3>${escapeHtml(item.title)}</h3>
          <p class="muted">${escapeHtml(item.type)} · ${escapeHtml(item.project)} · ${escapeHtml(item.status)}</p>
        </div>
        <div class="todos-detail-actions">
          <button type="button" class="btn-secondary" data-todo-action="active">Start</button>
          <button type="button" class="btn-secondary" data-todo-action="done">Done</button>
          <button type="button" class="btn-secondary" data-todo-action="archived">Archive</button>
        </div>
      </header>
      <dl class="todos-properties">
        <div><dt>Project</dt><dd>${escapeHtml(item.project)}</dd></div>
        <div><dt>Priority</dt><dd>${escapeHtml(item.priority || '-')}</dd></div>
        <div><dt>Tags</dt><dd>${(item.tags || []).length ? escapeHtml(item.tags.join(', ')) : '-'}</dd></div>
        <div><dt>Due</dt><dd>${escapeHtml(item.due || '-')}</dd></div>
      </dl>
      <nav class="todos-detail-tabs" aria-label="Todo detail tabs">
        ${DETAIL_TABS.map(tab => `<button type="button" class="${tab === detailTab ? 'active' : ''}" data-detail-tab="${tab}">${tab[0].toUpperCase()}${tab.slice(1)}</button>`).join('')}
      </nav>
      <div class="todos-detail-body">${detailBody}</div>
    `;
  }

  function render() {
    renderProjectOptions();
    if (contextLabelEl) contextLabelEl.textContent = contextLabel();
    if (storePathEl) storePathEl.textContent = storePath();
    renderOutline();
    renderDetail();
  }

  function addItem(type) {
    const id = `${type}-${Date.now()}`;
    const item = {
      id,
      type,
      title: type === 'task' ? 'New task' : 'New note',
      status: 'open',
      project: projectEl && projectEl.value !== 'all' ? projectEl.value : 'inbox',
      priority: type === 'task' ? 'P2' : '',
      tags: [],
      updatedAt: Date.now(),
      body: type === 'task' ? 'Describe the task in Markdown.' : 'Write the note in Markdown.',
      checklist: [],
      history: ['created locally in V0 UI'],
    };
    items = [item, ...items];
    selectedId = id;
    detailTab = 'raw';
    persist();
    render();
    setStatus(`${type === 'task' ? 'Task' : 'Note'} created locally.`);
  }

  function updateSelectedStatus(status) {
    items = items.map(item => item.id === selectedId ? {
      ...item, status, updatedAt: Date.now(), history: [...(item.history || []), `status set to ${status}`],
    } : item);
    persist();
    render();
    setStatus(`Status set to ${status}.`);
  }

  root.querySelectorAll('.todos-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = btn.dataset.todosTab || 'inbox';
      root.querySelectorAll('.todos-tab').forEach(b => {
        const active = b === btn;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      render();
    });
  });
  outlineEl && outlineEl.addEventListener('click', event => {
    const row = event.target.closest('.todos-row');
    if (!row) return;
    selectedId = row.dataset.todoId;
    detailTab = 'preview';
    render();
  });
  detailEl && detailEl.addEventListener('click', event => {
    const tabBtn = event.target.closest('[data-detail-tab]');
    if (tabBtn) {
      detailTab = tabBtn.dataset.detailTab;
      renderDetail();
      return;
    }
    const actionBtn = event.target.closest('[data-todo-action]');
    if (actionBtn) updateSelectedStatus(actionBtn.dataset.todoAction);
  });
  searchEl && searchEl.addEventListener('input', render);
  projectEl && projectEl.addEventListener('change', render);
  statusFilterEl && statusFilterEl.addEventListener('change', render);
  sortEl && sortEl.addEventListener('change', render);
  document.getElementById('todos-new-note')?.addEventListener('click', () => addItem('note'));
  document.getElementById('todos-new-task')?.addEventListener('click', () => addItem('task'));
  document.getElementById('todos-check')?.addEventListener('click', () => {
    setStatus('Store check is local-only in V0. Backend todocli wiring is next.');
  });
  document.getElementById('todos-init')?.addEventListener('click', () => {
    setStatus('Initialize will create .cam/worklog when the backend lands.');
  });
  document.getElementById('todos-refresh')?.addEventListener('click', () => {
    items = readItems();
    render();
    setStatus('Reloaded local Worklog cache.');
  });

  state && state.subscribe && state.subscribe(() => {
    if (state.get('mode') === 'todos') render();
  });
  render();
}
