/** Shared agent field helpers (Desktop + Mobile V2). */

export function agentName(agent) {
  return String(agent?.task_name || agent?.task?.name || agent?.id?.slice(0, 8) || '');
}

export function agentTags(agent) {
  const tags = [];
  if (Array.isArray(agent?.tags)) tags.push(...agent.tags);
  if (Array.isArray(agent?.task?.tags)) tags.push(...agent.task.tags);
  if (Array.isArray(agent?.task_tags)) tags.push(...agent.task_tags);
  return [...new Set(tags.map(t => String(t || '').trim()).filter(Boolean))];
}

export function agentAutoConfirm(agent) {
  for (const v of [agent?.auto_confirm, agent?.task?.auto_confirm, agent?.task_auto_confirm]) {
    if (typeof v === 'boolean') return v;
  }
  return true;
}

export function agentWorkspacePath(agent) {
  return String(agent?.context_path || agent?.path || agent?.cwd || '');
}

export function agentTool(agent) {
  return String(agent?.tool || agent?.adapter || agent?.task?.tool || agent?.task?.adapter || '').toLowerCase();
}

export function systemPromptFileName(agent) {
  const tool = agentTool(agent);
  if (tool === 'claude') return 'CLAUDE.md';
  if (tool === 'codex' || tool === 'cursor') return 'AGENTS.md';
  return '';
}

export function systemPromptBlockMarkers(agentId) {
  const id = String(agentId || '');
  return { begin: `<!-- camc:${id} begin -->`, end: `<!-- camc:${id} end -->` };
}

export function extractSystemPromptBlock(text, agentId) {
  const { begin, end } = systemPromptBlockMarkers(agentId);
  const raw = String(text || '');
  const start = raw.indexOf(begin);
  if (start < 0) return '';
  const bodyStart = start + begin.length;
  const stop = raw.indexOf(end, bodyStart);
  if (stop < 0) return '';
  return raw.slice(bodyStart, stop).replace(/^\r?\n/, '').replace(/\r?\n$/, '');
}

export function safeTag(s) {
  return /^[A-Za-z0-9_-]{1,32}$/.test(String(s || ''));
}

export function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

/** SSH endpoint hints for disambiguating duplicate agent ids across hosts. */
export function agentEndpointHints(agent) {
  if (!agent) return null;
  const host = agent.machine_host || '';
  if (!host || host === 'local') return null;
  return {
    machine_host: host,
    machine_user: agent.machine_user || '',
    machine_port: agent.machine_port == null ? '' : agent.machine_port,
  };
}

function shortHost(host) {
  const h = String(host || '').trim();
  if (!h) return '';
  const dot = h.indexOf('.');
  return dot > 0 ? h.slice(0, dot) : h;
}

export function agentMatchesEndpoint(agent, hints) {
  if (!agent || !hints || !hints.machine_host) return false;
  const ah = String(agent.machine_host || '').trim();
  const hh = String(hints.machine_host || '').trim();
  if (!ah || !hh) return false;
  const hostOk = ah === hh || shortHost(ah) === shortHost(hh);
  const userOk = String(agent.machine_user || '') === String(hints.machine_user || '');
  const ap = agent.machine_port == null || agent.machine_port === '' ? 22 : Number(agent.machine_port);
  const hp = hints.machine_port == null || hints.machine_port === '' ? 22 : Number(hints.machine_port);
  const portOk = Number.isFinite(ap) && Number.isFinite(hp) ? ap === hp : true;
  return hostOk && userOk && portOk;
}

/** Resolve agent by id; when ids collide across hosts, use endpoint hints. */
export function findAgentByIdentity(agents, agentId, hints = null) {
  const list = agents || [];
  const id = String(agentId || '').trim();
  if (!id) return null;
  const exact = list.filter(a => a && a.id === id);
  if (exact.length === 0) {
    return list.find(a => a && String(a.id || '').startsWith(id)) || null;
  }
  if (exact.length === 1) return exact[0];
  if (hints && hints.machine_host) {
    const matched = exact.filter(a => agentMatchesEndpoint(a, hints));
    if (matched.length === 1) return matched[0];
    if (matched.length > 1) return matched[0];
  }
  return exact[0];
}

export function agentRouteQuery(agent) {
  const hints = agentEndpointHints(agent);
  if (!hints) return '';
  const p = new URLSearchParams();
  p.set('host', hints.machine_host);
  if (hints.machine_user) p.set('user', hints.machine_user);
  if (hints.machine_port !== '' && hints.machine_port != null) p.set('port', String(hints.machine_port));
  const qs = p.toString();
  return qs ? `?${qs}` : '';
}

export function parseAgentRouteHints(search = '') {
  const raw = String(search || '').replace(/^\?/, '');
  if (!raw) return null;
  const p = new URLSearchParams(raw);
  const host = p.get('host') || '';
  if (!host) return null;
  return {
    machine_host: host,
    machine_user: p.get('user') || '',
    machine_port: p.get('port') || '',
  };
}
