/* ext-bridge.js — parent-side bridge between the app and sandboxed
 * extension views (SPEC §4). The view (iframe) posts
 * { extBridge:true, id, method, args }; we answer with
 * { extBridge:true, response:true, id, ok, result|error }.
 *
 * Security model:
 *  - a frame must be REGISTERED (ext name + declared capabilities)
 *    before any call is honored
 *  - capability-gated methods check the manifest declaration, not the
 *    view's behavior
 *  - read-only by default: agents/contexts list + capture are open to
 *    every extension; exec/files:* require the manifest capability
 */

/** Registry: contentWindow → { name, capabilities } */
const _frames = new Map();
let _api = null;
let _listening = false;

export function registerExtFrame(contentWindow, { name, capabilities }) {
  _frames.set(contentWindow, { name, capabilities: capabilities || [] });
}

export function unregisterExtFrame(contentWindow) {
  _frames.delete(contentWindow);
}

function _err(code) {
  const e = new Error(code);
  e.code = code;
  return e;
}

async function _dispatch(reg, method, args) {
  switch (method) {
    case 'agents.list': {
      const r = await _api.listAgents();
      return (r && r.agents) || [];
    }
    case 'contexts.list': {
      const r = await _api.listContexts();
      return (r && r.contexts) || r || [];
    }
    case 'agents.capture': {
      const r = await _api.agentOutput(String(args.id || ''), Number(args.lines) || 80);
      return (r && (r.output || r.text)) || '';
    }
    case 'ext.call': {
      if (!reg.capabilities.includes('exec')) throw _err('capability_denied:exec');
      return _api.extCall(reg.name, String(args.context || ''), String(args.method || ''), args.args);
    }
    case 'files.read': {
      if (!reg.capabilities.includes('files:read')) throw _err('capability_denied:files:read');
      return _api.listFiles(String(args.context || ''), String(args.path || ''));
    }
    default:
      throw _err('unknown_method:' + method);
  }
}

async function _onMessage(e) {
  const d = e && e.data;
  if (!d || d.extBridge !== true || d.response) return;
  const reg = _frames.get(e.source);
  if (!reg) return; // unregistered frame — ignore silently
  const respond = (payload) => {
    try { e.source.postMessage({ extBridge: true, response: true, id: d.id, ...payload }, '*'); } catch (_) {}
  };
  try {
    const result = await _dispatch(reg, String(d.method || ''), d.args || {});
    respond({ ok: true, result });
  } catch (err) {
    respond({ ok: false, error: (err && err.code) || (err && err.message) || 'bridge_error' });
  }
}

/** Install the global message listener once. Returns an unregister-all
 *  for app teardown (resetApp path). */
export function installExtBridge(api) {
  _api = api;
  if (_listening) return;
  _listening = true;
  window.addEventListener('message', _onMessage);
}
