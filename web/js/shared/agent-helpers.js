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
