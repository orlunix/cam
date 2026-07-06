/**
 * Shared Todos domain controller.
 *
 * Platform renderers consume this module instead of owning divergent project
 * archive and task mutation rules. Storage remains local for V0; the public
 * controller shape is intentionally independent of localStorage so a Hub
 * adapter can replace it later.
 */
import {
  readItems, saveItems, readStoredProjects, saveStoredProjects, normalizeItem,
} from './worklog-core.js';

export const PROJECT_META_KEY = 'cam_desktop_worklog_project_meta_v1';
export const TODO_TABS = ['tasks', 'projects', 'archive'];

function idFor(name) { return String(name || 'inbox').trim() || 'inbox'; }
function now() { return Date.now(); }

function readProjectMeta() {
  try {
    const parsed = JSON.parse(localStorage.getItem(PROJECT_META_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}

function normalizeProject(project) {
  const id = idFor(project && (project.id || project.name));
  return {
    id,
    name: String(project && project.name || id),
    status: project && project.status === 'archived' ? 'archived' : 'active',
    archivedAt: Number(project && project.archivedAt || 0),
  };
}

export function createTodosController() {
  let items = readItems().map(normalizeItem);
  let projectMeta = readProjectMeta().map(normalizeProject);
  let storedProjects = readStoredProjects().map(idFor);

  function projectNames() {
    return [...new Set([
      'inbox', ...storedProjects, ...items.map(item => idFor(item.project)),
      ...projectMeta.map(project => project.id),
    ])];
  }

  function projectById(id) {
    const key = idFor(id);
    return projectMeta.find(project => project.id === key)
      || normalizeProject({ id: key, name: key });
  }

  function projects(status = null) {
    return projectNames().map(projectById)
      .filter(project => !status || project.status === status)
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  function save() {
    saveItems(items);
    saveStoredProjects(storedProjects);
    try { localStorage.setItem(PROJECT_META_KEY, JSON.stringify(projectMeta)); } catch {}
  }

  function upsertProject(project) {
    const next = normalizeProject(project);
    projectMeta = [...projectMeta.filter(row => row.id !== next.id), next];
    if (!storedProjects.includes(next.id)) storedProjects.push(next.id);
    save();
    return next;
  }

  function updateTask(id, mutate, history) {
    let changed = null;
    items = items.map(item => {
      if (item.id !== id) return item;
      const next = normalizeItem(mutate({ ...item }));
      next.updatedAt = now();
      if (history) next.history = [...(next.history || []), history];
      changed = next;
      return next;
    });
    if (changed) save();
    return changed;
  }

  return {
    tabs: TODO_TABS,
    projects,
    activeProjects: () => projects('active'),
    archivedProjects: () => projects('archived'),
    allItems: () => [...items],
    replaceItems(nextItems) { items = (nextItems || []).map(normalizeItem); storedProjects = [...new Set([...storedProjects, ...items.map(item => idFor(item.project))])]; save(); return items.length; },
    task(id) { return items.find(item => item.id === id) || null; },
    tasks(filters = {}) {
      const query = String(filters.query || '').trim().toLowerCase();
      const project = filters.project || 'all';
      const status = filters.status || 'all';
      return items.filter(item => {
        const p = projectById(item.project);
        if (p.status !== 'active' || (item.kind || item.type) !== 'task') return false;
        if (project !== 'all' && item.project !== project) return false;
        if (status !== 'all' && item.status !== status) return false;
        return !query || [item.title, item.goal, item.body, item.project, ...(item.tags || [])]
          .join(' ').toLowerCase().includes(query);
      });
    },
    archivedProjectTasks(projectId) {
      return items.filter(item => item.project === projectId && (item.kind || item.type) === 'task');
    },
    projectTasks(projectId) { return items.filter(item => item.project === projectId && (item.kind || item.type) === 'task'); },
    addProject(name) { return upsertProject({ id: idFor(name), name, status: 'active' }); },
    renameProject(id, name) {
      const from = projectById(id);
      const to = idFor(name);
      if (from.id === to) return from;
      items = items.map(item => item.project === from.id
        ? { ...item, project: to, updatedAt: now(), history: [...(item.history || []), `moved to project ${to}`] }
        : item);
      projectMeta = projectMeta.filter(project => project.id !== from.id);
      storedProjects = storedProjects.filter(project => project !== from.id);
      return upsertProject({ ...from, id: to, name });
    },
    archiveProject(id) { return upsertProject({ ...projectById(id), status: 'archived', archivedAt: now() }); },
    unarchiveProject(id) { return upsertProject({ ...projectById(id), status: 'active', archivedAt: 0 }); },
    deleteEmptyProject(id) {
      if (items.some(item => item.project === id)) return false;
      projectMeta = projectMeta.filter(project => project.id !== id);
      storedProjects = storedProjects.filter(project => project !== id);
      save();
      return true;
    },
    moveTask(id, projectId) { return updateTask(id, item => ({ ...item, project: idFor(projectId) }), `moved to project ${idFor(projectId)}`); },
    toggleTask(id, done) { return updateTask(id, item => ({ ...item, status: done ? 'done' : 'open' }), done ? 'marked done' : 'reopened'); },
    createTask({ title, goal = '', body = '', project = 'inbox', priority = '', tags = [] }) {
      const name = String(title || '').trim();
      if (!name) return null;
      const task = normalizeItem({ id: `task-${now()}`, kind: 'task', type: 'task', title: name, goal, body, project: idFor(project), priority, tags, status: 'open', notes: [], checklist: [], history: ['created'], updatedAt: now() });
      items = [task, ...items];
      if (!storedProjects.includes(task.project)) storedProjects.push(task.project);
      save();
      return task;
    },
    addNote(id, text) {
      const value = String(text || '').trim();
      return value && updateTask(id, item => ({ ...item, notes: [...(item.notes || []), { id: `note-${now()}`, text: value, updatedAt: now() }] }), 'added note');
    },
    addChecklistItem(id, text) {
      const value = String(text || '').trim();
      return value && updateTask(id, item => ({ ...item, checklist: [...(item.checklist || []), { id: `check-${now()}`, text: value, done: false }] }), 'added checklist item');
    },
    toggleChecklistItem(id, entryId, done) { return updateTask(id, item => ({ ...item, checklist: (item.checklist || []).map(entry => entry.id === entryId ? { ...entry, done: !!done } : entry) }), done ? 'completed checklist item' : 'reopened checklist item'); },
    deleteNote(id, entryId) { return updateTask(id, item => ({ ...item, notes: (item.notes || []).filter(entry => entry.id !== entryId) }), 'deleted note'); },
    deleteChecklistItem(id, entryId) { return updateTask(id, item => ({ ...item, checklist: (item.checklist || []).filter(entry => entry.id !== entryId) }), 'deleted checklist item'); },
  };
}
