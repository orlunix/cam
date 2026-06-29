/**
 * CamUI Mobile V2 — Todos worklog (full parity with desktop todos-mode.js).
 */
import { state } from './app.js';
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
  contextLabelFromContexts,
  storePathForContexts,
  projectsFromItems,
  escapeAttr,
} from '../shared/worklog-core.js?v=2.3.0';
import { escHtml as esc } from '../shared/agent-helpers.js';

export function renderTodos(container) {
  let items = readItems();
  let storedProjects = readStoredProjects();
  let activeTab = 'inbox';
  let detailTab = 'preview';
  let selectedId = items[0]?.id || null;
  let composerKind = null;

  container.innerHTML = `
    <div class="page-header"><h2>Todos</h2></div>
    <p class="relay-hint">Local worklog (V0). Same store as Desktop until Hub <code>todocli</code> proxy lands.</p>

    <section class="todos-mobile-store" aria-label="Todo store">
      <div><span class="todos-mobile-label">Context</span><strong id="todos-ctx-label">—</strong></div>
      <div><span class="todos-mobile-label">Store</span><code id="todos-store-path">—</code></div>
      <div class="todos-mobile-store-actions">
        <button type="button" class="btn-secondary btn-sm" id="todos-check">Check</button>
        <button type="button" class="btn-secondary btn-sm" id="todos-init">Initialize</button>
        <button type="button" class="btn-secondary btn-sm" id="todos-refresh">Refresh</button>
      </div>
    </section>

    <div class="mobile-tab-bar todos-mobile-tabs" role="tablist">
      ${WORKLOG_TABS.map(t => `<button type="button" class="mobile-tab${t === 'inbox' ? ' active' : ''}" data-tab="${t}">${t[0].toUpperCase()}${t.slice(1)}</button>`).join('')}
    </div>

    <div class="todos-mobile-actions">
      <button type="button" class="btn-primary btn-sm" id="todos-new-task">New task</button>
      <button type="button" class="btn-secondary btn-sm" id="todos-new-note">New note</button>
    </div>

    <section class="todos-mobile-filters" aria-label="Todo filters">
      <label>Search<input type="search" id="todos-search" class="form-input" placeholder="Search tasks, notes, tags…" autocomplete="off"></label>
      <label>Project<select id="todos-project-filter" class="form-input"><option value="all">All</option></select></label>
      <label>Status<select id="todos-status-filter" class="form-input">
        <option value="open">Open</option>
        <option value="active">Active</option>
        <option value="done">Done</option>
        <option value="archived">Archived</option>
        <option value="all">All</option>
      </select></label>
      <label>Sort<select id="todos-sort" class="form-input">
        <option value="updated">Updated</option>
        <option value="priority">Priority</option>
        <option value="title">Title</option>
      </select></label>
    </section>

    <div id="todos-status" class="settings-status"></div>
    <div id="todos-outline" class="todos-mobile-outline"></div>
    <div id="todos-detail" class="todos-mobile-detail" hidden></div>
  `;

  const outlineEl = container.querySelector('#todos-outline');
  const detailEl = container.querySelector('#todos-detail');
  const statusEl = container.querySelector('#todos-status');
  const searchEl = container.querySelector('#todos-search');
  const projectEl = container.querySelector('#todos-project-filter');
  const statusFilterEl = container.querySelector('#todos-status-filter');
  const sortEl = container.querySelector('#todos-sort');

  function setStatus(msg, ok) {
    statusEl.textContent = msg || '';
    statusEl.className = 'settings-status' + (ok === true ? ' is-ok' : ok === false ? ' is-error' : '');
  }

  function persist() {
    saveItems(items);
    saveStoredProjects(storedProjects);
  }

  function projects() {
    return projectsFromItems(items, storedProjects);
  }

  function syncMeta() {
    const contexts = state.get('contexts') || [];
    const ctxLabel = container.querySelector('#todos-ctx-label');
    const storePathEl = container.querySelector('#todos-store-path');
    if (ctxLabel) ctxLabel.textContent = contextLabelFromContexts(contexts);
    if (storePathEl) storePathEl.textContent = storePathForContexts(contexts);
  }

  function renderProjectOptions() {
    if (!projectEl) return;
    const cur = projectEl.value || 'all';
    const opts = projects();
    projectEl.innerHTML = '<option value="all">All</option>' + opts.map(p => (
      `<option value="${escapeAttr(p)}">${esc(p)}</option>`
    )).join('');
    projectEl.value = opts.includes(cur) ? cur : 'all';
  }

  function filteredItems() {
    const query = (searchEl?.value || '').trim().toLowerCase();
    const project = projectEl?.value || 'all';
    const status = statusFilterEl?.value || 'open';
    const sort = sortEl?.value || 'updated';
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

  function renderInlineComposer() {
    if (!composerKind || activeTab === 'projects') return '';
    const isNote = composerKind === 'note';
    return `
      <article class="todos-mobile-composer">
        <h4>${isNote ? 'New note' : 'New task'}</h4>
        ${isNote ? '' : `<input id="todos-composer-title" class="form-input" placeholder="Task title" autocomplete="off">`}
        <textarea id="todos-composer-body" class="form-input" rows="${isNote ? 4 : 3}" placeholder="${isNote ? 'Note body (Markdown)' : 'Task description'}"></textarea>
        <div class="todos-mobile-composer-actions">
          <button type="button" class="btn-primary btn-sm" id="todos-composer-save">Save</button>
          <button type="button" class="btn-secondary btn-sm" id="todos-composer-cancel">Cancel</button>
        </div>
      </article>`;
  }

  function renderProjectManager() {
    const allProjects = projects();
    const byProject = allProjects.map(project => ({
      project,
      rows: items.filter(item => item.status !== 'archived' && item.project === project),
    }));
    outlineEl.innerHTML = `
      <section class="todos-mobile-project-manager">
        <div class="todos-mobile-composer">
          <input id="todos-project-new-name" class="form-input" placeholder="New project name" autocomplete="off">
          <button type="button" class="btn-primary btn-sm" data-project-action="add">Add project</button>
        </div>
        ${byProject.map(({ project, rows }) => `
          <article class="todos-mobile-project-card" data-project-name="${escapeAttr(project)}">
            <header class="todos-mobile-project-head">
              <input class="form-input todos-mobile-project-name" value="${escapeAttr(project)}" aria-label="Project name">
              <span class="muted">${rows.length} task(s)</span>
            </header>
            <div class="todos-mobile-project-actions">
              <button type="button" class="btn-secondary btn-sm" data-project-action="rename">Rename</button>
              <button type="button" class="btn-secondary btn-sm" data-project-action="archive-done">Archive done</button>
              <button type="button" class="btn-secondary btn-sm" data-project-action="delete-empty">Delete empty</button>
            </div>
            <div class="todos-mobile-project-tasks">
              ${rows.length ? rows.map(item => `
                <div class="todos-mobile-project-task" data-todo-id="${escapeAttr(item.id)}">
                  <button type="button" class="todos-mobile-row todos-mobile-project-task-title" data-todo-id="${escapeAttr(item.id)}">
                    <span class="todos-mobile-row-kind">${item.kind === 'note' ? 'N' : item.status === 'done' ? '[x]' : '[ ]'}</span>
                    <span class="todos-mobile-row-main">
                      <span class="todos-mobile-row-title">${item.priority ? `<strong>${esc(item.priority)}</strong> ` : ''}${esc(item.title)}</span>
                    </span>
                  </button>
                  <label class="todos-mobile-move-label">Move
                    <select class="form-input" data-project-move="${escapeAttr(item.id)}">
                      ${allProjects.map(p => `<option value="${escapeAttr(p)}" ${p === item.project ? 'selected' : ''}>${esc(p)}</option>`).join('')}
                    </select>
                  </label>
                </div>
              `).join('') : '<div class="empty-state">No tasks in this project.</div>'}
            </div>
          </article>
        `).join('')}
      </section>`;
  }

  function renderOutline() {
    syncMeta();
    renderProjectOptions();

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
      <article class="todos-mobile-project-group">
        <header class="todos-mobile-project-group-head">
          <span class="todos-mobile-project-toggle">v</span>
          <h3>${esc(project)}</h3>
          <span class="muted">${list.length} task(s)</span>
        </header>
        <div class="todos-mobile-item-list">
          ${list.map(item => `
            <button type="button" class="todos-mobile-row${item.id === selectedId ? ' active' : ''}" data-todo-id="${escapeAttr(item.id)}">
              <span class="todos-mobile-row-kind">${item.kind === 'note' ? 'N' : item.status === 'done' ? '[x]' : '[ ]'}</span>
              <span class="todos-mobile-row-main">
                <span class="todos-mobile-row-title">${item.priority ? `<strong>${esc(item.priority)}</strong> ` : ''}${esc(item.title)}</span>
                <span class="todos-mobile-row-meta muted">
                  <span>${esc(item.status)}</span>
                  <span>·</span>
                  <span>updated ${esc(relativeTime(item.updatedAt))}</span>
                  ${item.due ? `<span>·</span><span>due ${esc(item.due)}</span>` : ''}
                  ${(item.notes || []).length ? `<span>·</span><span>${item.notes.length} notes</span>` : ''}
                  ${checkboxProgress(item) ? `<span>·</span><span>${esc(checkboxProgress(item))} checklist</span>` : ''}
                </span>
              </span>
              <span class="todos-mobile-tags">${(item.tags || []).map(tag => `<span class="todos-mobile-tag">#${esc(tag)}</span>`).join('')}</span>
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
      <div class="todos-mobile-entry-list">
        ${list.length ? list.map(entry => `
          <article class="todos-mobile-entry${kind === 'check' ? ' todos-mobile-check-entry' : ''}" data-entry-id="${escapeAttr(entry.id)}">
            ${kind === 'check' ? `<label class="checkbox-row todos-mobile-check-toggle"><input type="checkbox" data-entry-action="toggle" ${entry.done ? 'checked' : ''}> Done</label>` : ''}
            <textarea class="form-input" data-entry-text rows="${kind === 'note' ? 3 : 2}">${esc(entry.text || '')}</textarea>
            <div class="todos-mobile-entry-actions">
              ${kind === 'note' ? `<span class="muted">${esc(relativeTime(entry.updatedAt))}</span>` : ''}
              <button type="button" class="btn-secondary btn-sm" data-entry-action="save">Save</button>
              <button type="button" class="btn-secondary btn-sm" data-entry-action="delete">Delete</button>
            </div>
          </article>
        `).join('') : `<div class="empty-state">${empty}</div>`}
        <div class="todos-mobile-add-entry">
          <textarea id="todos-new-entry-text" class="form-input" rows="${kind === 'note' ? 3 : 2}" placeholder="${escapeAttr(addPlaceholder)}"></textarea>
          <button type="button" class="btn-primary btn-sm" data-entry-action="add">${addText}</button>
        </div>
      </div>`;
  }

  function renderDetail() {
    const item = items.find(row => row.id === selectedId) || filteredItems()[0];
    if (!item) {
      detailEl.innerHTML = '';
      detailEl.hidden = true;
      return;
    }
    selectedId = item.id;
    detailEl.hidden = false;

    const detailBody = detailTab === 'raw'
      ? `<pre class="todos-mobile-pre">${esc(itemMarkdown(item))}</pre>`
      : detailTab === 'notes'
        ? renderEntries(item.notes || [], 'note')
        : detailTab === 'checklist'
          ? renderEntries(item.checklist || [], 'check')
          : detailTab === 'history'
            ? `<ul class="todos-mobile-history">${(item.history || []).length ? (item.history || []).map(x => `<li>${esc(x)}</li>`).join('') : '<li>No history yet.</li>'}</ul>`
            : `<div class="todos-mobile-preview">
                ${item.goal ? `<p class="todos-mobile-goal"><strong>${esc(item.goal)}</strong></p>` : ''}
                <p>${esc(item.body || 'No body yet.')}</p>
              </div>`;

    detailEl.innerHTML = `
      <header class="todos-mobile-detail-head">
        <div>
          <h3>${esc(item.title)}</h3>
          <p class="muted">${esc(item.kind || item.type)} · ${esc(item.project)} · ${esc(item.status)}</p>
        </div>
        <button type="button" class="btn-secondary btn-sm" id="todos-close-detail">Close</button>
      </header>
      <div class="todos-mobile-detail-actions">
        <button type="button" class="btn-secondary btn-sm" data-todo-action="active">Start</button>
        <button type="button" class="btn-secondary btn-sm" data-todo-action="done">Done</button>
        <button type="button" class="btn-secondary btn-sm" data-todo-action="archived">Archive</button>
      </div>
      <dl class="todos-mobile-properties">
        <div><dt>Project</dt><dd>${esc(item.project)}</dd></div>
        <div><dt>Priority</dt><dd>${esc(item.priority || '-')}</dd></div>
        <div><dt>Notes</dt><dd>${esc(String((item.notes || []).length))}</dd></div>
        <div><dt>Checklist</dt><dd>${esc(checkboxProgress(item) || '-')}</dd></div>
      </dl>
      <nav class="mobile-tab-bar todos-mobile-detail-tabs" aria-label="Todo detail tabs">
        ${DETAIL_TABS.map(tab => `<button type="button" class="mobile-tab${tab === detailTab ? ' active' : ''}" data-detail-tab="${tab}">${tab[0].toUpperCase()}${tab.slice(1)}</button>`).join('')}
      </nav>
      <div class="todos-mobile-detail-body" data-detail-kind="${escapeAttr(detailTab)}">${detailBody}</div>`;
  }

  function render() {
    renderOutline();
    renderDetail();
  }

  function addItem(kind = 'task', title = '', body = '') {
    const isNote = kind === 'note';
    const trimmedTitle = String(title || '').trim();
    const trimmedBody = String(body || '').trim();
    if (!isNote && !trimmedTitle) return setStatus('Task title is required.', false);
    if (isNote && !trimmedBody) return setStatus('Note body is required.', false);
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
    setStatus(isNote ? 'Note created.' : 'Task created.', true);
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
      setStatus(message, true);
    }
  }

  function addProject(name) {
    const project = String(name || '').trim();
    if (!project) return setStatus('Project name is empty.', false);
    if (!storedProjects.includes(project)) storedProjects = [...storedProjects, project].sort();
    persist();
    render();
    setStatus(`Project ${project} added.`, true);
  }

  function renameProject(oldName, newName) {
    const nextName = String(newName || '').trim();
    if (!oldName || !nextName) return setStatus('Project name is empty.', false);
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
    setStatus(`Project renamed to ${nextName}.`, true);
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
      return setStatus('Project is not empty.', false);
    }
    storedProjects = storedProjects.filter(p => p !== project);
    persist();
    render();
    setStatus(`Project ${project} removed.`, true);
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
    setStatus('Task moved.', true);
  }

  function addEntry(kind, text) {
    const body = String(text || '').trim();
    if (!body) return setStatus(kind === 'note' ? 'Note text is empty.' : 'Checklist item is empty.', false);
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

  container.querySelectorAll('.todos-mobile-tabs .mobile-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = WORKLOG_TABS.includes(btn.dataset.tab) ? btn.dataset.tab : 'inbox';
      composerKind = null;
      container.querySelectorAll('.todos-mobile-tabs .mobile-tab').forEach(b => b.classList.toggle('active', b === btn));
      render();
    });
  });

  outlineEl.addEventListener('click', e => {
    if (e.target.closest('#todos-composer-save')) {
      return addItem(
        composerKind || 'task',
        container.querySelector('#todos-composer-title')?.value || '',
        container.querySelector('#todos-composer-body')?.value || '',
      );
    }
    if (e.target.closest('#todos-composer-cancel')) {
      composerKind = null;
      return renderOutline();
    }
    const projectBtn = e.target.closest('[data-project-action]');
    if (projectBtn) {
      const action = projectBtn.dataset.projectAction;
      if (action === 'add') return addProject(container.querySelector('#todos-project-new-name')?.value || '');
      const card = projectBtn.closest('[data-project-name]');
      const oldName = card?.dataset.projectName || '';
      if (action === 'rename') return renameProject(oldName, card?.querySelector('.todos-mobile-project-name')?.value || '');
      if (action === 'archive-done') return archiveDoneInProject(oldName);
      if (action === 'delete-empty') return deleteEmptyProject(oldName);
    }
    const row = e.target.closest('[data-todo-id]');
    if (!row || e.target.closest('select,input,button[data-project-action]')) return;
    selectedId = row.dataset.todoId;
    detailTab = 'preview';
    render();
  });

  outlineEl.addEventListener('change', e => {
    const move = e.target.closest('[data-project-move]');
    if (move) moveTaskToProject(move.dataset.projectMove, move.value || 'inbox');
  });

  detailEl.addEventListener('click', e => {
    if (e.target.closest('#todos-close-detail')) {
      selectedId = null;
      return render();
    }
    const tabBtn = e.target.closest('[data-detail-tab]');
    if (tabBtn) {
      detailTab = tabBtn.dataset.detailTab;
      return renderDetail();
    }
    const actionBtn = e.target.closest('[data-todo-action]');
    if (actionBtn) {
      return patchSelected(i => { i.status = actionBtn.dataset.todoAction; }, `status set to ${actionBtn.dataset.todoAction}`);
    }
    const entryBtn = e.target.closest('[data-entry-action]');
    if (!entryBtn) return;
    const kind = detailTab === 'notes' ? 'note' : 'check';
    const action = entryBtn.dataset.entryAction;
    if (action === 'add') return addEntry(kind, detailEl.querySelector('#todos-new-entry-text')?.value || '');
    const row = entryBtn.closest('[data-entry-id]');
    const id = row?.dataset.entryId;
    if (action === 'save') return updateEntry(kind, id, { text: row?.querySelector('[data-entry-text]')?.value || '' });
    if (action === 'delete') return deleteEntry(kind, id);
    if (action === 'toggle') return updateEntry('check', id, { done: !!entryBtn.checked, text: row?.querySelector('[data-entry-text]')?.value || '' });
  });

  searchEl?.addEventListener('input', render);
  projectEl?.addEventListener('change', render);
  statusFilterEl?.addEventListener('change', render);
  sortEl?.addEventListener('change', render);
  container.querySelector('#todos-new-task')?.addEventListener('click', () => {
    composerKind = 'task';
    renderOutline();
    container.querySelector('#todos-composer-title')?.focus();
  });
  container.querySelector('#todos-new-note')?.addEventListener('click', () => {
    composerKind = 'note';
    if (activeTab === 'archive' || activeTab === 'projects') activeTab = 'notes';
    container.querySelectorAll('.todos-mobile-tabs .mobile-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === activeTab);
    });
    render();
    container.querySelector('#todos-composer-body')?.focus();
  });
  container.querySelector('#todos-check')?.addEventListener('click', () => {
    setStatus('Store check is local-only in V0. Backend todocli wiring is next.');
  });
  container.querySelector('#todos-init')?.addEventListener('click', () => {
    setStatus('Initialize will create .cam/worklog when the backend lands.');
  });
  container.querySelector('#todos-refresh')?.addEventListener('click', () => {
    items = readItems();
    storedProjects = readStoredProjects();
    render();
    setStatus('Reloaded local Worklog cache.', true);
  });

  const unsub = state.subscribe(() => {
    if (document.hidden) return;
    syncMeta();
  });

  render();
  return () => unsub();
}
