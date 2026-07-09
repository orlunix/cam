/**
 * Shared worklog / todos data model (Desktop + Mobile V0).
 * localStorage today; Hub todocli proxy replaces read/write later.
 */

export const STORAGE_KEY = 'cam_desktop_worklog_v0';
export const PROJECTS_KEY = 'cam_desktop_worklog_projects_v0';
export const WORKLOG_TABS = ['tasks', 'projects', 'archive'];
export const DETAIL_TABS = ['preview', 'raw', 'notes', 'checklist', 'history'];

export const seedItems = [
  {
    id: 'todo-20260610-layout',
    kind: 'task',
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
    kind: 'task',
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
    kind: 'task',
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
    id: 'note-20260610-mobile',
    kind: 'note',
    type: 'note',
    title: 'Mobile interaction notes',
    status: 'open',
    project: 'inbox',
    priority: '',
    tags: ['mobile'],
    updatedAt: Date.now() - 3 * 60 * 60 * 1000,
    goal: '',
    body: 'Document bottom-sheet filters and full-screen item detail behavior for the mobile version.',
    notes: [],
    checklist: [],
    history: ['captured in Inbox'],
  },
  {
    id: 'todo-20260610-personal',
    kind: 'task',
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

export function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
  }[ch]));
}

export function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, '&#39;');
}

export function normalizeItem(item) {
  const kind = item.kind || item.type || 'task';
  return {
    ...item,
    id: item.id || `todo-${Date.now()}`,
    kind,
    type: kind,
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

export function readItems() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return seedItems.map(normalizeItem);
    const parsed = JSON.parse(raw);
    return (Array.isArray(parsed) && parsed.length ? parsed : seedItems).map(normalizeItem);
  } catch {
    return seedItems.map(normalizeItem);
  }
}

export function saveItems(items) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(items)); } catch {}
}

export function readStoredProjects() {
  try {
    const parsed = JSON.parse(localStorage.getItem(PROJECTS_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.map(x => String(x || '').trim()).filter(Boolean) : [];
  } catch {
    return [];
  }
}

export function saveStoredProjects(projects) {
  try { localStorage.setItem(PROJECTS_KEY, JSON.stringify(projects)); } catch {}
}

export function relativeTime(ms) {
  const delta = Math.max(0, Date.now() - Number(ms || 0));
  const mins = Math.floor(delta / 60000);
  if (mins < 1) return 'now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** CAM-DESK-TODOS-011 tab membership from kind + status. */
export function itemMatchesTab(item, tab) {
  if (tab === 'projects') return false;
  if (tab === 'archive') return false; // Archive is project-level in the shared controller.
  if (item.status === 'archived') return false;
  const kind = item.kind || item.type || 'task';
  if (tab === 'tasks') {
    return kind === 'task';
  }
  return false;
}

export function itemMarkdown(item) {
  const tags = (item.tags || []).map(t => `"${String(t).replace(/"/g, '\\"')}"`).join(', ');
  return `---\nid: ${item.id}\nkind: ${item.kind || item.type}\ntitle: ${item.title}\ngoal: ${item.goal || ''}\nstatus: ${item.status}\nproject: ${item.project}\npriority: ${item.priority || ''}\ntags: [${tags}]\n---\n\n${item.body || ''}`;
}

export function checkboxProgress(item) {
  const list = item.checklist || [];
  if (!list.length) return '';
  const done = list.filter(x => x.done).length;
  return `${done}/${list.length}`;
}

export function projectsFromItems(items, storedProjects) {
  return Array.from(new Set([
    'inbox',
    ...(storedProjects || []),
    ...items.map(item => item.project || 'inbox'),
  ].map(p => String(p || '').trim()).filter(Boolean))).sort();
}

export function storePathForContexts(contexts) {
  const first = (contexts || []).find(ctx => ctx && ctx.path) || null;
  return `${first && first.path ? first.path : '/workspace'}/.cam/todos`;
}

export function contextLabelFromContexts(contexts) {
  const first = (contexts || []).find(ctx => ctx && ctx.machine && ctx.machine.type === 'ssh') || (contexts || [])[0];
  if (!first) return 'Current workspace context';
  const machine = first.machine || {};
  const host = machine.host || first.name || 'local';
  const user = machine.user ? `${machine.user}@` : '';
  return `${first.name || host} · ${user}${host}`;
}

/** Read every Markdown worklog file below a context-relative directory. */
export async function readWorklogMarkdown(api, contextId, root = '.cam/todos') {
  if (!api || !contextId) throw new Error('Workspace file API unavailable');
  const files = [];
  async function walk(path) {
    const listing = await api.listFiles(contextId, path);
    for (const entry of listing.entries || []) {
      const child = `${path}/${entry.name}`.replace(/^\//, '');
      if (entry.type === 'dir' || entry.type === 'directory') await walk(child);
      else if (entry.type === 'file' && /\.md$/i.test(entry.name || '')) files.push({ path: child, entry });
    }
  }
  await walk(root);
  const loaded = await Promise.all(files.map(async ({ path, entry }) => {
    const file = await api.readFile(contextId, path);
    return parseWorklogMarkdown(path, file.content || '', entry);
  }));
  return loaded.filter(Boolean);
}

export function parseWorklogMarkdown(path, content, entry = {}) {
  const match = String(content || '').match(/^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/);
  if (!match) return null;
  const meta = {};
  for (const line of match[1].split(/\r?\n/)) {
    const found = line.match(/^([\w-]+):\s*(.*)$/);
    if (found) meta[found[1]] = found[2].replace(/^['"]|['"]$/g, '').trim();
  }
  const tags = (meta.tags || '').replace(/^\[|\]$/g, '').split(',').map(x => x.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean);
  return normalizeItem({
    id: meta.id || `md-${path.replace(/[^\w]+/g, '-')}`,
    kind: meta.kind || meta.type || 'task', type: meta.kind || meta.type || 'task',
    title: meta.title || path.split('/').pop().replace(/\.md$/i, ''), goal: meta.goal || '',
    status: meta.status || 'open', project: meta.project || 'inbox', priority: meta.priority || '', tags,
    body: match[2].trim(), notes: [], checklist: [], history: [`loaded from ${path}`],
    updatedAt: Number(entry.mtime || entry.modified || entry.updated_at || Date.now()),
  });
}
