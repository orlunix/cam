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
