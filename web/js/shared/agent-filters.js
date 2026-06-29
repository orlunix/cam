/** Persisted agent list filter + sort preferences (mobile). */

const FILTERS_KEY = 'cam_agent_filters';

export const AGENT_SORT_OPTIONS = [
  { value: 'accessed', label: 'Opened' },
  { value: 'name', label: 'Name' },
  { value: 'created', label: 'Created' },
];

const DEFAULTS = {
  status: '',
  tool: '',
  machine: '',
  sort: 'accessed',
};

export function loadAgentFilters() {
  try {
    const raw = JSON.parse(localStorage.getItem(FILTERS_KEY) || '{}');
    return { ...DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveAgentFilters(filters) {
  if (!filters) return;
  try {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({
      status: filters.status || '',
      tool: filters.tool || '',
      machine: filters.machine || '',
      sort: filters.sort || DEFAULTS.sort,
    }));
  } catch { /* noop */ }
}

export function sortAgents(agents, sortKey, accessMap = null) {
  const rows = [...(agents || [])];
  const key = sortKey || 'accessed';
  const map = accessMap || {};

  const nameOf = (a) => String(a?.task_name || a?.task?.name || a?.id || '').toLowerCase();
  const createdOf = (a) => {
    const ts = new Date(a?.started_at || a?.created_at || 0).getTime();
    return Number.isFinite(ts) ? ts : 0;
  };
  const accessOf = (a) => {
    const id = a?.id;
    if (!id) return 0;
    if (map[id]) {
      const ts = new Date(map[id]).getTime();
      return Number.isFinite(ts) ? ts : 0;
    }
    for (const [k, v] of Object.entries(map)) {
      if (id.startsWith(k) || k.startsWith(id)) {
        const ts = new Date(v).getTime();
        return Number.isFinite(ts) ? ts : 0;
      }
    }
    return 0;
  };

  rows.sort((a, b) => {
    if (key === 'name') return nameOf(a).localeCompare(nameOf(b));
    if (key === 'created') return createdOf(b) - createdOf(a);
    return accessOf(b) - accessOf(a) || createdOf(b) - createdOf(a);
  });
  return rows;
}
