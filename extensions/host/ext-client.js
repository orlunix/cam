/* ext-client.js — bridge client for extension views. Served by the hub
 * at /ext/client.js (static, no auth — it carries no secrets; it is
 * the same code for every extension).
 *
 * Usage in a view:  <script src="../client.js"></script>
 *   const agents = await camExt.call('agents.list');
 *   const info   = await camExt.call('ext.call', { context, method, args });
 *
 * Errors reject the promise with Error(error-code). `camExt.name` is
 * the extension name inferred from the view URL (/ext/<name>/…). */
window.camExt = (() => {
  let seq = 0;
  const pending = new Map();
  const handlers = Object.create(null); // event name → [cb] (server push)
  window.addEventListener('message', (e) => {
    const d = e && e.data;
    if (!d || d.extBridge !== true) return;
    if (d.event) {
      // Server-push (e.g. assistant.event): no id, no response.
      const list = handlers[d.event];
      if (list) for (const cb of list) { try { cb(d.payload); } catch (_) {} }
      return;
    }
    if (!d.response) return;
    const p = pending.get(d.id);
    if (p) { pending.delete(d.id); p(d); }
  });
  function call(method, args) {
    const id = ++seq;
    return new Promise((resolve) => {
      pending.set(id, resolve);
      parent.postMessage({ extBridge: true, id, method, args: args || {} }, '*');
    }).then((d) => {
      if (d && d.ok) return d.result;
      throw new Error((d && d.error) || 'bridge_error');
    });
  }
  /** Subscribe to a server-push channel (e.g. 'assistant.event'). */
  function onEvent(name, cb) {
    if (typeof cb !== 'function') return;
    (handlers[name] = handlers[name] || []).push(cb);
  }
  /** Generic hub passthrough (needs the manifest's 'hub:api' capability):
   *  camExt.hubCall('GET', '/api/contexts') — any /api/* endpoint. */
  function hubCall(method, path, body) {
    return call('ext.hubCall', { method, path, body });
  }
  /* The ext's own durable store (hub-side ext-data/<name>/storage.json).
   * Sandboxed views have no localStorage (opaque origin) — use this. */
  function storageGet() { return call('ext.storageGet'); }
  function storageSet(storage) { return call('ext.storageSet', { storage }); }
  const m = /^\/ext\/([^/]+)\//.exec(location.pathname);
  return { call, onEvent, hubCall, storageGet, storageSet, name: m ? m[1] : '' };
})();
