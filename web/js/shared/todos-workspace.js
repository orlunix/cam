import { createTodosController } from './todos-controller.js';
import { checkboxProgress, escapeAttr, escapeHtml, itemMarkdown } from './worklog-core.js';

const esc = escapeHtml;
const bodyPreview = (text) => String(text || "").replace(/\s+/g, " ").slice(0, 180) || "No description yet.";
const notePreview = (notes) => {
  const latest = notes[notes.length - 1];
  return latest ? String(latest.text || "").replace(/\s+/g, " ").slice(0, 80) : "No notes";
};

const TASK_DETAIL_TABS = [['preview', 'Preview'], ['notes', 'Notes'], ['checklist', 'Checklist'], ['history', 'History'], ['raw', 'Raw']];
const todoIcons = {
  chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>',
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0 2 5M20 4v7h-7"/></svg>',
};
function todoIcon(name) { return todoIcons[name] || ''; }
function todoIconButton(action, name, label, attrs = '') { return `<button type="button" class="tw-icon-button" data-action="${action}" aria-label="${esc(label)}" title="${esc(label)}" ${attrs}>${todoIcon(name)}</button>`; }

function taskCard(task, expanded, detailTab) {
  const notes = task.notes || [];
  const meta = `${esc(task.project)} <i></i> ${esc(task.status)}${task.priority ? ` <i></i> ${esc(task.priority)}` : ''}`;
  return `<article class="tw-task${expanded ? ' is-open' : ''}" data-task-id="${escapeAttr(task.id)}">
    <header class="tw-task-summary">
      <label class="tw-complete"><input type="checkbox" data-action="toggle-task" ${task.status === 'done' ? 'checked' : ''}><span></span></label>
      <button type="button" class="tw-task-toggle" data-action="toggle-card" aria-expanded="${expanded}">
        <span class="tw-task-title-line"><strong>${esc(task.title)}</strong><span class="tw-task-meta">${meta}</span></span>
        ${task.goal ? `<span class="tw-goal">${esc(task.goal)}</span>` : ''}
        <span class="tw-preview">${esc(bodyPreview(task.body))}</span>
        <span class="tw-counts">${notes.length ? `${notes.length} note${notes.length === 1 ? '' : 's'}` : 'No notes'} <b></b> ${checkboxProgress(task) || 'No checklist'}</span>
      </button>
      <button type="button" class="tw-icon-button tw-chevron${expanded ? ' is-open' : ''}" data-action="toggle-card" aria-label="${expanded ? 'Collapse task' : 'Expand task'}">${todoIcon('chevron')}</button>
    </header>
    ${expanded ? taskDetail(task, detailTab) : ''}
  </article>`;
}

function taskDetail(task, activeTab) {
  const notes = task.notes || [];
  const checklist = task.checklist || [];
  const tabs = `<nav class="tw-detail-tabs" aria-label="Task detail sections">${TASK_DETAIL_TABS.map(([id, label]) => `<button type="button" data-action="task-tab" data-detail-tab="${id}" class="${activeTab === id ? 'active' : ''}">${label}</button>`).join('')}</nav>`;
  let panel = '';
  if (activeTab === 'notes') {
    panel = `<section class="tw-detail-panel"><div class="tw-section-head"><h4>Notes</h4><span>${notes.length}</span></div><div class="tw-entry-list">${notes.map(note => `<div class="tw-note"><span>${esc(note.text)}</span>${todoIconButton('delete-note', 'close', 'Delete note', `data-entry-id="${escapeAttr(note.id)}"`)}</div>`).join('') || '<p class="tw-empty-inline">No notes yet.</p>'}</div><div class="tw-add-row"><textarea data-note-input placeholder="Add a note..." rows="1"></textarea><button class="btn-secondary" data-action="add-note">Add</button></div></section>`;
  } else if (activeTab === 'checklist') {
    panel = `<section class="tw-detail-panel"><div class="tw-section-head"><h4>Checklist</h4><span>${checkboxProgress(task) || '0 items'}</span></div><div class="tw-entry-list">${checklist.map(entry => `<div class="tw-check${entry.done ? ' is-done' : ''}"><input type="checkbox" data-action="toggle-check" data-entry-id="${escapeAttr(entry.id)}" ${entry.done ? 'checked' : ''}><span>${esc(entry.text)}</span>${todoIconButton('delete-check', 'close', 'Delete checklist item', `data-entry-id="${escapeAttr(entry.id)}"`)}</div>`).join('') || '<p class="tw-empty-inline">No checklist items yet.</p>'}</div><div class="tw-add-row"><input data-check-input placeholder="Add checklist item"><button class="btn-secondary" data-action="add-check">Add</button></div></section>`;
  } else if (activeTab === 'history') {
    panel = `<section class="tw-detail-panel"><div class="tw-section-head"><h4>History</h4></div><ul class="tw-history">${(task.history || []).map(entry => `<li>${esc(entry)}</li>`).join('') || '<li>No history yet.</li>'}</ul></section>`;
  } else if (activeTab === 'raw') {
    panel = `<section class="tw-detail-panel"><div class="tw-section-head"><h4>Raw</h4></div><pre class="tw-raw">${esc(itemMarkdown(task))}</pre></section>`;
  } else {
    panel = `<section class="tw-detail-panel tw-detail-preview"><div class="tw-section-head"><h4>Preview</h4></div>${task.goal ? `<p class="tw-full-goal">${esc(task.goal)}</p>` : ''}<p>${esc(task.body || 'No description yet.')}</p></section>`;
  }
  return `<div class="tw-task-detail">${tabs}${panel}</div>`;
}

function projectCard(project, tasks, archived, open, allProjects) {
  return `<article class="tw-project${open ? ' is-open' : ''}" data-project-id="${escapeAttr(project.id)}"><header>
    <button class="tw-project-toggle" data-action="toggle-project" aria-expanded="${open}"><strong>${esc(project.name)}</strong><span>${tasks.length} task${tasks.length === 1 ? '' : 's'}</span></button>
    ${archived ? '<button class="btn-primary" data-action="unarchive-project">Unarchive</button>' : '<button class="btn-secondary" data-action="archive-project">Archive</button>'}
  </header>${open ? `<div class="tw-project-body">${archived ? tasks.map(task => `<div class="tw-archived-task">${esc(task.title)}</div>`).join('') || '<p class="tw-empty-inline">No tasks.</p>' : `<div class="tw-project-actions"><input data-project-name value="${escapeAttr(project.name)}"><button class="btn-secondary" data-action="rename-project">Rename</button><button class="btn-secondary" data-action="delete-project">Delete empty</button></div>${tasks.map(task => `<div class="tw-project-task"><span>${esc(task.title)}</span><select data-action="move-task" data-task-id="${escapeAttr(task.id)}">${allProjects.map(candidate => `<option value="${escapeAttr(candidate.id)}" ${candidate.id === task.project ? 'selected' : ''}>${esc(candidate.name)}</option>`).join('')}</select></div>`).join('') || '<p class="tw-empty-inline">No tasks.</p>'}`}</div>` : ''}</article>`;
}

export function mountTodosWorkspace(root, { platform = 'desktop', contextLabel = () => 'Current workspace context', storePath = () => '/workspace/.cam/worklog' } = {}) {
  const controller = createTodosController();
  const state = { tab: 'tasks', query: '', project: 'all', status: 'all', sort: 'updated', openTasks: new Set(), openProjects: new Set(), detailTabs: new Map(), compose: false, filtersOpen: false };
  root.classList.add("todos-workspace", `todos-${platform}`);

  function sorted(tasks) {
    return [...tasks].sort((a, b) => state.sort === 'title' ? a.title.localeCompare(b.title) : state.sort === 'priority' ? String(a.priority || 'P9').localeCompare(String(b.priority || 'P9')) : b.updatedAt - a.updatedAt);
  }
  function render() {
    const activeProjects = controller.activeProjects();
    const selectedProjectOptions = activeProjects.map(project => `<option value="${escapeAttr(project.id)}" ${state.project === project.id ? 'selected' : ''}>${esc(project.name)}</option>`).join('');
    let content = '';
    if (state.tab === 'tasks') {
      const tasks = sorted(controller.tasks(state));
      content = `${state.compose ? `<form class="tw-composer"><input name="title" placeholder="Task title" required><input name="goal" placeholder="Goal (optional)"><textarea name="body" placeholder="Description" rows="3"></textarea><select name="project">${activeProjects.map(project => `<option value="${escapeAttr(project.id)}">${esc(project.name)}</option>`).join('')}</select><div><button class="btn-primary">Create task</button><button type="button" class="btn-secondary" data-action="cancel-compose">Cancel</button></div></form>` : ''}<div class="tw-task-list">${tasks.map(task => taskCard(task, state.openTasks.has(task.id), state.detailTabs.get(task.id) || 'preview')).join('') || '<div class="empty-state">No active tasks.</div>'}</div>`;
    } else if (state.tab === 'projects') {
      content = `<form class="tw-project-create"><input name="project" placeholder="New project name" required><button class="btn-primary">Add project</button></form><div class="tw-project-list">${activeProjects.map(project => projectCard(project, controller.projectTasks(project.id), false, state.openProjects.has(project.id), activeProjects)).join('')}</div>`;
    } else {
      const archived = controller.archivedProjects();
      content = `<div class="tw-project-list">${archived.map(project => projectCard(project, controller.archivedProjectTasks(project.id), true, state.openProjects.has(project.id), activeProjects)).join('') || '<div class="empty-state">No archived projects.</div>'}</div>`;
    }
    root.innerHTML = `<header class="tw-header"><div><h2>Todos</h2><p>Structured work items, project mapping, and archived project history.</p></div><div class="tw-store-actions"><button type="button" class="tw-icon-button" data-action="refresh" aria-label="Refresh" title="Refresh">${todoIcon('refresh')}</button></div></header>
      <nav class="tw-tabs" aria-label="Todos tabs">${['tasks', 'projects', 'archive'].map(tab => `<button data-action="tab" data-tab="${tab}" class="${state.tab === tab ? 'active' : ''}">${tab[0].toUpperCase() + tab.slice(1)}</button>`).join('')}</nav>
      ${state.tab === 'tasks' ? `<section class="tw-toolbar"><div class="tw-panel-heading"><h3>Tasks</h3></div><div class="tw-toolbar-actions"><button class="btn-primary" data-action="new-task">New task</button><button class="btn-secondary tw-filter-trigger" data-action="toggle-filters" aria-expanded="${state.filtersOpen}">Filters</button></div><div class="tw-filter-controls${state.filtersOpen ? ' is-open' : ''}"><input data-filter="query" value="${escapeAttr(state.query)}" placeholder="Search tasks"><select data-filter="project"><option value="all">All projects</option>${selectedProjectOptions}</select><select data-filter="status">${['all','open','active','done'].map(value => `<option value="${value}" ${state.status === value ? 'selected' : ''}>${value[0].toUpperCase() + value.slice(1)}</option>`).join('')}</select><select data-filter="sort">${['updated','priority','title'].map(value => `<option value="${value}" ${state.sort === value ? 'selected' : ''}>${value[0].toUpperCase() + value.slice(1)}</option>`).join('')}</select></div></section>` : ''}
      <main class="tw-content">${content}</main>`;
  }
  function closest(target, selector) { return target.closest(selector); }
  root.addEventListener('input', event => { const filter = event.target.dataset.filter; if (filter) { state[filter] = event.target.value; render(); } });
  root.addEventListener('change', event => {
    const action = event.target.dataset.action; const card = closest(event.target, '[data-task-id]');
    if (action === 'toggle-task' && card) { controller.toggleTask(card.dataset.taskId, event.target.checked); render(); }
    if (action === 'toggle-check' && card) { controller.toggleChecklistItem(card.dataset.taskId, event.target.dataset.entryId, event.target.checked); render(); }
    if (action === 'move-task') { controller.moveTask(event.target.dataset.taskId, event.target.value); render(); }
  });
  root.addEventListener('submit', event => { event.preventDefault(); const form = event.target; if (form.classList.contains('tw-composer')) { const data = new FormData(form); controller.createTask({ title: data.get('title'), goal: data.get('goal'), body: data.get('body'), project: data.get('project') }); state.compose = false; render(); } else if (form.classList.contains('tw-project-create')) { const data = new FormData(form); controller.addProject(data.get('project')); render(); } });
  root.addEventListener('click', event => {
    const button = closest(event.target, '[data-action]'); if (!button) return; const action = button.dataset.action; const card = closest(button, '[data-task-id]'); const project = closest(button, '[data-project-id]');
    if (action === 'tab') { state.tab = button.dataset.tab; state.compose = false; render(); return; }
    if (action === 'new-task') { state.compose = true; render(); return; }
    if (action === 'toggle-filters') { state.filtersOpen = !state.filtersOpen; render(); return; }
    if (action === 'cancel-compose') { state.compose = false; render(); return; }
    if (action === 'toggle-card' && card) { state.openTasks.has(card.dataset.taskId) ? state.openTasks.delete(card.dataset.taskId) : state.openTasks.add(card.dataset.taskId); render(); return; }
    if (action === 'task-tab' && card) { state.detailTabs.set(card.dataset.taskId, button.dataset.detailTab); render(); return; }
    if (action === 'toggle-project' && project) { state.openProjects.has(project.dataset.projectId) ? state.openProjects.delete(project.dataset.projectId) : state.openProjects.add(project.dataset.projectId); render(); return; }
    if (action === 'archive-project' && project) { controller.archiveProject(project.dataset.projectId); render(); return; }
    if (action === 'unarchive-project' && project) { controller.unarchiveProject(project.dataset.projectId); render(); return; }
    if (action === 'delete-project' && project) { controller.deleteEmptyProject(project.dataset.projectId); render(); return; }
    if (action === 'rename-project' && project) { controller.renameProject(project.dataset.projectId, project.querySelector('[data-project-name]').value); render(); return; }
    if (action === 'add-note' && card) { controller.addNote(card.dataset.taskId, card.querySelector('[data-note-input]').value); render(); return; }
    if (action === 'add-check' && card) { controller.addChecklistItem(card.dataset.taskId, card.querySelector('[data-check-input]').value); render(); return; }
    if (action === 'delete-note' && card) { controller.deleteNote(card.dataset.taskId, button.dataset.entryId); render(); return; }
    if (action === 'delete-check' && card) { controller.deleteChecklistItem(card.dataset.taskId, button.dataset.entryId); render(); return; }
    if (action === 'refresh') { render(); }
  });
  render();
  return () => { root.innerHTML = ''; };
}
