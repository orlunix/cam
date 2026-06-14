/**
 * Desktop agent console — selected-agent output pane, quick-key buttons,
 * and input composer. Reads selection from AppState; sends via CamApi.
 *
 * Composer is a textarea:
 *   - Enter (no modifier)  → send
 *   - Shift+Enter          → newline
 *   - IME composition      → never send, even on Enter
 *
 * Output modes (CAM-DESK-OUT-010..022):
 *   - "plain": default; ANSI-stripped text, same path mobile/PWA uses.
 *   - "rich":  same captured text as plain, rendered into a sibling
 *              pane by a safe ANSI + shallow block renderer below.
 * The two panes hold independent hashes so a plain hash can never
 * suppress a rich response (and vice versa). On any rich-render error
 * we fall back to plain so input/key sending stays intact.
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function timeSince(dateStr) {
  if (!dateStr) return '';
  const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (s < 0) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d`;
}

function isActiveStatus(s) {
  return ['running', 'starting', 'pending'].includes(s);
}

/* ────────── ANSI SGR → HTML renderer (CAM-DESK-OUT-014) ──────────
 * Supports the supported set listed in the requirement:
 *   - reset (0)
 *   - bold (1) / dim (2) / italic (3) / underline (4) /
 *     inverse (7) / strike (9) and their "off" counterparts
 *   - 8-color fg (30..37) + bright fg (90..97) + default fg (39)
 *   - 8-color bg (40..47) + bright bg (100..107) + default bg (49)
 *   - 256-color   (38;5;N / 48;5;N)
 *   - truecolor   (38;2;R;G;B / 48;2;R;G;B)
 * Anything else (cursor moves, clears, OSC, …) is dropped safely.
 * Output is fully HTML-escaped before any tags are added.
 */
const ANSI_PALETTE_BASE = [
  '#0d1117', '#cc4444', '#3dd68c', '#d29922',
  '#4a9eff', '#a67ad6', '#5ec5d6', '#c9d1d9',
];
const ANSI_PALETTE_BRIGHT = [
  '#6e7681', '#f85149', '#56d364', '#e3b341',
  '#79c0ff', '#bc8cff', '#7ddcff', '#ffffff',
];

function ansiColor8(idx) {
  return ANSI_PALETTE_BASE[idx] || null;
}
function ansiColor8Bright(idx) {
  return ANSI_PALETTE_BRIGHT[idx] || null;
}
function ansi256(n) {
  if (n < 0 || n > 255) return null;
  if (n < 8)   return ANSI_PALETTE_BASE[n];
  if (n < 16)  return ANSI_PALETTE_BRIGHT[n - 8];
  if (n < 232) {
    n -= 16;
    const r = Math.floor(n / 36),
          g = Math.floor((n % 36) / 6),
          b = n % 6;
    const step = (v) => v === 0 ? 0 : 55 + 40 * v;
    return `rgb(${step(r)},${step(g)},${step(b)})`;
  }
  const v = 8 + (n - 232) * 10;
  return `rgb(${v},${v},${v})`;
}
function ansiTrueColor(r, g, b) {
  if ([r, g, b].some(v => v == null || v < 0 || v > 255)) return null;
  return `rgb(${r},${g},${b})`;
}

function defaultStyle() {
  return {
    fg: null, bg: null,
    bold: false, dim: false, italic: false,
    underline: false, inverse: false, strike: false,
  };
}

function applySgr(style, params) {
  // Walk params left-to-right; treat empty list as "reset" (CSI m).
  if (params.length === 0) params = [0];
  for (let i = 0; i < params.length; i++) {
    const n = params[i];
    if (n === 0) { Object.assign(style, defaultStyle()); continue; }
    if (n === 1) { style.bold = true; continue; }
    if (n === 2) { style.dim = true; continue; }
    if (n === 3) { style.italic = true; continue; }
    if (n === 4) { style.underline = true; continue; }
    if (n === 7) { style.inverse = true; continue; }
    if (n === 9) { style.strike = true; continue; }
    if (n === 22) { style.bold = false; style.dim = false; continue; }
    if (n === 23) { style.italic = false; continue; }
    if (n === 24) { style.underline = false; continue; }
    if (n === 27) { style.inverse = false; continue; }
    if (n === 29) { style.strike = false; continue; }
    if (n >= 30 && n <= 37) { style.fg = ansiColor8(n - 30); continue; }
    if (n === 38) {
      // 256 or truecolor extended fg
      const kind = params[i + 1];
      if (kind === 5) { style.fg = ansi256(params[i + 2]); i += 2; continue; }
      if (kind === 2) {
        style.fg = ansiTrueColor(params[i + 2], params[i + 3], params[i + 4]);
        i += 4; continue;
      }
      // unknown extended — skip the kind byte to avoid misparsing the next param
      i += 1; continue;
    }
    if (n === 39) { style.fg = null; continue; }
    if (n >= 40 && n <= 47) { style.bg = ansiColor8(n - 40); continue; }
    if (n === 48) {
      const kind = params[i + 1];
      if (kind === 5) { style.bg = ansi256(params[i + 2]); i += 2; continue; }
      if (kind === 2) {
        style.bg = ansiTrueColor(params[i + 2], params[i + 3], params[i + 4]);
        i += 4; continue;
      }
      i += 1; continue;
    }
    if (n === 49) { style.bg = null; continue; }
    if (n >= 90 && n <= 97) { style.fg = ansiColor8Bright(n - 90); continue; }
    if (n >= 100 && n <= 107) { style.bg = ansiColor8Bright(n - 100); continue; }
    // Anything else: ignored.
  }
}

function styleToAttrs(style) {
  const classes = [];
  const styles = [];
  if (style.bold) classes.push('ansi-bold');
  if (style.dim) classes.push('ansi-dim');
  if (style.italic) classes.push('ansi-italic');
  if (style.underline) classes.push('ansi-underline');
  if (style.strike) classes.push('ansi-strike');
  if (style.inverse) classes.push('ansi-inverse');
  if (style.fg) styles.push(`color:${style.fg}`);
  if (style.bg) styles.push(`background:${style.bg}`);
  const cls = classes.length ? ` class="${classes.join(' ')}"` : '';
  const sty = styles.length ? ` style="${styles.join(';')}"` : '';
  return cls + sty;
}

function isStyleNeutral(style) {
  return !style.fg && !style.bg && !style.bold && !style.dim &&
    !style.italic && !style.underline && !style.strike && !style.inverse;
}

export function ansiToHtml(input) {
  if (input == null) return '';
  const text = String(input);
  let html = '';
  let buf = '';
  const style = defaultStyle();
  let spanOpen = false;
  const flush = () => {
    if (!buf) return;
    const safe = escapeHtml(buf);
    if (isStyleNeutral(style)) {
      html += safe;
    } else {
      html += `<span${styleToAttrs(style)}>${safe}</span>`;
    }
    buf = '';
    spanOpen = false;
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text.charCodeAt(i);
    if (ch !== 0x1b) { buf += text[i]; continue; }
    // ESC seen. Recognize CSI ( ESC [ ... <final byte> ) and skip OSC ( ESC ] ... BEL/ST ).
    const next = text[i + 1];
    if (next === '[') {
      // CSI: parse params until a byte in 0x40..0x7e
      let j = i + 2;
      let params = '';
      while (j < text.length) {
        const c = text.charCodeAt(j);
        if (c >= 0x40 && c <= 0x7e) break;
        params += text[j]; j++;
      }
      const finalByte = text[j];
      if (finalByte === 'm') {
        // SGR
        flush();
        const nums = params.length
          ? params.split(';').map(p => p === '' ? 0 : parseInt(p, 10) || 0)
          : [];
        applySgr(style, nums);
      }
      // else: cursor moves / clears — ignore, but consume the sequence
      i = j;
      continue;
    }
    if (next === ']') {
      // OSC: skip up to BEL (0x07) or ST (ESC \\)
      let j = i + 2;
      while (j < text.length) {
        const c = text.charCodeAt(j);
        if (c === 0x07) { break; }
        if (c === 0x1b && text[j + 1] === '\\') { j += 1; break; }
        j++;
      }
      i = j;
      continue;
    }
    // Unrecognized ESC sequence — drop the ESC and continue (best effort).
  }
  flush();
  return html;
}


function normalizeOutput(input) {
  return String(input == null ? '' : input).replace(/\r\n?/g, '\n');
}

function stripAnsi(input) {
  const text = String(input == null ? '' : input);
  let out = '';
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) !== 0x1b) { out += text[i]; continue; }
    const next = text[i + 1];
    if (next === '[') {
      let j = i + 2;
      while (j < text.length) {
        const c = text.charCodeAt(j);
        if (c >= 0x40 && c <= 0x7e) break;
        j++;
      }
      i = j;
      continue;
    }
    if (next === ']') {
      let j = i + 2;
      while (j < text.length) {
        const c = text.charCodeAt(j);
        if (c === 0x07) break;
        if (c === 0x1b && text[j + 1] === '\\') { j += 1; break; }
        j++;
      }
      i = j;
      continue;
    }
  }
  return out;
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, '&quot;');
}

function hasAnsi(s) {
  return String(s || '').includes('\x1b');
}

function richLineRecords(input) {
  return normalizeOutput(input).split('\n').map(raw => ({ raw, plain: stripAnsi(raw) }));
}

function renderMarkedSafeText(safe) {
  return safe
    .replace(/\*\*([^*][\s\S]*?[^*])\*\*/g, '<strong class="rich-strong">$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*\n]{0,120}?[^*\s])\*(?=([\s).,!?;:]|$))/g,
      '$1<em class="rich-em">$2</em>');
}

function renderPlainInline(raw) {
  const text = String(raw == null ? '' : raw);
  const codeParts = text.split(/(`[^`]*`)/g);
  return codeParts.map(part => {
    if (part.length >= 2 && part.startsWith('`') && part.endsWith('`')) {
      return `<code class="rich-inline-code">${escapeHtml(part.slice(1, -1))}</code>`;
    }
    let html = '';
    let last = 0;
    const urlRe = /https?:\/\/[^\s<>")'\]]+/g;
    let m;
    while ((m = urlRe.exec(part))) {
      html += renderMarkedSafeText(escapeHtml(part.slice(last, m.index)));
      const href = m[0];
      html += `<a class="rich-link" href="${escapeAttr(href)}" target="_blank" rel="noreferrer">${escapeHtml(href)}</a>`;
      last = m.index + href.length;
    }
    html += renderMarkedSafeText(escapeHtml(part.slice(last)));
    return html;
  }).join('');
}

function renderRichInline(raw) {
  return hasAnsi(raw) ? ansiToHtml(raw) : renderPlainInline(raw);
}

function renderRichFallback(input) {
  return normalizeOutput(input).split('\n')
    .map(line => `<div class="rich-line">${escapeHtml(line)}</div>`)
    .join('');
}

function splitTableCells(line) {
  const t = stripAnsi(line).trim();
  if (!t.includes('|')) return null;
  const body = t.replace(/^\|/, '').replace(/\|$/, '');
  const cells = body.split('|').map(c => c.trim());
  return cells.length >= 2 ? cells : null;
}

function isTableSeparator(cells) {
  return Array.isArray(cells) && cells.length >= 2 &&
    cells.every(c => /^:?-{3,}:?$/.test(c));
}

function renderTable(lines, start) {
  const head = splitTableCells(lines[start].raw);
  const sep = splitTableCells(lines[start + 1]?.raw || '');
  if (!head || !isTableSeparator(sep) || head.length !== sep.length) return null;
  let i = start + 2;
  const rows = [];
  while (i < lines.length) {
    const cells = splitTableCells(lines[i].raw);
    if (!cells || cells.length < 2) break;
    rows.push(cells);
    i++;
  }
  const th = head.map(c => `<th>${enrichInline(c)}</th>`).join('');
  const body = rows.map(row =>
    '<tr>' + row.map(c => `<td>${enrichInline(c)}</td>`).join('') + '</tr>'
  ).join('');
  return {
    next: i,
    html: `<div class="rich-table-wrap"><table class="rich-table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`,
  };
}

function renderList(lines, start) {
  const items = [];
  let i = start;
  const re = /^(\s*)([-*+]|\d+[.])\s+(\[[ xX]\]\s+)?(.+)$/;
  while (i < lines.length) {
    const m = lines[i].plain.match(re);
    if (!m) break;
    const depth = Math.min(4, Math.floor((m[1] || '').replace(/\t/g, '  ').length / 2));
    const checked = m[3] ? /x/i.test(m[3]) : null;
    const marker = checked == null
      ? escapeHtml(m[2])
      : `<span class="rich-check ${checked ? 'checked' : ''}">${checked ? '☑' : '☐'}</span>`;
    items.push(`<div class="rich-list-item" style="--depth:${depth}"><span class="rich-list-marker">${marker}</span><span>${enrichInline(m[4])}</span></div>`);
    i++;
  }
  return { next: i, html: `<div class="rich-list">${items.join('')}</div>` };
}

function renderQuote(lines, start) {
  const parts = [];
  let i = start;
  while (i < lines.length) {
    const m = lines[i].plain.match(/^\s*>\s?(.*)$/);
    if (!m) break;
    parts.push(`<div class="rich-quote-line">${enrichInline(m[1])}</div>`);
    i++;
  }
  return { next: i, html: `<blockquote class="rich-quote">${parts.join('')}</blockquote>` };
}

function isIndentedCodeLine(plain) {
  if (!/^ {4,}\S/.test(plain)) return false;
  const body = plain.replace(/^ {4}/, '');
  const trimmed = body.trim();
  if (!trimmed) return false;
  // Path-only / attachment lines and indented prose should not create
  // scrollable code panels. Require a cheap code-ish signal.
  if (/^[~/]|^\.\.?\//.test(trimmed)) return false;
  return /^(?:const|let|var|function|return|if|else|for|while|switch|case|class|import|export|async|await|try|catch|finally|def|elif|except|raise|with|from|pass|local|export|source|set\s|cd\s|echo\s|printf\s|#|\/\/|;|@@|\+|-|\$)\b/.test(trimmed) ||
    /[{}()[\];=]/.test(trimmed) ||
    /^[A-Za-z_][A-Za-z0-9_.-]*\s*\(/.test(trimmed);
}

function renderIndentedCode(lines, start) {
  const parts = [];
  let i = start;
  while (i < lines.length && isIndentedCodeLine(lines[i].plain)) {
    parts.push(lines[i].plain.replace(/^ {4}/, ''));
    i++;
  }
  return { next: i, html: `<pre class="rich-code"><code>${highlightCodeBlock(parts.join('\n'), '')}</code></pre>` };
}

function renderFencedCode(lines, start) {
  const m = lines[start].plain.match(/^\s*(```|~~~)\s*([A-Za-z0-9_.+-]*)\s*$/);
  if (!m) return null;
  const fence = m[1];
  const lang = m[2] || '';
  const parts = [];
  let i = start + 1;
  while (i < lines.length) {
    if (lines[i].plain.trim() === fence) {
      i++;
      break;
    }
    parts.push(lines[i].raw);
    i++;
  }
  const label = lang ? `<div class="rich-code-lang">${escapeHtml(lang)}</div>` : '';
  const langCls = lang ? ` rich-code-${escapeAttr(lang.toLowerCase())}` : '';
  return {
    next: i,
    html: `<div class="rich-code-block${langCls}">${label}<pre class="rich-code"><code>${highlightCodeBlock(parts.join('\n'), lang)}</code></pre></div>`,
  };
}

/* ────────── Rich v1: small, bounded syntax / status helpers ──────────
 *
 * All of these are linear per line, capped to LINE_HL_MAX chars, and
 * never reach across line boundaries. None of them allocates more
 * than one combined regex per line scan. Output is always
 * HTML-escaped at the token boundary, so the caller can splice
 * unsanitized line text in safely.
 */

const LINE_HL_MAX = 500;

// Compact keyword sets. Intersection is fine — first hit wins per token.
// Kept small on purpose so the per-line tokenizer stays cheap.
const RICH_CODE_KW = new Set([
  // JS / TS
  'const', 'let', 'var', 'function', 'return', 'if', 'else', 'for',
  'while', 'do', 'switch', 'case', 'break', 'continue', 'class',
  'extends', 'new', 'this', 'super', 'async', 'await', 'try', 'catch',
  'finally', 'throw', 'import', 'export', 'from', 'default', 'typeof',
  'instanceof', 'of', 'in', 'delete', 'void', 'yield', 'null',
  'undefined', 'true', 'false',
  // Python
  'def', 'elif', 'except', 'raise', 'as', 'with', 'lambda', 'pass',
  'global', 'nonlocal', 'True', 'False', 'None', 'and', 'or', 'not',
  'is',
  // Shell / POSIX
  'then', 'fi', 'esac', 'done', 'until', 'export', 'source', 'local',
  'readonly',
]);

function highlightCodeLine(line) {
  // Strip ANSI for classification only; render text comes from the
  // ANSI-free representation so escapes don't bleed into the DOM.
  const ansiFree = stripAnsi(line);
  if (ansiFree.length > LINE_HL_MAX) return escapeHtml(ansiFree);

  // Diff prefixes first — unambiguous and skipped tokens later.
  if (/^@@.*@@/.test(ansiFree)) {
    return `<span class="rich-code-diff-hunk">${escapeHtml(ansiFree)}</span>`;
  }
  if (/^\+\+\+\s/.test(ansiFree) || /^---\s/.test(ansiFree)) {
    return `<span class="rich-code-diff-meta">${escapeHtml(ansiFree)}</span>`;
  }
  if (/^\+/.test(ansiFree)) {
    return `<span class="rich-code-diff-add">${escapeHtml(ansiFree)}</span>`;
  }
  if (/^-/.test(ansiFree) && !/^---/.test(ansiFree)) {
    return `<span class="rich-code-diff-del">${escapeHtml(ansiFree)}</span>`;
  }

  // Shebang lines stay plain text so `#!/bin/sh` doesn't get
  // misclassified as a comment.
  if (/^\s*#!/.test(ansiFree)) return escapeHtml(ansiFree);

  // Whole-line comments. Treat // / # / -- / ; at line start as
  // comments (catches the easy case before the per-token sweep).
  {
    const cm = ansiFree.match(/^(\s*)(\/\/|#|--|;)(.*)$/);
    if (cm) {
      return `${escapeHtml(cm[1])}` +
             `<span class="rich-code-comment">${escapeHtml(cm[2] + cm[3])}</span>`;
    }
  }

  // Per-token sweep. The combined regex matches in order:
  //   1. a triple-quote string ", ', or `;
  //   2. an inline comment `//` or `#` (not `#!`) that starts at
  //      input start or after whitespace and runs to EOL;
  //   3. an identifier (word);
  //   4. a run of whitespace;
  //   5. any other non-letter/non-string chunk.
  // Each alternative consumes ≥ 1 character so total work stays
  // linear. URLs like `https://example.com` keep their `//` because
  // the lookbehind requires whitespace or input-start before the
  // comment marker.
  const re = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|(?<=^|\s)(?:\/\/|#(?!!))[^\n]*|\b[A-Za-z_][A-Za-z0-9_]*\b|\s+|[^\s"'`A-Za-z_]+)/g;
  let out = '';
  let m;
  while ((m = re.exec(ansiFree))) {
    const tok = m[0];
    const c0 = tok.charCodeAt(0);
    // 34 " | 39 ' | 96 `
    if (c0 === 34 || c0 === 39 || c0 === 96) {
      out += `<span class="rich-code-string">${escapeHtml(tok)}</span>`;
    } else if (tok.startsWith('//') || (tok.startsWith('#') && !tok.startsWith('#!'))) {
      out += `<span class="rich-code-comment">${escapeHtml(tok)}</span>`;
    } else if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(tok) && RICH_CODE_KW.has(tok)) {
      out += `<span class="rich-code-kw">${escapeHtml(tok)}</span>`;
    } else {
      out += escapeHtml(tok);
    }
  }
  return out;
}

function highlightCodeBlock(text, _lang) {
  return String(text).split('\n').map(highlightCodeLine).join('\n');
}

/* Shell-command segment tokenizer (no remote shell invocation; we're
 * just colouring the captured text). Splits on whitespace + quotes,
 * classifies the first non-env token as the command name, KEY=val
 * as env, --flag/-x as flags, paths heuristically, everything else
 * stays inline. */
function isPathLikeToken(tok) {
  const plain = String(tok || '').replace(/^['"]/, '').replace(/['"]$/, '');
  return /^[~/]/.test(plain) || /^\.\.?\//.test(plain) ||
    /\//.test(plain) || /\.[A-Za-z0-9]{1,8}$/.test(plain);
}

function highlightShellCommand(text) {
  const src = String(text || '');
  if (src.length > LINE_HL_MAX) return renderPlainInline(src);

  const tokens = [];
  const re = /(\s+|"[^"]*"|'[^']*'|\S+)/g;
  let m;
  while ((m = re.exec(src))) tokens.push(m[0]);

  const nonSpace = tokens.filter(t => !/^\s+$/.test(t));
  const pathOnly = nonSpace.length > 0 && nonSpace.every(t =>
    /^[A-Za-z_][A-Za-z0-9_]*=/.test(t.replace(/^['"]/, '').replace(/['"]$/, '')) ||
    isPathLikeToken(t)
  );

  let out = '';
  let nameAssigned = false;
  for (const tok of tokens) {
    if (/^\s+$/.test(tok)) { out += escapeHtml(tok); continue; }
    const plain = tok.replace(/^['"]/, '').replace(/['"]$/, '');

    // KEY=val env assignment — only legal before the command name.
    if (!nameAssigned && /^[A-Za-z_][A-Za-z0-9_]*=/.test(plain)) {
      const eq = plain.indexOf('=');
      out += `<span class="rich-cmd-env">${escapeHtml(plain.slice(0, eq))}</span>=` +
             `<span class="rich-cmd-env-val">${escapeHtml(plain.slice(eq + 1))}</span>`;
      continue;
    }
    // Prompt lines that only contain file/attachment paths are not
    // commands; keep them warm-highlighted as paths instead of making
    // the first path look like an executable name.
    if (pathOnly && isPathLikeToken(tok)) {
      out += `<span class="rich-cmd-path">${escapeHtml(tok)}</span>`;
      continue;
    }
    if (!nameAssigned) {
      nameAssigned = true;
      out += `<span class="rich-cmd-name">${escapeHtml(tok)}</span>`;
      continue;
    }
    // Long form / short form flags. Optional =value attached.
    if (/^--?[A-Za-z0-9_][\w-]*(=.*)?$/.test(plain)) {
      const eq = plain.indexOf('=');
      if (eq < 0) {
        out += `<span class="rich-cmd-flag">${escapeHtml(tok)}</span>`;
      } else {
        out += `<span class="rich-cmd-flag">${escapeHtml(plain.slice(0, eq))}</span>=` +
               `<span class="rich-cmd-flag-val">${escapeHtml(plain.slice(eq + 1))}</span>`;
      }
      continue;
    }
    if (isPathLikeToken(tok)) {
      out += `<span class="rich-cmd-path">${escapeHtml(tok)}</span>`;
      continue;
    }
    out += escapeHtml(tok);
  }
  return out;
}

/* Status/keyword recognizer. Matches at line start so PASS embedded
 * in prose isn't flagged. Returns the badge keyword and the colour
 * class, or null. Bounded — one regex test. */
const RICH_STATUS_RE = /^(\s*)(PASS|FAIL|ERROR|WARN(?:ING)?|STATUS|IMPLEMENTATION|IMPLEMENTATION_SUMMARY|FILES_CHANGED|TESTS|SMOKE|BUILD|INSTALL|BLOCKERS|REQ_STATUS|MOBILE_COMPAT|NOTES|REVIEW_STATUS)(:|\b)/;
const RICH_STATUS_CLS = {
  PASS: 'pass',  FAIL: 'fail',  ERROR: 'error',
  WARN: 'warn',  WARNING: 'warn',
};
function richStatusMatch(plain) {
  const m = plain.match(RICH_STATUS_RE);
  if (m) {
    const kw = m[2].toUpperCase();
    return { kw: m[2], cls: RICH_STATUS_CLS[kw] || 'info', indent: m[1], punct: m[3] };
  }
  // Exit code lines: `exit code 0`, `Exit Code: 137`, `exit status 1`.
  const exit = plain.match(/^(\s*)(exit\s+code|exit\s+status|Exit\s+Code)\s*[:=]?\s*(-?\d+)\b/);
  if (exit) {
    const n = Number(exit[3]);
    return { kw: 'EXIT ' + exit[3], cls: n === 0 ? 'pass' : 'fail', indent: exit[1], punct: '' };
  }
  // HTTP status lines: `HTTP/1.1 404 Not Found`, `HTTP/2 200 OK`.
  const http = plain.match(/^(\s*)HTTP\/[\d.]+\s+(\d{3})\b/);
  if (http) {
    const n = Number(http[2]);
    const cls = n >= 500 ? 'error' : n >= 400 ? 'fail' : n >= 300 ? 'warn' : 'info';
    return { kw: 'HTTP ' + http[2], cls, indent: http[1], punct: '' };
  }
  return null;
}

/* Low-signal recognizer for noisy build/install/dep lines. We dim
 * but never hide — the user can still copy them out. Linear, single
 * regex test per line. */
const RICH_LOW_SIGNAL_RE = /^\s*(?:npm\s+(?:WARN|notice|info|http|verb)|electron-builder\s|app-builder\s|node_modules[\\/]|\(node:\d+\)\s+(?:Deprecation|\[DEP)|gyp\s+(?:info|verb|http)|⠹|⠼|⠧|\[#+\s*\d+\/\d+\])/i;
function isRichLowSignal(plain) {
  return RICH_LOW_SIGNAL_RE.test(plain);
}

/* ─────────── Rich v2: agent event / progress detector ───────────
 *
 * Matches a small, opinionated set of TUI-style verb prefixes
 * (Claude Code / Codex style: "Cooked for 10s", "Working ...",
 * "Reading foo", "Edited bar", etc.). Bullet-glyph prefixes
 * (✦ ✻ ★ ⏺ • etc.) are tolerated optionally. Bounded to a single
 * regex test per line. Returns `{ glyph, verb, tail }` or `null`.
 */
const RICH_EVENT_VERBS = (
  'Brewed|Cooked|Cooking|Crafted|Crafting|Crunched|Crunching|Smooshed|Smooshing|' +
  'Working|Running|Ran|Searching|Searched|Read|Reading|Editing|Edited|' +
  'Writing|Wrote|Asked|Asking|Listed|Listing|Updated|Updating|Created|' +
  'Creating|Deleted|Deleting|Removed|Removing|Fetched|Fetching|' +
  'Loaded|Loading|Saved|Saving|Sent|Sending|Received|Receiving|' +
  'Compiled|Compiling|Installed|Installing|Tool call|Tool result|' +
  'Approved|Denied|Skipped|Skipping'
);
const RICH_EVENT_RE = new RegExp(
  '^(\\s*)' +
  '(?:([\\u2600-\\u26FF\\u2700-\\u27BF\\u2B00-\\u2BFF\\u2730-\\u273F\\u2780-\\u27BF\\u25A0-\\u25FF\\u23F0-\\u23FF]|[\\u2022\\u25CF\\u25E6\\u2043\\u2219\\u2218\\u26AB\\u26AA\\u23F8\\u23F9])\\s+)?' +
  '(' + RICH_EVENT_VERBS + ')(\\b.*)?$'
);
function richEventMatch(plain) {
  if (plain.length > LINE_HL_MAX) return null;
  const m = plain.match(RICH_EVENT_RE);
  if (!m) return null;
  return { indent: m[1] || '', glyph: m[2] || '', verb: m[3], tail: m[4] || '' };
}

/* ─────────── Rich v2: inline token tokenizer (prose pass) ───────────
 *
 * One bounded pass over plain text. Identifies: URLs, file:line[:col]
 * references, filesystem paths (Unix / relative / Windows / UNC),
 * quoted strings, duration / percent / bytes / count / exit-code /
 * port metrics, important keywords (TODO/FIXME/BLOCKER/SECURITY/
 * IMPORTANT), and #tags. ANSI is preserved by the caller
 * (`renderRichInline` keeps that pre-pass).
 *
 * Conservatism: all token alts are anchored so we don't fire on
 * prose that merely contains the substring (mid-word PASS, etc.).
 * Each alternative consumes ≥ 1 char and the global regex is
 * stepped linearly per line, so total work is O(line length).
 */
const INLINE_RE = new RegExp(
  [
    // 1. URL (full token; includes path/query)
    '(?<url>https?:\\/\\/[^\\s<>")\']+)',
    // 2. File reference with :line[:col]. Path prefix must look
    //    path-ish (slash, dot-slash, tilde, drive, UNC) so prose
    //    like "foo:42" doesn't match.
    '(?<fileref>(?:[~/]|\\.{1,2}\\/|[A-Za-z]:[\\\\/]|\\\\\\\\)[^\\s"\'<>():]*:\\d+(?::\\d+)?)',
    // 3. Filesystem path (no :line). UNC \\host\share\..., Windows
    //    C:\foo\bar (only when at least one separator follows),
    //    Unix /foo/bar, ~/.config/foo, ./rel, ../rel.
    '(?<path>\\\\\\\\[A-Za-z0-9._-]+(?:[\\\\/][^\\s"\'<>()]+)+|' +
    '[A-Za-z]:[\\\\/](?:[^\\s"\'<>()\\\\/]+[\\\\/])*[^\\s"\'<>()\\\\/]*|' +
    '~\\/[^\\s"\'<>()]+|' +
    '\\.{1,2}\\/[^\\s"\'<>()]+|' +
    '\\/[A-Za-z0-9._][^\\s"\'<>()]*)',
    // 4. Quoted string. Bounded length so we don't span paragraphs.
    '(?<quoted>"[^"\\n]{0,200}"|\'[^\'\\n]{0,200}\')',
    // 5. Metrics — duration, percent, bytes, plain count.
    '(?<metric>\\b\\d+(?:\\.\\d+)?(?:ms|s|m|h|d)\\b|' +
    '\\b\\d+(?:\\.\\d+)?%(?!\\w)|' +
    '\\b\\d+(?:\\.\\d+)?\\s?(?:[KMG]B|[KMG]iB|kB|MB|GB|TB|bytes?)\\b|' +
    '\\b(?:exit\\s+code|exit\\s+status|port)\\s+\\d+\\b|' +
    '\\b\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?\\b)',
    // 6. Important keywords (whole-word, all-caps).
    '(?<kwattn>\\b(?:IMPORTANT|TODO|FIXME|BLOCKER|BLOCKERS|SECURITY|DEPRECATED)\\b)',
    // 7. Tag — `#word` preceded by start-of-string or whitespace.
    //    The `(?<=^|\\s)` lookbehind keeps URLs (`https://...#frag`)
    //    and pre-word `#` (shebang, fragment) from misfiring.
    '(?<tag>(?<=^|\\s)#[A-Za-z][\\w-]{0,40})',
  ].join('|'),
  'g'
);

function classifyInlineToken(m) {
  const g = m.groups || {};
  if (g.url)      return { cls: 'rich-link',     href: g.url };
  if (g.fileref)  return { cls: 'rich-fileref' };
  if (g.path)     return { cls: 'rich-fs-path' };
  if (g.quoted)   return { cls: 'rich-quoted' };
  if (g.metric)   return { cls: 'rich-metric' };
  if (g.kwattn)   return { cls: 'rich-kw-attn' };
  if (g.tag)      return { cls: 'rich-tag' };
  return null;
}

/* Tokenize one prose segment (already free of backticks). The
 * `*bold*` and italic shorthand still runs over the inter-token
 * gaps via `renderMarkedSafeText`. Capped at LINE_HL_MAX to avoid
 * pathological lines locking the renderer. */
function enrichProseSegment(text) {
  const src = String(text == null ? '' : text);
  if (src.length > LINE_HL_MAX) return renderMarkedSafeText(escapeHtml(src));
  let out = '';
  let last = 0;
  // Reset lastIndex defensively — INLINE_RE is module-scope shared.
  INLINE_RE.lastIndex = 0;
  let m;
  while ((m = INLINE_RE.exec(src)) !== null) {
    if (m.index === INLINE_RE.lastIndex) { INLINE_RE.lastIndex++; continue; }
    // Some alts include a leading whitespace (the #tag lookbehind
    // case is handled by lookbehind in the source regex, so this
    // is mostly the bytes-metric `\b\d+ KB` form). Emit the gap
    // verbatim then the classified token.
    const gap = src.slice(last, m.index);
    if (gap) out += renderMarkedSafeText(escapeHtml(gap));
    const cls = classifyInlineToken(m);
    const raw = m[0];
    if (cls && cls.cls === 'rich-link') {
      out += `<a class="rich-link" href="${escapeAttr(cls.href)}" target="_blank" rel="noreferrer">${escapeHtml(raw)}</a>`;
    } else if (cls) {
      out += `<span class="${cls.cls}">${escapeHtml(raw)}</span>`;
    } else {
      out += renderMarkedSafeText(escapeHtml(raw));
    }
    last = m.index + raw.length;
  }
  if (last < src.length) {
    out += renderMarkedSafeText(escapeHtml(src.slice(last)));
  }
  return out;
}

/* v2 prose-inline pass: backticks first (existing v0 behaviour),
 * then `enrichProseSegment` for non-code parts. Replaces the v0
 * `renderPlainInline` for prose / list / quote / status-tail
 * contexts. ANSI lines bypass this path via `renderRichInline`. */
function enrichInline(raw) {
  const text = String(raw == null ? '' : raw);
  if (hasAnsi(text)) return ansiToHtml(text);
  const codeParts = text.split(/(`[^`]*`)/g);
  return codeParts.map(part => {
    if (part.length >= 2 && part.startsWith('`') && part.endsWith('`')) {
      return `<code class="rich-inline-code">${escapeHtml(part.slice(1, -1))}</code>`;
    }
    return enrichProseSegment(part);
  }).join('');
}

export function renderRichOutput(input) {
  try {
    const lines = richLineRecords(input);
    const out = [];
    // v2 subblock state: every time we emit a `.rich-shell` line we
    // mark the following non-block prose lines as command output so
    // they read as muted stdout instead of bright prose. Any block
    // detector that fires resets the flag.
    let inCmdOutput = false;
    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      const plain = line.plain;
      const trimmed = plain.trim();

      const fenced = renderFencedCode(lines, i);
      if (fenced) { inCmdOutput = false; out.push(fenced.html); i = fenced.next; continue; }

      const table = i + 1 < lines.length ? renderTable(lines, i) : null;
      if (table) { inCmdOutput = false; out.push(table.html); i = table.next; continue; }

      if (!trimmed) {
        inCmdOutput = false;
        out.push('<div class="rich-line rich-empty"></div>'); i++; continue;
      }

      if (/^(?:-{3,}|={3,}|\*{3,}|_{3,}|─{3,}|═{3,})$/.test(trimmed)) {
        inCmdOutput = false;
        const cls = trimmed.length > 24 ? 'rich-terminal-rule' : 'rich-divider';
        out.push(`<div class="${cls}" aria-hidden="true"></div>`); i++; continue;
      }

      const shell = plain.match(/^\s*(\$|❯|PS>|cmd>)\s+(.+)$/i) ||
        plain.match(/^\s*(>)\s+((?:npm|npx|node|git|cd|ls|cat|echo|ssh|cam|camc|python|python3|pip|uv|make|pytest|docker|kubectl|powershell|cmd)\b.*)$/i);
      if (shell) {
        inCmdOutput = true;
        out.push(
          `<div class="rich-shell">` +
            `<span class="rich-shell-prompt">${escapeHtml(shell[1])}</span>` +
            `<span class="rich-shell-command">${highlightShellCommand(shell[2])}</span>` +
          `</div>`
        );
        i++; continue;
      }

      if (/^\s*>\s?/.test(plain)) {
        inCmdOutput = false;
        const quote = renderQuote(lines, i);
        out.push(quote.html); i = quote.next; continue;
      }

      if (/^\s*([-*+]|\d+[.])\s+/.test(plain)) {
        inCmdOutput = false;
        const list = renderList(lines, i);
        out.push(list.html); i = list.next; continue;
      }

      const heading = plain.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        inCmdOutput = false;
        out.push(`<div class="rich-heading rich-heading-${heading[1].length}">${enrichInline(heading[2])}</div>`);
        i++; continue;
      }

      // v2 agent event / progress: bullet glyph (optional) + verb +
      // tail. Verb gets its own span so CSS can give it accent.
      const event = richEventMatch(plain);
      if (event) {
        inCmdOutput = false;
        const indent  = escapeHtml(event.indent);
        const glyph   = event.glyph ? `<span class="rich-event-glyph">${escapeHtml(event.glyph)}</span>` : '';
        const verb    = `<span class="rich-event-verb">${escapeHtml(event.verb)}</span>`;
        const tail    = event.tail ? enrichInline(event.tail) : '';
        out.push(
          `<div class="rich-line rich-event">` + indent + glyph + verb + tail + `</div>`
        );
        i++; continue;
      }

      if (isIndentedCodeLine(plain)) {
        inCmdOutput = false;
        const code = renderIndentedCode(lines, i);
        out.push(code.html); i = code.next; continue;
      }

      // Status / PASS / FAIL / exit code / HTTP. The badge takes the
      // leading keyword and the rest of the line passes through the
      // ordinary inline renderer so URLs/code/bold still work.
      const status = richStatusMatch(plain);
      if (status) {
        inCmdOutput = false;
        const indent = escapeHtml(status.indent);
        const punct = status.punct === ':' ? ':' : '';
        const consumed = status.indent.length + status.kw.length + (status.punct === ':' ? 1 : 0);
        const rest = line.raw.slice(consumed);
        out.push(
          `<div class="rich-line rich-status rich-status-${status.cls}">` +
            indent +
            `<span class="rich-status-badge rich-status-badge-${status.cls}">${escapeHtml(status.kw.toUpperCase())}</span>` +
            (punct ? `<span class="rich-status-punct">${punct}</span>` : '') +
            enrichInline(rest) +
          `</div>`
        );
        i++; continue;
      }

      // Low-signal lines (npm/electron-builder progress, dep warns).
      // Dim only; never hide. Inline classifier still runs so URLs /
      // `code` / paths inside the dim line stay scannable.
      if (isRichLowSignal(plain)) {
        inCmdOutput = false;
        out.push(`<div class="rich-line rich-dim">${enrichInline(line.raw)}</div>`);
        i++; continue;
      }

      // v2 default prose. If we're still inside a command's output
      // window (set when the previous block was a shell line and
      // never cleared by a block detector), tag the line as
      // .rich-cmd-output so CSS can mute it. Otherwise it's a
      // normal prose line.
      const proseCls = inCmdOutput ? 'rich-line rich-cmd-output' : 'rich-line';
      out.push(`<div class="${proseCls}">${enrichInline(line.raw)}</div>`);
      i++;
    }
    return out.join('');
  } catch (err) {
    console.warn('rich output render failed:', err);
    return renderRichFallback(input);
  }
}


/* ────────────────────────────────────────────────────────────────── */

const OUTPUT_MODE_KEY = 'cam_desktop_output_mode'; // 'plain' | 'rich'
export const OUTPUT_HISTORY_INITIAL_LINES = 200;
export const OUTPUT_HISTORY_STEPS = [200, 1000, 2000, 4000, 8000];
const TERMINAL_AGENT_STATUSES = new Set(['completed', 'failed', 'timeout', 'killed']);

export function nextOutputHistoryState(lines = OUTPUT_HISTORY_INITIAL_LINES, full = false) {
  if (full) {
    return { lines: Number(lines) || OUTPUT_HISTORY_STEPS[OUTPUT_HISTORY_STEPS.length - 1], full: true };
  }
  const current = Number(lines) || OUTPUT_HISTORY_INITIAL_LINES;
  const next = OUTPUT_HISTORY_STEPS.find(n => n > current);
  if (next) return { lines: next, full: false };
  return { lines: current, full: true };
}

export function mountAgentConsole({ api, state, showToast }) {
  const titleEl = document.getElementById('agent-header-title');
  const metaEl = document.getElementById('agent-header-meta');
  const outputEl = document.getElementById('agent-output');
  const outputRichEl = document.getElementById('agent-output-rich');
  const terminalEl = document.getElementById('agent-terminal');
  const inputEl = document.getElementById('composer-input');
  const sendBtn = document.getElementById('composer-send');
  const composer = document.getElementById('agent-composer');
  const quickKeyBtns = composer.querySelectorAll('.btn-quick');
  const expandKeysBtn = document.getElementById('expand-keys');
  const extraKeysEl = document.getElementById('extra-keys');
  const modeBtns = document.querySelectorAll('.output-mode-btn');
  const attachBtn = document.getElementById('composer-attach');
  const fileInput = document.getElementById('composer-file-input');
  const uploadStatusEl = document.getElementById('composer-upload-status');
  let pollTimer = null;
  let outputHashPlain = null;
  let outputHashRich = null;
  let inflightOutput = false;
  let inflightHistory = false;
  let pendingHistoryLoad = false;
  let currentAgentId = null;
  let autoScroll = true;
  let composing = false; // IME composition state
  let outputHistoryLines = OUTPUT_HISTORY_INITIAL_LINES;
  let outputHistoryFull = false;
  let historyEndChecked = false;
  let historyStatusText = '';
  let historyStatusKind = 'info';
  let historyStatusTimer = null;
  let term = null;
  let termFit = null;
  let termSessionId = null;
  let termAgentId = null;
  let termOpening = false;
  let termUnsubData = null;
  let termUnsubStatus = null;
  let termResizeObserver = null;

  function readOutputMode() {
    try {
      const v = localStorage.getItem(OUTPUT_MODE_KEY);
      return (v === 'rich' || v === 'terminal') ? v : 'plain';
    } catch { return 'plain'; }
  }
  let outputMode = readOutputMode();

  function activePane() {
    if (outputMode === 'terminal') return terminalEl || outputEl;
    return outputMode === 'rich' ? outputRichEl : outputEl;
  }

  function resetOutputHistory() {
    outputHistoryLines = OUTPUT_HISTORY_INITIAL_LINES;
    outputHistoryFull = false;
    historyEndChecked = false;
    inflightHistory = false;
    pendingHistoryLoad = false;
    historyStatusText = '';
    historyStatusKind = 'info';
    if (historyStatusTimer) {
      clearTimeout(historyStatusTimer);
      historyStatusTimer = null;
    }
  }

  function noteHistoryForText(text, full = false) {
    const lineCount = text ? String(text).split('\n').length : 0;
    const requested = Math.max(outputHistoryLines, OUTPUT_HISTORY_INITIAL_LINES);
    const covering = OUTPUT_HISTORY_STEPS.find(n => n >= Math.max(lineCount, requested)) ||
      OUTPUT_HISTORY_STEPS[OUTPUT_HISTORY_STEPS.length - 1];
    outputHistoryLines = Math.max(outputHistoryLines, covering);
    if (full) outputHistoryFull = true;
  }

  function outputLineCount(text) {
    if (typeof text !== 'string' || !text) return 0;
    return text.split('\n').length;
  }

  function setHistoryStatus(text, kind = 'info', ttl = 2400) {
    historyStatusText = text || '';
    historyStatusKind = kind || 'info';
    if (historyStatusTimer) {
      clearTimeout(historyStatusTimer);
      historyStatusTimer = null;
    }
    if (historyStatusText && ttl > 0) {
      historyStatusTimer = setTimeout(() => {
        historyStatusText = '';
        historyStatusTimer = null;
        updateHistoryControls();
      }, ttl);
    }
    updateHistoryControls();
  }

  function isTerminalAgent(agent) {
    return TERMINAL_AGENT_STATUSES.has(agent?.status);
  }

  function selectedAgent() {
    const id = state.get('selectedAgentId');
    if (!id) return null;
    return (state.get('agents') || []).find(a => a.id === id) || null;
  }

  function isAgentsMode() {
    return (state.get('mode') || 'agents') === 'agents';
  }

  function renderPlaceholder(text) {
    const pane = activePane();
    pane.textContent = text;
    pane.classList.add('placeholder');
  }

  function isConnected() {
    return (state.get('connectionMode') || 'disconnected') !== 'disconnected';
  }

  function isDirectConnection() {
    return (state.get('connectionMode') || 'disconnected') === 'direct';
  }

  function canUseTerminalMode() {
    return isDirectConnection() && !!termBridge();
  }

  function setEnabled(enabled) {
    inputEl.disabled = !enabled;
    sendBtn.disabled = !enabled;
    quickKeyBtns.forEach(b => { b.disabled = !enabled; });
    // CAM-DESK-INP-014: attach is enabled only when a running agent is
    // selected AND the app is connected.
    if (attachBtn) attachBtn.disabled = !(enabled && isConnected());
  }

  function renderHeader(agent) {
    if (!agent) {
      titleEl.textContent = 'No agent selected';
      metaEl.textContent = '';
      return;
    }
    const name = agent.task_name || agent.id?.slice(0, 8) || '';
    titleEl.innerHTML =
      `<span>${escapeHtml(name)}</span>` +
      `<span class="badge badge-${escapeHtml(agent.status || '')}">${escapeHtml(agent.status || 'unknown')}</span>`;
    const parts = [];
    if (agent.tool) parts.push(agent.tool);
    if (agent.context_name) parts.push(agent.context_name);
    if (agent.machine_host) parts.push(agent.machine_host.split('.')[0]);
    if (agent.started_at) parts.push(timeSince(agent.started_at));
    if (agent.exit_reason) parts.push(agent.exit_reason);
    metaEl.textContent = parts.join(' · ');
  }

  function syncModeToggle() {
    const terminalAllowed = canUseTerminalMode();
    if (outputMode === 'terminal' && !terminalAllowed) {
      outputMode = 'plain';
      try { localStorage.setItem(OUTPUT_MODE_KEY, outputMode); } catch {}
      void closeTerminalSession();
    }
    modeBtns.forEach(b => {
      const active = b.dataset.mode === outputMode;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
      if (b.dataset.mode === 'terminal') {
        b.disabled = !terminalAllowed;
        b.title = terminalAllowed
          ? 'Attach to the selected agent over Direct SSH'
          : 'Terminal mode is available only with Direct connection';
      }
    });
    if (outputMode === 'terminal') {
      outputEl.setAttribute('hidden', '');
      outputRichEl.setAttribute('hidden', '');
      if (terminalEl) terminalEl.removeAttribute('hidden');
      if (composer) composer.hidden = true;
      scheduleTerminalFit();
    } else if (outputMode === 'rich') {
      outputEl.setAttribute('hidden', '');
      outputRichEl.removeAttribute('hidden');
      if (terminalEl) terminalEl.setAttribute('hidden', '');
      if (composer) composer.hidden = false;
    } else {
      outputRichEl.setAttribute('hidden', '');
      if (terminalEl) terminalEl.setAttribute('hidden', '');
      outputEl.removeAttribute('hidden');
      if (composer) composer.hidden = false;
    }
  }

  function termBridge() {
    return window.CamBridge && window.CamBridge.term ? window.CamBridge.term : null;
  }

  function writeTerminalStatus(text) {
    if (!term) return;
    term.write(`\r\n\x1b[2m${String(text || '')}\x1b[0m\r\n`);
  }

  function fitTerminalAndNotify() {
    if (!term || !termFit) return;
    try { termFit.fit(); } catch (_) {}
    if (termSessionId) {
      const bridge = termBridge();
      if (bridge) bridge.resize({ sessionId: termSessionId, cols: term.cols, rows: term.rows });
    }
  }

  function scheduleTerminalFit() {
    if (!term || outputMode !== 'terminal') return;
    const raf = window.requestAnimationFrame || ((fn) => window.setTimeout(fn, 0));
    raf(() => {
      fitTerminalAndNotify();
      raf(fitTerminalAndNotify);
    });
  }

  function ensureTerminal() {
    if (!terminalEl) return false;
    if (term) return true;
    const TermCtor = window.Terminal;
    if (!TermCtor) {
      terminalEl.textContent = 'Terminal renderer is unavailable: xterm.js was not loaded.';
      return false;
    }
    term = new TermCtor({
      cursorBlink: true,
      convertEol: false,
      scrollback: 5000,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 12,
      theme: {
        background: '#0d1117',
        foreground: '#e6edf3',
        cursor: '#58a6ff',
        selectionBackground: '#1f6feb66',
      },
    });
    const FitCtor = window.FitAddon && window.FitAddon.FitAddon;
    if (FitCtor) {
      termFit = new FitCtor();
      term.loadAddon(termFit);
    }
    term.open(terminalEl);
    term.onData((data) => {
      const bridge = termBridge();
      if (!bridge || !termSessionId) return;
      bridge.input({ sessionId: termSessionId, data });
    });
    term.onResize(({ cols, rows }) => {
      const bridge = termBridge();
      if (!bridge || !termSessionId) return;
      bridge.resize({ sessionId: termSessionId, cols, rows });
    });
    if (window.ResizeObserver && terminalEl) {
      termResizeObserver = new ResizeObserver(() => scheduleTerminalFit());
      termResizeObserver.observe(terminalEl);
    } else {
      window.addEventListener('resize', scheduleTerminalFit);
    }
    return true;
  }

  function setupTerminalEvents() {
    const bridge = termBridge();
    if (!bridge || termUnsubData || termUnsubStatus) return;
    termUnsubData = bridge.onData((msg) => {
      if (!msg || msg.sessionId !== termSessionId || !term) return;
      term.write(String(msg.data || ''));
    });
    termUnsubStatus = bridge.onStatus((msg) => {
      if (!msg || msg.sessionId !== termSessionId) return;
      if (msg.kind === 'closed') {
        const suffix = msg.error ? `: ${msg.error}` : (msg.code != null ? ` (exit ${msg.code})` : '');
        writeTerminalStatus(`terminal detached${suffix}`);
        termSessionId = null;
      }
    });
  }

  async function closeTerminalSession() {
    const bridge = termBridge();
    const sid = termSessionId;
    termSessionId = null;
    termAgentId = null;
    termOpening = false;
    if (bridge && sid) {
      try { await bridge.close({ sessionId: sid }); } catch (_) {}
    }
  }

  async function openTerminalForSelected(opts = {}) {
    const force = !!(opts && opts.force);
    const agent = selectedAgent();
    if (!agent) {
      if (term) term.clear();
      return;
    }
    const bridge = termBridge();
    if (!bridge) {
      if (terminalEl) terminalEl.textContent = 'Terminal bridge is unavailable.';
      return;
    }
    if (!ensureTerminal()) return;
    setupTerminalEvents();
    scheduleTerminalFit();
    if (!force && (termOpening || (termSessionId && termAgentId === agent.id))) {
      term.focus();
      return;
    }
    await closeTerminalSession();
    termAgentId = agent.id;
    termOpening = true;
    term.clear();
    term.write(`\x1b[2mConnecting terminal to ${agent.task_name || agent.id}...\x1b[0m\r\n`);
    try {
      const res = await bridge.open({ agentId: agent.id, cols: term.cols || 100, rows: term.rows || 30 });
      if (outputMode !== 'terminal' || selectedAgent()?.id !== agent.id) {
        if (res && res.ok && res.sessionId) await bridge.close({ sessionId: res.sessionId });
        return;
      }
      if (!res || !res.ok) {
        term.write(`\r\n\x1b[31mTerminal attach failed: ${res && (res.detail || res.error) || 'unknown'}\x1b[0m\r\n`);
        termSessionId = null;
        return;
      }
      termSessionId = res.sessionId;
      termOpening = false;
      term.write('\x1b[2mAttached. Type directly in this terminal.\x1b[0m\r\n');
      scheduleTerminalFit();
      term.focus();
    } catch (e) {
      term.write(`\r\n\x1b[31mTerminal attach failed: ${e && e.message || e}\x1b[0m\r\n`);
      termSessionId = null;
    } finally {
      termOpening = false;
    }
  }

  function renderCachedOutput(cachedText) {
    if (!cachedText) return false;
    if (outputMode === 'rich') {
      outputRichEl.classList.remove('placeholder');
      outputRichEl.innerHTML = renderRichOutput(cachedText);
      if (autoScroll) outputRichEl.scrollTop = outputRichEl.scrollHeight;
      return true;
    }
    outputEl.textContent = cachedText;
    outputEl.classList.remove('placeholder');
    if (autoScroll) outputEl.scrollTop = outputEl.scrollHeight;
    return true;
  }

  function setMode(next) {
    if (next !== 'plain' && next !== 'rich' && next !== 'terminal') next = 'plain';
    if (next === 'terminal' && !canUseTerminalMode()) {
      syncModeToggle();
      showToast('Terminal mode is available only with Direct connection.', 'warning', 3500);
      return;
    }
    if (next === outputMode) {
      if (next === 'terminal') {
        stopPolling();
        setEnabled(false);
        void openTerminalForSelected({ force: true });
      }
      return;
    }
    const prevMode = outputMode;
    outputMode = next;
    try { localStorage.setItem(OUTPUT_MODE_KEY, outputMode); } catch {}
    syncModeToggle();
    if (prevMode === 'terminal' && outputMode !== 'terminal') {
      void closeTerminalSession();
    }
    if (outputMode === 'terminal') {
      stopPolling();
      setEnabled(false);
      void openTerminalForSelected();
      updateHistoryControls();
      return;
    }
    // CAM-DESK-OUT-022: Plain and Rich share the same captured text.
    // A mode switch must render from cache immediately and NOT trigger
    // a fresh fetch (the bytes are the same; only the local renderer
    // changes). Only when the cache is empty (first-time mode switch
    // before any poll completed) do we kick a single load.
    outputHashRich = null;
    if (currentAgentId) {
      const cached = state.getOutput(currentAgentId);
      if (cached?.text) {
        renderCachedOutput(cached.text);
      } else {
        renderPlaceholder('Loading…');
        void loadOutput({ viaPoll: false });
      }
      updateHistoryControls();
    }
  }

  modeBtns.forEach(b => {
    b.addEventListener('click', () => setMode(b.dataset.mode));
  });

  function selectAgent(agentId) {
    if (agentId === currentAgentId) return;
    currentAgentId = agentId;
    outputHashPlain = null;
    outputHashRich = null;
    autoScroll = true;
    inflightOutput = false;
    resetOutputHistory();

    const agent = selectedAgent();
    renderHeader(agent);

    if (!agent) {
      renderPlaceholder('Select an agent on the left to view output.');
      setEnabled(false);
      stopPolling();
      return;
    }

    if (outputMode === 'terminal') {
      syncModeToggle();
      setEnabled(false);
      stopPolling();
      void openTerminalForSelected({ force: true });
      updateHistoryControls();
      return;
    }

    const cached = state.getOutput(agent.id);
    if (cached?.text && renderCachedOutput(cached.text)) {
      if (outputMode === 'plain') outputHashPlain = cached.hash || null;
    } else {
      renderPlaceholder('Loading…');
    }

    setEnabled(isActiveStatus(agent.status) && isAgentsMode());
    void loadOutput({ viaPoll: false });
    startPolling(agent);
    updateHistoryControls();
  }

  function refreshHeader() {
    const agent = selectedAgent();
    renderHeader(agent);
    if (agent) setEnabled(isActiveStatus(agent.status) && isAgentsMode());
  }

  async function loadOutput(opts = {}) {
    if (outputMode === 'terminal') return;
    const agent = selectedAgent();
    if (!agent || inflightOutput || inflightHistory) return;
    // CAM-DESK-OUT-022 (manual history): when the user has scrolled away
    // from the bottom and is reading older output, suppress poll-driven
    // refreshes so the visible viewport stays put. Manual refreshes
    // (initial select, mode switch with empty cache, jump-to-bottom)
    // pass viaPoll=false and proceed normally. A session that has
    // already loaded full scrollback is exempt because re-applying the
    // same immutable text is a no-op.
    if (opts.viaPoll && !autoScroll && !isTerminalAgent(agent) && !outputHistoryFull) {
      return;
    }
    inflightOutput = true;
    const fetchMode = outputMode;
    try {
      if (outputHistoryFull) {
        const data = await api.agentFullOutput(agent.id, 0);
        if (data?.output) {
          outputHistoryFull = true;
          applyOutput(agent.id, data.output, fetchMode, { full: true });
        } else if (
          activePane().textContent === '' ||
          activePane().classList.contains('placeholder')
        ) {
          renderPlaceholder('(no captured output)');
        }
      } else {
        const hash = fetchMode === 'rich' ? outputHashRich : outputHashPlain;
        const data = await api.agentOutput(agent.id, outputHistoryLines, hash);
        if (data?.hash) {
          if (fetchMode === 'rich') outputHashRich = data.hash;
          else outputHashPlain = data.hash;
        }
        // Do not mark history full from the bounded tail response. Some
        // backends report returned-line counts conservatively, and More+
        // should be the explicit full-buffer check.
        if (!data?.unchanged && typeof data?.output === 'string') {
          applyOutput(agent.id, data.output, fetchMode, { full: outputHistoryFull });
        } else if (
          activePane().textContent === 'Loading…' ||
          (activePane().classList.contains('placeholder') && !activePane().textContent)
        ) {
          renderPlaceholder('(waiting for output)');
        }
      }
    } catch (e) {
      if (api.mode !== 'disconnected') console.warn('agentOutput failed:', e);
      if (activePane().textContent === 'Loading…') {
        const msg = e?.message || String(e);
        renderPlaceholder(`Output unavailable: ${msg}`);
      }
    } finally {
      inflightOutput = false;
      if (pendingHistoryLoad && !inflightHistory) {
        window.setTimeout(() => { void loadMoreOutputHistory('pending'); }, 0);
      }
    }
  }

  async function loadMoreOutputHistory(trigger = 'manual') {
    const agent = selectedAgent();
    if (!agent || inflightHistory || (outputHistoryFull && historyEndChecked)) return;
    if (inflightOutput) {
      pendingHistoryLoad = true;
      setHistoryStatus('Loading more...', 'loading', 0);
      updateHistoryControls();
      return;
    }
    pendingHistoryLoad = false;

    const previousFull = outputHistoryFull;
    const previousEndChecked = historyEndChecked;
    const pane = activePane();
    const previousHeight = pane.scrollHeight;
    const previousTop = pane.scrollTop;
    const beforeText = state.getOutput(agent.id)?.text || '';
    const beforeLines = outputLineCount(beforeText);
    const nextHistory = nextOutputHistoryState(outputHistoryLines, false);
    const requestLines = nextHistory.lines;
    const useFullOutput = nextHistory.full && requestLines === outputHistoryLines;
    outputHashPlain = null;
    outputHashRich = null;
    inflightHistory = true;
    autoScroll = false;
    setHistoryStatus('Loading more...', 'loading', 0);
    updateHistoryControls();

    const fetchMode = outputMode;
    try {
      // Manual load-more grows the tail window first: 200 -> 1000 -> 2000
      // -> 4000 -> 8000. Only after exhausting that ladder do we fall back
      // to fulloutput as a final end-of-buffer check.
      const data = useFullOutput
        ? await api.agentFullOutput(agent.id, 0)
        : await api.agentOutput(agent.id, requestLines, null);
      if (agent.id !== currentAgentId) {
        pendingHistoryLoad = false;
        return;
      }
      if (data?.hash) {
        if (fetchMode === 'rich') outputHashRich = data.hash;
        else outputHashPlain = data.hash;
      }
      if (typeof data?.output === 'string') {
        const afterLines = outputLineCount(data.output);
        const returnedLines = Number.isFinite(Number(data.lines)) ? Number(data.lines) : afterLines;
        outputHistoryLines = Math.max(outputHistoryLines, requestLines);
        outputHistoryFull = useFullOutput || returnedLines < requestLines;
        historyEndChecked = outputHistoryFull;
        applyOutput(agent.id, data.output, fetchMode, {
          full: outputHistoryFull,
          preservePane: pane,
          previousHeight,
          previousTop,
        });
        if (afterLines > beforeLines) {
          setHistoryStatus('More content loaded', 'success', 2400);
        } else if (outputHistoryFull) {
          setHistoryStatus('No more content', 'done', 0);
        } else {
          setHistoryStatus('No new lines', 'done', 2400);
        }
      } else {
        setHistoryStatus('Load failed', 'error', 3000);
      }
    } catch (e) {
      outputHistoryFull = previousFull;
      historyEndChecked = previousEndChecked;
      setHistoryStatus('Load failed', 'error', 3000);
      if (api.mode !== 'disconnected') console.warn(`loadMoreOutputHistory(${trigger}) failed:`, e);
    } finally {
      inflightHistory = false;
      updateHistoryControls();
    }
  }

  function applyOutput(agentId, text, fetchMode, opts = {}) {
    if (agentId !== currentAgentId) return;
    // Mode may have flipped while a fetch was in flight — only render
    // when the fetched format matches the active mode.
    if (fetchMode && fetchMode !== outputMode) return;

    noteHistoryForText(text, Boolean(opts.full));
    const preservePane = opts.preservePane || null;
    const previousHeight = Number(opts.previousHeight) || 0;
    const previousTop = Number(opts.previousTop) || 0;
    const cacheHash = fetchMode === 'rich' ? outputHashRich : outputHashPlain;
    state.setOutput(agentId, text, cacheHash);

    if (outputMode === 'rich') {
      let html;
      try {
        html = renderRichOutput(text);
      } catch (e) {
        console.warn('ansi render failed, falling back to plain:', e);
        outputMode = 'plain';
        try { localStorage.setItem(OUTPUT_MODE_KEY, 'plain'); } catch {}
        syncModeToggle();
        outputEl.textContent = text;
        outputEl.classList.remove('placeholder');
        showToast('Rich render failed; switched to plain.', 'warning', 5000);
        if (autoScroll) outputEl.scrollTop = outputEl.scrollHeight;
        return;
      }
      outputRichEl.classList.remove('placeholder');
      outputRichEl.innerHTML = html;
      if (preservePane === outputRichEl) {
        outputRichEl.scrollTop = Math.max(0, outputRichEl.scrollHeight - previousHeight + previousTop);
      } else if (autoScroll) {
        outputRichEl.scrollTop = outputRichEl.scrollHeight;
      }
    } else {
      outputEl.classList.remove('placeholder');
      outputEl.textContent = text;
      if (preservePane === outputEl) {
        outputEl.scrollTop = Math.max(0, outputEl.scrollHeight - previousHeight + previousTop);
      } else if (autoScroll) {
        outputEl.scrollTop = outputEl.scrollHeight;
      }
    }
    updateHistoryControls();
  }

  function startPolling(agent) {
    stopPolling();
    if (!agent || !isActiveStatus(agent.status)) return;
    pollTimer = setInterval(() => { void loadOutput({ viaPoll: true }); }, 2000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // CAM-DESK-OUT-022: history expansion is MANUAL. Scroll only updates
  // the autoScroll flag (so polling can pause) and the top-edge More+
  // affordance; wheel + PageUp do NOT auto-fetch older history.
  const onScroll = (pane) => {
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
    autoScroll = atBottom;
    updateHistoryControls();
  };
  outputEl.addEventListener('scroll', () => onScroll(outputEl));
  outputRichEl.addEventListener('scroll', () => onScroll(outputRichEl));

  /* ── Floating output controls (CAM-DESK-OUT-022) ──
   * Built dynamically so this slice doesn't touch desktop.html. Hosts:
   *   .output-more-history   — top-right; manual full-history trigger
   *                            and same-slot status indicator
   *   .output-jump-bottom    — bottom-right; appears when user is
   *                            away from bottom; resumes auto-follow.
   * All controls live inside .agent-output-wrap (position:relative)
   * so they float over the active pane without altering layout.
   */
  const outputWrap = outputEl.closest('.agent-output-wrap');
  let moreHistoryBtn = null;
  let jumpBottomBtn = null;
  if (outputWrap) {
    const controls = document.createElement('div');
    controls.className = 'output-float-controls';
    controls.innerHTML = ''
      + '<button type="button" class="output-more-history" hidden '
      +   'aria-live="polite" '
      +   'title="Fetch more content from the tmux buffer">More +</button>'
      + '<button type="button" class="output-jump-bottom" hidden '
      +   'title="Jump to bottom and resume auto-follow">'
      +   '<span aria-hidden="true">&#x2913;</span>'
      +   '<span class="sr-only">Jump to bottom</span>'
      + '</button>';
    outputWrap.appendChild(controls);
    moreHistoryBtn = controls.querySelector('.output-more-history');
    jumpBottomBtn = controls.querySelector('.output-jump-bottom');

    if (moreHistoryBtn) {
      moreHistoryBtn.addEventListener('click', () => {
        if (moreHistoryBtn.disabled) return;
        void loadMoreOutputHistory('manual');
      });
    }
    if (jumpBottomBtn) {
      jumpBottomBtn.addEventListener('click', () => {
        const pane = activePane();
        if (pane) pane.scrollTop = pane.scrollHeight;
        autoScroll = true;
        updateHistoryControls();
        // Trigger one fresh refresh now that we've returned to the
        // live tail (viaPoll=false ensures it isn't suppressed).
        void loadOutput({ viaPoll: false });
      });
    }
  }

  function paneIsAtBottom(pane) {
    if (!pane) return true;
    return pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
  }

  function paneIsAtTop(pane) {
    if (!pane) return false;
    return pane.scrollTop <= 30;
  }

  function updateHistoryControls() {
    if (outputMode === 'terminal') {
      if (moreHistoryBtn) moreHistoryBtn.hidden = true;
      if (jumpBottomBtn) jumpBottomBtn.hidden = true;
      return;
    }
    const agent = selectedAgent();
    const pane = activePane();
    const atTop = paneIsAtTop(pane);
    const busy = inflightHistory || pendingHistoryLoad;
    // More+: present only at the top edge. It doubles as the same-slot
    // status indicator so the top-right affordance never jumps between
    // separate button and status-pill locations.
    if (moreHistoryBtn) {
      let text = 'More +';
      let kind = 'ready';
      let disabled = false;
      if (busy) {
        text = 'Loading...';
        kind = 'loading';
        disabled = true;
      } else if (historyStatusText) {
        text = historyStatusText;
        kind = historyStatusKind;
        disabled = true;
      } else if (outputHistoryFull && historyEndChecked) {
        text = 'No more content';
        kind = 'done';
        disabled = true;
      }
      const visible = !!agent && atTop && (!outputHistoryFull || historyEndChecked || busy || !!historyStatusText);
      moreHistoryBtn.hidden = !visible;
      moreHistoryBtn.disabled = !visible || disabled;
      moreHistoryBtn.textContent = text;
      moreHistoryBtn.dataset.kind = kind;
    }
    // Jump-to-bottom: only when user is away from bottom.
    if (jumpBottomBtn) {
      const showJump = !!agent && !paneIsAtBottom(pane);
      jumpBottomBtn.hidden = !showJump;
    }
  }

  /* ── Composer (textarea, IME-safe) ── */

  function isRelayRequestTimeout(err) {
    return /relay request timeout/i.test(err && err.message ? err.message : String(err || ''));
  }

  function refreshAfterSendTimeout(label) {
    showToast(`${label} confirmation timed out; refreshing output.`, 'warning', 5000);
    window.setTimeout(() => { void loadOutput({ viaPoll: false }); }, 250);
  }

  async function doSend() {
    const agent = selectedAgent();
    if (!agent || !isAgentsMode()) return;
    const text = inputEl.value;
    if (!text) return;
    inputEl.value = '';
    sendBtn.disabled = true;
    try {
      await api.sendInput(agent.id, text, true);
    } catch (e) {
      if (isRelayRequestTimeout(e)) {
        refreshAfterSendTimeout('Send');
      } else {
        inputEl.value = text;
        showToast(`Send failed: ${e.message}`, 'error', 5000);
      }
    } finally {
      sendBtn.disabled = false;
      inputEl.focus();
    }
  }

  sendBtn.addEventListener('click', () => { doSend(); });

  inputEl.addEventListener('compositionstart', () => { composing = true; });
  inputEl.addEventListener('compositionend', () => { composing = false; });

  inputEl.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    // IME-safe: do not send during composition.
    if (composing || e.isComposing || e.keyCode === 229) return;
    // Shift+Enter inserts a newline (textarea default — let it through).
    if (e.shiftKey) return;
    // Plain Enter sends.
    e.preventDefault();
    doSend();
  });

  /* ── Composer attachment (CAM-DESK-INP-010..015) ── */
  function setUploadStatus(text, cls = '') {
    if (!uploadStatusEl) return;
    uploadStatusEl.textContent = text || '';
    uploadStatusEl.classList.remove('is-error', 'is-ok');
    if (cls) uploadStatusEl.classList.add(cls);
    if (text) uploadStatusEl.removeAttribute('hidden');
    else uploadStatusEl.setAttribute('hidden', '');
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result || '';
        const idx = String(result).indexOf(',');
        resolve(idx >= 0 ? String(result).slice(idx + 1) : '');
      };
      reader.onerror = () => reject(new Error('Failed to read file'));
      reader.readAsDataURL(file);
    });
  }

  // Map a paste-event MIME type to a friendly extension for clipboard
  // items that have no filename (most commonly a screenshot from the
  // OS shortcut — Chromium / WebKit deliver such items as a File with
  // an empty `.name`). Anything we don't know about falls back to
  // 'bin' so the upload still goes through.
  const MIME_EXT = {
    'image/png':'png', 'image/jpeg':'jpg', 'image/jpg':'jpg',
    'image/gif':'gif', 'image/webp':'webp', 'image/svg+xml':'svg',
    'image/bmp':'bmp',
    'application/pdf':'pdf',
    'text/plain':'txt', 'text/markdown':'md', 'text/csv':'csv',
    'application/json':'json',
  };
  function generatePastedFilename(mime, index = 0) {
    const ext = MIME_EXT[String(mime || '').toLowerCase()] || 'bin';
    // ISO → YYYYMMDD-HHMMSS (UTC), e.g. 20260602-113045
    const ts = new Date().toISOString()
      .replace(/[-:]/g, '')
      .replace(/\..*/, '')
      .replace('T', '-');
    const suffix = index > 0 ? `-${index + 1}` : '';
    return `pasted-image-${ts}${suffix}.${ext}`;
  }

  // Single-file upload helper used by both the paperclip click path and
  // the paste-to-attach path. Returns a promise that resolves whether
  // the upload succeeded or failed — the caller (sequential loop for
  // paste) does not need to branch. Status text and toasts are updated
  // here; typed composer text is never touched (CAM-DESK-INP-013).
  async function uploadFileAndSend(file, { displayName, progressText } = {}) {
    const agent = selectedAgent();
    if (!agent || !file) return { ok: false, error: 'no_agent_or_file' };
    if (!isAgentsMode() || !isConnected()) {
      setUploadStatus('Cannot upload: agent not active or disconnected.', 'is-error');
      return { ok: false, error: 'not_active' };
    }
    const filename = displayName || file.name || generatePastedFilename(file.type);
    if (attachBtn) attachBtn.disabled = true;
    setUploadStatus(progressText || `Uploading ${filename}…`);
    try {
      // CAM-DESK-INP-015: only the user-chosen filename + base64 bytes
      // leave the browser. We never serialize a local filesystem path.
      const b64 = await fileToBase64(file);
      const resp = await api.uploadFile(agent.id, filename, b64);
      // CAM-DESK-INP-012: send the returned workspace path (matching
      // mobile behavior: send without Enter so the agent sees just the
      // path string and the user can wrap it with prose if they want).
      if (resp && resp.path) {
        await api.sendInput(agent.id, resp.path, false);
        setUploadStatus(`Sent ${filename} -> ${resp.path}`, 'is-ok');
      } else {
        setUploadStatus(`Uploaded ${filename} (no path returned)`, 'is-ok');
      }
      setTimeout(() => {
        if (uploadStatusEl && uploadStatusEl.classList.contains('is-ok')) {
          setUploadStatus('');
        }
      }, 4000);
      return { ok: true, path: resp && resp.path };
    } catch (err) {
      const msg = err?.message || String(err);
      setUploadStatus(`Upload failed: ${msg}`, 'is-error');
      showToast(`Upload failed: ${msg}`, 'error', 5000);
      return { ok: false, error: msg };
    } finally {
      const agentNow = selectedAgent();
      if (attachBtn) {
        attachBtn.disabled = !(agentNow && isActiveStatus(agentNow.status)
          && isAgentsMode() && isConnected());
      }
    }
  }

  if (attachBtn && fileInput) {
    attachBtn.addEventListener('click', () => {
      const agent = selectedAgent();
      if (!agent || attachBtn.disabled) return;
      fileInput.click();
    });
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = '';   // allow re-picking the same file
      if (!file) return;
      await uploadFileAndSend(file);
    });
  }

  // CAM-DESK-INP-016: paste-to-attach. When the user pastes content
  // that contains image/file items (e.g. a screenshot from the OS
  // shortcut, or a file copied in the host file manager), upload each
  // file through the same paperclip path and send the returned
  // workspace path. Ordinary text paste is left untouched — we only
  // intercept when at least one file item is present. Typed text in
  // the composer is preserved across the upload (we never mutate
  // inputEl.value here).
  if (inputEl) {
    inputEl.addEventListener('paste', async (e) => {
      const cd = e.clipboardData || window.clipboardData;
      if (!cd || !cd.items || !cd.items.length) return;
      const files = [];
      for (let i = 0; i < cd.items.length; i++) {
        const item = cd.items[i];
        if (item && item.kind === 'file') {
          const f = item.getAsFile && item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (!files.length) return;   // plain-text paste → default behavior
      // Suppress default paste so the browser does not try to insert a
      // blob URL string into the textarea.
      e.preventDefault();
      const agent = selectedAgent();
      if (!agent || (attachBtn && attachBtn.disabled)) {
        setUploadStatus('Cannot upload pasted content: agent not active or disconnected.', 'is-error');
        return;
      }
      // Sequential to keep status text legible and avoid hammering the
      // upload endpoint. Each call independently updates status; the
      // last successful one stays on screen until the auto-clear.
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const name = file.name || generatePastedFilename(file.type, i);
        const progressText = files.length > 1
          ? `Uploading pasted file ${i + 1}/${files.length}: ${name}…`
          : '';
        // eslint-disable-next-line no-await-in-loop
        await uploadFileAndSend(file, { displayName: name, progressText });
      }
    });
  }

  /* ── Quick keys ── */
  if (expandKeysBtn && extraKeysEl) {
    expandKeysBtn.addEventListener('click', () => {
      if (expandKeysBtn.disabled) return;
      const hidden = extraKeysEl.classList.toggle('hidden');
      expandKeysBtn.textContent = hidden ? '...' : 'x';
      expandKeysBtn.setAttribute('aria-expanded', hidden ? 'false' : 'true');
    });
  }

  function restoreQuickBtn(btn, agent) {
    btn.disabled = !(agent && isActiveStatus(agent.status) && isAgentsMode());
  }

  composer.querySelectorAll('.btn-quick[data-input]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const agent = selectedAgent();
      if (!agent || !isAgentsMode() || btn.disabled) return;
      const text = btn.dataset.input || '';
      btn.disabled = true;
      try {
        await api.sendInput(agent.id, text, false);
      } catch (e) {
        if (isRelayRequestTimeout(e)) refreshAfterSendTimeout('Input');
        else showToast(`Input failed: ${e.message}`, 'error', 5000);
      } finally {
        restoreQuickBtn(btn, selectedAgent() || agent);
      }
    });
  });

  composer.querySelectorAll('.btn-quick[data-key]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const agent = selectedAgent();
      if (!agent || !isAgentsMode() || btn.disabled) return;
      const key = btn.dataset.key;
      btn.disabled = true;
      try {
        await api.sendKey(agent.id, key);
      } catch (e) {
        if (isRelayRequestTimeout(e)) refreshAfterSendTimeout('Key');
        else showToast(`Key failed: ${e.message}`, 'error', 5000);
      } finally {
        restoreQuickBtn(btn, selectedAgent() || agent);
      }
    });
  });

  window.addEventListener('beforeunload', () => {
    if (termUnsubData) { try { termUnsubData(); } catch (_) {} termUnsubData = null; }
    if (termUnsubStatus) { try { termUnsubStatus(); } catch (_) {} termUnsubStatus = null; }
    if (termResizeObserver) { try { termResizeObserver.disconnect(); } catch (_) {} termResizeObserver = null; }
    void closeTerminalSession();
  });

  /* ── Reactivity ── */
  syncModeToggle();
  let prevSelected = state.get('selectedAgentId');
  let prevAgents = state.get('agents');
  let prevMode = state.get('mode');
  let prevConn = state.get('connectionMode');
  state.subscribe(() => {
    const sel = state.get('selectedAgentId');
    const agents = state.get('agents');
    const mode = state.get('mode');
    const conn = state.get('connectionMode');
    if (mode !== prevMode) {
      prevMode = mode;
      if (mode !== 'agents') {
        stopPolling();
        setEnabled(false);
        if (outputMode === 'terminal') void closeTerminalSession();
      } else {
        const agent = selectedAgent();
        if (agent) {
          if (outputMode === 'terminal') {
            stopPolling();
            setEnabled(false);
            void openTerminalForSelected();
          } else {
            startPolling(agent);
            setEnabled(isActiveStatus(agent.status));
          }
        }
      }
      }
    if (sel !== prevSelected) {
      prevSelected = sel;
      selectAgent(sel);
      } else if (agents !== prevAgents) {
      prevAgents = agents;
      refreshHeader();
      const agent = selectedAgent();
      if (agent && isAgentsMode()) {
        if (outputMode === 'terminal') {
          stopPolling();
          setEnabled(false);
        } else if (isActiveStatus(agent.status)) {
          if (!pollTimer) startPolling(agent);
        } else {
          stopPolling();
        }
      }
    }
    if (conn !== prevConn) {
      prevConn = conn;
      syncModeToggle();
      if (outputMode === 'terminal' && !canUseTerminalMode()) {
        setMode('plain');
      }
      // Connection flip can re-enable/disable the attach button and
      // the Direct-only Terminal tab.
      const agent = selectedAgent();
      if (agent && isAgentsMode()) {
        setEnabled(isActiveStatus(agent.status) && outputMode !== 'terminal');
      }
    }
  });

  selectAgent(state.get('selectedAgentId') || null);
}
