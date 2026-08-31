/* Diagnostics mode: read-only viewer for the app diag log
 * (userData/cam-desktop.log) — SSH connect/handshake lines, channel
 * opens, attach stages, resets, renderer events. Category filters,
 * manual/auto refresh, one-click copy for support reports. */

export function mountDiagnosticsMode({ panel }) {
  const logEl = panel.querySelector('#diag-log');
  const statusEl = panel.querySelector('#diag-status');
  const refreshBtn = panel.querySelector('#diag-refresh');
  const copyBtn = panel.querySelector('#diag-copy');
  const autoCb = panel.querySelector('#diag-auto');
  const filterBar = panel.querySelector('#diag-filters');
  if (!logEl || !refreshBtn || !copyBtn) return;

  let filter = 'all';
  let autoTimer = null;
  let lastLines = [];

  const FILTERS = {
    exec:    [/\[ssh\] exec /],
    connect: [/\[ssh\] connect /],
    channel: [/\[ssh\] channel /],
    attach:  [/\[attach\]/],
    reset:   [/\[app:reset\]|reload:/],
    renderer:[/\[renderer\]/],
  };

  function bridge() {
    return window.CamBridge || null;
  }

  function applyFilter(lines) {
    if (filter === 'all') return lines;
    const pats = FILTERS[filter] || [];
    return lines.filter(l => pats.some(p => p.test(l)));
  }

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text || '';
  }

  async function refresh() {
    const b = bridge();
    if (!b || typeof b.diagTail !== 'function') {
      logEl.textContent = 'Diagnostics requires the desktop app bridge.';
      return;
    }
    refreshBtn.disabled = true;
    try {
      // 400-line tails drowned discrete events (camc run, sync) in the
      // continuous tmux polling chatter — pull the handler's max window.
      const r = await b.diagTail(2000);
      lastLines = (r && r.lines) || [];
      const shown = applyFilter(lastLines.filter(l => l.trim()));
      logEl.textContent = shown.length ? shown.join('\n') : '(no matching log lines)';
      logEl.scrollTop = logEl.scrollHeight;
      setStatus(`${shown.length} line(s) · filter: ${filter} · ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setStatus(`refresh failed: ${(e && e.message) || e}`);
    } finally {
      refreshBtn.disabled = false;
    }
  }

  filterBar?.addEventListener('click', (ev) => {
    const btn = ev.target && ev.target.closest('[data-diag-filter]');
    if (!btn) return;
    filter = btn.dataset.diagFilter || 'all';
    filterBar.querySelectorAll('.diag-filter').forEach(b => b.classList.toggle('active', b === btn));
    const shown = applyFilter(lastLines.filter(l => l.trim()));
    logEl.textContent = shown.length ? shown.join('\n') : '(no matching log lines)';
    logEl.scrollTop = logEl.scrollHeight;
    setStatus(`${shown.length} line(s) · filter: ${filter}`);
  });

  refreshBtn.addEventListener('click', () => { void refresh(); });

  copyBtn.addEventListener('click', async () => {
    const text = applyFilter(lastLines.filter(l => l.trim())).join('\n');
    if (!text) { setStatus('nothing to copy'); return; }
    try {
      await navigator.clipboard.writeText(text);
      setStatus('copied to clipboard');
    } catch (e) {
      setStatus(`copy failed: ${(e && e.message) || e}`);
    }
  });

  autoCb?.addEventListener('change', () => {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    if (autoCb.checked) {
      autoTimer = setInterval(() => { void refresh(); }, 5000);
    }
  });

  void refresh();
}
