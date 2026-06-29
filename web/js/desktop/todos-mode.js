/**
 * Todos / Notes Worklog mode (Desktop).
 *
 * V0: local Markdown-backed worklog via shared worklog-core store.
 * Next: Hub workspace-services proxy to todocli.
 */

import {
  WORKLOG_TABS,
  DETAIL_TABS,
  readItems,
  saveItems,
  readStoredProjects,
  saveStoredProjects,
  normalizeItem,
  itemMatchesTab,
  itemMarkdown,
  checkboxProgress,
  relativeTime,
  escapeHtml,
  escapeAttr,
  projectsFromItems,
  storePathForContexts,
  contextLabelFromContexts,
} from '../shared/worklog-core.js?v=0.65.0';

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
  let composerKind = null; // 'task' | 'note' | null

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
    return contextLabelFromContexts(contexts);
  }

  function storePath() {
    const contexts = state && state.get ? (state.get('contexts') || []) : [];
    return storePathForContexts(contexts);
  }

  function projects() {
    return projectsFromItems(items, storedProjects);
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

  function renderInlineComposer() {
    if (!composerKind || activeTab === 'projects') return '';
    const isNote = composerKind === 'note';
    return `
      <article class="todos-composer">
        <h4>${isNote ? 'New note' : 'New task'}</h4>
        ${isNote ? '' : `<input id="todos-composer-title" placeholder="Task title" autocomplete="off">`}
        <textarea id="todos-composer-body" rows="${isNote ? 4 : 3}" placeholder="${isNote ? 'Note body (Markdown)' : 'Task description'}"></textarea>
        <div class="todos-composer-actions">
          <button type="button" class="btn-primary" id="todos-composer-save">Save</button>
          <button type="button" class="btn-secondary" id="todos-composer-cancel">Cancel</button>
        </div>
      </article>`;
  }

  function renderOutline() {
    if (!outlineEl) return;
    if (activeTab === 'projects') {
      renderProjectManager();
      return;
    }
    const rows = filteredItems();
    const composer = renderInlineComposer();
    if (!rows.length && !composer) {
      outlineEl.innerHTML = '<div class="empty-state">No matching todos.</div>';
      return;
    }
    const grouped = rows.reduce((acc, item) => {
      const key = item.project || 'inbox';
      (acc[key] = acc[key] || []).push(item);
      return acc;
    }, {});
    outlineEl.innerHTML = composer + Object.entries(grouped).map(([project, list]) => `
      <article class="todos-project">
        <header class="todos-project-head">
          <span class="todos-project-toggle">v</span>
          <h3>${escapeHtml(project)}</h3>
          <span class="muted">${list.length} task(s)</span>
        </header>
        <div class="todos-item-list">
          ${list.map(item => `
            <button type="button" class="todos-row ${item.id === selectedId ? 'active' : ''}" data-todo-id="${escapeAttr(item.id)}">
              <span class="todos-row-kind">${item.kind === 'note' ? 'N' : item.status === 'done' ? '[x]' : '[ ]'}</span>
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
          <p class="muted">${escapeHtml(item.kind || item.type)} · ${escapeHtml(item.project)} · ${escapeHtml(item.status)}</p>
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

  function addItem(kind = 'task', title = '', body = '') {
    const isNote = kind === 'note';
    const trimmedTitle = String(title || '').trim();
    const trimmedBody = String(body || '').trim();
    if (!isNote && !trimmedTitle) return setStatus('Task title is required.', 'error');
    if (isNote && !trimmedBody) return setStatus('Note body is required.', 'error');
    const id = `${isNote ? 'note' : 'task'}-${Date.now()}`;
    const item = normalizeItem({
      id,
      kind,
      type: kind,
      title: trimmedTitle || (isNote ? trimmedBody.split('\n')[0].slice(0, 80) : 'New task'),
      status: 'open',
      project: projectEl && projectEl.value !== 'all' ? projectEl.value : 'inbox',
      priority: isNote ? '' : 'P2',
      tags: [],
      updatedAt: Date.now(),
      goal: '',
      body: trimmedBody || (isNote ? '' : 'Describe the task in Markdown.'),
      notes: [],
      checklist: [],
      history: [`created locally as ${kind}`],
    });
    items = [item, ...items];
    selectedId = id;
    detailTab = isNote ? 'preview' : 'raw';
    composerKind = null;
    persist();
    render();
    setStatus(isNote ? 'Note created.' : 'Task created.');
  }

  function addTask() {
    composerKind = 'task';
    renderOutline();
    document.getElementById('todos-composer-title')?.focus();
  }

  function addNote() {
    composerKind = 'note';
    activeTab = activeTab === 'archive' || activeTab === 'projects' ? 'notes' : activeTab;
    render();
    document.getElementById('todos-composer-body')?.focus();
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
      activeTab = WORKLOG_TABS.includes(btn.dataset.todosTab) ? btn.dataset.todosTab : 'inbox';
      composerKind = null;
      root.querySelectorAll('.todos-tab').forEach(b => {
        const active = b === btn;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      render();
    });
  });

  outlineEl && outlineEl.addEventListener('click', event => {
    if (event.target.closest('#todos-composer-save')) {
      return addItem(
        composerKind || 'task',
        document.getElementById('todos-composer-title')?.value || '',
        document.getElementById('todos-composer-body')?.value || '',
      );
    }
    if (event.target.closest('#todos-composer-cancel')) {
      composerKind = null;
      return renderOutline();
    }
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
  document.getElementById('todos-new-task')?.addEventListener('click', addTask);
  document.getElementById('todos-new-note')?.addEventListener('click', addNote);
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
