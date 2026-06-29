/**
 * Shared xterm + SSH PTY session manager (Desktop agent-console parity).
 * Mobile: remount-on-rerender, DOM renderer, composer fallback via sendTerminalInput.
 */

import { getTermBridge } from './term-bridge.js';
import { isAgentHostEnabled } from './node-host-meta.js';
import { agentEndpointHints } from './agent-helpers.js';

export const TERMINAL_CACHE_LIMIT = 6; // one SSH PTY per agent; LRU evicts the 7th
const TERMINAL_MIN_NOTIFY_WIDTH = 120;
const TERMINAL_PREPARE_SHOW_MS = 80;

const terminalSessions = new Map();
const reconnectTimers = new Map();
const openInflight = new Map();
let termUnsubData = null;
let termUnsubStatus = null;
let termBridgeBound = null;
let termAgentId = null;
let globalBridge = null;
let statusTimer = null;

function isAndroidWebView() {
  return !!(typeof window !== 'undefined'
    && window.CamBridge
    && typeof window.CamBridge.term_open === 'function');
}

function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  } catch { return fallback; }
}

export function terminalFontSizeFromCss() {
  const raw = cssVar('--output-font-size', '12px');
  const n = Number.parseFloat(String(raw).replace('px', ''));
  return Number.isFinite(n) ? Math.max(10, Math.min(22, n)) : 12;
}

function terminalFontFamily() {
  return 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
}

function terminalFontSize(ent) {
  const n = ent?.term?.options?.fontSize || terminalFontSizeFromCss();
  return Math.max(10, Math.min(22, Number(n) || terminalFontSizeFromCss()));
}

function canvasCellSize(fontSize) {
  try {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.font = `${fontSize}px ${terminalFontFamily()}`;
    const width = ctx.measureText('W').width;
    if (!width || width <= 0) return null;
    return { width, height: Math.ceil(fontSize * 1.35) };
  } catch {
    return null;
  }
}

function terminalViewportPixels(ent) {
  const host = ent?.hostEl;
  const container = ent?.container;
  const el = (host && host.classList.contains('is-active')) ? host : (container || host);
  if (!el) return { width: 0, height: 0 };
  const style = getComputedStyle(el);
  const padX = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
  const padY = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  let width = Math.max(0, (el.clientWidth || 0) - padX);
  let height = Math.max(0, (el.clientHeight || 0) - padY);
  if (width <= 0) {
    const rect = el.getBoundingClientRect();
    width = Math.max(0, rect.width - padX);
    height = Math.max(0, rect.height - padY);
  }
  // First paint: host may not be laid out yet — use phone viewport as estimate.
  if (width <= 0 && isAndroidWebView()) {
    width = window.visualViewport?.width || window.innerWidth || document.documentElement.clientWidth || 0;
    height = height || Math.max(200, (window.visualViewport?.height || window.innerHeight || 640) * 0.55);
  }
  return { width, height };
}

function terminalMeasuredCellSize(ent) {
  const fontSize = terminalFontSize(ent);
  try {
    const cell = ent?.term?._core?._renderService?.dimensions?.css?.cell;
    if (cell && cell.width > 0.5 && cell.height > 0.5) {
      return { width: cell.width, height: cell.height };
    }
  } catch { /* noop */ }
  const probed = canvasCellSize(fontSize);
  if (probed) return probed;
  return { width: Math.max(6, fontSize * 0.62), height: Math.max(12, fontSize * 1.35) };
}

function fittedColsForWidth(ent, widthPx) {
  const cell = terminalMeasuredCellSize(ent);
  if (!cell.width || widthPx <= 0) return 20;
  return Math.max(20, Math.floor(widthPx / cell.width));
}

function fittedRowsForHeight(ent, heightPx) {
  const cell = terminalMeasuredCellSize(ent);
  if (!cell.height || heightPx <= 0) return 12;
  const minRows = isAndroidWebView() ? 12 : 20;
  return Math.max(minRows, Math.floor(heightPx / cell.height));
}

function terminalThemeFromCss() {
  return {
    background: cssVar('--terminal-bg', '#0d1117'),
    foreground: cssVar('--terminal-fg', '#e6edf3'),
    cursor: cssVar('--ansi-bright-blue', '#79c0ff'),
    cursorAccent: cssVar('--terminal-bg', '#0d1117'),
    selectionBackground: cssVar('--terminal-selection-bg', '#1f6feb66'),
  };
}

function cancelAutoReconnect(agentId) {
  const timer = reconnectTimers.get(agentId);
  if (timer) {
    clearTimeout(timer);
    reconnectTimers.delete(agentId);
  }
}

function scheduleAutoReconnect(agentId) {
  cancelAutoReconnect(agentId);
  const ent = terminalSessions.get(agentId);
  if (!ent || ent.opening || ent.sessionId || ent.attachState === 'connecting') return;
  if (ent.keepSessionAlive === false) return;
  if (!ent.agentRef || !ent.apiRef || !ent.hostEl) return;

  ent.reconnectAttempt = (ent.reconnectAttempt || 0) + 1;
  if (ent.reconnectAttempt > 12) {
    setTerminalStatus(ent.hostEl, 'SSH session ended — tap Reattach to reconnect', 'error', 0);
    return;
  }
  const delay = Math.min(1200, 250 + ent.reconnectAttempt * 150);
  setTerminalStatus(ent.hostEl, `Reconnecting… (${ent.reconnectAttempt})`, 'info', 0);
  const timer = setTimeout(() => {
    reconnectTimers.delete(agentId);
    const live = terminalSessions.get(agentId);
    if (!live || live.sessionId || live.opening || live.attachState === 'connecting' || live.keepSessionAlive === false) return;
    void openTerminalForAgent(live.apiRef, live.agentRef, live.hostEl, { force: false });
  }, delay);
  reconnectTimers.set(agentId, timer);
}

function terminalEntryBySession(sessionId) {
  for (const ent of terminalSessions.values()) {
    if (ent.sessionId === sessionId) return ent;
  }
  return null;
}

function terminalScrollToBottom(ent) {
  if (!ent?.term) return;
  try { ent.term.scrollToBottom(); } catch { /* noop */ }
  ent.needsBottom = false;
}

function terminalShouldForceBottom(ent) {
  if (!ent) return false;
  return ent.needsBottom || (ent.forceBottomUntil && Date.now() < ent.forceBottomUntil);
}

function terminalEntryCanAutoResize(ent) {
  if (!ent?.container) return false;
  if (ent.viewActive === false) return false;
  if (ent.agentId !== termAgentId) return false;
  if (ent.container.hidden) return false;
  if (ent.container.style.visibility === 'hidden') return false;
  const host = ent.hostEl;
  if (host && !host.classList.contains('is-active')) return false;
  return true;
}

function appendLiveText(ent, chunk) {
  if (!ent || !chunk) return;
  ent.liveText = (ent.liveText || '') + chunk;
  if (ent.liveText.length > 600000) {
    ent.liveText = ent.liveText.slice(-500000);
  }
}

export function getTerminalLiveText(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.term) return ent?.liveText || '';
  try {
    const buf = ent.term.buffer.active;
    const lines = [];
    for (let i = 0; i < buf.length; i++) {
      const line = buf.getLine(i);
      if (line) lines.push(line.translateToString(true));
    }
    const rendered = lines.join('\n');
    if (rendered.trim()) return rendered;
  } catch { /* fall through */ }
  return ent.liveText || '';
}

/** Keep SSH session alive; only hide/show xterm when switching output modes. */
export function setTerminalViewActive(agentId, active) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return;
  ent.viewActive = active !== false;
  if (ent.container) {
    if (!active) {
      ent.container.style.visibility = 'hidden';
    } else if (!ent.container.hidden) {
      ent.container.style.visibility = '';
    }
  }
}

export function setTerminalStatus(hostEl, message, tone = 'info', ttl = 0) {
  if (!hostEl) return;
  if (statusTimer) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }

  if (isAndroidWebView()) {
    const root = hostEl.closest('#content') || document;
    const bar = root.querySelector('#terminal-meta-bar');
    if (bar) {
      let connEl = bar.querySelector('.terminal-meta-conn');
      if (!connEl) {
        connEl = document.createElement('span');
        connEl.className = 'terminal-meta-conn';
        bar.appendChild(connEl);
      }
      if (!message) {
        delete connEl.dataset.holdMessage;
        connEl.removeAttribute('data-tone');
        try {
          window.dispatchEvent(new CustomEvent('cam-terminal-status'));
        } catch { /* noop */ }
      } else {
        connEl.textContent = message;
        connEl.dataset.tone = tone;
        connEl.dataset.holdMessage = '1';
      }
      if (ttl > 0 && message) {
        statusTimer = setTimeout(() => setTerminalStatus(hostEl, '', tone), ttl);
      }
      const overlay = hostEl.querySelector('.terminal-status-banner');
      if (overlay) overlay.remove();
      return;
    }
  }

  let el = hostEl.querySelector('.terminal-status-banner');
  if (!message) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement('div');
    el.className = 'terminal-status-banner';
    hostEl.appendChild(el);
  }
  el.dataset.tone = tone;
  el.textContent = message;
  if (ttl > 0) {
    statusTimer = setTimeout(() => setTerminalStatus(hostEl, '', tone), ttl);
  }
}

function updateTerminalSizeHint(ent, ttl = 2500) {
  if (isAndroidWebView()) return;
  if (!ent?.hostEl || !ent.term || !ent.sessionId) return;
  const cols = Number(ent.term.cols) || 0;
  const rows = Number(ent.term.rows) || 0;
  if (cols >= 20 && rows >= 4) {
    const vp = ent.viewportWidth ? ` · ${Math.round(ent.viewportWidth)}px` : '';
    const fs = ent.fontSizePx ? ` · ${ent.fontSizePx}px` : '';
    setTerminalStatus(ent.hostEl, `Terminal ${cols}×${rows}${vp}${fs}`, 'ok', ttl);
  }
}

function clampTerminalGrid() { /* legacy no-op */ }

function fitTerminalEntry(ent, { notifyRemote = true } = {}) {
  if (!ent?.term || !ent.container) return false;
  const vp = terminalViewportPixels(ent);
  if (vp.width < TERMINAL_MIN_NOTIFY_WIDTH || vp.height < 40) return false;

  const desiredCols = fittedColsForWidth(ent, vp.width);
  const desiredRows = fittedRowsForHeight(ent, vp.height);
  const cols = Number(ent.term.cols) || 0;
  const rows = Number(ent.term.rows) || 0;

  if (desiredCols !== cols || desiredRows !== rows) {
    try { ent.term.resize(desiredCols, desiredRows); } catch { /* noop */ }
    try { ent.term.refresh && ent.term.refresh(0, Math.max(0, desiredRows - 1)); } catch { /* noop */ }
  }

  const cell = terminalMeasuredCellSize(ent);
  ent.lastCols = desiredCols;
  ent.lastRows = desiredRows;
  ent.viewportWidth = vp.width;
  ent.viewportHeight = vp.height;
  ent.cellWidth = cell.width;
  ent.cellHeight = cell.height;
  ent.fontSizePx = terminalFontSize(ent);

  if (notifyRemote && globalBridge && ent.sessionId
      && desiredCols >= 20 && desiredRows >= 4
      && terminalEntryCanAutoResize(ent)) {
    globalBridge.resize({ sessionId: ent.sessionId, cols: desiredCols, rows: desiredRows });
  }
  if (vp.width < TERMINAL_MIN_NOTIFY_WIDTH || desiredCols < 20 || desiredRows < 4) {
    return false;
  }
  try {
    window.dispatchEvent(new CustomEvent('cam-terminal-fit', { detail: { agentId: ent.agentId } }));
  } catch { /* noop */ }
  return true;
}

function setTerminalConnecting(hostEl, connecting) {
  if (!hostEl) return;
  hostEl.classList.toggle('is-connecting', !!connecting);
}

function scheduleTerminalFitDeferred(ent, opts = {}) {
  if (!ent) return;
  const keepBottom = opts.keepBottom !== false;
  const pass = () => {
    fitTerminalEntry(ent);
    if (keepBottom) terminalScrollToBottom(ent);
  };
  const raf = window.requestAnimationFrame || ((fn) => window.setTimeout(fn, 0));
  if (isAndroidWebView() && opts.once) {
    raf(pass);
    return;
  }
  raf(() => {
    pass();
    raf(pass);
    window.setTimeout(pass, 80);
    window.setTimeout(pass, 220);
  });
  const observeEl = ent.container;
  if (typeof ResizeObserver !== 'undefined' && observeEl) {
    if (!ent._resizeObs) {
      ent._resizeObs = new ResizeObserver(() => {
        if (terminalEntryCanAutoResize(ent)) {
          fitTerminalEntry(ent);
          updateTerminalSizeHint(ent, 1500);
        }
      });
      ent._resizeObs.observe(observeEl);
    }
    if (ent.hostEl && !ent._hostResizeObs) {
      ent._hostResizeObs = new ResizeObserver(() => scheduleTerminalFitDeferred(ent, opts));
      ent._hostResizeObs.observe(ent.hostEl);
    }
  }
}

function hideTerminalEntries() {
  for (const ent of terminalSessions.values()) {
    if (ent.container) {
      ent.container.hidden = true;
      ent.container.style.display = 'none';
    }
  }
}

function remountTerminalContainer(ent, hostEl) {
  if (!ent?.container || !hostEl) return;
  for (const orphan of hostEl.querySelectorAll('.agent-terminal-pane')) {
    if (orphan !== ent.container) orphan.remove();
  }
  if (!hostEl.contains(ent.container)) {
    hostEl.appendChild(ent.container);
  }
  ent.container.hidden = false;
  ent.container.style.display = 'block';
  ent.container.style.visibility = 'visible';
  ent.container.style.height = '100%';
  ent.container.style.width = '100%';
  scheduleTerminalFitDeferred(ent, { keepBottom: true });
  try { ent.term.refresh && ent.term.refresh(0, Math.max(0, (ent.term.rows || 24) - 1)); } catch { /* noop */ }
}

function showTerminalEntry(agentId, hostEl, opts = {}) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return ent;
  hideTerminalEntries();
  if (hostEl) remountTerminalContainer(ent, hostEl);
  termAgentId = agentId;
  ent.lastUsed = Date.now();
  if (opts.keepBottom !== false) {
    ent.needsBottom = true;
    ent.forceBottomUntil = Date.now() + 1200;
    terminalScrollToBottom(ent);
  }
  if (ent.container) {
    ent.container.hidden = false;
    ent.container.style.display = 'block';
    ent.container.style.visibility = '';
  }
  scheduleTerminalFitDeferred(ent, { keepBottom: opts.keepBottom !== false });
  requestAnimationFrame(() => {
    try { ent.term?.focus(); } catch { /* noop */ }
  });
  return ent;
}

function prepareThenShowTerminalEntry(agentId, hostEl, delay = TERMINAL_PREPARE_SHOW_MS) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.container || !hostEl) return ent || null;
  remountTerminalContainer(ent, hostEl);
  ent.container.hidden = false;
  ent.container.style.display = 'block';
  ent.container.style.visibility = 'hidden';
  fitTerminalEntry(ent, { notifyRemote: false });
  scheduleTerminalFitDeferred(ent, { keepBottom: true });
  window.setTimeout(() => {
    if (terminalSessions.has(agentId)) {
      showTerminalEntry(agentId, hostEl, { keepBottom: true });
    }
  }, delay);
  return ent;
}

function applyTerminalAppearance(ent) {
  if (!ent?.term) return;
  const theme = terminalThemeFromCss();
  const fontSize = terminalFontSizeFromCss();
  ent.term.options.theme = theme;
  ent.term.options.fontSize = fontSize;
  ent.term.options.fontFamily = terminalFontFamily();
  if (ent.hostEl) ent.hostEl.style.background = theme.background;
  ent.fontSizePx = fontSize;
}

function refreshAllTerminalAppearance() {
  for (const ent of terminalSessions.values()) {
    applyTerminalAppearance(ent);
    scheduleTerminalFitDeferred(ent, { keepBottom: false });
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('cam-mobile-appearance', refreshAllTerminalAppearance);
  window.addEventListener('cam-node-host-meta-changed', () => {
    for (const [agentId, ent] of [...terminalSessions.entries()]) {
      const ag = ent?.agentRef;
      if (ag && !isAgentHostEnabled(ag)) {
        void disposeTerminalForAgent(agentId);
      }
    }
  });
}

export function setTerminalFontSize(agentId, fontSize) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.term) return false;
  const n = Math.max(10, Math.min(22, Number(fontSize) || terminalFontSizeFromCss()));
  ent.term.options.fontSize = n;
  scheduleTerminalFitDeferred(ent, { keepBottom: false });
  return true;
}

function createTerminalEntry(agent, hostEl) {
  if (!hostEl || !agent) return null;
  const existing = terminalSessions.get(agent.id);
  if (existing?.term) {
    existing.hostEl = hostEl;
    existing.agentRef = agent;
    applyTerminalAppearance(existing);
    if (!hostEl.contains(existing.container)) {
      remountTerminalContainer(existing, hostEl);
    }
    return existing;
  }

  const TermCtor = window.Terminal;
  if (!TermCtor) {
    hostEl.textContent = 'xterm.js not loaded';
    return null;
  }

  const container = document.createElement('div');
  container.className = 'agent-terminal-pane';
  container.dataset.agentId = agent.id;
  container.style.height = '100%';
  container.style.width = '100%';
  hostEl.appendChild(container);

  const termOpts = {
    cursorBlink: true,
    convertEol: false,
    scrollback: 5000,
    fontFamily: terminalFontFamily(),
    fontSize: terminalFontSizeFromCss(),
    theme: terminalThemeFromCss(),
  };
  if (isAndroidWebView()) {
    termOpts.rendererType = 'dom';
  }

  const entry = {
    agentId: agent.id,
    container,
    hostEl,
    term: new TermCtor(termOpts),
    fit: null,
    sessionId: null,
    opening: false,
    attachState: 'idle',
    lastUsed: Date.now(),
    hasConnected: false,
    bytesReceived: 0,
    liveText: '',
    suppressDisplay: false,
    viewActive: true,
    keepSessionAlive: true,
    agentRef: agent,
    apiRef: null,
    reconnectAttempt: 0,
    needsBottom: true,
    forceBottomUntil: 0,
    lastCols: 100,
    lastRows: 30,
  };
  const FitCtor = window.FitAddon && window.FitAddon.FitAddon;
  if (FitCtor) {
    entry.fit = new FitCtor();
    entry.term.loadAddon(entry.fit);
  }
  entry.term.open(container);
  entry.term.onData((data) => {
    if (!globalBridge || !entry.sessionId) return;
    globalBridge.input({ sessionId: entry.sessionId, data });
  });
  entry.term.onResize(({ cols, rows }) => {
    if (!globalBridge || !entry.sessionId) return;
    if (!terminalEntryCanAutoResize(entry)) return;
    if ((Number(cols) || 0) < 20 || (Number(rows) || 0) < 4) return;
    globalBridge.resize({ sessionId: entry.sessionId, cols, rows });
    updateTerminalSizeHint(entry, 1500);
  });
  terminalSessions.set(agent.id, entry);
  applyTerminalAppearance(entry);
  applyMobileTerminalFont(entry);
  return entry;
}

function setupTerminalEvents() {
  const bridge = globalBridge;
  if (!bridge) return;
  if (termBridgeBound === bridge && termUnsubData) return;
  if (termUnsubData) {
    try { termUnsubData(); } catch { /* noop */ }
    termUnsubData = null;
  }
  if (termUnsubStatus) {
    try { termUnsubStatus(); } catch { /* noop */ }
    termUnsubStatus = null;
  }
  termBridgeBound = bridge;
  termUnsubData = bridge.onData((msg) => {
    if (!msg) return;
    const ent = terminalEntryBySession(msg.sessionId);
    if (!ent?.term) return;
    const chunk = String(msg.data || '');
    if (!chunk) return;
    ent.bytesReceived = (ent.bytesReceived || 0) + chunk.length;
    appendLiveText(ent, chunk);
    if (ent.suppressDisplay) return;
    const shouldFollow = ent.agentId === termAgentId || terminalShouldForceBottom(ent);
    ent.term.write(chunk, () => {
      if (shouldFollow || terminalShouldForceBottom(ent)) terminalScrollToBottom(ent);
    });
    if (ent.hostEl && ent.bytesReceived > 0) {
      setTerminalStatus(ent.hostEl, '', 'info');
    }
  });
  termUnsubStatus = bridge.onStatus((msg) => {
    if (!msg) return;
    const sid = msg.sessionId;
    const ent = sid ? terminalEntryBySession(sid) : null;
    if (!ent) return;
    if (msg.kind === 'closed') {
      if (ent.sessionId !== sid) return;
      ent.sessionId = null;
      if (ent.attachState === 'connecting') return;
      ent.opening = false;
      ent.attachState = 'idle';
      cancelAutoReconnect(ent.agentId);
      if (ent.keepSessionAlive !== false && ent.agentRef && ent.apiRef) {
        try { ent.term.write('\r\n\x1b[2mConnection lost — reconnecting…\x1b[0m\r\n'); } catch { /* noop */ }
        scheduleAutoReconnect(ent.agentId);
      } else {
        const suffix = msg.error ? `: ${msg.error}` : (msg.code != null ? ` (exit ${msg.code})` : '');
        try { ent.term.write(`\r\n\x1b[2mSSH session ended${suffix}\x1b[0m\r\n`); } catch { /* noop */ }
        if (ent.hostEl && ent.viewActive !== false) {
          setTerminalStatus(ent.hostEl, 'SSH session ended — tap Reattach to reconnect', 'error', 0);
        }
      }
    }
  });
}

export async function closeTerminalSession(agentId, opts = {}) {
  cancelAutoReconnect(agentId);
  const ent = terminalSessions.get(agentId);
  if (opts.stopKeepAlive && ent) ent.keepSessionAlive = false;
  if (!ent) return;
  const closingSid = ent.sessionId;
  if (globalBridge && closingSid) {
    try { await globalBridge.close({ sessionId: closingSid }); } catch { /* noop */ }
  }
  if (ent.sessionId === closingSid) ent.sessionId = null;
  if (ent.attachState !== 'connecting') {
    ent.opening = false;
    ent.attachState = 'idle';
  }
}

export async function detachTerminalSession(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.sessionId) return false;
  const sid = ent.sessionId;
  ent.sessionId = null;
  ent.opening = false;
  ent.attachState = 'idle';
  if (globalBridge && sid) {
    try { await globalBridge.close({ sessionId: sid }); } catch { /* noop */ }
  }
  if (ent.hostEl) {
    setTerminalStatus(ent.hostEl, 'Session detached — scrollback kept locally', 'info', 4000);
  }
  return true;
}

async function evictTerminalCacheIfNeeded(activeAgentId) {
  const live = [...terminalSessions.values()].filter(ent => ent.agentId !== activeAgentId);
  live.sort((a, b) => (a.lastUsed || 0) - (b.lastUsed || 0));
  while (terminalSessions.size > TERMINAL_CACHE_LIMIT && live.length) {
    const ent = live.shift();
    // eslint-disable-next-line no-await-in-loop
    await disposeTerminalForAgent(ent.agentId);
  }
}

/** Hide xterm UI but keep SSH session alive (LRU pool). */
export async function parkTerminalForAgent(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return;
  ent.viewActive = false;
  ent.lastUsed = Date.now();
  if (ent.container) {
    ent.container.hidden = true;
    ent.container.style.display = 'none';
    ent.container.style.visibility = '';
  }
  ent.hostEl = null;
  if (termAgentId === agentId) termAgentId = null;
}

/** Re-show a pooled session in a new host element (no SSH re-handshake). */
export async function resumeTerminalForAgent(api, agent, hostEl) {
  if (!agent || !hostEl) return { ok: false, error: 'no_agent' };
  const ent = terminalSessions.get(agent.id);
  if (!ent?.sessionId || ent.attachState !== 'ready') {
    return { ok: false, error: 'not_ready' };
  }
  globalBridge = getTermBridge(api);
  if (!globalBridge) return { ok: false, error: 'no_bridge' };
  setupTerminalEvents();
  ent.hostEl = hostEl;
  ent.viewActive = true;
  ent.agentRef = agent;
  ent.apiRef = api;
  ent.lastUsed = Date.now();
  remountTerminalContainer(ent, hostEl);
  showTerminalEntry(agent.id, hostEl, { keepBottom: true });
  scheduleTerminalFitDeferred(ent, { keepBottom: true });
  await stabilizeTerminalAfterAttach(agent.id, { notifyRemote: true });
  updateTerminalSizeHint(ent, 2500);
  return { ok: true, reused: true };
}

export function terminalPoolSize() {
  return terminalSessions.size;
}

export async function disposeTerminalForAgent(agentId) {
  cancelAutoReconnect(agentId);
  const ent = terminalSessions.get(agentId);
  if (!ent) return;
  await closeTerminalSession(agentId, { stopKeepAlive: true });
  if (ent._resizeObs) {
    try { ent._resizeObs.disconnect(); } catch { /* noop */ }
  }
  if (ent._hostResizeObs) {
    try { ent._hostResizeObs.disconnect(); } catch { /* noop */ }
  }
  try { ent.term?.dispose(); } catch { /* noop */ }
  if (ent.container?.parentNode) {
    try { ent.container.parentNode.removeChild(ent.container); } catch { /* noop */ }
  }
  terminalSessions.delete(agentId);
  if (termAgentId === agentId) termAgentId = null;
}

export async function closeAllTerminalSessions() {
  const ids = [...terminalSessions.keys()];
  for (const id of ids) {
    // eslint-disable-next-line no-await-in-loop
    await disposeTerminalForAgent(id);
  }
}

export function scrollTerminalToBottom(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return false;
  ent.needsBottom = true;
  ent.forceBottomUntil = Date.now() + 800;
  terminalScrollToBottom(ent);
  return true;
}

export function clearTerminalScrollback(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.term) return false;
  try { ent.term.clear(); } catch { /* noop */ }
  if (ent.hostEl) {
    setTerminalStatus(ent.hostEl, 'Scrollback cleared locally', 'info', 2500);
  }
  return true;
}

export function resetTerminalSize(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return false;
  scheduleTerminalFitDeferred(ent, { keepBottom: true });
  updateTerminalSizeHint(ent, 3000);
  return true;
}

export function getTerminalSessionStats(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent) return null;
  return {
    agentId: ent.agentId,
    sessionId: ent.sessionId || '',
    connected: !!ent.sessionId,
    opening: !!ent.opening,
    attachState: ent.attachState || 'idle',
    cols: Number(ent.term?.cols) || 0,
    rows: Number(ent.term?.rows) || 0,
    viewportWidth: ent.viewportWidth || 0,
    viewportHeight: ent.viewportHeight || 0,
    cellWidth: ent.cellWidth || 0,
    fontSizePx: ent.fontSizePx || terminalFontSize(ent),
    bytesReceived: ent.bytesReceived || 0,
    scrollback: ent.term?.options?.scrollback || 0,
    cachedSessions: terminalSessions.size,
  };
}

export function terminalAttachReady(agentId) {
  const ent = terminalSessions.get(agentId);
  return !!(ent && ent.attachState === 'ready' && ent.sessionId);
}

export function terminalAttachInflight(agentId) {
  const ent = terminalSessions.get(agentId);
  return !!(ent && (ent.attachState === 'connecting' || ent.opening || openInflight.has(agentId)));
}

export function seedTerminalPreview(agentId, text) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.term || !text) return false;
  if (ent.bytesReceived > 0 || ent.sessionId) return false;
  const chunk = String(text);
  ent.liveText = chunk;
  try {
    ent.term.write(chunk, () => {
      terminalScrollToBottom(ent);
    });
  } catch { /* noop */ }
  return true;
}

export async function reattachTerminalForAgent(api, agent, hostEl) {
  return openTerminalForAgent(api, agent, hostEl, { force: true });
}

export async function openTerminalForAgent(api, agent, hostEl, opts = {}) {
  globalBridge = getTermBridge(api);
  if (!globalBridge) {
    setTerminalStatus(hostEl, 'Terminal bridge unavailable — reload the app', 'error');
    return { ok: false, error: 'no_bridge' };
  }
  if (!agent) {
    setTerminalStatus(hostEl, 'No agent selected', 'error');
    return { ok: false, error: 'no_agent' };
  }
  if (!isAgentHostEnabled(agent)) {
    setTerminalStatus(hostEl, 'Node is disabled — enable it in Nodes', 'error', 0);
    return { ok: false, error: 'host_disabled' };
  }

  setupTerminalEvents();
  setTerminalStatus(hostEl, 'Starting terminal…', 'info');
  const ent = createTerminalEntry(agent, hostEl);
  if (!ent) {
    setTerminalStatus(hostEl, 'xterm.js not loaded', 'error');
    return { ok: false, error: 'no_xterm' };
  }
  ent.hostEl = hostEl;
  ent.viewActive = true;
  ent.agentRef = agent;
  ent.apiRef = api;
  if (opts.force) {
    try { ent.term?.clear(); } catch { /* noop */ }
    ent.reconnectAttempt = 0;
  }

  remountTerminalContainer(ent, hostEl);

  if (!opts.force && ent.sessionId && ent.attachState === 'ready') {
    showTerminalEntry(agent.id, hostEl);
    ent.lastUsed = Date.now();
    scheduleTerminalFitDeferred(ent, { keepBottom: true });
    if (!(ent.bytesReceived > 0) && ent.liveText) {
      try { ent.term.write(ent.liveText, () => terminalScrollToBottom(ent)); } catch { /* noop */ }
    }
    void stabilizeTerminalAfterAttach(agent.id);
    return { ok: true, reused: true };
  }

  if (!opts.force && (openInflight.has(agent.id) || ent.attachState === 'connecting')) {
    showTerminalEntry(agent.id, hostEl);
    ent.hostEl = hostEl;
    remountTerminalContainer(ent, hostEl);
    try {
      await openInflight.get(agent.id);
    } catch { /* surfaced by inflight owner */ }
    scheduleTerminalFitDeferred(ent, { keepBottom: true });
    const live = terminalSessions.get(agent.id);
    return { ok: !!(live && live.sessionId), reused: true };
  }

  if (opts.force && ent.sessionId) {
    await closeTerminalSession(agent.id);
    ent.sessionId = null;
    ent.bytesReceived = 0;
    ent.liveText = '';
    ent.hasConnected = false;
  }

  ent.opening = true;
  ent.attachState = 'connecting';
  ent.suppressDisplay = true;
  ent.bytesReceived = 0;
  ent.liveText = '';
  try { ent.term.clear(); } catch { /* noop */ }
  setTerminalConnecting(hostEl, true);
  setTerminalStatus(hostEl, 'Connecting via SSH…', 'info');
  if (isAndroidWebView()) {
    await waitForTerminalLayout(hostEl, 1200);
    showTerminalEntry(agent.id, hostEl, { keepBottom: true });
    fitTerminalEntry(ent, { notifyRemote: false });
  } else {
    prepareThenShowTerminalEntry(agent.id, hostEl);
    fitTerminalEntry(ent, { notifyRemote: false });
  }
  scheduleTerminalFitDeferred(ent, { keepBottom: true, once: isAndroidWebView() });
  const vp = terminalViewportPixels(ent);
  const openCols = fittedColsForWidth(ent, vp.width);
  const openRows = fittedRowsForHeight(ent, vp.height);

  const runOpen = async () => {
  try {
    const earlyData = [];
    const earlyUnsub = globalBridge.onData((msg) => {
      if (ent.opening) earlyData.push(msg);
    });
    const res = await globalBridge.open({
      agentId: agent.id,
      cols: openCols,
      rows: openRows,
      ...(agentEndpointHints(agent) || {}),
    });
    earlyUnsub();
    if (!res || !res.ok) {
      const detail = res && (res.detail || res.error) || 'unknown';
      ent.suppressDisplay = false;
      ent.attachState = 'idle';
      setTerminalConnecting(hostEl, false);
      ent.term.write(`\r\n\x1b[31mTerminal attach failed: ${detail}\x1b[0m\r\n`);
      setTerminalStatus(hostEl, `Attach failed: ${detail}`, 'error');
      ent.sessionId = null;
      return { ok: false, error: res && res.error };
    }
    ent.sessionId = res.sessionId;
    for (const msg of earlyData) {
      if (msg && msg.sessionId === ent.sessionId && msg.data) {
        const chunk = String(msg.data);
        ent.bytesReceived = (ent.bytesReceived || 0) + chunk.length;
        appendLiveText(ent, chunk);
      }
    }
    ent.hasConnected = true;
    ent.lastUsed = Date.now();
    ent.reconnectAttempt = 0;
    cancelAutoReconnect(agent.id);
    showTerminalEntry(agent.id, hostEl, { keepBottom: true });
    await stabilizeTerminalAfterAttach(agent.id, { notifyRemote: false });
    await revealTerminalBuffer(ent, hostEl);
    await evictTerminalCacheIfNeeded(agent.id);
    return { ok: true, reused: !!res.reused };
  } catch (e) {
    const detail = e && e.message || e;
    ent.suppressDisplay = false;
    ent.attachState = 'idle';
    setTerminalConnecting(hostEl, false);
    ent.term.write(`\r\n\x1b[31mTerminal attach failed: ${detail}\x1b[0m\r\n`);
    setTerminalStatus(hostEl, `Attach failed: ${detail}`, 'error');
    ent.sessionId = null;
    return { ok: false, error: String(e) };
  } finally {
    ent.opening = false;
  }
  };

  const inflight = runOpen();
  openInflight.set(agent.id, inflight);
  try {
    return await inflight;
  } finally {
    openInflight.delete(agent.id);
  }
}

export function scheduleTerminalFit(agentId) {
  const ent = agentId ? terminalSessions.get(agentId) : null;
  if (ent) scheduleTerminalFitDeferred(ent, { keepBottom: true });
}

/** Wait until terminal host has real pixel dimensions (flex layout settled). */
export function waitForTerminalLayout(hostEl, timeoutMs = 3000) {
  return new Promise((resolve) => {
    if (!hostEl) { resolve(false); return; }
    const started = Date.now();
    const ok = () => {
      const w = hostEl.clientWidth || 0;
      const h = hostEl.clientHeight || 0;
      return w >= 80 && h >= 60;
    };
    const tick = () => {
      if (ok()) { resolve(true); return; }
      if (Date.now() - started >= timeoutMs) { resolve(false); return; }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(() => requestAnimationFrame(tick));
  });
}

async function revealTerminalBuffer(ent, hostEl) {
  if (!ent?.term) return;
  ent.liveText = '';
  ent.bytesReceived = 0;
  try { ent.term.clear(); } catch { /* noop */ }
  fitTerminalEntry(ent, { notifyRemote: true });
  await new Promise((r) => setTimeout(r, 120));
  fitTerminalEntry(ent, { notifyRemote: true });
  await new Promise((r) => setTimeout(r, 200));
  const fresh = ent.liveText || '';
  ent.suppressDisplay = false;
  try { ent.term.clear(); } catch { /* noop */ }
  if (fresh) {
    try { ent.term.write(fresh, () => terminalScrollToBottom(ent)); } catch { /* noop */ }
  }
  try { ent.term.refresh(0, Math.max(0, (ent.term.rows || 24) - 1)); } catch { /* noop */ }
  setTerminalConnecting(hostEl, false);
  ent.attachState = 'ready';
  if (ent.bytesReceived > 0 || fresh) {
    terminalScrollToBottom(ent);
    setTerminalStatus(hostEl, '', 'info');
    updateTerminalSizeHint(ent, 2500);
  } else {
    setTerminalStatus(hostEl, 'Attached — waiting for output…', 'info');
  }
}

async function stabilizeTerminalAfterAttach(agentId, opts = {}) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.term) return;
  const notifyRemote = opts.notifyRemote !== false;
  const delays = isAndroidWebView() ? [0, 150] : [0, 80, 200, 500, 1000];
  for (const delay of delays) {
    if (delay) await new Promise((r) => setTimeout(r, delay));
    fitTerminalEntry(ent, { notifyRemote });
    try { ent.term.refresh(0, Math.max(0, (ent.term.rows || 24) - 1)); } catch { /* noop */ }
    terminalScrollToBottom(ent);
  }
}

const TERMINAL_KEY_BYTES = {
  Enter: '\r',
  Escape: '\x1b',
  BSpace: '\x7f',
  Tab: '\t',
  BTab: '\x1b[Z',
  'C-c': '\x03',
  Up: '\x1b[A',
  Down: '\x1b[B',
  Left: '\x1b[D',
  Right: '\x1b[C',
  Home: '\x1b[H',
  End: '\x1b[F',
  PPage: '\x1b[5~',
  NPage: '\x1b[6~',
  DC: '\x1b[3~',
};

export function terminalSessionReady(agentId) {
  const ent = terminalSessions.get(agentId);
  return !!(ent && ent.sessionId && globalBridge);
}

export async function sendTerminalInput(agentId, data, opts = {}) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.sessionId || !globalBridge) {
    throw new Error('Terminal not connected');
  }
  let payload = String(data ?? '');
  const enter = opts.enter !== false;
  if (enter && payload && !payload.endsWith('\r') && !payload.endsWith('\n')) {
    payload += '\r';
  }
  const res = await globalBridge.input({ sessionId: ent.sessionId, data: payload });
  if (res && res.ok === false) {
    throw new Error(res.detail || res.error || 'terminal input failed');
  }
  return res;
}

export function focusTerminalForAgent(agentId) {
  const ent = terminalSessions.get(agentId);
  if (!ent?.term) return false;
  try {
    ent.term.focus();
    return true;
  } catch {
    return false;
  }
}

export async function sendTerminalRaw(agentId, data) {
  return sendTerminalInput(agentId, data, { enter: false });
}

export async function sendTerminalKey(agentId, key) {
  const bytes = TERMINAL_KEY_BYTES[key];
  if (bytes == null) throw new Error(`Unknown key: ${key}`);
  return sendTerminalInput(agentId, bytes, { enter: false });
}

export function applyMobileTerminalFont(ent) {
  if (!ent?.term || !isAndroidWebView()) return;
  ent.term.options.fontSize = terminalFontSizeFromCss();
  ent.term.options.fontFamily = terminalFontFamily();
}
