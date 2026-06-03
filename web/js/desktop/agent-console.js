/**
 * Desktop agent console — selected-agent output pane, quick-key buttons,
 * and input composer. Reads selection from AppState; sends via CamApi.
 *
 * Composer is a textarea:
 *   - Enter (no modifier)  → send
 *   - Shift+Enter          → newline
 *   - IME composition      → never send, even on Enter
 *
 * Output modes (CAM-DESK-OUT-010..018):
 *   - "plain": default; ANSI-stripped text, same path mobile/PWA uses.
 *   - "rich":  GET /api/agents/<id>/output?format=ansi; rendered into a
 *              sibling pane by an inline SGR-to-HTML renderer below.
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

/* ────────────────────────────────────────────────────────────────── */

const OUTPUT_MODE_KEY = 'cam_desktop_output_mode'; // 'plain' | 'rich'

export function mountAgentConsole({ api, state, showToast, setMode }) {
  const titleEl = document.getElementById('agent-header-title');
  const metaEl = document.getElementById('agent-header-meta');
  const outputEl = document.getElementById('agent-output');
  const outputRichEl = document.getElementById('agent-output-rich');
  const inputEl = document.getElementById('composer-input');
  const sendBtn = document.getElementById('composer-send');
  const composer = document.getElementById('agent-composer');
  const quickKeyBtns = composer.querySelectorAll('.btn-quick');
  const expandKeysBtn = document.getElementById('expand-keys');
  const extraKeysEl = document.getElementById('extra-keys');
  const modeBtns = document.querySelectorAll('.output-mode-btn');
  const editBtn = document.getElementById('agent-edit-btn');
  const attachBtn = document.getElementById('composer-attach');
  const fileInput = document.getElementById('composer-file-input');
  const uploadStatusEl = document.getElementById('composer-upload-status');

  let pollTimer = null;
  let outputHashPlain = null;
  let outputHashRich = null;
  let inflightOutput = false;
  let currentAgentId = null;
  let autoScroll = true;
  let composing = false; // IME composition state

  function readOutputMode() {
    try {
      const v = localStorage.getItem(OUTPUT_MODE_KEY);
      return v === 'rich' ? 'rich' : 'plain';
    } catch { return 'plain'; }
  }
  let outputMode = readOutputMode();

  function activePane() {
    return outputMode === 'rich' ? outputRichEl : outputEl;
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

  function setEnabled(enabled) {
    inputEl.disabled = !enabled;
    sendBtn.disabled = !enabled;
    quickKeyBtns.forEach(b => { b.disabled = !enabled; });
    // CAM-DESK-INP-014: attach is enabled only when a running agent is
    // selected AND the app is connected.
    if (attachBtn) attachBtn.disabled = !(enabled && isConnected());
  }

  function updateEditEnabled() {
    if (!editBtn) return;
    // CAM-DESK-EDIT-010: Edit lives in the Agents view as a
    // selected-agent action; it's available whenever an agent is
    // selected (any status — name/auto_confirm are editable post-run).
    const agent = selectedAgent();
    editBtn.disabled = !agent;
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
    modeBtns.forEach(b => {
      const active = b.dataset.mode === outputMode;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (outputMode === 'rich') {
      outputEl.setAttribute('hidden', '');
      outputRichEl.removeAttribute('hidden');
    } else {
      outputRichEl.setAttribute('hidden', '');
      outputEl.removeAttribute('hidden');
    }
  }

  function setMode(next) {
    if (next !== 'plain' && next !== 'rich') next = 'plain';
    if (next === outputMode) return;
    outputMode = next;
    try { localStorage.setItem(OUTPUT_MODE_KEY, outputMode); } catch {}
    syncModeToggle();
    // Force a fresh fetch in the new format.
    outputHashRich = null;
    if (currentAgentId) {
      const cached = state.getOutput(currentAgentId);
      if (cached?.text && outputMode === 'plain') {
        outputEl.textContent = cached.text;
        outputEl.classList.remove('placeholder');
      } else {
        renderPlaceholder('Loading…');
      }
      loadOutput();
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

    const agent = selectedAgent();
    renderHeader(agent);

    if (!agent) {
      renderPlaceholder('Select an agent on the left to view output.');
      setEnabled(false);
      stopPolling();
      return;
    }

    const cached = state.getOutput(agent.id);
    if (outputMode === 'plain' && cached?.text) {
      outputEl.textContent = cached.text;
      outputEl.classList.remove('placeholder');
      outputHashPlain = cached.hash || null;
    } else {
      renderPlaceholder('Loading…');
    }

    setEnabled(isActiveStatus(agent.status) && isAgentsMode());
    updateEditEnabled();
    loadOutput();
    startPolling(agent);
  }

  function refreshHeader() {
    const agent = selectedAgent();
    renderHeader(agent);
    if (agent) setEnabled(isActiveStatus(agent.status) && isAgentsMode());
    updateEditEnabled();
  }

  async function loadOutput() {
    const agent = selectedAgent();
    if (!agent || inflightOutput) return;
    inflightOutput = true;
    const fetchMode = outputMode;
    try {
      const terminal = ['completed', 'failed', 'timeout', 'killed'].includes(agent.status);
      if (terminal) {
        const data = await api.agentFullOutput(
          agent.id, 0, fetchMode === 'rich' ? 'ansi' : null);
        if (data?.output) {
          applyOutput(agent.id, data.output, fetchMode);
        } else if (
          activePane().textContent === '' ||
          activePane().classList.contains('placeholder')
        ) {
          renderPlaceholder('(no captured output)');
        }
      } else {
        const hash = fetchMode === 'rich' ? outputHashRich : outputHashPlain;
        const data = await api.agentOutput(
          agent.id, 200, hash, fetchMode === 'rich' ? 'ansi' : null);
        if (data?.hash) {
          if (fetchMode === 'rich') outputHashRich = data.hash;
          else outputHashPlain = data.hash;
        }
        if (!data?.unchanged && data?.output) {
          applyOutput(agent.id, data.output, fetchMode);
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
    }
  }

  function applyOutput(agentId, text, fetchMode) {
    if (agentId !== currentAgentId) return;
    // Mode may have flipped while a fetch was in flight — only render
    // when the fetched format matches the active mode.
    if (fetchMode && fetchMode !== outputMode) return;

    if (outputMode === 'rich') {
      let html;
      try {
        html = ansiToHtml(text);
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
      if (autoScroll) outputRichEl.scrollTop = outputRichEl.scrollHeight;
    } else {
      // Plain output is the only path AppState's output cache speaks
      // (it's used by the mobile/PWA detail view too).
      state.setOutput(agentId, text, outputHashPlain);
      outputEl.classList.remove('placeholder');
      outputEl.textContent = text;
      if (autoScroll) outputEl.scrollTop = outputEl.scrollHeight;
    }
  }

  function startPolling(agent) {
    stopPolling();
    if (!agent || !isActiveStatus(agent.status)) return;
    pollTimer = setInterval(loadOutput, 2000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  const onScroll = (pane) => {
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
    autoScroll = atBottom;
  };
  outputEl.addEventListener('scroll', () => onScroll(outputEl));
  outputRichEl.addEventListener('scroll', () => onScroll(outputRichEl));

  /* ── Composer (textarea, IME-safe) ── */

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
      inputEl.value = text;
      showToast(`Send failed: ${e.message}`, 'error', 5000);
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

  /* ── Edit button (CAM-DESK-EDIT-010) ── */
  if (editBtn && typeof setMode === 'function') {
    editBtn.addEventListener('click', () => {
      const agent = selectedAgent();
      if (!agent) return;
      state.set('editAgentId', agent.id);
      setMode('edit');
    });
  }

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
        showToast(`Input failed: ${e.message}`, 'error', 5000);
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
        showToast(`Key failed: ${e.message}`, 'error', 5000);
      } finally {
        restoreQuickBtn(btn, selectedAgent() || agent);
      }
    });
  });

  /* ── Reactivity ── */
  syncModeToggle();
  updateEditEnabled();
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
      } else {
        const agent = selectedAgent();
        if (agent) {
          startPolling(agent);
          setEnabled(isActiveStatus(agent.status));
        }
      }
      updateEditEnabled();
    }
    if (sel !== prevSelected) {
      prevSelected = sel;
      selectAgent(sel);
      updateEditEnabled();
    } else if (agents !== prevAgents) {
      prevAgents = agents;
      refreshHeader();
      const agent = selectedAgent();
      if (agent && isAgentsMode()) {
        if (isActiveStatus(agent.status)) {
          if (!pollTimer) startPolling(agent);
        } else {
          stopPolling();
        }
      }
    }
    if (conn !== prevConn) {
      prevConn = conn;
      // Connection flip can re-enable/disable the attach button.
      const agent = selectedAgent();
      if (agent && isAgentsMode()) {
        setEnabled(isActiveStatus(agent.status));
      }
    }
  });

  selectAgent(state.get('selectedAgentId') || null);
}
