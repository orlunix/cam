/* Terminal trap layer (2026-10-14) — ONE place that owns every input
 * interception for a terminal pane, before the event reaches xterm's own
 * handling or the PTY/tmux. Native-terminal semantics throughout: with
 * tmux copy mode retired and the alt-screen stripped from the live
 * stream (agent-console sanitizeTerminalStream), the local 20000-line
 * scrollback is the history — so scrolling, paging, copy, and paste all
 * operate locally and never eat keystrokes the application expects.
 *
 * The traps:
 *
 *   PageUp / PageDown (with or without Shift)
 *       Page the LOCAL buffer. ev.shiftKey is deliberately ignored:
 *       Shift+PageUp/PageDown is the native-terminal paging convention
 *       and CAM gives the plain keys the same meaning, because agent
 *       TUIs (claude, kimi) swallow the plain keys anyway.
 *
 *   Ctrl/Cmd+C
 *       WITH an active selection → write it to the OS clipboard.
 *       WITHOUT a selection → pass through to the PTY as SIGINT —
 *       never eat that.
 *
 *   Ctrl+V / Ctrl+Shift+V
 *       Detect the clipboard and paste: text goes to the pane, files/
 *       images go through the attachment upload flow (via onPaste).
 *
 *   Mouse wheel
 *       Always scrolls the LOCAL buffer; nothing is forwarded to the
 *       PTY. Also pauses auto-follow (needsBottom/forceBottomUntil).
 *
 *   Right-click (contextmenu)
 *       WITH a selection → copy it instantly (no menu). WITHOUT a
 *       selection → paste. Always suppresses the native Electron
 *       context menu on the terminal pane.
 *
 * Wiring: the caller injects clipboard accessors and callbacks, so this
 * module stays free of window.CamBridge / status-bar knowledge. All
 * listeners are returned in a single detach() for entry teardown.
 */

export function attachTerminalTrap(entry, {
  container,
  writeClipboardText = null,   // (text) => void | Promise
  onPaste = null,              // async () => void — clipboard detect + paste/upload
  onViewportChange = null,     // () => void — user scrolled the pane
  onStatus = null,             // (text, kind) => void — optional feedback
} = {}) {
  const term = entry && entry.term;
  if (!term || !container) return { detach() {} };

  const report = (text, kind) => { try { if (onStatus) onStatus(text, kind); } catch (_) {} };
  const copySelection = () => {
    let sel = '';
    try { sel = term.hasSelection() ? term.getSelection() : ''; } catch (_) {}
    if (!sel) return false;
    try { if (writeClipboardText) void writeClipboardText(sel); } catch (_) {}
    report(`Copied ${sel.length} chars`, 'info');
    return true;
  };
  const doPaste = () => { if (onPaste) void onPaste(); };

  /* ── key trap ─────────────────────────────────────────────────── */
  term.attachCustomKeyEventHandler((ev) => {
    if (!ev || ev.type !== 'keydown') return true;
    const key = String(ev.key || '');
    // CAM owns paging: PageUp/PageDown scroll the LOCAL buffer and never
    // reach the application — most agent TUIs (claude, kimi) swallow
    // these keys, so the viewport pages uniformly. ev.shiftKey is
    // deliberately ignored: Shift+PageUp/PageDown is the native-terminal
    // paging convention and CAM gives the plain keys the same meaning.
    if (!ev.ctrlKey && !ev.altKey && !ev.metaKey
        && (key === 'PageUp' || key === 'PageDown')) {
      ev.preventDefault();
      ev.stopPropagation();
      try { term.scrollPages(key === 'PageUp' ? -1 : 1); } catch (_) {}
      if (onViewportChange) onViewportChange();
      return false;
    }
    // Ctrl/Cmd+C (or Ctrl+Shift+C) WITH an active selection copies it to
    // the clipboard. WITHOUT a selection the key passes through to the
    // PTY as SIGINT — never eat that.
    if (key.toLowerCase() === 'c' && (ev.ctrlKey || ev.metaKey) && !ev.altKey) {
      if (!copySelection()) return true;
      ev.preventDefault();
      ev.stopPropagation();
      return false;
    }
    // Ctrl+V and Ctrl+Shift+V both detect + paste from the clipboard
    // (the Shift+ variant previously leaked \x16 into the PTY).
    if (!ev.ctrlKey || ev.altKey || ev.metaKey) return true;
    if (key.toLowerCase() !== 'v') return true;
    ev.preventDefault();
    ev.stopPropagation();
    doPaste();
    return false;
  });

  /* ── wheel trap ───────────────────────────────────────────────── */
  const wheelHandler = (ev) => {
    if (!ev.deltaY) return;
    const count = Math.max(1, Math.min(4, Math.ceil(Math.abs(ev.deltaY) / 80)));
    const lines = ev.deltaY < 0 ? -count : count;
    ev.preventDefault();
    ev.stopPropagation();
    try { term.scrollLines(lines); } catch (_) {}
    entry.needsBottom = false;
    entry.forceBottomUntil = 0;
    if (onViewportChange) onViewportChange();
  };
  container.addEventListener('wheel', wheelHandler, { capture: true, passive: false });

  /* ── right-click trap ─────────────────────────────────────────── */
  // PuTTY/Windows-Terminal style: selection → instant copy; no
  // selection → paste. preventDefault keeps Electron's native
  // context-menu (main.cjs) from popping over the pane.
  const contextMenuHandler = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (!copySelection()) doPaste();
  };
  container.addEventListener('contextmenu', contextMenuHandler, { capture: true });

  return {
    wheelHandler,
    detach() {
      try { container.removeEventListener('wheel', wheelHandler, { capture: true }); } catch (_) {}
      try { container.removeEventListener('contextmenu', contextMenuHandler, { capture: true }); } catch (_) {}
      try { term.attachCustomKeyEventHandler(() => true); } catch (_) {}
    },
  };
}

/* Terminal touch trap (mobile) — two layers, selected by entry.copyModeActive:
 *
 *   copy mode OFF → vertical drag scrolls the LOCAL scrollback; horizontal
 *                   drag pages the LOCAL buffer
 *   copy mode ON  → swipes are left to the terminal view's tmux copy-mode
 *                   key handler (Up/Down/PPage/NPage)
 *
 * In both layers a long press selects the word under the finger and the
 * floating toolbar copies/pastes via the native Android clipboard shim.
 * tap clears the selection / focuses the terminal. */
export function attachTerminalTouchTrap(entry, {
  container = null,
  longPressMs = 450,
  moveThreshold = 8,
  pageThreshold = 36,
  onStatus = null,
  onViewportChange = null,
} = {}) {
  const term = entry && entry.term;
  const host = container || (entry && entry.container);
  if (!term || !host || typeof window === 'undefined') {
    return { detach() {}, setActive() {}, sync() {}, copySelection() {}, pasteClipboard() {} };
  }
  const doc = host.ownerDocument || document;
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const WORD_BREAK = /[\s()[\]{}'"`,;:!?<>|，。！？；：、]/;

  const report = (text, kind = 'info') => {
    if (!text) return;
    try { if (onStatus) onStatus(text, kind); } catch (_) {}
    try {
      if (typeof CustomEvent === 'function') {
        host.dispatchEvent(new CustomEvent('cam-terminal-status', {
          bubbles: true, detail: { text, kind },
        }));
      }
    } catch (_) {}
  };

  let active = true;
  let gesture = null;
  let longPressTimer = null;
  let selection = null;          // { start:{col,row}, end:{col,row} } normalized
  let toolbar = null;
  let handleStart = null;
  let handleEnd = null;
  let draggingHandle = null;
  let selectionSub = null;
  let scrollSub = null;

  function metrics() {
    const screen = term.element && typeof term.element.querySelector === 'function'
      ? term.element.querySelector('.xterm-screen') : null;
    const el = screen || term.element || host;
    if (!el || typeof el.getBoundingClientRect !== 'function') return null;
    const rect = el.getBoundingClientRect();
    let cell = null;
    try {
      const dimensions = term._core && term._core._renderService
        && term._core._renderService.dimensions;
      cell = dimensions && dimensions.css && dimensions.css.cell;
    } catch (_) {}
    const cw = cell && cell.width > 0 ? cell.width : rect.width / Math.max(1, term.cols);
    const ch = cell && cell.height > 0 ? cell.height : rect.height / Math.max(1, term.rows);
    if (!cw || !ch) return null;
    return { rect, cw, ch };
  }

  function pointToCell(clientX, clientY) {
    const m = metrics();
    if (!m) return null;
    const col = clamp(Math.floor((clientX - m.rect.left) / m.cw), 0, term.cols - 1);
    const vrow = clamp(Math.floor((clientY - m.rect.top) / m.ch), 0, term.rows - 1);
    return { col, row: term.buffer.active.viewportY + vrow };
  }

  function isWordCell(line, index) {
    if (!line || index < 0 || index >= term.cols) return false;
    let cell;
    try { cell = line.getCell(index); } catch (_) { return false; }
    if (!cell || cell.getWidth() === 0) return false;
    const chars = cell.getChars();
    return !!chars && !WORD_BREAK.test(chars);
  }

  function wordRangeAt(cell) {
    const line = term.buffer.active.getLine(cell.row);
    if (!line) return { startCol: cell.col, endCol: clamp(cell.col + 1, 0, term.cols) };
    let col = cell.col;
    let head = null;
    try { head = line.getCell(col); } catch (_) {}
    if (head && head.getWidth() === 0) col = Math.max(0, col - 1);
    if (!isWordCell(line, col)) {
      return { startCol: col, endCol: clamp(col + 1, 0, term.cols) };
    }
    let start = col;
    let end = col + 1;
    while (start > 0 && isWordCell(line, start - 1)) start -= 1;
    while (end < term.cols && isWordCell(line, end)) end += 1;
    return { startCol: start, endCol: end };
  }

  function normalize(a, b) {
    if (!a || !b) return null;
    if (b.row < a.row || (b.row === a.row && b.col < a.col)) return { start: b, end: a };
    return { start: a, end: b };
  }

  function applySelection(a, b) {
    const normalized = normalize(a, b);
    selection = normalized;
    if (!normalized) {
      try { term.clearSelection(); } catch (_) {}
      return;
    }
    const length = (normalized.end.row - normalized.start.row) * term.cols
      + (normalized.end.col - normalized.start.col);
    if (length <= 0) {
      try { term.clearSelection(); } catch (_) {}
      return;
    }
    try { term.select(normalized.start.col, normalized.start.row, length); } catch (_) {}
  }

  function viewportPoint(cell) {
    const m = metrics();
    if (!m) return null;
    const vrow = cell.row - term.buffer.active.viewportY;
    if (vrow < 0 || vrow >= term.rows) return null;
    return {
      x: m.rect.left + cell.col * m.cw,
      y: m.rect.top + vrow * m.ch,
      bottom: m.rect.top + (vrow + 1) * m.ch,
    };
  }

  function hideUi() {
    if (toolbar) toolbar.classList.add('hidden');
    if (handleStart) handleStart.style.display = 'none';
    if (handleEnd) handleEnd.style.display = 'none';
  }

  function syncUi() {
    if (!active) { hideUi(); return; }
    let pos = null;
    try { pos = term.getSelectionPosition(); } catch (_) {}
    if (!pos || !pos.start || !pos.end) { selection = null; hideUi(); return; }
    ensureUi();
    // getSelectionPosition() returns {x,y}; viewportPoint() works in cells.
    const start = viewportPoint({ col: pos.start.x, row: pos.start.y });
    const end = viewportPoint({ col: pos.end.x, row: pos.end.y });
    if (start) {
      handleStart.style.display = 'block';
      handleStart.style.left = `${start.x}px`;
      handleStart.style.top = `${start.y}px`;
    } else {
      handleStart.style.display = 'none';
    }
    if (end) {
      handleEnd.style.display = 'block';
      handleEnd.style.left = `${end.x}px`;
      handleEnd.style.top = `${end.y}px`;
    } else {
      handleEnd.style.display = 'none';
    }
    const anchor = start || end;
    if (!anchor) { toolbar.classList.add('hidden'); return; }
    toolbar.classList.remove('hidden');
    const width = toolbar.offsetWidth || 184;
    const left = clamp(anchor.x - width / 2, 8, (window.innerWidth || 360) - width - 8);
    let top = Math.min(
      start ? start.y : anchor.y,
      end ? end.y : anchor.y,
    ) - 52;
    if (top < 8) {
      top = Math.max(
        start ? start.bottom : anchor.bottom,
        end ? end.bottom : anchor.bottom,
      ) + 10;
    }
    toolbar.style.left = `${left}px`;
    toolbar.style.top = `${top}px`;
  }

  function onHandleStart(ev) {
    draggingHandle = ev.currentTarget && ev.currentTarget.dataset
      ? ev.currentTarget.dataset.handle : null;
    stopEvent(ev);
  }

  function onHandleMove(ev) {
    if (!draggingHandle || !selection) return;
    const touch = ev.touches && ev.touches[0];
    if (!touch) return;
    stopEvent(ev);
    const cell = pointToCell(touch.clientX, touch.clientY);
    if (!cell) return;
    if (draggingHandle === 'start') {
      applySelection({ col: cell.col, row: cell.row }, selection.end);
    } else {
      applySelection(selection.start, { col: cell.col, row: cell.row });
    }
    syncUi();
  }

  function onHandleEnd(ev) {
    draggingHandle = null;
    stopEvent(ev);
  }

  function ensureUi() {
    if (toolbar) return;
    toolbar = doc.createElement('div');
    toolbar.className = 'cam-term-toolbar hidden';
    toolbar.setAttribute('role', 'toolbar');
    toolbar.setAttribute('aria-label', 'Terminal selection');
    const makeButton = (label, action) => {
      const button = doc.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.dataset.action = action;
      return button;
    };
    toolbar.append(
      makeButton('Copy', 'copy'),
      makeButton('Paste', 'paste'),
      makeButton('All', 'all'),
      makeButton('Done', 'done'),
    );
    // Keep the tap as a click: only stop propagation, never preventDefault.
    toolbar.addEventListener('touchstart', (ev) => {
      try { ev.stopPropagation(); } catch (_) {}
    }, { passive: true });
    toolbar.addEventListener('mousedown', stopEvent);
    toolbar.addEventListener('click', (ev) => {
      const button = ev.target && ev.target.closest
        ? ev.target.closest('button[data-action]') : null;
      if (!button) return;
      const action = button.dataset.action;
      if (action === 'copy') void copySelection();
      else if (action === 'paste') void pasteClipboard();
      else if (action === 'all') {
        try { term.selectAll(); } catch (_) {}
        syncUi();
      } else if (action === 'done') {
        try { term.clearSelection(); } catch (_) {}
        hideUi();
      }
    });
    doc.body.appendChild(toolbar);

    handleStart = doc.createElement('div');
    handleStart.className = 'cam-term-handle';
    handleStart.dataset.handle = 'start';
    handleEnd = doc.createElement('div');
    handleEnd.className = 'cam-term-handle';
    handleEnd.dataset.handle = 'end';
    for (const handle of [handleStart, handleEnd]) {
      handle.addEventListener('touchstart', onHandleStart, { capture: true, passive: false });
      handle.addEventListener('touchmove', onHandleMove, { capture: true, passive: false });
      handle.addEventListener('touchend', onHandleEnd, { capture: true, passive: false });
      handle.addEventListener('touchcancel', onHandleEnd, { capture: true, passive: false });
      doc.body.appendChild(handle);
    }
    hideUi();
  }

  async function writeClipboard(text) {
    const value = String(text == null ? '' : text);
    if (!value) return false;
    try {
      const native = window.__camClipboard;
      if (native && typeof native.writeText === 'function' && native.writeText(value)) {
        return true;
      }
    } catch (_) {}
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (_) {}
    try {
      const area = doc.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;';
      doc.body.appendChild(area);
      area.select();
      const ok = doc.execCommand('copy');
      area.remove();
      return !!ok;
    } catch (_) { return false; }
  }

  async function readClipboard() {
    try {
      const native = window.__camClipboard;
      if (native && typeof native.readText === 'function') {
        const value = native.readText();
        if (value) return String(value);
      }
    } catch (_) {}
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        const value = await navigator.clipboard.readText();
        if (value) return String(value);
      }
    } catch (_) {}
    return '';
  }

  async function copySelection() {
    let text = '';
    try { text = term.hasSelection() ? term.getSelection() : ''; } catch (_) {}
    if (!text) { report('Nothing selected', 'error'); return false; }
    const ok = await writeClipboard(text);
    report(ok ? `Copied ${text.length} chars` : 'Copy failed', ok ? 'info' : 'error');
    return ok;
  }

  async function pasteClipboard() {
    if (!entry || !entry.sessionId) {
      report('Terminal not attached', 'error');
      return false;
    }
    const text = await readClipboard();
    if (!text) { report('Clipboard empty or unavailable', 'error'); return false; }
    try {
      term.paste(text);
      report(`Pasted ${text.length} chars`, 'info');
      return true;
    } catch (_) {
      report('Paste failed', 'error');
      return false;
    }
  }

  function cancelLongPress() {
    if (longPressTimer) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  }

  function startLongPress() {
    cancelLongPress();
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      if (!active || !gesture || gesture.moved || gesture.mode !== 'pending') return;
      const cell = pointToCell(gesture.x0, gesture.y0);
      if (!cell) return;
      gesture.mode = 'select';
      const range = wordRangeAt(cell);
      applySelection(
        { col: range.startCol, row: cell.row },
        { col: range.endCol, row: cell.row },
      );
      syncUi();
      try { if (navigator.vibrate) navigator.vibrate(12); } catch (_) {}
      report('Selected — drag to adjust', 'info');
    }, longPressMs);
  }

  function stopEvent(ev) {
    try { ev.preventDefault(); } catch (_) {}
    try { ev.stopPropagation(); } catch (_) {}
  }

  function findTouch(touches, id) {
    for (let i = 0; i < touches.length; i += 1) {
      if (touches[i].identifier === id) return touches[i];
    }
    return null;
  }

  function onTouchStart(ev) {
    if (!active) return;
    if (ev.touches.length !== 1) {
      cancelLongPress();
      gesture = null;
      return;
    }
    const touch = ev.touches[0];
    gesture = {
      id: touch.identifier,
      x0: touch.clientX,
      y0: touch.clientY,
      lastY: touch.clientY,
      mode: 'pending',
      moved: false,
      scrollAcc: 0,
      pageDone: false,
    };
    startLongPress();
  }

  function onTouchMove(ev) {
    if (!active || !gesture) return;
    if (ev.touches.length !== 1) {
      cancelLongPress();
      gesture = null;
      return;
    }
    const touch = findTouch(ev.touches, gesture.id);
    if (!touch) return;

    const dx = touch.clientX - gesture.x0;
    const dy = touch.clientY - gesture.y0;

    if (gesture.mode === 'pending') {
      if (Math.abs(dx) < moveThreshold && Math.abs(dy) < moveThreshold) return;
      gesture.moved = true;
      cancelLongPress();
      if (entry.copyModeActive) {
        // Copy mode owns the swipe: leave the event alone so the view's
        // Up/Down/PPage/NPage handler receives it.
        gesture.mode = 'none';
        return;
      }
      gesture.mode = Math.abs(dx) > Math.abs(dy) * 1.4 && Math.abs(dx) > pageThreshold
        ? 'page' : 'scroll';
    }

    if (gesture.mode === 'scroll') {
      stopEvent(ev);
      const m = metrics();
      if (!m) return;
      gesture.scrollAcc += touch.clientY - gesture.lastY;
      gesture.lastY = touch.clientY;
      const lines = Math.trunc(gesture.scrollAcc / m.ch);
      if (!lines) return;
      gesture.scrollAcc -= lines * m.ch;
      try { term.scrollLines(-lines); } catch (_) {}
      entry.needsBottom = false;
      entry.forceBottomUntil = 0;
      if (onViewportChange) { try { onViewportChange(); } catch (_) {} }
    } else if (gesture.mode === 'page') {
      stopEvent(ev);
      if (gesture.pageDone) return;
      gesture.pageDone = true;
      try { term.scrollPages(dx < 0 ? 1 : -1); } catch (_) {}
      entry.needsBottom = false;
      entry.forceBottomUntil = 0;
      if (onViewportChange) { try { onViewportChange(); } catch (_) {} }
    } else if (gesture.mode === 'select') {
      stopEvent(ev);
      if (!selection) return;
      const cell = pointToCell(touch.clientX, touch.clientY);
      if (!cell) return;
      applySelection(selection.start, { col: cell.col, row: cell.row });
      syncUi();
    }
  }

  function onTouchEnd(ev) {
    if (!gesture) return;
    const moved = gesture.mode !== 'pending' || gesture.moved;
    const wasTap = gesture.mode === 'pending' && !gesture.moved;
    cancelLongPress();
    if (wasTap) {
      if (term.hasSelection()) {
        try { term.clearSelection(); } catch (_) {}
        hideUi();
      } else {
        try { term.focus(); } catch (_) {}
      }
    }
    gesture = null;
    if (ev && ev.cancelable && moved) { try { ev.preventDefault(); } catch (_) {} }
  }

  function onContextMenu(ev) {
    stopEvent(ev);
  }

  function setActive(value) {
    active = !!value;
    if (!active) {
      cancelLongPress();
      gesture = null;
      draggingHandle = null;
      hideUi();
    }
  }

  // Hardware-keyboard parity with the Desktop trap: Ctrl/Cmd+C copies a
  // selection (without one it stays SIGINT), Ctrl/Cmd+V pastes.
  const keyHandler = (ev) => {
    if (!ev || ev.type !== 'keydown') return true;
    const key = String(ev.key || '').toLowerCase();
    if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && key === 'c') {
      if (typeof term.hasSelection !== 'function' || !term.hasSelection()) return true;
      try { ev.preventDefault(); ev.stopPropagation(); } catch (_) {}
      void copySelection();
      return false;
    }
    if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && key === 'v') {
      try { ev.preventDefault(); ev.stopPropagation(); } catch (_) {}
      void pasteClipboard();
      return false;
    }
    return true;
  };

  function detach() {
    setActive(false);
    cancelLongPress();
    try { if (selectionSub) selectionSub.dispose(); } catch (_) {}
    try { if (scrollSub) scrollSub.dispose(); } catch (_) {}
    selectionSub = null;
    scrollSub = null;
    try { host.removeEventListener('touchstart', onTouchStart, true); } catch (_) {}
    try { host.removeEventListener('touchmove', onTouchMove, true); } catch (_) {}
    try { host.removeEventListener('touchend', onTouchEnd, true); } catch (_) {}
    try { host.removeEventListener('touchcancel', onTouchEnd, true); } catch (_) {}
    try { host.removeEventListener('contextmenu', onContextMenu, true); } catch (_) {}
    try { term.attachCustomKeyEventHandler(() => true); } catch (_) {}
    if (toolbar && toolbar.parentNode) toolbar.parentNode.removeChild(toolbar);
    if (handleStart && handleStart.parentNode) handleStart.parentNode.removeChild(handleStart);
    if (handleEnd && handleEnd.parentNode) handleEnd.parentNode.removeChild(handleEnd);
    toolbar = null;
    handleStart = null;
    handleEnd = null;
  }

  try {
    if (typeof term.onSelectionChange === 'function') {
      selectionSub = term.onSelectionChange(syncUi);
    }
    if (typeof term.onScroll === 'function') {
      scrollSub = term.onScroll(syncUi);
    }
    if (typeof term.attachCustomKeyEventHandler === 'function') {
      term.attachCustomKeyEventHandler(keyHandler);
    }
  } catch (_) {}
  host.addEventListener('touchstart', onTouchStart, { capture: true, passive: false });
  host.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
  host.addEventListener('touchend', onTouchEnd, { capture: true, passive: false });
  host.addEventListener('touchcancel', onTouchEnd, { capture: true, passive: false });
  host.addEventListener('contextmenu', onContextMenu, { capture: true });

  return { detach, setActive, sync: syncUi, copySelection, pasteClipboard };
}
