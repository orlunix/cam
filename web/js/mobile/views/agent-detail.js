import { api, state, navigate } from '../app.js';
import { ensureMobileCamBridgeShim } from '../mobile-bridge.js?v=2.3.39';
import { canUseTerminalMode } from '../../shared/term-bridge.js';
import {
  openTerminalForAgent,
  scheduleTerminalFit,
  parkTerminalForAgent,
  resumeTerminalForAgent,
  disposeTerminalForAgent,
  sendTerminalInput,
  sendTerminalKey,
  sendTerminalRaw,
  focusTerminalForAgent,
  scrollTerminalToBottom,
  clearTerminalScrollback,
  detachTerminalSession,
  reattachTerminalForAgent,
  resetTerminalSize,
  getTerminalSessionStats,
  terminalAttachReady,
  terminalAttachInflight,
  setTerminalFontSize,
  getTerminalLiveText,
  setTerminalViewActive,
  terminalSessionReady,
  seedTerminalPreview,
  setTerminalStatus,
} from '../../shared/terminal-mount.js';
import { agentOpsSupported, getHubCapabilities } from '../../shared/hub-capabilities.js';
import { recordAgentAccess } from '../../shared/agent-access.js';
import { readMobileOutputFont, clearTerminalChromeInlineFont } from '../../shared/mobile-appearance.js';
import {
  findAgentByIdentity,
  parseAgentRouteHints,
  agentEndpointHints,
} from '../../shared/agent-helpers.js';
import {
  renderRichOutput,
  setRichOutputVisible,
  setRichOutputFontSize,
  scrollRichOutputToBottom,
  disposeRichOutput,
} from '../rich-output.js';

const OUTPUT_MODE_KEY = 'cam_mobile_output_mode';
const OUTPUT_MODE_DEFAULT_KEY = 'cam_mobile_output_mode_default_v4';
const OUTPUT_MODE_DEFAULT = 'live';

function mobileDirectTerminalDefault() {
  return !!(typeof window !== 'undefined'
    && window.CamBridge
    && typeof window.CamBridge.term_open === 'function');
}

function stripAnsi(text) {
  const s = String(text == null ? '' : text);
  let out = '';
  for (let i = 0; i < s.length; i++) {
    if (s.charCodeAt(i) !== 0x1b) { out += s[i]; continue; }
    const next = s[i + 1];
    if (next === '[') {
      let j = i + 2;
      while (j < s.length) {
        const c = s.charCodeAt(j);
        if (c >= 0x40 && c <= 0x7e) break;
        j++;
      }
      i = j;
      continue;
    }
    if (next === ']') {
      let j = i + 2;
      while (j < s.length) {
        const c = s.charCodeAt(j);
        if (c === 0x07) break;
        if (c === 0x1b && s[j + 1] === '\\') { j += 1; break; }
        j++;
      }
      i = j;
      continue;
    }
    i += 1;
  }
  return out;
}

function outputCaptureSupported() {
  return agentOpsSupported(getHubCapabilities());
}

function loadOutputMode() {
  try {
    // v4: phone Direct defaults to Terminal (auto SSH attach on entry).
    if (localStorage.getItem(OUTPUT_MODE_DEFAULT_KEY) !== 'v4') {
      localStorage.setItem(OUTPUT_MODE_DEFAULT_KEY, 'v4');
      if (mobileDirectTerminalDefault() && canUseTerminalMode(api)) {
        localStorage.setItem(OUTPUT_MODE_KEY, 'terminal');
      } else if (!localStorage.getItem(OUTPUT_MODE_KEY)) {
        localStorage.setItem(OUTPUT_MODE_KEY, OUTPUT_MODE_DEFAULT);
      }
    }
    const v = localStorage.getItem(OUTPUT_MODE_KEY);
    if (v === 'terminal' && canUseTerminalMode(api)) return 'terminal';
    if (v === 'rich' && outputCaptureSupported()) return 'rich';
    if ((v === 'live' || v === 'full') && outputCaptureSupported()) return v;
    if (v === 'full') return 'full';
    if (mobileDirectTerminalDefault() && canUseTerminalMode(api)) return 'terminal';
    return OUTPUT_MODE_DEFAULT;
  } catch {
    return mobileDirectTerminalDefault() ? 'terminal' : OUTPUT_MODE_DEFAULT;
  }
}

export function renderAgentDetail(container, agentId, routeSearch = '') {
  recordAgentAccess(agentId);
  ensureMobileCamBridgeShim();
  const endpointHints = parseAgentRouteHints(routeSearch);
  function resolveAgent(list) {
    return findAgentByIdentity(list || state.get('agents') || [], agentId, endpointHints);
  }
  function agentHints() {
    return endpointHints || agentEndpointHints(agent);
  }

  let outputTimer = null;
  let elapsedTimer = null;
  let outputOffset = 0;
  let useFullOutput = false;
  let outputMode = loadOutputMode();
  let isFullscreen = false;
  let autoScroll = true;
  let _scrollByCode = false;
  let _touching = false;
  let _deferredUpdate = null;
  let _directInput = false;
  let _errorCount = 0;
  let _outputPollMs = 2000;
  let _inflightStart = 0;
  let _inflightTimer = null;     // interval updating the in-flight toast
  let _inflightAbort = null;     // AbortController for cancelling slow requests
  let _lastResponseMs = 0;       // last successful response time in ms
  // Restore last-seen output so the view renders instantly on re-entry
  const _prevOutput = state.getOutput(agentId);
  let cachedOutput = _prevOutput?.text || '';
  let _outputHash = _prevOutput?.hash || null;
  let agent = resolveAgent(state.get('agents') || []);
  const isActiveAgent = agent && ['running', 'starting', 'pending'].includes(agent.status);
  // Phone Direct: always open in Terminal + auto SSH attach when entering a live agent.
  if (isActiveAgent && mobileDirectTerminalDefault() && canUseTerminalMode(api)) {
    outputMode = 'terminal';
    useFullOutput = false;
  }
  // Terminal only when user chose it — capture-only Live mode avoids heavy SSH attach.
  if (canUseTerminalMode(api) && !outputCaptureSupported() && isActiveAgent) {
    outputMode = 'terminal';
    useFullOutput = false;
  }
  // Auto-switch to full output for completed agents (live capture returns empty)
  if (agent && ['completed', 'failed', 'killed', 'timeout'].includes(agent.status)) {
    useFullOutput = true;
    outputMode = 'full';
  } else if (outputMode === 'full') {
    useFullOutput = true;
  } else if (outputMode === 'live') {
    useFullOutput = false;
  }
  let fsOverlay = null;
  let _attachAttempt = 0;
  // Keep this per-detail-view so opening the native keyboard never collapses keys.
  let keybarExpanded = false;
  let bottomStatusTimer = null;
  // tmux copy-mode browsing state (terminal mode only). Only set after the
  // hub verifies the real pane state — mirrors the desktop invariant.
  let copyModeActive = false;
  const UPLOAD_RAW_MAX_BYTES = 18 * 1024 * 1024;

  // Output font size — pinch-to-zoom; mobile terminal default 12px (readable, more cols).
  const FONT_MIN = 8, FONT_MAX = 24;
  const MOBILE_TERMINAL_FONT_DEFAULT = 12;
  let _fontSize = readMobileOutputFont();

  function agentDisplayName(ag) {
    if (!ag) return '';
    return ag.task_name || ag.task?.name || ag.id.slice(0, 8);
  }

  function agentNodeLabel(ag) {
    if (!ag) return '';
    return ag.context_name || ag.hostname || ag.machine_host || '';
  }

  function agentToolLabel(ag) {
    if (!ag) return '';
    return ag.tool || ag.task?.tool || '';
  }

  function terminalMetaLeftLabel(ag, statsAgentId = agentId) {
    const parts = [];
    const tool = agentToolLabel(ag);
    const node = agentNodeLabel(ag);
    if (tool) parts.push(tool);
    if (node) parts.push(node);
    const stats = getTerminalSessionStats(statsAgentId);
    if (stats?.cols && stats?.rows) parts.push(`${stats.cols}×${stats.rows}`);
    const fs = stats?.fontSizePx || _fontSize;
    if (fs) parts.push(`${Math.round(Number(fs))}px`);
    return parts.join(' \u00b7 ') || ag?.id?.slice(0, 8) || '';
  }

  function terminalConnectionState(agentId) {
    const stats = getTerminalSessionStats(agentId);
    if (stats?.attachState === 'ready') {
      return (stats.bytesReceived > 0) ? 'attached' : 'waiting';
    }
    if (stats?.attachState === 'connecting' || stats?.opening) return 'connecting';
    return 'detached';
  }

  function terminalMetaRightLabel(agentId) {
    return terminalConnectionState(agentId);
  }

  function terminalConnectionLabel(ag) {
    if (!ag) return '';
    const user = ag.machine_user || '';
    const host = ag.machine_host || '';
    const port = ag.machine_port;
    if (user && host) {
      const suffix = port && Number(port) !== 22 ? `:${port}` : '';
      const label = `${user}@${host}${suffix}`;
      return label.length > 28 ? `${label.slice(0, 26)}…` : label;
    }
    return ag.context_name || ag.task_name || ag.id.slice(0, 8);
  }

  function bottomStatusHTML() {
    return `
      <div class="output-status-row" id="output-status-row">
        <button type="button" class="jump-bottom-btn history-btn hidden" id="term-history" title="Browse tmux history (copy mode)" aria-label="History"><span aria-hidden="true">\u2912</span></button>
        <span class="output-status-text" id="output-status-text" aria-live="polite"></span>
        <button type="button" class="jump-bottom-btn hidden" id="jump-bottom" title="Jump to bottom and resume auto-follow" aria-label="Jump to bottom"><span aria-hidden="true">&#x2913;</span></button>
      </div>`;
  }

  /** Show/refresh the History button (terminal mode + native bridge only). */
  function syncHistoryChrome() {
    const btn = container.querySelector('#term-history');
    if (!btn) return;
    const visible = isTerminalMode() && mobileTerminalInput();
    btn.classList.toggle('hidden', !visible);
    btn.classList.toggle('is-active', copyModeActive);
    btn.title = copyModeActive ? 'Page up (½ screen) — ⤓ exits copy mode' : 'Browse tmux history (copy mode)';
  }

  function setBottomStatus(text = '', tone = '', clearAfter = 0) {
    const status = container.querySelector('#output-status-text');
    if (!status) return;
    if (bottomStatusTimer) clearTimeout(bottomStatusTimer);
    status.textContent = text;
    status.dataset.tone = tone || '';
    if (clearAfter > 0 && text) {
      bottomStatusTimer = setTimeout(() => {
        if (status.textContent === text) {
          status.textContent = '';
          status.dataset.tone = '';
        }
      }, clearAfter);
    }
  }

  function terminalKeyBarHTML(visible = false) {
    return `
      <div class="terminal-keybar${visible ? '' : ' hidden'}${keybarExpanded ? ' is-expanded' : ''}" id="terminal-keybar">
        <div class="terminal-keybar-row terminal-keybar-primary">
          <label class="term-key term-key-attach" title="Attach image" aria-label="Attach image"><input type="file" id="file-input" accept="image/*" class="terminal-file-input">\u{1F4CE}</label>
          <button type="button" class="term-key" data-term-char="y">y</button>
          <button type="button" class="term-key" data-term-char="1">1</button>
          <button type="button" class="term-key" data-term-key="Enter" title="Enter">\u21b5</button>
          <button type="button" class="term-key" data-term-key="Escape">Esc</button>
          <button type="button" class="term-key" data-term-key="C-c" title="Ctrl-C">^C</button>
          <button type="button" class="term-key" data-term-key="Down" title="Down">\u2193</button>
          <button type="button" class="term-key" data-term-key="BSpace" title="Backspace">⌫</button>
          <button type="button" class="term-key term-key-more" data-term-action="more" aria-expanded="${keybarExpanded}" title="${keybarExpanded ? 'Hide extra keys' : 'Show more keys'}">${keybarExpanded ? 'Less' : 'More'}</button>
        </div>
        <div class="terminal-keybar-row terminal-keybar-secondary">
          <button type="button" class="term-key" data-term-char="2">2</button>
          <button type="button" class="term-key" data-term-char="3">3</button>
          <button type="button" class="term-key" data-term-char="4">4</button>
          <button type="button" class="term-key" data-term-key="Tab">Tab</button>
          <button type="button" class="term-key" data-term-key="BTab">S-Tab</button>
          <button type="button" class="term-key term-key-mod" data-term-mod="ctrl">Ctrl</button>
          <button type="button" class="term-key term-key-mod" data-term-mod="alt">Alt</button>
        </div>
        <div class="terminal-keybar-row terminal-keybar-secondary">
          <button type="button" class="term-key" data-term-key="Up" title="Up">\u2191</button>
          <button type="button" class="term-key" data-term-key="Left" title="Left">\u2190</button>
          <button type="button" class="term-key" data-term-key="Right" title="Right">\u2192</button>
          <button type="button" class="term-key" data-term-key="Home">Home</button>
          <button type="button" class="term-key" data-term-key="End">End</button>
          <button type="button" class="term-key" data-term-key="PPage">PgUp</button>
          <button type="button" class="term-key" data-term-key="NPage">PgDn</button>
        </div>
      </div>`;
  }

  function updateTerminalChrome(termOn) {
    const useTermUi = termOn && mobileTerminalInput();
    // Live Output shares Terminal's compact chrome; only its composer remains visible.
    const useOutputUi = agentIsRunnable() && mobileTerminalInput();
    document.body.classList.toggle('terminal-fullscreen', useOutputUi);
    contentEl.classList.toggle('terminal-ui-active', useOutputUi);
    const title = container.querySelector('.detail-title h2');
    const badge = container.querySelector('.detail-title .badge');
    const meta = container.querySelector('#meta-line');
    const metaBar = container.querySelector('#terminal-meta-bar');
    const keybar = container.querySelector('#terminal-keybar');
    const inputSection = container.querySelector('#input-section');
    if (useOutputUi) {
      if (title) title.textContent = agentDisplayName(agent);
      if (badge) badge.classList.add('hidden');
      clearTerminalChromeInlineFont();
      if (meta) meta.classList.add('hidden');
      if (metaBar) {
        metaBar.classList.remove('hidden');
        updateTerminalMetaBar();
      }
      if (keybar) keybar.classList.remove('hidden');
      const jumpBottom = container.querySelector('#jump-bottom');
      if (jumpBottom) {
        if (useTermUi) {
          // Terminal mode: always available — snaps to bottom, and exits
          // tmux copy mode while browsing history.
          jumpBottom.classList.remove('hidden');
        } else {
          const pane = container.querySelector('#output-pane');
          const atBottom = !pane || pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
          jumpBottom.classList.toggle('hidden', atBottom);
        }
      }
      if (inputSection) {
        inputSection.classList.toggle('is-terminal-hidden', useTermUi);
        inputSection.style.display = useTermUi ? 'none' : '';
      }
    } else {
      document.body.classList.remove('terminal-fullscreen');
      if (title) title.textContent = agentDisplayName(agent);
      if (badge) badge.classList.remove('hidden');
      if (meta) meta.classList.remove('hidden');
      if (metaBar) {
        metaBar.classList.add('hidden');
        const connEl = metaBar.querySelector('.terminal-meta-conn');
        if (connEl) {
          connEl.textContent = '';
          connEl.removeAttribute('data-tone');
          delete connEl.dataset.holdMessage;
        }
      }
      if (keybar) keybar.classList.toggle('hidden', !agentIsRunnable());
      if (inputSection) {
        inputSection.classList.remove('is-terminal-hidden');
        inputSection.style.display = '';
      }
    }
    syncHistoryChrome();
  }

  function wireTerminalKeyBar() {
    const keybar = container.querySelector('#terminal-keybar');
    if (!keybar) return;
    let ctrlLatch = false;
    let altLatch = false;

    const syncModButtons = () => {
      keybar.querySelectorAll('[data-term-mod="ctrl"]').forEach(el => {
        el.classList.toggle('active', ctrlLatch);
      });
      keybar.querySelectorAll('[data-term-mod="alt"]').forEach(el => {
        el.classList.toggle('active', altLatch);
      });
    };

    const sendRaw = async (data) => {
      if (isTerminalMode() && mobileTerminalInput()) {
        if (!terminalSessionReady(agentId)) {
          state.toast('Terminal not connected', 'error');
          return;
        }
        await sendTerminalRaw(agentId, data);
        focusTerminalForAgent(agentId);
        return;
      }
      await sendAgentInput(data, false);
    };

    keybar.querySelectorAll('[data-term-mod]').forEach(btn => {
      btn.addEventListener('click', () => {
        const mod = btn.dataset.termMod;
        if (mod === 'ctrl') {
          ctrlLatch = !ctrlLatch;
          if (ctrlLatch) altLatch = false;
        } else if (mod === 'alt') {
          altLatch = !altLatch;
          if (altLatch) ctrlLatch = false;
        }
        syncModButtons();
      });
    });

    keybar.querySelectorAll('[data-term-key]').forEach(btn => {
      btn.addEventListener('click', () => {
        void sendAgentKey(btn.dataset.termKey).catch(e => state.toast(e.message, 'error'));
        ctrlLatch = false;
        altLatch = false;
        syncModButtons();
      });
    });

    keybar.querySelectorAll('[data-term-char]').forEach(btn => {
      btn.addEventListener('click', () => {
        let ch = btn.dataset.termChar || '';
        if (ctrlLatch && ch.length === 1) {
          const code = ch.toLowerCase().charCodeAt(0);
          ch = code >= 97 && code <= 122 ? String.fromCharCode(code - 96) : ch;
        } else if (altLatch && ch.length === 1) {
          ch = `\x1b${ch}`;
        }
        void sendRaw(ch).catch(e => state.toast(e.message, 'error'));
        ctrlLatch = false;
        altLatch = false;
        syncModButtons();
      });
    });

    keybar.querySelectorAll('[data-term-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.termAction;
        if (action === 'fit') {
          resetTerminalSize(agentId);
          scrollTerminalToBottom(agentId);
        } else if (action === 'more') {
          keybarExpanded = !keybarExpanded;
          keybar.classList.toggle('is-expanded', keybarExpanded);
          btn.textContent = keybarExpanded ? 'Less' : 'More';
          btn.title = keybarExpanded ? 'Hide extra keys' : 'Show more keys';
          btn.setAttribute('aria-expanded', String(keybarExpanded));
          if (_syncVisualViewport) _syncVisualViewport();
          if (isTerminalMode()) requestAnimationFrame(() => scheduleTerminalFit(agentId));
        }
      });
    });

    const host = container.querySelector('#terminal-host');
    if (host) {
      host.addEventListener('click', () => focusTerminalForAgent(agentId));
    }
  }

  function terminalMenuHTML() {
    if (!canUseTerminalMode(api)) return '';
    return `
              <hr>
              <button class="overflow-menu-item" id="term-scroll-bottom">Scroll to bottom</button>
              <button class="overflow-menu-item" id="term-reattach">Reattach terminal</button>
              <button class="overflow-menu-item" id="term-detach">Detach session</button>
`;
  }

  function updateFontSizeLabel() {
    const el = container.querySelector('#font-size-label');
    if (el) el.textContent = `Font: ${_fontSize}px · pinch or A±`;
  }

  function bumpFontSize(delta) {
    _fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round((_fontSize + delta) * 2) / 2));
    applyFontSize();
    state.toast(`Font ${_fontSize}px`, 'success', 1200);
  }

  function fontMenuItemsHTML() {
    return `
              <button class="overflow-menu-item" id="font-larger">Text larger (A+)</button>
              <button class="overflow-menu-item" id="font-smaller">Text smaller (A−)</button>
              <button class="overflow-menu-item" id="reset-zoom">Reset font (${MOBILE_TERMINAL_FONT_DEFAULT}px)</button>
              <div class="overflow-menu-hint" id="font-size-label">Font: ${_fontSize}px · pinch or A±</div>`;
  }

  function _applyFontSize() {
    const sz = _fontSize.toFixed(1) + 'px';
    document.body.style.setProperty('--output-font-size', sz);
    if (document.documentElement) {
      document.documentElement.style.setProperty('--output-font-size', sz);
    }
    document.querySelectorAll('#output-pane, #fs-output-pane').forEach(el => { el.style.fontSize = sz; });
    localStorage.setItem('cam_output_font_size', _fontSize.toFixed(1));
    updateFontSizeLabel();
  }

  function applyFontSize() {
    _applyFontSize();
    if (isTerminalMode()) {
      setTerminalFontSize(agentId, _fontSize);
    }
    if (isRichOutputMode()) {
      setRichOutputFontSize(agentId, _fontSize);
    }
    updateTerminalMetaBar();
  }

  function _wirePinchZoom(pane) {
    let startDist = 0, startSize = 0;
    const onStart = (e) => {
      if (e.touches.length === 2) {
        startDist = Math.hypot(e.touches[1].pageX - e.touches[0].pageX, e.touches[1].pageY - e.touches[0].pageY);
        startSize = _fontSize;
      }
    };
    const onMove = (e) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        const dist = Math.hypot(e.touches[1].pageX - e.touches[0].pageX, e.touches[1].pageY - e.touches[0].pageY);
        if (!startDist) startDist = dist;
        const scale = dist / startDist;
        _fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(startSize * scale * 2) / 2));
        applyFontSize();
      }
    };
    pane.addEventListener('touchstart', onStart, { passive: true, capture: true });
    pane.addEventListener('touchmove', onMove, { passive: false, capture: true });
  }

  // Floating "Copy" button — appears when text is selected inside an output pane.
  // Android WebView's native copy popup doesn't work with immersive fullscreen.
  let _copyCleanups = null;
  function _wireCopyButton(pane) {
    let copyBtn = null;
    const show = (x, y) => {
      if (!copyBtn) {
        copyBtn = document.createElement('button');
        copyBtn.textContent = 'Copy';
        copyBtn.className = 'copy-float-btn';
        document.body.appendChild(copyBtn);
        copyBtn.addEventListener('click', () => {
          const sel = window.getSelection();
          const text = sel ? sel.toString() : '';
          if (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(text).then(
                () => state.toast('Copied', 'success'),
                () => _fallbackCopy(text),
              );
            } else {
              _fallbackCopy(text);
            }
          }
          sel && sel.removeAllRanges();
          hide();
        });
      }
      copyBtn.style.top = Math.max(8, y - 44) + 'px';
      copyBtn.style.left = Math.min(window.innerWidth - 70, Math.max(8, x - 28)) + 'px';
      copyBtn.classList.remove('hidden');
    };
    const hide = () => { if (copyBtn) copyBtn.classList.add('hidden'); };
    const _fallbackCopy = (text) => {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); state.toast('Copied', 'success'); }
      catch { state.toast('Copy failed', 'error'); }
      document.body.removeChild(ta);
    };
    const check = () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString()) { hide(); return; }
      try {
        if (!pane.contains(sel.anchorNode)) { hide(); return; }
      } catch { hide(); return; }
      const r = sel.getRangeAt(0).getBoundingClientRect();
      show(r.left + r.width / 2, r.top);
    };
    document.addEventListener('selectionchange', check);
    // Flush deferred output update once user clears their selection
    const flushDeferred = () => {
      const sel = window.getSelection();
      if ((!sel || sel.isCollapsed) && _deferredUpdate) {
        const { pane: p, text } = _deferredUpdate;
        _deferredUpdate = null;
        _applyPaneUpdate(p, text);
      }
    };
    document.addEventListener('selectionchange', flushDeferred);
    // Clean up on view teardown
    const origCleanup = _copyCleanups;
    _copyCleanups = () => {
      if (origCleanup) origCleanup();
      document.removeEventListener('selectionchange', check);
      document.removeEventListener('selectionchange', flushDeferred);
      if (copyBtn) { copyBtn.remove(); copyBtn = null; }
    };
  }

  // #content is the parent — we toggle flex layout on it
  const contentEl = document.getElementById('content');

  // ======= visualViewport: keyboard padding only (never pin #app height — resume drift) =======
  let _vvCleanup = null;
  let _syncVisualViewport = null;
  let _lastVisualKeyboardGap = -1;
  function setupVisualViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    const getInput = () => container.querySelector('.input-section');
    const sync = () => {
      if (document.hidden) return;
      const gap = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      const keyboardOpen = gap > 40;
      const inp = getInput();
      const bottomControls = container.querySelector('#output-bottom-controls');
      // Active output views keep the composer inside the shared bottom
      // accessory, so only that wrapper receives the keyboard offset.
      if (inp) inp.style.marginBottom = bottomControls ? '' : (keyboardOpen ? `${gap}px` : '');
      if (bottomControls) {
        // Status, keys, and the Live composer move as one accessory above the
        // native keyboard. This preserves More/Less state without overlap.
        bottomControls.style.marginBottom = keyboardOpen ? `${gap}px` : '';
        bottomControls.classList.toggle('keyboard-open', keyboardOpen);
      }
      if (Math.abs(gap - _lastVisualKeyboardGap) > 1) {
        _lastVisualKeyboardGap = gap;
        if (bottomControls && isTerminalMode()) requestAnimationFrame(() => scheduleTerminalFit(agentId));
      }
      if (keyboardOpen && autoScroll) {
        const pane = container.querySelector('#output-pane');
        if (pane) requestAnimationFrame(() => { pane.scrollTop = pane.scrollHeight; });
        const fsPre = fsOverlay ? fsOverlay.querySelector('#fs-output-pane') : null;
        if (fsPre) requestAnimationFrame(() => { fsPre.scrollTop = fsPre.scrollHeight; });
      }
    };
    vv.addEventListener('resize', sync);
    vv.addEventListener('scroll', sync);
    sync();
    _syncVisualViewport = sync;
    _vvCleanup = () => {
      vv.removeEventListener('resize', sync);
      vv.removeEventListener('scroll', sync);
      const inp = getInput();
      if (inp) inp.style.marginBottom = '';
      const bottomControls = container.querySelector('#output-bottom-controls');
      if (bottomControls) {
        bottomControls.style.marginBottom = '';
        bottomControls.classList.remove('keyboard-open');
      }
      _syncVisualViewport = null;
    };
  }
  setupVisualViewport();

  function timeSince(dateStr) {
    if (!dateStr) return '';
    const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d`;
  }

  function renderMeta() {
    const parts = [agent.tool];
    if (agent.context_name) parts.push(agent.context_name);
    parts.push(agent.status);
    if (agent.started_at) parts.push(timeSince(agent.started_at));
    if (agent.auto_confirm === false) parts.push('manual');
    if (agent.exit_reason) parts.push(agent.exit_reason);
    return parts.join(' \u00b7 ');
  }

  function updateTerminalMetaBar() {
    const bar = container.querySelector('#terminal-meta-bar');
    if (!bar) return;
    if (!isTerminalMode() || !mobileTerminalInput()) {
      bar.classList.add('hidden');
      return;
    }
    let leftEl = bar.querySelector('.terminal-meta-left');
    let connEl = bar.querySelector('.terminal-meta-conn');
    if (!leftEl || !connEl) {
      bar.innerHTML = '<span class="terminal-meta-left"></span><span class="terminal-meta-conn"></span>';
      leftEl = bar.querySelector('.terminal-meta-left');
      connEl = bar.querySelector('.terminal-meta-conn');
    }
    leftEl.textContent = terminalMetaLeftLabel(agent, agentId);
    if (!connEl.dataset.holdMessage) {
      const state = terminalConnectionState(agentId);
      connEl.textContent = terminalMetaRightLabel(agentId);
      connEl.dataset.tone = state === 'attached' ? 'ok'
        : (state === 'waiting' || state === 'connecting' ? 'info' : '');
    }
    bar.classList.remove('hidden');
  }

  function isTerminalMode() {
    return outputMode === 'terminal' && canUseTerminalMode(api);
  }

  function isRichOutputMode() {
    return outputMode === 'rich' && outputCaptureSupported();
  }

  function mobileTerminalInput() {
    return !!(window.CamBridge && typeof window.CamBridge.term_open === 'function');
  }

  async function sendAgentInput(text, withEnter = true) {
    if (isTerminalMode() && mobileTerminalInput() && terminalSessionReady(agentId)) {
      return sendTerminalInput(agentId, text, { enter: withEnter });
    }
    return api.sendInput(agentId, text, withEnter, agentHints());
  }

  async function sendAgentKey(key) {
    if (isTerminalMode() && mobileTerminalInput() && terminalSessionReady(agentId)) {
      return sendTerminalKey(agentId, key);
    }
    return api.sendKey(agentId, key, agentHints());
  }

  async function prefetchCapturePreview() {
    if (!outputCaptureSupported()) return;
    const snap = state.getOutput(agentId)?.text || cachedOutput;
    if (snap) {
      seedTerminalPreview(agentId, snap);
      return;
    }
    const hostEl = container.querySelector('#terminal-host');
    if (hostEl) setTerminalStatus(hostEl, 'Loading snapshot…', 'info');
    try {
      const data = await api.agentOutput(agentId, 200, _outputHash, null, agentHints());
      if (data?.hash) _outputHash = data.hash;
      if (data?.output) {
        cachedOutput = data.output;
        state.setOutput(agentId, data.output, data.hash);
        seedTerminalPreview(agentId, data.output);
      }
    } catch { /* attach continues in parallel */ }
  }

  function patchAgentHeader(updated) {
    agent = updated;
    const title = container.querySelector('.detail-title h2');
    if (title) {
      title.textContent = (isTerminalMode() && mobileTerminalInput())
        ? agentDisplayName(updated)
        : (updated.task_name || updated.id.slice(0, 8));
    }
    const badge = container.querySelector('.detail-title .badge');
    if (badge) {
      badge.textContent = updated.status;
      badge.className = `badge badge-${updated.status}`;
    }
    const meta = container.querySelector('#meta-line');
    if (meta && !meta.classList.contains('hidden')) {
      meta.textContent = renderMeta();
    }
    updateTerminalMetaBar();
  }

  function agentIsRunnable() {
    return agent && ['running', 'starting', 'pending'].includes(agent.status);
  }

  function scheduleAutoAttach() {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => { void ensureAgentTerminalAttach(); });
    });
  }

  async function ensureAgentTerminalAttach() {
    if (!agentIsRunnable() || !mobileTerminalInput() || !canUseTerminalMode(api)) return null;
    const terminalHost = container.querySelector('#terminal-host');
    if (!terminalHost) return null;

    if (terminalAttachInflight(agentId)) {
      updateTerminalMetaBar();
      return null;
    }
    if (terminalAttachReady(agentId)) {
      if (isTerminalMode()) {
        terminalHost.classList.add('is-active');
        terminalHost.classList.remove('is-connecting');
        const pane = container.querySelector('#output-pane');
        if (pane) pane.classList.add('is-hidden');
        setTerminalViewActive(agentId, true);
        updateTerminalChrome(true);
        await resumeTerminalForAgent(api, agent, terminalHost);
        focusTerminalForAgent(agentId);
      }
      updateTerminalMetaBar();
      return { ok: true, reused: true };
    }

    const attempt = ++_attachAttempt;

    // Show terminal chrome when user is in Terminal mode; attach runs either way.
    if (isTerminalMode()) {
      terminalHost.classList.add('is-active');
      const pane = container.querySelector('#output-pane');
      if (pane) pane.classList.add('is-hidden');
      setTerminalViewActive(agentId, true);
      updateTerminalChrome(true);
    }

    const res = await openTerminalForAgent(api, agent, terminalHost, { force: false });
    if (attempt !== _attachAttempt) return res;
    if (res && !res.ok && res.error) {
      state.toast(`Terminal attach: ${res.error}`, 'error', 6000);
    } else if (res?.ok && isTerminalMode()) {
      scheduleTerminalFit(agentId);
      focusTerminalForAgent(agentId);
    }
    updateTerminalMetaBar();
    return res;
  }

  function applyOutputMode() {
    const terminalHost = container.querySelector('#terminal-host');
    const richHost = container.querySelector('#rich-output-host');
    const pane = container.querySelector('#output-pane');
    const inputSection = container.querySelector('#input-section');
    const termOn = outputMode === 'terminal';
    const richOn = isRichOutputMode();
    if (termOn && !canUseTerminalMode(api)) {
      outputMode = outputCaptureSupported() ? 'live' : 'full';
      useFullOutput = outputMode === 'full';
      try { localStorage.setItem(OUTPUT_MODE_KEY, outputMode); } catch {}
    }
    const showTerminal = outputMode === 'terminal' && canUseTerminalMode(api);
    if (terminalHost) {
      terminalHost.classList.toggle('is-active', showTerminal);
    }
    setRichOutputVisible(agentId, richHost, richOn);
    if (pane) {
      pane.classList.toggle('is-hidden', showTerminal || richOn);
    }
    setTerminalViewActive(agentId, showTerminal);
    updateTerminalChrome(showTerminal);
    if (inputSection && !showTerminal) {
      const inputText = inputSection.querySelector('#input-text');
      if (inputText) inputText.placeholder = 'Send input...';
    }
    // Terminal attach is only for Terminal mode; Raw/Rich use camc output/input APIs.
    if (showTerminal && agentIsRunnable() && mobileTerminalInput() && canUseTerminalMode(api)) {
      clearInterval(outputTimer);
      outputTimer = null;
      scheduleAutoAttach();
    } else if (!showTerminal && !useFullOutput && outputCaptureSupported()) {
      restartOutputPoll();
    }
    applyFontSize();
  }

  function updateOutputModeMenu() {
    const modes = [
      ['#toggle-terminal', 'terminal'],
      ['#toggle-live', 'live'],
      ['#toggle-rich', 'rich'],
      ['#toggle-full', 'full'],
    ];
    for (const [sel, mode] of modes) {
      const btn = container.querySelector(sel);
      if (btn) btn.classList.toggle('active', outputMode === mode);
    }
  }

  function switchOutputMode(mode) {
    if (outputMode === mode) return;
    outputMode = mode;
    if (mode !== 'terminal') copyModeActive = false;
    useFullOutput = mode === 'full';
    if (mode === 'full') outputOffset = 0;
    if (mode === 'live' || mode === 'rich') {
      cachedOutput = '';
      _outputHash = null;
      autoScroll = true;
      // Raw/Rich share the same long-lived camc output source.
    }
    try { localStorage.setItem(OUTPUT_MODE_KEY, outputMode); } catch {}
    clearInterval(outputTimer);
    outputTimer = null;
    updateOutputModeMenu();
    applyOutputMode();
    const active = agent && ['running', 'starting', 'pending'].includes(agent.status);
    if ((outputMode === 'live' || outputMode === 'rich') && active) {
      void loadOutput();
      restartOutputPoll();
    }
  }

  function restartOutputPoll() {
    clearInterval(outputTimer);
    outputTimer = null;
    if (isTerminalMode() || useFullOutput) return;
    outputTimer = setInterval(loadOutput, _outputPollMs);
  }

  let _fetchActive = false;

  function inputHTML() {
    return `
      <div class="input-bar-sticky live-composer-row">
        <button class="btn-direct btn-sm${_directInput ? ' active' : ''}" id="direct-btn" title="Direct input mode">Aa</button>
        <input type="text" id="input-text" class="input-field" placeholder="Send input..." autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="send">
        <button class="btn-primary btn-sm" id="send-btn">Send</button>
      </div>`;
  }

  const render = async (freshAgent) => {
    if (freshAgent) {
      agent = freshAgent;
    } else {
      agent = resolveAgent(state.get('agents') || []);
      if (!agent) {
        try {
          agent = await api.getAgent(agentId, agentHints());
        } catch (e) {
          container.innerHTML = '<div class="error-state">Agent not found</div>';
          return;
        }
      }
    }

    if (agent && ['running', 'starting', 'pending'].includes(agent.status)
        && mobileDirectTerminalDefault() && canUseTerminalMode(api)) {
      outputMode = 'terminal';
      useFullOutput = false;
    }

    const existingPane = container.querySelector('#output-pane');
    if (existingPane && existingPane.textContent !== 'Loading...') {
      cachedOutput = existingPane.textContent;
    }

    const isActive = ['running', 'starting', 'pending'].includes(agent.status);
    const prompt = agent.prompt || '';
    const showTerminalOnRender = isActive && outputMode === 'terminal' && canUseTerminalMode(api);
    const showRichOnRender = outputMode === 'rich' && outputCaptureSupported();
    const showOutputChromeOnRender = isActive && mobileTerminalInput();

    // Active: chat-style layout — output fills space, input anchored at bottom
    // Completed: normal scrolling layout with prompt/logs/delete
    if (isActive) {
      container.innerHTML = `
        <div class="detail-header">
          <button class="back-btn" id="back-btn">&larr;</button>
          <div class="detail-title">
            <h2>${escapeHtml(agent.task_name || agent.id.slice(0, 8))}</h2>
            <span class="badge badge-${agent.status}">${agent.status}</span>
          </div>
          <div class="detail-actions-inline" style="position:relative;">
            <button class="overflow-menu-btn" id="menu-btn">\u22ee</button>
            <div class="overflow-menu hidden" id="overflow-menu">
              ${canUseTerminalMode(api) ? `<button class="overflow-menu-item ${outputMode === 'terminal' ? 'active' : ''}" id="toggle-terminal">Terminal</button>` : ''}
              ${outputCaptureSupported() ? `<button class="overflow-menu-item ${outputMode === 'live' ? 'active' : ''}" id="toggle-live">Raw output</button>` : ''}
              ${outputCaptureSupported() ? `<button class="overflow-menu-item ${outputMode === 'rich' ? 'active' : ''}" id="toggle-rich">Rich output</button>` : ''}
              <button class="overflow-menu-item" id="toggle-browse">Browse</button>
              <button class="overflow-menu-item" id="refresh-output">Refresh</button>
              <button class="overflow-menu-item" id="toggle-wrap">Scroll mode</button>
              ${fontMenuItemsHTML()}
              ${terminalMenuHTML()}
              <hr>
              <button class="overflow-menu-item" id="agent-settings-btn">Settings</button>
              <button class="overflow-menu-item danger" id="stop-btn">Stop agent</button>
            </div>
          </div>
        </div>

        <div class="detail-meta-compact${showOutputChromeOnRender ? ' hidden' : ''}" id="meta-line">${renderMeta()}</div>
        <div class="terminal-meta-bar${showOutputChromeOnRender ? '' : ' hidden'}" id="terminal-meta-bar"><span class="terminal-meta-left"></span><span class="terminal-meta-conn"></span></div>

        <div class="output-section" id="output-section">
          <div class="output-wrap">
            <div id="terminal-host" class="terminal-host${showTerminalOnRender ? ' is-active is-connecting' : ''}"></div>
            <div id="rich-output-host" class="rich-output-host${showRichOnRender ? ' is-active' : ''}"></div>
            <pre class="output-pane${showTerminalOnRender || showRichOnRender ? ' is-hidden' : ''}" id="output-pane"></pre>
            ${bottomStatusHTML()}
          </div>
          <div class="output-bottom-controls" id="output-bottom-controls">
            ${terminalKeyBarHTML(isActive)}
            <div class="input-section${showTerminalOnRender ? ' is-terminal-hidden' : ''}" id="input-section"${showTerminalOnRender ? ' style="display:none"' : ''}>
              ${inputHTML()}
            </div>
          </div>
        </div>
      `;
      contentEl.classList.add('agent-detail-active');
      if (showOutputChromeOnRender) {
        contentEl.classList.add('terminal-ui-active');
        document.body.classList.add('terminal-fullscreen');
      }
    } else {
      container.innerHTML = `
        <div class="detail-header">
          <button class="back-btn" id="back-btn">&larr;</button>
          <div class="detail-title">
            <h2>${escapeHtml(agent.task_name || agent.id.slice(0, 8))}</h2>
            <span class="badge badge-${agent.status}">${agent.status}</span>
          </div>
          <div class="detail-actions-inline" style="position:relative;">
            <button class="overflow-menu-btn" id="menu-btn">\u22ee</button>
            <div class="overflow-menu hidden" id="overflow-menu">
              ${canUseTerminalMode(api) ? `<button class="overflow-menu-item ${outputMode === 'terminal' ? 'active' : ''}" id="toggle-terminal">Terminal</button>` : ''}
              ${outputCaptureSupported() ? `<button class="overflow-menu-item ${outputMode === 'live' ? 'active' : ''}" id="toggle-live">Raw output</button>` : ''}
              ${outputCaptureSupported() ? `<button class="overflow-menu-item ${outputMode === 'rich' ? 'active' : ''}" id="toggle-rich">Rich output</button>` : ''}
              <button class="overflow-menu-item" id="toggle-browse">Browse</button>
              <button class="overflow-menu-item" id="refresh-output">Refresh</button>
              <button class="overflow-menu-item" id="toggle-wrap">Scroll mode</button>
              ${fontMenuItemsHTML()}
              ${terminalMenuHTML()}
              <hr>
              <button class="overflow-menu-item" id="agent-settings-btn">Settings</button>
              <button class="overflow-menu-item" id="restart-btn">Restart</button>
              <button class="overflow-menu-item danger" id="delete-btn">Delete agent</button>
            </div>
          </div>
        </div>

        <div class="detail-meta-compact" id="meta-line">${renderMeta()}</div>
        <div class="terminal-meta-bar hidden" id="terminal-meta-bar"><span class="terminal-meta-left"></span><span class="terminal-meta-conn"></span></div>

        <div class="output-section" id="output-section">
          <div class="output-wrap">
            <div id="terminal-host" class="terminal-host"></div>
            <div id="rich-output-host" class="rich-output-host${showRichOnRender ? ' is-active' : ''}"></div>
            <pre class="output-pane${showRichOnRender ? ' is-hidden' : ''}" id="output-pane"></pre>
            <button class="jump-bottom-btn hidden" id="jump-bottom">\u2193 Bottom</button>
          </div>
        </div>

        ${prompt ? `
        <details class="detail-collapse">
          <summary class="collapse-summary">Prompt</summary>
          <div class="prompt-text">${escapeHtml(prompt)}</div>
        </details>` : ''}

        <details class="detail-collapse" id="logs-section">
          <summary class="collapse-summary" id="logs-summary">Logs</summary>
          <div class="log-entries" id="log-entries">Loading...</div>
        </details>
      `;
      contentEl.classList.remove('agent-detail-active');
    }

    const pane = container.querySelector('#output-pane');
    if (pane && cachedOutput && !showTerminalOnRender && !showRichOnRender) {
      const shortened = _shortenBoxLines(cachedOutput, pane);
      pane.textContent = shortened;
      cachedOutput = shortened;
      if (autoScroll) pane.scrollTop = pane.scrollHeight;
    } else if (cachedOutput && showRichOnRender) {
      renderRichOutput(agentId, container.querySelector('#rich-output-host'), cachedOutput);
    }

    wireEvents(isActive);
    if (isActive) wireTerminalKeyBar();
    if (_syncVisualViewport) _syncVisualViewport();
    applyFontSize();
    applyOutputMode();
    if (!isTerminalMode()) {
      loadOutput();
    }
    if (!isActive) loadLogs();
    startElapsedTimer(isActive);

    // Sync fullscreen overlay if active
    if (isFullscreen) {
      closeFullscreen();
      openFullscreen(isActive);
    }
  };

  // ======= Fullscreen overlay (IM-style) =======
  function openFullscreen(isActive) {
    if (fsOverlay) return;

    const name = escapeHtml(agent.task_name || agent.id.slice(0, 8));
    const overlay = document.createElement('div');
    overlay.id = 'fs-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999;background:#111;display:flex;flex-direction:column;font-family:inherit;color:#e0e0e0;';

    overlay.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid #333;flex-shrink:0;">
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-weight:600;font-size:14px;">${name}</span>
          <span class="badge badge-${agent.status}" style="font-size:10px;">${agent.status}</span>
        </div>
        <div style="display:flex;gap:4px;">
          <button class="btn-sm" id="fs-refresh">\u21bb</button>
          <button class="btn-sm" id="fs-close">\u2715</button>
        </div>
      </div>
      <div style="flex:1;min-height:0;display:flex;flex-direction:column;position:relative;padding:0 4px;">
        <pre id="fs-output-pane" style="flex:1;overflow-y:auto;overflow-x:auto;margin:0;padding:8px;background:#0d1117;color:#c9d1d9;font-family:SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word;border-radius:4px;-webkit-overflow-scrolling:touch;-webkit-user-select:text;user-select:text;"></pre>
        <button class="jump-bottom-btn hidden" id="fs-jump-bottom" style="position:absolute;bottom:12px;right:16px;">\u2193 Bottom</button>
      </div>
      ${isActive ? `<div style="flex-shrink:0;padding:6px 8px;border-top:1px solid #333;">${inputHTML()}</div>` : ''}
    `;

    document.body.appendChild(overlay);
    fsOverlay = overlay;

    const pre = overlay.querySelector('#fs-output-pane');
    pre.textContent = cachedOutput || 'Loading...';
    _applyFontSize();
    _applyWrap();
    _wirePinchZoom(pre);

    overlay.querySelector('#fs-close').addEventListener('click', closeFullscreen);
    overlay.querySelector('#fs-refresh').addEventListener('click', () => {
      outputOffset = 0; cachedOutput = ''; pre.textContent = ''; loadFsOutput();
    });

    const jumpBtn = overlay.querySelector('#fs-jump-bottom');
    pre.addEventListener('scroll', () => {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
      autoScroll = atBottom;
      jumpBtn.classList.toggle('hidden', atBottom);
    });
    pre.addEventListener('touchstart', () => { _touching = true; }, { passive: true });
    pre.addEventListener('touchend', _onTouchEnd, { passive: true });
    pre.addEventListener('touchcancel', _onTouchEnd, { passive: true });
    jumpBtn.addEventListener('click', () => {
      autoScroll = true; jumpBtn.classList.add('hidden'); pre.scrollTop = pre.scrollHeight;
    });

    if (isActive) wireInputButtons(overlay);
    loadFsOutput();
    if (autoScroll) pre.scrollTop = pre.scrollHeight;
  }

  function closeFullscreen() {
    isFullscreen = false;
    if (fsOverlay) {
      fsOverlay.remove();
      fsOverlay = null;
    }
  }

  async function loadFsOutput() {
    const pre = fsOverlay ? fsOverlay.querySelector('#fs-output-pane') : null;
    if (!pre) return;

    try {
      if (useFullOutput) {
        const data = await api.agentFullOutput(agentId, outputOffset);
        if (data.output) {
          updatePane(pre, data.output);
          outputOffset = data.next_offset || outputOffset;
        }
      } else {
        const data = await api.agentOutput(agentId, 200, _outputHash, null, agentHints());
        if (data.hash) _outputHash = data.hash;
        if (!data.unchanged && data.output) {
          updatePane(pre, data.output);
        }
      }
      _errorCount = 0;
    } catch {
      _errorCount++;
      if (_errorCount >= 3) _outputHash = null;
    }
  }

  function wireInputButtons(root) {
    const inputText = root.querySelector('#input-text');
    const sendBtn = root.querySelector('#send-btn');
    if (sendBtn && inputText) {
      let composing = false;
      let sending = false;
      inputText.addEventListener('compositionstart', () => { composing = true; });
      inputText.addEventListener('compositionend', () => { composing = false; });
      if (window.CamBridge) {
        inputText.addEventListener('focus', () => {
          setTimeout(() => {
            try { inputText.scrollIntoView({ block: 'end', behavior: 'smooth' }); } catch {}
          }, 300);
        });
      }
      const doSend = async () => {
        const text = inputText.value;
        if (!text || sending) return;
        // Optimistic: clear input and disable button immediately
        inputText.value = '';
        sending = true;
        sendBtn.disabled = true;
        sendBtn.textContent = '...';
        const preview = text.length > 20 ? text.slice(0, 20) + '\u2026' : text;
        try {
          await _tracked(`Sending "${preview}"`, () => sendAgentInput(text), true);
          if (!isTerminalMode()) {
            _outputHash = null;
            void loadOutput();
          }
        }
        catch (e) {
          // Restore text on failure so user can retry
          inputText.value = text;
          state.toast(e.message, 'error');
        }
        sending = false;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send';
      };
      sendBtn.addEventListener('click', () => setTimeout(doSend, 50));
      inputText.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !composing) { e.preventDefault(); setTimeout(doSend, 50); }
      });
      // Direct input mode: send each new character immediately without Enter
      let _prevDirectVal = '';
      inputText.addEventListener('input', (e) => {
        if (!_directInput || composing) return;
        const cur = inputText.value;
        // Find the newly typed portion
        const added = cur.startsWith(_prevDirectVal) ? cur.slice(_prevDirectVal.length) : cur;
        _prevDirectVal = cur;
        if (!added) return;
        sendAgentInput(added, false).catch(err => state.toast(err.message, 'error'));
      });
    }

    // Direct input toggle
    const directBtn = root.querySelector('#direct-btn');
    if (directBtn && inputText) {
      directBtn.addEventListener('click', () => {
        _directInput = !_directInput;
        directBtn.classList.toggle('active', _directInput);
        if (_directInput) {
          _prevDirectVal = inputText.value;
          inputText.placeholder = 'Direct mode: keys sent instantly';
        } else {
          inputText.value = '';
          _prevDirectVal = '';
          inputText.placeholder = 'Send input...';
        }
        inputText.focus();
      });
    }

    const expandBtn = root.querySelector('#expand-keys');
    const extraKeys = root.querySelector('#extra-keys');
    if (expandBtn && extraKeys) {
      expandBtn.addEventListener('click', () => {
        const show = extraKeys.classList.toggle('hidden');
        expandBtn.textContent = show ? '\u00b7\u00b7\u00b7' : '\u00d7';
      });
    }

    // Quick-action buttons: fire-and-forget with brief disable to prevent double-tap
    const _keyLabels = { 'C-c': '^C', 'Escape': 'Esc', 'Enter': 'Enter', 'BSpace': 'Backspace', 'Tab': 'Tab', 'BTab': 'Shift-Tab', 'DC': 'Del', 'Left': '\u2190', 'Right': '\u2192', 'Up': '\u2191', 'Down': '\u2193', 'PPage': 'PgUp', 'NPage': 'PgDn', 'Home': 'Home', 'End': 'End' };
    function quickSend(btn, label, fn) {
      btn.addEventListener('click', () => {
        if (btn.disabled) return;
        btn.disabled = true;
        btn.style.opacity = '0.4';
        _tracked(label, fn, true).catch(e => state.toast(e.message, 'error'))
          .finally(() => { btn.disabled = false; btn.style.opacity = ''; });
      });
    }
    root.querySelectorAll('.btn-quick[data-input]').forEach(btn => {
      const ch = btn.dataset.input;
      quickSend(btn, `Sending "${ch}"`, () => sendAgentInput(ch, false));
    });
    root.querySelectorAll('.btn-quick[data-key]').forEach(btn => {
      const key = btn.dataset.key;
      const label = `Sending ${_keyLabels[key] || key}`;
      quickSend(btn, label, () => sendAgentKey(key));
    });

    // File upload. The server accepts Base64, not a phone-local URI.
    const uploadBtn = root.querySelector('#upload-btn');
    const fileInput = root.querySelector('#file-input');
    if (fileInput) {
      if (uploadBtn) uploadBtn.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', async () => {
        const file = fileInput.files[0];
        if (!file) return;
        fileInput.value = '';
        if (file.size > UPLOAD_RAW_MAX_BYTES) {
          setBottomStatus(`Attachment blocked: ${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB; max 18 MB`, 'warning', 1200);
          return;
        }
        if (isTerminalMode() && mobileTerminalInput() && !terminalSessionReady(agentId)) {
          setBottomStatus('Attachment blocked: terminal is not connected', 'warning', 1200);
          return;
        }
        setBottomStatus(`Uploading ${file.name}…`, 'info', 0);
        try {
          const b64 = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
              const result = String(reader.result || '');
              const comma = result.indexOf(',');
              if (comma < 0) reject(new Error('Could not encode image'));
              else resolve(result.slice(comma + 1));
            };
            reader.onerror = () => reject(new Error('Failed to read image'));
            reader.readAsDataURL(file);
          });
          const resp = await api.uploadFile(agentId, file.name, b64);
          const path = resp && String(resp.path || '').trim();
          if (!path) {
            setBottomStatus(`Upload warning: ${file.name}; server returned no path`, 'warning', 1200);
            return;
          }
          await sendAgentInput(path, false);
          setBottomStatus(`Attached ${file.name}`, 'ok', 1200);
        } catch (e) {
          const msg = e?.message || (typeof e === 'string' ? e : JSON.stringify(e));
          setBottomStatus(`Upload failed: ${msg}`, 'error', 1200);
        }
      });
    }
  }

  // ======= Normal mode event wiring =======
  function wireEvents(isActive) {
    container.querySelector('#back-btn').addEventListener('click', () => navigate('/'));

    // Overflow menu toggle
    const menuBtn = container.querySelector('#menu-btn');
    const menu = container.querySelector('#overflow-menu');
    if (menuBtn && menu) {
      menuBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        menu.classList.toggle('hidden');
      });
      document.addEventListener('click', (e) => {
        if (!menu.contains(e.target) && e.target !== menuBtn) {
          menu.classList.add('hidden');
        }
      });
    }

    const closeMenu = () => { if (menu) menu.classList.add('hidden'); };

    container.querySelector('#refresh-output').addEventListener('click', () => {
      closeMenu();
      if (isTerminalMode()) {
        const terminalHost = container.querySelector('#terminal-host');
        void openTerminalForAgent(api, agent, terminalHost, { force: true });
        return;
      }
      outputOffset = 0;
      cachedOutput = '';
      const pane = container.querySelector('#output-pane');
      if (pane) pane.textContent = '';
      loadOutput();
    });



    const toggleTerminal = container.querySelector('#toggle-terminal');
    if (toggleTerminal) {
      toggleTerminal.addEventListener('click', () => {
        closeMenu();
        switchOutputMode('terminal');
      });
    }

    container.querySelector('#toggle-live').addEventListener('click', () => {
      closeMenu();
      switchOutputMode('live');
    });

    container.querySelector('#toggle-rich')?.addEventListener('click', () => {
      closeMenu();
      switchOutputMode('rich');
    });

    container.querySelector('#toggle-browse')?.addEventListener('click', () => {
      closeMenu();
      navigate(`/agent/${encodeURIComponent(agentId)}/files`);
    });



    // Scroll mode: toggle between pre-wrap (wraps lines) and pre (horizontal scroll)
    let _wrapMode = localStorage.getItem('cam_output_wrap') !== 'scroll';
    const _applyWrap = () => {
      document.querySelectorAll('#output-pane, #fs-output-pane').forEach(el => {
        el.style.whiteSpace = _wrapMode ? 'pre-wrap' : 'pre';
        el.style.wordBreak = _wrapMode ? 'break-word' : 'normal';
      });
      const btn = container.querySelector('#toggle-wrap');
      if (btn) btn.textContent = _wrapMode ? 'Scroll mode' : 'Wrap mode';
    };
    container.querySelector('#toggle-wrap')?.addEventListener('click', () => {
      closeMenu();
      _wrapMode = !_wrapMode;
      localStorage.setItem('cam_output_wrap', _wrapMode ? 'wrap' : 'scroll');
      _applyWrap();
    });

    // Reset font to default
    container.querySelector('#font-larger')?.addEventListener('click', () => {
      closeMenu();
      bumpFontSize(1);
    });
    container.querySelector('#font-smaller')?.addEventListener('click', () => {
      closeMenu();
      bumpFontSize(-1);
    });
    container.querySelector('#reset-zoom')?.addEventListener('click', () => {
      closeMenu();
      _fontSize = MOBILE_TERMINAL_FONT_DEFAULT;
      applyFontSize();
      if (isTerminalMode()) resetTerminalSize(agentId);
    });

    const wireTermBtn = (id, fn) => {
      const btn = container.querySelector(id);
      if (!btn) return;
      btn.addEventListener('click', async () => {
        closeMenu();
        try {
          await fn();
        } catch (e) {
          state.toast(e?.message || String(e), 'error');
        }
      });
    };
    wireTermBtn('#term-scroll-bottom', async () => {
      if (!scrollTerminalToBottom(agentId)) throw new Error('Terminal not open');
      state.toast('Scrolled to bottom', 'success', 1500);
    });
    wireTermBtn('#term-reattach', async () => {
      const host = container.querySelector('#terminal-host');
      const res = await reattachTerminalForAgent(api, agent, host);
      if (!res?.ok) throw new Error(res?.error || 'Reattach failed');
      state.toast('Terminal reattached', 'success', 2000);
    });
    wireTermBtn('#term-detach', async () => {
      if (!await detachTerminalSession(agentId)) throw new Error('No active session');
      state.toast('Session detached', 'success', 2000);
    });



    const settingsBtn = container.querySelector('#agent-settings-btn');
    if (settingsBtn) settingsBtn.addEventListener('click', () => {
      closeMenu();
      navigate(`/agent/${agentId}/settings`);
    });

    const outputWrap = container.querySelector('.output-wrap');
    if (outputWrap) _wirePinchZoom(outputWrap);

    const pane = container.querySelector('#output-pane');
    if (pane) {
      _applyWrap(); // Apply saved wrap/scroll mode
      _wirePinchZoom(pane);
      _wireCopyButton(pane);
      pane.addEventListener('scroll', () => {
        if (_scrollByCode) return;
        const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
        if (!atBottom) autoScroll = false;
        const jumpBtn = container.querySelector('#jump-bottom');
        if (jumpBtn) jumpBtn.classList.toggle('hidden', atBottom);
      });
      pane.addEventListener('touchstart', () => { _touching = true; }, { passive: true });
      pane.addEventListener('touchend', _onTouchEnd, { passive: true });
      pane.addEventListener('touchcancel', _onTouchEnd, { passive: true });
    }

    updateFontSizeLabel();

    const jumpBtn = container.querySelector('#jump-bottom');
    const historyBtn = container.querySelector('#term-history');
    if (historyBtn) {
      historyBtn.addEventListener('click', async () => {
        if (!isTerminalMode()) return;
        // Key-stream copy mode: bytes ride the live PTY (typing speed),
        // no per-click SSH exec. State is optimistic — camc sessions use
        // the default C-b prefix (camc tmux.conf sets no prefix).
        try {
          if (!copyModeActive) {
            await sendTerminalRaw(agentId, '\x02['); // C-b [ → copy mode
            copyModeActive = true;
            syncHistoryChrome();
            setBottomStatus('Copy mode — ⤒ ½ up · ⤓ exit', 'info');
          } else {
            const stats = getTerminalSessionStats(agentId);
            const half = Math.max(1, Math.floor((stats?.rows || 24) / 2));
            await sendTerminalRaw(agentId, '\x1b[A'.repeat(half)); // Up × rows/2
          }
        } catch (e) {
          setBottomStatus(e.message || 'History failed', 'error', 3000);
        }
      });
    }

    if (jumpBtn) {
      jumpBtn.addEventListener('click', async () => {
        autoScroll = true;
        if (isTerminalMode()) {
          if (copyModeActive) {
            try {
              await sendTerminalRaw(agentId, 'q'); // copy-mode quit
            } catch (e) {
              setBottomStatus(e.message || 'Could not exit copy mode', 'error', 3000);
              return;
            }
            copyModeActive = false;
            syncHistoryChrome();
            setBottomStatus('', 'info');
          }
          scrollTerminalToBottom(agentId);
          return;
        }
        if (isRichOutputMode()) {
          scrollRichOutputToBottom(agentId);
          jumpBtn.classList.add('hidden');
          return;
        }
        jumpBtn.classList.add('hidden');
        if (pane) {
          _scrollByCode = true;
          pane.scrollTop = pane.scrollHeight;
          requestAnimationFrame(() => { _scrollByCode = false; });
        }
      });
    }
    syncHistoryChrome();

    const stopBtn = container.querySelector('#stop-btn');
    if (stopBtn) stopBtn.addEventListener('click', async () => {
      closeMenu();
      try { await api.stopAgent(agentId); state.toast('Agent stopped', 'success'); }
      catch (e) { state.toast(e.message, 'error'); }
    });

    const restartBtn = container.querySelector('#restart-btn');
    if (restartBtn) restartBtn.addEventListener('click', async () => {
      closeMenu();
      try {
        const newAgent = await api.restartAgent(agentId);
        state.toast('Agent restarted', 'success');
        const resp = await api.listAgents({ limit: 50 });
        state.set('agents', resp.agents || []);
        navigate(`/agent/${newAgent.id}`);
      } catch (e) { state.toast(e.message, 'error'); }
    });

    const deleteBtn = container.querySelector('#delete-btn');
    if (deleteBtn) deleteBtn.addEventListener('click', async () => {
      closeMenu();
      if (!confirm('Delete this agent from history?')) return;
      try {
        await api.deleteAgentHistory(agentId);
        state.toast('Agent deleted', 'success');
        const resp = await api.listAgents({ limit: 50 });
        state.set('agents', resp.agents || []);
        navigate('/');
      } catch (e) { state.toast(e.message, 'error'); }
    });

    if (isActive) wireInputButtons(container);
  }

  function startElapsedTimer(isActive) {
    clearInterval(elapsedTimer);
    if (!isActive || !agent.started_at) return;
    elapsedTimer = setInterval(() => {
      const el = container.querySelector('#meta-line');
      if (el) el.textContent = renderMeta();
      if (isTerminalMode()) updateTerminalMetaBar();
    }, 1000);
  }

  // Shorten box-drawing horizontal lines to fit pane width
  const _boxCharRe = /[─━═╌╍┄┅┈┉╶╴╸╺]+/g;
  let _cachedCharW = 0;
  let _cachedPaneW = 0;
  function _measureCharW(pane) {
    if (!pane) return 7.2;
    const pw = pane.clientWidth;
    if (_cachedCharW && pw === _cachedPaneW) return _cachedCharW;
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:inherit;';
    probe.textContent = '──────────'; // 10 box chars
    pane.appendChild(probe);
    _cachedCharW = probe.offsetWidth / 10 || 7.2;
    pane.removeChild(probe);
    _cachedPaneW = pw;
    return _cachedCharW;
  }
  // Invalidate cached charW on window resize
  const _onResize = () => { _cachedPaneW = 0; };
  window.addEventListener('resize', _onResize);

  function _shortenBoxLines(text, pane) {
    const charW = _measureCharW(pane);
    const contentW = pane ? (pane.clientWidth - 24) : 300; // subtract left+right padding (12+12)
    const maxCols = Math.floor(contentW / charW);
    if (maxCols >= 100) return text;
    return text.split('\n').map(line => {
      if (line.length <= maxCols) return line;
      let overflow = line.length - maxCols;
      if (overflow <= 0) return line;
      // Shrink box-drawing runs from longest to shortest
      return line.replace(_boxCharRe, (m) => {
        if (overflow <= 0 || m.length < 6) return m;
        const cut = Math.min(m.length - 3, overflow);
        overflow -= cut;
        return m.slice(0, m.length - cut);
      });
    }).join('\n');
  }

  function updatePane(pane, newText) {
    // Only touch DOM if content actually changed — avoids scroll glitch
    const cleaned = stripAnsi(newText).replace(/\n([ \t]*\n)*[ \t]*$/, '\n');
    // Cache raw (unshortenened) output so re-entry can re-shorten for current pane width
    state.setOutput(agentId, cleaned, _outputHash);
    const trimmed = _shortenBoxLines(cleaned, pane);
    if (cachedOutput === trimmed) return;
    // Defer DOM update while user is actively touching/scrolling
    if (_touching) {
      _deferredUpdate = { pane, text: trimmed };
      return;
    }
    _applyPaneUpdate(pane, trimmed);
  }

  function _onTouchEnd() {
    _touching = false;
    // Don't flush deferred update if user has an active selection (let them copy first)
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
    if (_deferredUpdate) {
      const { pane, text } = _deferredUpdate;
      _deferredUpdate = null;
      _applyPaneUpdate(pane, text);
    }
  }

  function _applyPaneUpdate(pane, trimmed) {
    // Don't nuke DOM while user has text selected — defer until selection clears
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) {
      try {
        if (pane.contains(sel.anchorNode)) {
          _deferredUpdate = { pane, text: trimmed };
          return;
        }
      } catch { /* anchorNode may be detached */ }
    }
    _scrollByCode = true;
    pane.textContent = trimmed;
    cachedOutput = trimmed;
    if (autoScroll) {
      pane.scrollTop = pane.scrollHeight;
    }
    requestAnimationFrame(() => { _scrollByCode = false; });
  }

  // --- In-flight status toast ---
  // Tracks any in-flight request (output fetch, key send, text input)
  // and shows a floating toast with live context when it takes > 1s.
  let _inflightLabel = '';  // e.g. "Fetching output", "Sending Esc", "Sending ^C"

  function _viaPath() {
    const mode = state.get('connectionMode') || 'direct';
    const ctx = agent?.context_name || '';
    let via = mode === 'relay' ? 'relay' : 'direct';
    if (ctx) via += ' \u2192 ' + ctx;
    return via;
  }

  function _showInflightToast() {
    let el = document.getElementById('inflight-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'inflight-toast';
      el.className = 'inflight-toast';
      document.body.appendChild(el);
    }
    return el;
  }
  function _removeInflightToast() {
    clearInterval(_inflightTimer);
    _inflightTimer = null;
    const el = document.getElementById('inflight-toast');
    if (el) {
      el.classList.add('inflight-toast-out');
      setTimeout(() => el.remove(), 200);
    }
  }
  let _inflightImmediate = false;  // true = user action in progress (higher priority)
  let _inflightId = 0;             // tracks which action owns the toast
  function _startInflightTracking(label, immediate) {
    // Don't let background fetch overwrite an active user action
    if (!immediate && _inflightImmediate && _inflightStart) return null;
    // If a send preempts, the old tracking is orphaned (its finish will be ignored)
    const id = ++_inflightId;
    _inflightLabel = label || 'Request';
    _inflightStart = Date.now();
    _inflightImmediate = !!immediate;
    clearInterval(_inflightTimer);
    if (immediate) {
      _updateInflightToast();
      _inflightTimer = setInterval(_updateInflightToast, 200);
    } else {
      _inflightTimer = setTimeout(() => {
        _inflightTimer = setInterval(_updateInflightToast, 200);
        _updateInflightToast();
      }, 1000);
    }
    return id;
  }
  function _updateInflightToast() {
    if (!_inflightStart) return;
    const elapsed = ((Date.now() - _inflightStart) / 1000).toFixed(1);
    const via = _viaPath();

    const el = _showInflightToast();
    el.classList.remove('inflight-toast-out', 'inflight-toast-ok', 'inflight-toast-err');
    const slow = parseFloat(elapsed) >= 5.0;
    el.innerHTML = slow
      ? `<span>\u26a0 ${_inflightLabel} via ${via} \u2026 ${elapsed}s</span><button class="inflight-cancel" id="inflight-cancel-btn">Cancel</button>`
      : `<span>\u23f3 ${_inflightLabel} via ${via} \u2026 ${elapsed}s</span>`;
    const cancelBtn = el.querySelector('#inflight-cancel-btn');
    if (cancelBtn) {
      cancelBtn.onclick = () => {
        if (_inflightAbort) _inflightAbort.abort();
        _removeInflightToast();
      };
    }
  }
  function _finishInflightTracking(id, success, doneLabel) {
    // Ignore if a newer action has taken over the toast
    if (id !== _inflightId) return;
    if (!_inflightStart) return;
    const elapsed = Date.now() - _inflightStart;
    _lastResponseMs = elapsed;
    _inflightStart = 0;
    _inflightImmediate = false;
    clearInterval(_inflightTimer);
    _inflightTimer = null;

    // Always show result briefly
    const el = _showInflightToast();
    const via = _viaPath();
    const secs = (elapsed / 1000).toFixed(1);
    const label = doneLabel || _inflightLabel;
    el.innerHTML = success
      ? `<span>\u2713 ${label} \u00b7 ${secs}s via ${via}</span>`
      : `<span>\u2717 ${label} failed \u00b7 ${secs}s</span>`;
    el.classList.remove('inflight-toast-out', 'inflight-toast-ok', 'inflight-toast-err');
    el.classList.toggle('inflight-toast-ok', success);
    el.classList.toggle('inflight-toast-err', !success);
    setTimeout(() => _removeInflightToast(), 1200);
  }

  // Convenience: wrap an async action with inflight tracking
  // immediate=true shows toast instantly (for user actions like send/key)
  async function _tracked(label, fn, immediate = false) {
    if (label === 'Fetching output') return await fn();
    const id = _startInflightTracking(label, immediate);
    try {
      const result = await fn();
      if (id != null) _finishInflightTracking(id, true);
      return result;
    } catch (e) {
      if (id != null) _finishInflightTracking(id, false);
      throw e;
    }
  }

  async function loadOutput() {
    if (isTerminalMode()) return;
    if (fsOverlay) {
      loadFsOutput();
      return;
    }
    const pane = container.querySelector('#output-pane');
    if (!pane) return;

    if (_fetchActive) return;
    _fetchActive = true;

    try {
      await _tracked('Fetching output', async () => {
        if (useFullOutput) {
          const data = await api.agentFullOutput(agentId, outputOffset);
          if (data.output) {
            updatePane(pane, data.output);
            outputOffset = data.next_offset || outputOffset;
          }
        } else {
          const data = await api.agentOutput(agentId, 200, _outputHash, null, agentHints());
          if (data.hash) _outputHash = data.hash;
          if (!data.unchanged && data.output) {
            if (isRichOutputMode()) {
              cachedOutput = data.output;
              state.setOutput(agentId, data.output, _outputHash);
              renderRichOutput(agentId, container.querySelector('#rich-output-host'), data.output);
            } else {
              updatePane(pane, data.output);
            }
          }
        }
      });
      _errorCount = 0;
      if (_outputPollMs !== 2000) {
        _outputPollMs = 2000;
        restartOutputPoll();
      }
    } catch (e) {
      if (e?.name === 'AbortError') { _fetchActive = false; return; }
      _errorCount++;
      if (_errorCount >= 2) {
        const next = Math.min(30000, _outputPollMs * 2);
        if (next !== _outputPollMs) {
          _outputPollMs = next;
          restartOutputPoll();
        }
      }
      if (_errorCount >= 3) _outputHash = null;
      if (pane && !pane.textContent.trim()) {
        const fallback = stripAnsi(getTerminalLiveText(agentId));
        if (fallback.trim()) {
          updatePane(pane, fallback);
        } else {
          const msg = canUseTerminalMode(api)
            ? 'Raw output empty — switch to Terminal or tap Refresh'
            : (e?.message || 'Raw output fetch failed');
          if (isRichOutputMode()) renderRichOutput(agentId, container.querySelector('#rich-output-host'), msg);
          else updatePane(pane, msg);
        }
      }
    }
    _fetchActive = false;
  }

  async function loadLogs() {
    const el = container.querySelector('#log-entries');
    const summary = container.querySelector('#logs-summary');
    if (!el) return;
    try {
      const data = await api.agentLogs(agentId, 50);
      const entries = data.entries || [];
      if (summary) summary.textContent = `Logs (${entries.length})`;
      if (entries.length === 0) {
        el.innerHTML = '<div class="empty-state">No logs yet</div>';
        return;
      }
      el.innerHTML = entries.map(e => {
        const ts = e.ts ? new Date(e.ts).toLocaleTimeString() : '';
        const type = e.type || '';
        return `<div class="log-entry log-${type}"><span class="log-ts">${ts}</span> <span class="log-type">[${type}]</span> ${escapeHtml(e.output || e.state || JSON.stringify(e.data || ''))}</div>`;
      }).join('');
    } catch {
      el.innerHTML = '<div class="empty-state">Logs unavailable</div>';
    }
  }

  const _onResizeTerminal = () => {
    scheduleTerminalFit(agentId);
    requestAnimationFrame(() => updateTerminalMetaBar());
  };
  window.addEventListener('resize', _onResizeTerminal);
  const _onTerminalFit = (e) => {
    if (e?.detail?.agentId === agentId) updateTerminalMetaBar();
  };
  const _onTerminalStatus = () => updateTerminalMetaBar();
  window.addEventListener('cam-terminal-fit', _onTerminalFit);
  window.addEventListener('cam-terminal-status', _onTerminalStatus);

  // Initial render + auto-refresh
  // Pass cached agent from state to avoid blocking on a network fetch —
  // the page layout appears instantly, output loads in background.
  render(agent).then(() => {
    if (!isTerminalMode() && ['running', 'starting', 'pending'].includes(agent?.status) && !useFullOutput) {
      restartOutputPoll();
    }
  });

  const unsub = state.subscribe((data) => {
    const updated = resolveAgent(data.agents || []);
    if (updated && updated.status !== agent?.status) {
      patchAgentHeader(updated);
      agent = updated;
      const nowActive = ['running', 'starting', 'pending'].includes(updated.status);
      if (!nowActive) {
        void disposeTerminalForAgent(agentId);
      } else if (nowActive && mobileDirectTerminalDefault() && canUseTerminalMode(api)) {
        outputMode = 'terminal';
        useFullOutput = false;
        applyOutputMode();
      }
    }
  });

  function onMobileAppearance(ev) {
    const { ui, output } = ev?.detail || {};
    if (Number.isFinite(output)) {
      _fontSize = output;
      applyFontSize();
    }
    if (Number.isFinite(ui) && isTerminalMode()) {
      clearTerminalChromeInlineFont();
    }
    if (Number.isFinite(output) && isTerminalMode()) {
      scheduleTerminalFit(agentId);
    }
    if (Number.isFinite(output) && isRichOutputMode()) {
      setRichOutputFontSize(agentId, output);
    }
  }
  window.addEventListener('cam-mobile-appearance', onMobileAppearance);

  return () => {
    _attachAttempt += 1;
    clearInterval(outputTimer);
    clearInterval(elapsedTimer);
    copyModeActive = false;
    setTerminalViewActive(agentId, false);
    void parkTerminalForAgent(agentId);
    _removeInflightToast();
    if (_inflightAbort) _inflightAbort.abort();
    if (_copyCleanups) _copyCleanups();
    contentEl.classList.remove('agent-detail-active');
    contentEl.classList.remove('terminal-ui-active');
    document.body.classList.remove('terminal-fullscreen');
    closeFullscreen();
    disposeRichOutput(agentId);
    window.removeEventListener('resize', _onResize);
    window.removeEventListener('resize', _onResizeTerminal);
    window.removeEventListener('cam-terminal-fit', _onTerminalFit);
    window.removeEventListener('cam-terminal-status', _onTerminalStatus);
    window.removeEventListener('cam-mobile-appearance', onMobileAppearance);
    if (bottomStatusTimer) clearTimeout(bottomStatusTimer);
    if (_vvCleanup) _vvCleanup();
    unsub();
  };
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
