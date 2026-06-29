/** Per-host display metadata (node name, enabled) — localStorage on device. */

const META_KEY = 'cam_node_host_meta';

function loadMap() {
  try {
    const raw = JSON.parse(localStorage.getItem(META_KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function saveMap(map) {
  try {
    localStorage.setItem(META_KEY, JSON.stringify(map));
    window.dispatchEvent(new CustomEvent('cam-node-host-meta-changed'));
  } catch {}
}

export function normalizePort(port, isSSH = false) {
  const n = Number.parseInt(port, 10);
  if (Number.isFinite(n) && n >= 1 && n <= 65535) return n;
  return isSSH ? 22 : null;
}

export function hostKeyForMachine({ type, host, user, port }) {
  const h = host || 'local';
  const isSSH = !!(type === 'ssh' || (h && h !== 'local'));
  if (!isSSH) return 'local';
  return `${user || ''}@${h}:${normalizePort(port, true)}`;
}

export function hostKeyFromAgent(agent) {
  if (!agent) return 'local';
  const host = agent.machine_host || 'local';
  return hostKeyForMachine({
    type: agent.machine_type,
    host,
    user: agent.machine_user || '',
    port: agent.machine_port,
  });
}

export function hostKeyFromContext(ctx) {
  const m = (ctx && ctx.machine) || {};
  const host = m.host || 'local';
  return hostKeyForMachine({
    type: m.type,
    host,
    user: m.user || '',
    port: m.port,
  });
}

export function isContextEnabled(ctx) {
  return isHostEnabled(hostKeyFromContext(ctx));
}

export function filterContextsOnEnabledHosts(contexts) {
  return (contexts || []).filter(isContextEnabled);
}

export function getHostMeta(hostKey) {
  const map = loadMap();
  const row = map[hostKey];
  if (!row || typeof row !== 'object') {
    return { nodeName: '', enabled: true };
  }
  return {
    nodeName: typeof row.nodeName === 'string' ? row.nodeName : '',
    enabled: row.enabled !== false,
  };
}

export function setHostMeta(hostKey, patch) {
  if (!hostKey) return;
  const map = loadMap();
  const prev = getHostMeta(hostKey);
  map[hostKey] = {
    nodeName: patch.nodeName != null ? String(patch.nodeName).trim() : prev.nodeName,
    enabled: patch.enabled != null ? !!patch.enabled : prev.enabled,
  };
  saveMap(map);
}

export function removeHostMeta(hostKey) {
  if (!hostKey) return;
  const map = loadMap();
  if (!map[hostKey]) return;
  delete map[hostKey];
  saveMap(map);
}

export function toggleHostEnabled(hostKey) {
  const meta = getHostMeta(hostKey);
  setHostMeta(hostKey, { enabled: !meta.enabled });
  return !meta.enabled;
}

export function isHostEnabled(hostKey) {
  return getHostMeta(hostKey).enabled !== false;
}

export function isAgentHostEnabled(agent) {
  return isHostEnabled(hostKeyFromAgent(agent));
}

export function getNodeDisplayName(hostKey, fallback = '') {
  const name = getHostMeta(hostKey).nodeName.trim();
  return name || fallback || hostKey;
}

export function filterAgentsOnEnabledHosts(agents) {
  return (agents || []).filter(isAgentHostEnabled);
}

export function machineLabelForAgent(agent) {
  const host = agent?.machine_host || 'local';
  const fb = host === 'local' ? 'local' : host.split('.')[0];
  return getNodeDisplayName(hostKeyFromAgent(agent), fb);
}
