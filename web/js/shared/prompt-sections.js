/** Parse system prompt into Goal / Checklist / Verify sections (markdown headers). */

const HEADER_RE = /^#{1,3}\s*(goal|checklist|check list|steps|verify|verification)\s*:?\s*$/i;

export function parsePromptSections(text) {
  const raw = String(text || '');
  const out = { goal: '', checklist: '', verify: '', body: raw };
  if (!raw.trim()) return out;

  const lines = raw.replace(/\r\n?/g, '\n').split('\n');
  let section = '';
  const buckets = { goal: [], checklist: [], verify: [], other: [] };

  for (const line of lines) {
    const m = line.match(HEADER_RE);
    if (m) {
      const key = m[1].toLowerCase().replace(/\s+/g, '');
      if (key === 'goal') section = 'goal';
      else if (key === 'checklist' || key === 'steps') section = 'checklist';
      else if (key === 'verify' || key === 'verification') section = 'verify';
      continue;
    }
    if (section === 'goal') buckets.goal.push(line);
    else if (section === 'checklist') buckets.checklist.push(line);
    else if (section === 'verify') buckets.verify.push(line);
    else buckets.other.push(line);
  }

  out.goal = buckets.goal.join('\n').trim();
  out.checklist = buckets.checklist.join('\n').trim();
  out.verify = buckets.verify.join('\n').trim();
  const other = buckets.other.join('\n').trim();
  if (!out.goal && !out.checklist && !out.verify && other) {
    out.body = other;
  } else if (other) {
    out.body = other;
  } else {
    out.body = '';
  }
  return out;
}

export function composePromptSections({ goal, checklist, verify, body }) {
  const parts = [];
  const g = String(goal || '').trim();
  const c = String(checklist || '').trim();
  const v = String(verify || '').trim();
  const b = String(body || '').trim();
  if (g) parts.push(`## Goal\n${g}`);
  if (c) parts.push(`## Checklist\n${c}`);
  if (v) parts.push(`## Verify\n${v}`);
  if (b) parts.push(b);
  if (!parts.length) return '';
  return parts.join('\n\n').trim() + '\n';
}

export function checklistItems(text) {
  return String(text || '').split('\n').map(l => l.trim()).filter(Boolean);
}

export function renderChecklistPreviewHtml(text, escHtml) {
  const items = checklistItems(text);
  if (!items.length) return '<span class="muted">No checklist items.</span>';
  return `<ul class="prompt-checklist-preview">${items.map(item => {
    const done = /^[-*]\s*\[[xX]\]/.test(item);
    const label = item.replace(/^[-*]\s*\[[ xX]?\]\s*/, '').replace(/^[-*]\s+/, '');
    return `<li class="${done ? 'is-done' : ''}">${escHtml(label || item)}</li>`;
  }).join('')}</ul>`;
}
