const STORAGE_KEYS = {
  todos: 'cam_mobile_todos_context_id',
  skills: 'cam_mobile_skills_context_id',
};

export function isWorkspaceNode(context) {
  const machine = (context && context.machine) || {};
  return machine.type === 'ssh' || !!machine.host;
}

export function workspaceContextId(context) {
  return String((context && (context.id || context.name)) || '');
}

export function selectedWorkspaceContext(contexts = [], scope = 'todos') {
  const nodes = contexts.filter(isWorkspaceNode);
  let selected = '';
  try { selected = localStorage.getItem(STORAGE_KEYS[scope] || STORAGE_KEYS.todos) || ''; } catch {}
  return nodes.find(context => workspaceContextId(context) === selected || context.name === selected) || nodes[0] || null;
}

export function saveWorkspaceContext(context, scope = 'todos') {
  const id = typeof context === 'string' ? context : workspaceContextId(context);
  const key = STORAGE_KEYS[scope] || STORAGE_KEYS.todos;
  try {
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  } catch {}
  return id;
}

export function workspaceNodeLabel(context) {
  if (!context) return 'No node selected';
  const machine = context.machine || {};
  const user = machine.user ? `${machine.user}@` : '';
  return `${context.name || machine.host || 'Workspace'} · ${user}${machine.host || 'ssh'}`;
}
