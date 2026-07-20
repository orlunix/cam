const richOutputs = new Map();

function cssVar(name, fallback) {
  if (typeof window === 'undefined' || typeof getComputedStyle !== 'function') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function installRichOutputStyle() {
  if (typeof document === 'undefined' || document.getElementById('cam-rich-output-style')) return;
  const style = document.createElement('style');
  style.id = 'cam-rich-output-style';
  style.textContent = `
    .rich-output-host {
      display: none;
      flex: 1 1 auto;
      min-height: 0;
      min-width: 0;
      width: 100%;
      max-width: 100%;
      background: var(--terminal-bg, #0d1117);
      overflow: hidden;
      position: relative;
      box-sizing: border-box;
    }
    .rich-output-host.is-active {
      display: block;
      overflow: auto;
      -webkit-overflow-scrolling: touch;
    }
    .rich-output-pane {
      min-height: 100%;
      box-sizing: border-box;
      padding: 8px 8px 14px;
      color: var(--terminal-fg, #c9d1d9);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: var(--output-font-size, 12px);
      line-height: 1.38;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .rich-line {
      min-height: 1.38em;
    }
    .rich-line-heading {
      color: var(--text-primary, #f0f6fc);
      font-weight: 650;
    }
    .rich-line-command {
      color: #d2a8ff;
    }
    .rich-line-muted {
      color: var(--text-muted, #8b949e);
    }
    .rich-line-error {
      color: #ffa198;
      font-weight: 650;
    }
    .rich-line-warning {
      color: #e3b341;
      font-weight: 600;
    }
    .rich-line-success {
      color: #7ee787;
    }
    .rich-line-separator {
      height: 1.38em;
      min-height: 1.38em;
      position: relative;
    }
    .rich-line-separator::before {
      content: '';
      position: absolute;
      left: 0;
      right: 0;
      top: 50%;
      border-top: 1px solid rgba(139, 148, 158, 0.45);
    }
    .rich-inline-path {
      color: #79c0ff;
    }
    .rich-inline-code {
      color: #d2a8ff;
      background: rgba(210, 168, 255, 0.08);
      border-radius: 3px;
      padding: 0 2px;
    }
    .rich-inline-quote {
      color: #a5d6ff;
    }
  `;
  document.head.appendChild(style);
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function isAtBottom(hostEl) {
  if (!hostEl) return true;
  return hostEl.scrollHeight - hostEl.scrollTop - hostEl.clientHeight < 30;
}

function isRichSeparatorLine(line) {
  const normalized = String(line == null ? '' : line)
    .replace(/[\s​‌‍﻿­]/g, '');
  if (normalized.length >= 4) {
    const first = normalized[0];
    for (let i = 1; i < normalized.length; i++) {
      if (normalized[i] !== first) return false;
    }
    return true;
  }
  return false;
}

export function classifyRichLine(line) {
  const raw = String(line == null ? '' : line);
  const trimmed = raw.trim();
  if (!trimmed) return 'blank';
  if (isRichSeparatorLine(raw)) return 'separator';
  const indent = raw.length - raw.replace(/^\s+/, '').length;
  const lower = trimmed.toLowerCase();
  if (/\b(error|failed|failure|exception|traceback|panic)\b/.test(lower)) return 'error';
  if (/\b(blocked|warning|warn|action required|cannot|timeout)\b/.test(lower)) return 'warning';
  if (/\b(done|success|succeeded|passed|complete|completed|ok)\b/.test(lower)) return 'success';
  if (/^[$#>]\s+/.test(trimmed) || /^ran\s+/.test(lower)) return 'command';
  if (indent >= 6) return 'muted';
  if (indent <= 2 && (/^[-*•✔✓]\s+/.test(trimmed) || /^[0-9]+[.)]\s+/.test(trimmed) || /:$/.test(trimmed))) return 'heading';
  if (indent <= 2 && trimmed.length <= 90 && /^[A-Z][\w\s/.-]+$/.test(trimmed)) return 'heading';
  return 'normal';
}

function decorateEscapedText(escaped) {
  let out = escaped;
  out = out.replace(/`([^`]+)`/g, '<span class="rich-inline-code">`$1`</span>');
  out = out.replace(/(&quot;[^&]+?&quot;|'[^']+?')/g, '<span class="rich-inline-quote">$1</span>');
  out = out.replace(/(^|[\s(])((?:~|\.|\/)[A-Za-z0-9._~\/:-]+|[A-Za-z]:\\[A-Za-z0-9._~\\:-]+)/g,
    '$1<span class="rich-inline-path">$2</span>');
  return out;
}

export function renderInlineRichText(text) {
  return decorateEscapedText(escapeHtml(text));
}

function createRichOutput(agentId, hostEl) {
  if (!hostEl) return null;
  installRichOutputStyle();
  hostEl.classList.add('rich-output-host');
  let ent = richOutputs.get(agentId);
  if (ent?.pane) {
    if (!hostEl.contains(ent.pane)) hostEl.appendChild(ent.pane);
    ent.hostEl = hostEl;
    return ent;
  }
  hostEl.textContent = '';
  const pane = document.createElement('div');
  pane.className = 'rich-output-pane';
  pane.dataset.agentId = agentId;
  hostEl.appendChild(pane);
  ent = { agentId, hostEl, pane, rendered: '' };
  richOutputs.set(agentId, ent);
  return ent;
}

export function renderRichOutput(agentId, hostEl, text) {
  const ent = createRichOutput(agentId, hostEl);
  if (!ent?.pane) return false;
  const raw = String(text == null ? '' : text).replace(/\r\n/g, '\n');
  if (ent.rendered === raw) return true;
  const follow = isAtBottom(ent.hostEl);
  const lines = raw.split('\n');
  ent.pane.innerHTML = lines.map((line) => {
    const kind = classifyRichLine(line);
    if (kind === 'separator') return '<div class="rich-line rich-line-separator" aria-hidden="true"></div>';
    const cls = kind === 'blank' ? 'rich-line' : `rich-line rich-line-${kind}`;
    return `<div class="${cls}">${renderInlineRichText(line) || '&nbsp;'}</div>`;
  }).join('');
  ent.rendered = raw;
  if (follow) {
    requestAnimationFrame(() => {
      try { ent.hostEl.scrollTop = ent.hostEl.scrollHeight; } catch { /* noop */ }
    });
  }
  return true;
}

export function setRichOutputVisible(agentId, hostEl, visible) {
  if (!hostEl) return false;
  installRichOutputStyle();
  hostEl.classList.add('rich-output-host');
  hostEl.classList.toggle('is-active', !!visible);
  if (visible) createRichOutput(agentId, hostEl);
  return true;
}

export function setRichOutputFontSize() {
  return true;
}

export function scrollRichOutputToBottom(agentId) {
  const ent = richOutputs.get(agentId);
  if (!ent?.hostEl) return false;
  try { ent.hostEl.scrollTop = ent.hostEl.scrollHeight; return true; }
  catch { return false; }
}

export function disposeRichOutput(agentId) {
  const ent = richOutputs.get(agentId);
  if (!ent) return false;
  richOutputs.delete(agentId);
  try { ent.pane?.remove(); } catch { /* noop */ }
  return true;
}
