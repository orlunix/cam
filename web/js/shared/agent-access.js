/** Local last-opened timestamps for agent list sort (per device). */

const ACCESS_KEY = 'cam_agent_accessed';

function loadAccessMap() {
  try {
    const raw = JSON.parse(localStorage.getItem(ACCESS_KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function saveAccessMap(map) {
  try {
    localStorage.setItem(ACCESS_KEY, JSON.stringify(map));
  } catch { /* quota / private mode */ }
}

function resolveAccessKey(agentId, map) {
  if (!agentId) return null;
  if (map[agentId]) return agentId;
  const id = String(agentId);
  for (const key of Object.keys(map)) {
    if (id.startsWith(key) || key.startsWith(id)) return key;
  }
  return null;
}

export function recordAgentAccess(agentId) {
  if (!agentId) return;
  const map = loadAccessMap();
  map[String(agentId)] = new Date().toISOString();
  saveAccessMap(map);
}

export function getAgentAccessedAt(agentId) {
  const map = loadAccessMap();
  const key = resolveAccessKey(agentId, map);
  return key ? map[key] : null;
}

export function getAgentAccessMap() {
  return loadAccessMap();
}

export function agentAccessTimestamp(agentId, map = null) {
  const m = map || loadAccessMap();
  const key = resolveAccessKey(agentId, m);
  if (!key || !m[key]) return 0;
  const ts = new Date(m[key]).getTime();
  return Number.isFinite(ts) ? ts : 0;
}
