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
  window.addEventListener('message', (e) => {
    const d = e && e.data;
    if (!d || d.extBridge !== true || !d.response) return;
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
  const m = /^\/ext\/([^/]+)\//.exec(location.pathname);
  return { call, name: m ? m[1] : '' };
})();
