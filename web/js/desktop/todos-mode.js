/**
 * Todos / Notes Worklog mode.
 *
 * V0 is intentionally renderer-local: it establishes the structural
 * Markdown-backed UI and interaction model before the todocli / Hub API is
 * wired. Markdown files remain the planned source of truth; localStorage is
 * only a mock-safe cache for this UI slice.
 */

const STORAGE_KEY = 'cam_desktop_worklog_v0';
const PROJECTS_KEY = 'cam_desktop_worklog_projects_v0';
const TABS = ['inbox', 'projects', 'archive'];
const DETAIL_TABS = ['preview', 'raw', 'notes', 'checklist', 'history'];

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
    goal: 'Make Desktop management pages visually consistent and usable for daily work.',
    body: 'Need align Nodes, Skills, and Todos pages on the same full-width rail. Keep the list structural and mobile-friendly.',
    notes: [
      { id: 'n-layout-1', text: 'User prefers structural rows over decorative cards.', updatedAt: Date.now() - 4 * 60 * 1000 },
    ],
    checklist: [
      { id: 'api', text: 'Add API contract', done: false },
      { id: 'ui', text: 'Add outline UI', done: true },
      { id: 'win', text: 'Test Windows install', done: false },
    ],
    history: ['created from Inbox', 'moved to project camui', 'priority set to P1'],
  },
  {
    id: 'todo-20260610-design',
    type: 'task',
    title: 'Todo UI design',
    status: 'open',
    project: 'camui',
    tags: ['design'],
    updatedAt: Date.now() - 60 * 60 * 1000,
    goal: 'Keep todo and note interaction mobile-friendly.',
    body: 'Use a Notion-like row equals page model, but keep the default view as a todo outline. Markdown remains the source of truth.',
    notes: [
      { id: 'n-design-1', text: 'Notes are task-scoped activity entries in the target model.', updatedAt: Date.now() - 55 * 60 * 1000 },
    ],
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
    goal: 'Wire todocli behind the same Markdown-backed task model.',
    body: 'Bundle todo.py like camc and call it with --config <workspace>/.cam/worklog. Keep Markdown files as source of truth.',
    notes: [],
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
    goal: 'Capture mobile design constraints.',
    body: 'Document bottom-sheet filters and full-screen item detail behavior for the mobile version.',
    notes: [],
    checklist: [],
    history: ['created in Inbox'],
  },
];

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
  }[ch]));
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, '&#39;');
}

function readItems() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return seedItems.map(normalizeItem);
    const parsed = JSON.parse(raw);
    return (Array.isArray(parsed) && parsed.length ? parsed : seedItems).map(normalizeItem);
  } catch {
    return seedItems.map(normalizeItem);
  }
}

function saveItems(items) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(items)); } catch {}
}

function readStoredProjects() {
  try {
    const parsed = JSON.parse(localStorage.getItem(PROJECTS_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.map(x => String(x || '').trim()).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function saveStoredProjects(projects) {
  try { localStorage.setItem(PROJECTS_KEY, JSON.stringify(projects)); } catch {}
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
  if (tab === 'archive') return item.status === 'archived';
  return item.status !== 'archived';
}

function itemMarkdown(item) {
  const tags = (item.tags || []).map(t => `"${String(t).replace(/"/g, '\\"')}"`).join(', ');
  return `---\nid: ${item.id}\ntype: ${item.type}\ntitle: ${item.title}\ngoal: ${item.goal || ''}\nstatus: ${item.status}\nproject: ${item.project}\npriority: ${item.priority || ''}\ntags: [${tags}]\n---\n\n${item.body || ''}`;
}

function checkboxProgress(item) {
  const list = item.checklist || [];
  if (!list.length) return '';
  const done = list.filter(x => x.done).length;
  return `${done}/${list.length}`;
}

function normalizeItem(item) {
  return {
    ...item,
    id: item.id || `todo-${Date.now()}`,
    type: 'task',
    status: item.status || 'open',
    project: item.project || 'inbox',
    priority: item.priority || '',
    tags: Array.isArray(item.tags) ? item.tags : [],
    notes: Array.isArray(item.notes) ? item.notes : [],
    checklist: Array.isArray(item.checklist) ? item.checklist : [],
    history: Array.isArray(item.history) ? item.history : [],
    updatedAt: Number(item.updatedAt || Date.now()),
  };
}

function splitListText(text) {
  return String(text || '').split(/[,\n]/).map(x => x.trim()).filter(Boolean);
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
  let storedProjects = readStoredProjects();
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
    saveStoredProjects(storedProjects);
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
    return Array.from(new Set([
      'inbox',
      ...storedProjects,
      ...items.map(item => item.project || 'inbox'),
    ].map(p => String(p || '').trim()).filter(Boolean))).sort();
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
      return [item.title, item.goal, item.body, item.project, ...(item.tags || [])]
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
    const opts = projects();
    projectEl.innerHTML = '<option value="all">All</option>' + opts.map(p => (
      `<option value="${escapeAttr(p)}">${escapeHtml(p)}</option>`
    )).join('');
    projectEl.value = opts.includes(cur) ? cur : 'all';
  }

  function renderProjectManager() {
    if (!outlineEl) return;
    const allProjects = projects();
    const byProject = allProjects.map(project => ({
      project,
      rows: items.filter(item => item.status !== 'archived' && item.project === project),
    }));
    outlineEl.innerHTML = `
      <section class="todos-project-manager">
        <div class="todos-project-create">
          <input id="todos-project-new-name" placeholder="New project name" autocomplete="off">
          <button type="button" class="btn-primary" data-project-action="add">Add project</button>
        </div>
        ${byProject.map(({ project, rows }) => `
          <article class="todos-project todos-project-manage" data-project-name="${escapeAttr(project)}">
            <header class="todos-project-head todos-project-manage-head">
              <input class="todos-project-name-input" value="${escapeAttr(project)}" aria-label="Project name">
              <span class="muted">${rows.length} task(s)</span>
              <div class="todos-project-actions">
                <button type="button" class="btn-secondary" data-project-action="rename">Rename</button>
                <button type="button" class="btn-secondary" data-project-action="archive-done">Archive done</button>
                <button type="button" class="btn-secondary" data-project-action="delete-empty">Delete empty</button>
              </div>
            </header>
            <div class="todos-project-task-list">
              ${rows.length ? rows.map(item => `
                <div class="todos-project-task" data-todo-id="${escapeAttr(item.id)}">
                  <button type="button" class="todos-project-task-title" data-todo-id="${escapeAttr(item.id)}">
                    <strong>${item.priority ? escapeHtml(item.priority) : '-'}</strong>
                    <span>${escapeHtml(item.title)}</span>
                  </button>
                  <select data-project-move="${escapeAttr(item.id)}" aria-label="Move task to project">
                    ${allProjects.map(p => `<option value="${escapeAttr(p)}" ${p === item.project ? 'selected' : ''}>${escapeHtml(p)}</option>`).join('')}
                  </select>
                </div>
              `).join('') : '<div class="empty-state">No tasks in this project.</div>'}
            </div>
          </article>
        `).join('')}
      </section>
    `;
  }

  function renderOutline() {
    if (!outlineEl) return;
    if (activeTab === 'projects') {
      renderProjectManager();
      return;
    }
    const rows = filteredItems();
    if (!rows.length) {
      outlineEl.innerHTML = '<div class="empty-state">No matching todos.</div>';
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
          <span class="muted">${list.length} task(s)</span>
        </header>
        <div class="todos-item-list">
          ${list.map(item => `
            <button type="button" class="todos-row ${item.id === selectedId ? 'active' : ''}" data-todo-id="${escapeAttr(item.id)}">
              <span class="todos-row-kind">${item.status === 'done' ? '[x]' : '[ ]'}</span>
              <span class="todos-row-main">
                <span class="todos-row-title">${item.priority ? `<strong>${escapeHtml(item.priority)}</strong>` : ''}${escapeHtml(item.title)}</span>
                <span class="todos-row-meta">
                  <span>${escapeHtml(item.status)}</span>
                  <span>·</span>
                  <span>updated ${escapeHtml(relativeTime(item.updatedAt))}</span>
                  ${item.due ? `<span>·</span><span>due ${escapeHtml(item.due)}</span>` : ''}
                  ${(item.notes || []).length ? `<span>·</span><span>${item.notes.length} notes</span>` : ''}
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

  function renderEntries(list, kind) {
    const empty = kind === 'note' ? 'No notes yet.' : 'No checklist items yet.';
    const addPlaceholder = kind === 'note' ? 'Add a note for this task...' : 'Add checklist item...';
    const addText = kind === 'note' ? 'Add note' : 'Add item';
    return `
      <div class="todos-entry-list">
        ${list.length ? list.map(entry => `
          <article class="todos-entry ${kind === 'check' ? 'todos-check-entry' : ''}" data-entry-id="${escapeAttr(entry.id)}">
            ${kind === 'check' ? `<label class="todos-check-toggle"><input type="checkbox" data-entry-action="toggle" ${entry.done ? 'checked' : ''}> Done</label>` : ''}
            <textarea data-entry-text rows="${kind === 'note' ? 3 : 2}">${escapeHtml(entry.text || '')}</textarea>
            <div class="todos-entry-actions">
              ${kind === 'note' ? `<span class="muted">${escapeHtml(relativeTime(entry.updatedAt))}</span>` : ''}
              <button type="button" class="btn-secondary" data-entry-action="save">Save</button>
              <button type="button" class="btn-secondary" data-entry-action="delete">Delete</button>
            </div>
          </article>
        `).join('') : `<div class="empty-state">${empty}</div>`}
        <div class="todos-add-entry">
          <textarea id="todos-new-entry-text" rows="${kind === 'note' ? 3 : 2}" placeholder="${escapeAttr(addPlaceholder)}"></textarea>
          <button type="button" class="btn-primary" data-entry-action="add">${addText}</button>
        </div>
      </div>
    `;
  }

  function renderDetail() {
    if (!detailEl) return;
    const item = items.find(row => row.id === selectedId) || filteredItems()[0];
    if (!item) {
      detailEl.innerHTML = '<div class="empty-state">Select a task to view details.</div>';
      return;
    }
    selectedId = item.id;
    const detailBody = detailTab === 'raw'
      ? `<pre>${escapeHtml(itemMarkdown(item))}</pre>`
      : detailTab === 'notes'
        ? renderEntries(item.notes || [], 'note')
        : detailTab === 'checklist'
          ? renderEntries(item.checklist || [], 'check')
          : detailTab === 'history'
            ? `<ul class="todos-history">${(item.history || []).length ? (item.history || []).map(x => `<li>${escapeHtml(x)}</li>`).join('') : '<li>No history yet.</li>'}</ul>`
            : `<div class="todos-preview">
                ${item.goal ? `<p class="todos-goal">${escapeHtml(item.goal)}</p>` : ''}
                <p>${escapeHtml(item.body || 'No body yet.')}</p>
              </div>`;
    detailEl.innerHTML = `
      <header class="todos-detail-head">
        <div>
          <h3>${escapeHtml(item.title)}</h3>
          <p class="muted">task · ${escapeHtml(item.project)} · ${escapeHtml(item.status)}</p>
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
        <div><dt>Notes</dt><dd>${escapeHtml(String((item.notes || []).length))}</dd></div>
        <div><dt>Checklist</dt><dd>${escapeHtml(checkboxProgress(item) || '-')}</dd></div>
      </dl>
      <nav class="todos-detail-tabs" aria-label="Todo detail tabs">
        ${DETAIL_TABS.map(tab => `<button type="button" class="${tab === detailTab ? 'active' : ''}" data-detail-tab="${tab}">${tab[0].toUpperCase()}${tab.slice(1)}</button>`).join('')}
      </nav>
      <div class="todos-detail-body" data-detail-kind="${escapeAttr(detailTab)}">${detailBody}</div>
    `;
  }

  function render() {
    renderProjectOptions();
    if (contextLabelEl) contextLabelEl.textContent = contextLabel();
    if (storePathEl) storePathEl.textContent = storePath();
    renderOutline();
    renderDetail();
  }

  function addItem() {
    const id = `task-${Date.now()}`;
    const item = {
      id,
      type: 'task',
      title: 'New task',
      status: 'open',
      project: projectEl && projectEl.value !== 'all' ? projectEl.value : 'inbox',
      priority: 'P2',
      tags: [],
      updatedAt: Date.now(),
      goal: '',
      body: 'Describe the task in Markdown.',
      notes: [],
      checklist: [],
      history: ['created locally in V0 UI'],
    };
    items = [item, ...items];
    selectedId = id;
    detailTab = 'raw';
    persist();
    render();
    setStatus('Task created locally.');
  }

  function patchSelected(mutator, message) {
    let changed = false;
    items = items.map(item => {
      if (item.id !== selectedId) return item;
      const next = normalizeItem({ ...item });
      mutator(next);
      next.updatedAt = Date.now();
      next.history = [...(next.history || []), message];
      changed = true;
      return next;
    });
    if (changed) {
      persist();
      render();
      setStatus(message, 'ok');
    }
  }

  function updateSelectedStatus(status) {
    patchSelected(item => { item.status = status; }, `status set to ${status}`);
  }

  function addProject(name) {
    const project = String(name || '').trim();
    if (!project) return setStatus('Project name is empty.', 'error');
    if (!storedProjects.includes(project)) storedProjects = [...storedProjects, project].sort();
    persist();
    render();
    setStatus(`Project ${project} added.`, 'ok');
  }

  function renameProject(oldName, newName) {
    const nextName = String(newName || '').trim();
    if (!oldName || !nextName) return setStatus('Project name is empty.', 'error');
    if (oldName === nextName) return setStatus('No project rename needed.');
    storedProjects = Array.from(new Set(storedProjects.map(p => p === oldName ? nextName : p).concat(nextName))).sort();
    items = items.map(item => item.project === oldName ? {
      ...item,
      project: nextName,
      updatedAt: Date.now(),
      history: [...(item.history || []), `project renamed ${oldName} -> ${nextName}`],
    } : item);
    persist();
    render();
    setStatus(`Project renamed to ${nextName}.`, 'ok');
  }

  function archiveDoneInProject(project) {
    let count = 0;
    items = items.map(item => {
      if (item.project !== project || item.status !== 'done') return item;
      count += 1;
      return { ...item, status: 'archived', updatedAt: Date.now(), history: [...(item.history || []), 'archived from project manager'] };
    });
    persist();
    render();
    setStatus(count ? `Archived ${count} done task(s).` : 'No done tasks to archive.');
  }

  function deleteEmptyProject(project) {
    if (items.some(item => item.project === project && item.status !== 'archived')) {
      return setStatus('Project is not empty.', 'error');
    }
    storedProjects = storedProjects.filter(p => p !== project);
    persist();
    render();
    setStatus(`Project ${project} removed.`, 'ok');
  }

  function moveTaskToProject(id, project) {
    items = items.map(item => item.id === id ? {
      ...item,
      project,
      updatedAt: Date.now(),
      history: [...(item.history || []), `moved to project ${project}`],
    } : item);
    if (!storedProjects.includes(project)) storedProjects = [...storedProjects, project].sort();
    persist();
    render();
    setStatus('Task moved.', 'ok');
  }

  function addEntry(kind, text) {
    const body = String(text || '').trim();
    if (!body) return setStatus(kind === 'note' ? 'Note text is empty.' : 'Checklist item is empty.', 'error');
    patchSelected(item => {
      if (kind === 'note') item.notes = [...(item.notes || []), { id: `note-${Date.now()}`, text: body, updatedAt: Date.now() }];
      else item.checklist = [...(item.checklist || []), { id: `check-${Date.now()}`, text: body, done: false }];
    }, kind === 'note' ? 'Added note.' : 'Added checklist item.');
  }

  function updateEntry(kind, id, patch) {
    patchSelected(item => {
      const key = kind === 'note' ? 'notes' : 'checklist';
      item[key] = (item[key] || []).map(entry => entry.id === id ? { ...entry, ...patch, updatedAt: Date.now() } : entry);
    }, kind === 'note' ? 'Saved note.' : 'Updated checklist item.');
  }

  function deleteEntry(kind, id) {
    patchSelected(item => {
      const key = kind === 'note' ? 'notes' : 'checklist';
      item[key] = (item[key] || []).filter(entry => entry.id !== id);
    }, kind === 'note' ? 'Deleted note.' : 'Deleted checklist item.');
  }

  root.querySelectorAll('.todos-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = TABS.includes(btn.dataset.todosTab) ? btn.dataset.todosTab : 'inbox';
      root.querySelectorAll('.todos-tab').forEach(b => {
        const active = b === btn;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      render();
    });
  });

  outlineEl && outlineEl.addEventListener('click', event => {
    const projectBtn = event.target.closest('[data-project-action]');
    if (projectBtn) {
      const action = projectBtn.dataset.projectAction;
      if (action === 'add') return addProject(document.getElementById('todos-project-new-name')?.value || '');
      const card = projectBtn.closest('[data-project-name]');
      const oldName = card?.dataset.projectName || '';
      if (action === 'rename') return renameProject(oldName, card?.querySelector('.todos-project-name-input')?.value || '');
      if (action === 'archive-done') return archiveDoneInProject(oldName);
      if (action === 'delete-empty') return deleteEmptyProject(oldName);
    }
    const row = event.target.closest('[data-todo-id]');
    if (!row || event.target.closest('select,input,button[data-project-action]')) return;
    selectedId = row.dataset.todoId;
    detailTab = 'preview';
    render();
  });

  outlineEl && outlineEl.addEventListener('change', event => {
    const move = event.target.closest('[data-project-move]');
    if (move) moveTaskToProject(move.dataset.projectMove, move.value || 'inbox');
  });

  detailEl && detailEl.addEventListener('click', event => {
    const tabBtn = event.target.closest('[data-detail-tab]');
    if (tabBtn) {
      detailTab = tabBtn.dataset.detailTab;
      renderDetail();
      return;
    }
    const actionBtn = event.target.closest('[data-todo-action]');
    if (actionBtn) {
      updateSelectedStatus(actionBtn.dataset.todoAction);
      return;
    }
    const entryBtn = event.target.closest('[data-entry-action]');
    if (!entryBtn) return;
    const kind = detailTab === 'notes' ? 'note' : 'check';
    const action = entryBtn.dataset.entryAction;
    if (action === 'add') return addEntry(kind, document.getElementById('todos-new-entry-text')?.value || '');
    const row = entryBtn.closest('[data-entry-id]');
    const id = row?.dataset.entryId;
    if (action === 'save') return updateEntry(kind, id, { text: row?.querySelector('[data-entry-text]')?.value || '' });
    if (action === 'delete') return deleteEntry(kind, id);
    if (action === 'toggle') return updateEntry('check', id, { done: !!entryBtn.checked, text: row?.querySelector('[data-entry-text]')?.value || '' });
  });

  searchEl && searchEl.addEventListener('input', render);
  projectEl && projectEl.addEventListener('change', render);
  statusFilterEl && statusFilterEl.addEventListener('change', render);
  sortEl && sortEl.addEventListener('change', render);
  document.getElementById('todos-new-task')?.addEventListener('click', addItem);
  document.getElementById('todos-check')?.addEventListener('click', () => {
    setStatus('Store check is local-only in V0. Backend todocli wiring is next.');
  });
  document.getElementById('todos-init')?.addEventListener('click', () => {
    setStatus('Initialize will create .cam/worklog when the backend lands.');
  });
  document.getElementById('todos-refresh')?.addEventListener('click', () => {
    items = readItems();
    storedProjects = readStoredProjects();
    render();
    setStatus('Reloaded local Worklog cache.');
  });

  state && state.subscribe && state.subscribe(() => {
    if (state.get('mode') === 'todos' && !root.contains(document.activeElement)) render();
  });
  render();
}
