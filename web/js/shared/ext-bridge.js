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

/** Registry: contentWindow → { name, capabilities, context } */
const _frames = new Map();
let _api = null;
let _listening = false;

export function registerExtFrame(contentWindow, { name, capabilities, context }) {
  _frames.set(contentWindow, {
    name,
    capabilities: capabilities || [],
    // Bound context for per-agent mounts (SPEC v2 mounts: [agent]):
    // { agentId, contextName } — surfaced via the app.context call.
    context: context || null,
  });
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
    // Bound context for per-agent mounts (opened from an agent page).
    case 'app.context': {
      return reg.context || {};
    }
    // Agent's cron jobs / loops (read-only; needs an agent binding).
    case 'agents.cronJobs': {
      if (!reg.context || !reg.context.agentId) throw _err('no_agent_binding');
      return _api.agentCronJobs(reg.context.agentId);
    }
    // Agent-workspace file access for per-agent mounts (e.g. previewing
    // workflow yaml files). Read-only; requires an agent binding.
    case 'agents.workspaceList': {
      if (!reg.context || !reg.context.agentId) throw _err('no_agent_binding');
      return _api.agentListWorkspaceFiles(reg.context.agentId, String(args.path || ''));
    }
    case 'agents.workspaceRead': {
      if (!reg.context || !reg.context.agentId) throw _err('no_agent_binding');
      return _api.agentReadWorkspaceFile(reg.context.agentId, String(args.path || ''));
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
