/**
 * Local node registry for Mobile Direct (phone SSH client).
 * Hub API is not used; contexts persist in localStorage until phone SSH camc lands.
 */

const CONTEXTS_KEY = 'cam_mobile_direct_contexts';

function loadRaw() {
  try {
    const rows = JSON.parse(localStorage.getItem(CONTEXTS_KEY) || '[]');
    return Array.isArray(rows) ? rows : [];
  } catch {
    return [];
  }
}

function saveRaw(rows) {
  localStorage.setItem(CONTEXTS_KEY, JSON.stringify(rows));
}

function newId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `ctx-${Date.now().toString(36)}`;
}

function normalizeCreateBody(body) {
  const machine = {
    host: body.host || '',
    user: body.user || '',
    port: body.port || 22,
    auth_method: body.auth_method || (body.key_file ? 'key' : 'agent'),
    key_file: body.key_file || '',
    env_setup: body.env_setup || '',
  };
  return {
    id: newId(),
    name: body.name,
    path: body.path,
    machine,
    transport_type: 'ssh',
  };
}

function applyUpdate(ctx, body) {
  const next = { ...ctx };
  if (body.path !== undefined) next.path = body.path;
  if (body.env_setup !== undefined) {
    next.machine = { ...(next.machine || {}), env_setup: body.env_setup };
  }
  const machineFields = ['host', 'user', 'port', 'auth_method', 'key_file'];
  for (const k of machineFields) {
    if (body[k] !== undefined) {
      next.machine = { ...(next.machine || {}), [k]: body[k] };
    }
  }
  return next;
}

export function createPhoneDirectApi(baseApi) {
  const local = {
    listContexts() {
      const contexts = loadRaw();
      return Promise.resolve({ contexts, count: contexts.length });
    },

    createContext(body) {
      const rows = loadRaw();
      if (rows.some(c => c.name === body.name)) {
        return Promise.reject(new Error(`Context "${body.name}" already exists`));
      }
      const ctx = normalizeCreateBody(body);
      rows.push(ctx);
      saveRaw(rows);
      return Promise.resolve({ ok: true, context: ctx });
    },

    updateContext(nameOrId, body) {
      const rows = loadRaw();
      const idx = rows.findIndex(c => c.name === nameOrId || c.id === nameOrId);
      if (idx < 0) return Promise.reject(new Error('Context not found'));
      rows[idx] = applyUpdate(rows[idx], body);
      saveRaw(rows);
      return Promise.resolve({ ok: true, context: rows[idx] });
    },

    deleteContext(nameOrId) {
      const rows = loadRaw();
      const next = rows.filter(c => c.name !== nameOrId && c.id !== nameOrId);
      if (next.length === rows.length) return Promise.reject(new Error('Context not found'));
      saveRaw(next);
      return Promise.resolve({ ok: true });
    },

    syncContext() {
      return Promise.reject(new Error('Sync Host requires a workstation Hub (Relay mode)'));
    },

    sshConfigHosts() {
      return Promise.resolve({ hosts: [], message: 'SSH config import is not available on phone Direct yet' });
    },
  };

  return new Proxy(baseApi, {
    get(target, prop) {
      if (prop === 'mode') return 'direct';
      if (prop in local) return local[prop];
      const val = target[prop];
      return typeof val === 'function' ? val.bind(target) : val;
    },
  });
}
