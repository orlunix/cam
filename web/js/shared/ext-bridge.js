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
let _assistantPushWired = false;

/* Server-push rail for the assistant family: main broadcasts every
 * assistant event the moment it exists (assistant:event); we forward it
 * into registered assistant frames as { extBridge, event, payload }.
 * The view renders on receipt — no dependence on iframe timer ticks,
 * which main-thread pressure (busy xterm pages) can delay arbitrarily. */
function _wireAssistantPush() {
  if (_assistantPushWired) return;
  const a = window.CamBridge && window.CamBridge.assistant;
  if (!a || typeof a.onEvent !== 'function') return;
  _assistantPushWired = true;
  a.onEvent((ev) => {
    for (const [win, reg] of _frames) {
      if (reg.name !== 'assistant') continue;
      try { win.postMessage({ extBridge: true, event: 'assistant.event', payload: ev }, '*'); } catch (_) {}
    }
  });
}

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
    // Generic hub passthrough (capability 'hub:api'): the view may call any
    // /api/* endpoint with any verb — the same power the app itself has,
    // which is why the manifest declaration is the consent surface and every
    // call is audit-logged. This is what lets built-in pages (skills/todos)
    // live as self-managed extensions instead of app-shell code.
    case 'ext.hubCall': {
      if (!reg.capabilities.includes('hub:api')) throw _err('capability_denied:hub:api');
      const p = String(args.path || '');
      if (!/^\/api\//.test(p)) throw _err('path_forbidden');
      const m = String(args.method || 'GET').toUpperCase();
      if (!/^(GET|POST|PUT|PATCH|DELETE)$/.test(m)) throw _err('bad_method');
      console.log(`[ext:${reg.name}] ${m} ${p}`);
      return _api.request(m, p, args.body);
    }
    // The calling extension's own durable key/value store (hub-side
    // ext-data/<name>/storage.json). Sandboxed views run in an opaque
    // origin — localStorage throws — so this is their local-state channel.
    // Always allowed for the calling ext (its own data only).
    case 'ext.storageGet': {
      const r = await _api.extStorageGet(reg.name);
      return (r && r.storage) || {};
    }
    case 'ext.storageSet': {
      return _api.extStorageSet(reg.name, (args && args.storage) || {});
    }
    // The calling extension's own attributes (per-ext config, edited in
    // the app via Extensions → Settings). Read-only from views; always allowed.
    case 'ext.config': {
      const r = await _api.extConfigGet(reg.name);
      return (r && r.config) || {};
    }
    case 'files.read': {
      if (!reg.capabilities.includes('files:read')) throw _err('capability_denied:files:read');
      return _api.listFiles(String(args.context || ''), String(args.path || ''));
    }
    // Built-in assistant family (docs/desktop/assistant-design.md):
    // scoped to the assistant extension ONLY — this is not a general
    // ext API. The bridge forwards to the main-process AssistantHost
    // via the preload surface; absent in a plain browser (mobile web).
    case 'assistant.status':
    case 'assistant.start':
    case 'assistant.stop':
    case 'assistant.reset': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a[method.slice('assistant.'.length)]();
    }
    case 'assistant.models': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.models({ apiUrl: args.apiUrl, token: args.token });
    }
    case 'assistant.configure': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.configure({ apiUrl: args.apiUrl, model: args.model, token: args.token, shellUrl: args.shellUrl, shellToken: args.shellToken });
    }
    case 'assistant.send': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.send({ text: String(args.text || '') });
    }
    case 'assistant.poll': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.poll({ since: Number(args.since) || 0 });
    }
    case 'assistant.threads': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.threads();
    }
    case 'assistant.newChat': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.newChat();
    }
    case 'assistant.openThread': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.openThread(String(args.id || ''));
    }
    case 'assistant.deleteThread': {
      if (reg.name !== 'assistant') throw _err('forbidden:' + method);
      const a = window.CamBridge && window.CamBridge.assistant;
      if (!a) throw _err('assistant_unavailable');
      return a.deleteThread(String(args.id || ''));
    }
    default:
      throw _err('unknown_method:' + method);
  }
}

async function _onMessage(e) {
  const d = e && e.data;
  if (!d || d.extBridge !== true || d.response) return;
  let reg = _frames.get(e.source);
  if (!reg) {
    // Boot race guard: a view's first call can arrive before its frame is
    // registered (the postMessage task can precede the host's registration
    // on slower mounts). Wait a beat and retry once — dropping the message
    // would leave the view's await hanging forever with no reply.
    await new Promise((r) => setTimeout(r, 300));
    reg = _frames.get(e.source);
    if (!reg) return; // still unregistered — ignore silently
  }
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
  _wireAssistantPush();
  if (_listening) return;
  _listening = true;
  window.addEventListener('message', _onMessage);
}
