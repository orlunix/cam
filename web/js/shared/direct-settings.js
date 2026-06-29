/**
 * Direct Settings — shared by Desktop and Mobile (CAM-DESK-DIRECT-010..019).
 * Normal path: no Hub URL / API token fields. Embedded Hub lifecycle via
 * CamBridge.directHub when available; credentials persist in localStorage.
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function shortRedact(fp) {
  if (!fp) return '(none)';
  if (typeof fp !== 'string') return '(?)';
  return fp.length > 24 ? fp.slice(0, 24) + '…' : fp;
}

export function bridgeDirectHub() {
  if (typeof window === 'undefined') return null;
  if (window.__camDirectHub) return window.__camDirectHub;
  const b = window.CamBridge;
  return (b && b.directHub) || null;
}

export function isLoopbackUrl(u) {
  try {
    const h = new URL(u).hostname;
    return h === '127.0.0.1' || h === 'localhost' || h === '::1';
  } catch { return false; }
}

export async function startEmbeddedHubAndPersist({ saveConfig, setProfileKind }) {
  const bridge = bridgeDirectHub();
  if (!bridge) return false;
  let res;
  try { res = await bridge.start(); }
  catch (e) { console.warn('directHub.start failed:', e); return false; }
  if (!res || res.ok !== true || !res.apiUrl || !res.apiToken) return false;
  saveConfig({
    serverUrl: res.apiUrl,
    token: res.apiToken,
    relayUrl: '',
    relayToken: '',
  });
  if (typeof setProfileKind === 'function') setProfileKind('direct');
  else try { localStorage.setItem('cam_profile_kind', 'direct'); } catch {}
  return true;
}

/**
 * Mount Direct settings panel (Desktop `#settings-tab-direct` or Mobile
 * `#settings-form-direct`). Expects the same inner element IDs as desktop.html.
 */
export function mountDirectSettings({
  panel,
  api,
  state,
  showToast,
  readConfig,
  saveConfig,
  connect,
  setProfileKind,
}) {
  if (!panel) return;

  const badgeEl   = panel.querySelector('#direct-state-badge');
  const detailEl  = panel.querySelector('#direct-state-detail');
  const targetEl  = panel.querySelector('#direct-target');
  const hintEl    = panel.querySelector('#direct-hint');
  const statusEl  = panel.querySelector('#direct-status');
  const checkBtn  = panel.querySelector('#direct-check');
  const startBtn  = panel.querySelector('#direct-start');
  const stopBtn   = panel.querySelector('#direct-stop');
  const restartBt = panel.querySelector('#direct-restart');
  const diagGrid  = panel.querySelector('#direct-diag-grid');
  const diagLogs  = panel.querySelector('#direct-diag-logs');
  const diagRefr  = panel.querySelector('#direct-diag-refresh');

  function setBadge(summary) {
    if (!badgeEl) return;
    badgeEl.className = 'direct-state-badge state-' + (summary || 'stopped');
    badgeEl.textContent = (summary || 'stopped').replace(/-/g, ' ');
  }
  function setHint(text, cls = '') {
    if (!hintEl) return;
    hintEl.textContent = text || '';
    hintEl.classList.remove('is-error', 'is-ok');
    if (cls) hintEl.classList.add(cls);
  }
  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function summaryDetail(r) {
    if (!r) return '';
    const range = r.apiPortRange && r.apiPortRange.start != null && r.apiPortRange.end != null
      ? `${r.apiPortRange.start}..${r.apiPortRange.end}`
      : `${r.apiPort || 8420}+`;
    switch (r.summary) {
      case 'running':       return 'Embedded CAM Hub is running on this machine.';
      case 'starting':      return 'Embedded CAM Hub is starting…';
      case 'port-conflict': return `No fixed Direct port in ${range} could be bound. Open Diagnostics for port details.`;
      case 'stopped':
        if (r.apiPortStatus && r.apiPortStatus.state === 'fallback-free') {
          return `Fixed Direct ports (${range}) are unavailable, but an OS-assigned loopback port is available. Tap Restart to recover.`;
        }
        return 'Embedded CAM Hub is stopped. Tap Enable or Start to launch it.';
      case 'connected':     return 'Connected to saved Direct Hub profile.';
      case 'disconnected':  return 'Direct profile saved but Hub is not reachable.';
      case 'error':         return r.lastError || 'Embedded CAM Hub failed to start.';
      default:              return r.summary || '';
    }
  }

  function renderTarget(r, cfg) {
    if (!targetEl) return;
    if (r && r.platform) {
      targetEl.innerHTML = `Runtime: <strong>embedded</strong> (Electron Node · ${escapeHtml(r.platform)})`;
      return;
    }
    const url = (cfg && cfg.serverUrl) || '';
    if (url) {
      try {
        const u = new URL(url);
        targetEl.innerHTML = `Saved Hub: <code>${escapeHtml(u.host)}</code>`;
      } catch {
        targetEl.textContent = 'Saved Direct profile';
      }
    } else {
      targetEl.textContent = 'No Direct profile yet — Start launches the embedded Hub when available.';
    }
  }

  function portCandidateSummary(r) {
    const c = r && r.apiPortCandidates;
    if (!c) return '—';
    const errors = c.errors && Object.keys(c.errors).length
      ? ' · ' + Object.entries(c.errors).map(([k, v]) => `${k}:${v}`).join(', ')
      : '';
    const first = c.firstFree == null ? 'none' : c.firstFree;
    const fallback = c.fallback
      ? ` · OS fallback: ${c.fallback}${c.fallbackError ? ` (${c.fallbackError})` : ''}`
      : '';
    return `${c.free}/${c.count} free · first free: ${first}${fallback}${errors}`;
  }

  function tokenFingerprint(token) {
    if (!token) return '(none)';
    return token.slice(0, 8) + '…' + token.slice(-4);
  }

  function renderDiagnostics(r, cfg) {
    if (!diagGrid) return;
    const portState = r && r.apiPortStatus && r.apiPortStatus.state;
    const rows = [
      ['Profile kind', (cfg && cfg.profileKind) || '—'],
      ['Saved Direct URL', (cfg && cfg.serverUrl) || '—'],
      ['Token (redacted)', tokenFingerprint(cfg && cfg.token)],
      ['Renderer API mode', api && api.mode ? api.mode : (state && state.get ? state.get('connectionMode') : '—')],
    ];
    if (r && r.apiPort != null) {
      rows.splice(1, 0,
        ['API port', `${r.apiPort}${portState ? ' — ' + portState : ''}`],
        ['Port scan range', r.apiPortRange ? `${r.apiPortRange.start}..${r.apiPortRange.end}` : '—'],
        ['Port candidates', portCandidateSummary(r)],
      );
    }
    if (r && r.platform) rows.splice(1, 0, ['Runtime', 'embedded (Electron Node)'], ['Platform', r.platform]);
    if (r && r.state && r.state.server) {
      const s = r.state.server;
      rows.push(
        ['Hub owned', String(!!s.owned)],
        ['Hub PID', s.pid || '—'],
        ['Hub token (sha256)', shortRedact(s.tokenFingerprint)],
      );
    }
    if (r && r.lastError) rows.push(['Last error', r.lastError]);
    diagGrid.innerHTML = rows.map(([k, v]) =>
      `<div class="diag-key">${escapeHtml(k)}</div><div class="diag-val">${escapeHtml(String(v))}</div>`,
    ).join('');
  }

  function applyButtonState(r, cfg) {
    const supported = !!bridgeDirectHub();
    const owned = !!(r && r.state && r.state.server && r.state.server.owned);
    const summary = r && r.summary;
    const hasSaved = !!(cfg && cfg.serverUrl && cfg.token);

    if (checkBtn) checkBtn.disabled = false;
    if (startBtn) {
      startBtn.textContent = supported ? 'Start' : 'Connect';
      startBtn.disabled = supported
        ? (!supported || owned || summary === 'port-conflict')
        : !hasSaved;
    }
    if (stopBtn) {
      stopBtn.disabled = supported ? !owned : !(hasSaved && state.get('connectionMode') === 'direct');
    }
    if (restartBt) restartBt.disabled = supported ? !supported : !hasSaved;
  }

  async function doEnable() {
    const mode = state.get('connectionMode');
    if (mode === 'direct') {
      await refreshCheck();
      setStatus('Direct already enabled.', 'is-ok');
      return;
    }
    await doStart();
  }

  async function refreshCheck() {
    const cfg = typeof readConfig === 'function' ? readConfig() : {};

    if (bridgeDirectHub()) {
      setBadge('checking');
      if (detailEl) detailEl.textContent = 'Checking embedded Hub state…';
      let r;
      try { r = await bridgeDirectHub().check(); }
      catch (e) {
        setBadge('error');
        if (detailEl) detailEl.textContent = `Check failed: ${e?.message || e}`;
        return null;
      }
      setBadge(r.summary);
      if (detailEl) detailEl.textContent = summaryDetail(r);
      renderTarget(r, cfg);
      renderDiagnostics(r, cfg);
      applyButtonState(r, cfg);
      return r;
    }

    // Mobile / browser: no embedded Hub bridge — probe saved profile only.
    setBadge('checking');
    if (detailEl) detailEl.textContent = 'Checking saved Direct profile…';
    if (!cfg.serverUrl || !cfg.token) {
      setBadge('stopped');
      if (detailEl) detailEl.textContent =
        'Direct uses an app-managed Hub profile (same as Desktop). Tap Start on Desktop to create one, or use Relay on mobile.';
      renderTarget(null, cfg);
      renderDiagnostics({ summary: 'stopped' }, cfg);
      applyButtonState({ summary: 'stopped' }, cfg);
      return null;
    }

    let ok = false;
    try {
      const headers = { Authorization: `Bearer ${cfg.token}` };
      const resp = await fetch(`${cfg.serverUrl.replace(/\/$/, '')}/api/system/health`, {
        headers,
        signal: AbortSignal.timeout(12000),
      });
      ok = resp.ok;
    } catch { ok = false; }

    const mode = state.get('connectionMode');
    const summary = ok && mode === 'direct' ? 'connected' : ok ? 'running' : 'disconnected';
    setBadge(summary);
    if (detailEl) detailEl.textContent = ok
      ? 'Saved Direct Hub is reachable.'
      : 'Saved Direct Hub is not reachable from this device.';
    renderTarget(null, cfg);
    renderDiagnostics({ summary }, cfg);
    applyButtonState({ summary }, cfg);
    return { summary };
  }

  async function doStart() {
    if (bridgeDirectHub()) {
      setStatus('Starting embedded Hub…');
      setBadge('starting');
      if (startBtn) startBtn.disabled = true;
      if (stopBtn) stopBtn.disabled = true;
      if (restartBt) restartBt.disabled = true;
      let res;
      try { res = await bridgeDirectHub().start(); }
      catch (e) {
        setStatus(`Start failed: ${e?.message || e}`, 'is-error');
        await refreshCheck();
        return;
      }
      if (!res || res.ok !== true) {
        const msg = res && res.error ? res.error : 'Start failed.';
        setStatus(msg, 'is-error');
        setHint(msg, 'is-error');
        await refreshCheck();
        return;
      }
      saveConfig({
        serverUrl: res.apiUrl,
        token: res.apiToken,
        relayUrl: '',
        relayToken: '',
      });
      if (typeof setProfileKind === 'function') setProfileKind('direct');
      setStatus('Connecting…');
      const mode = await connect();
      if (mode !== 'disconnected') {
        setStatus(`Connected (${mode}).`, 'is-ok');
        setHint('Embedded Hub is running and connected.', 'is-ok');
        showToast('Embedded Hub started', 'success');
      } else {
        setStatus('Hub started but client could not connect.', 'is-error');
      }
      await refreshCheck();
      return;
    }

    const cfg = readConfig();
    if (!cfg.serverUrl || !cfg.token) {
      setStatus('No Direct profile saved yet.', 'is-error');
      setHint('On phone use the Relay tab. Direct Hub runs in CamUI Desktop.', 'is-error');
      return;
    }
    setStatus('Connecting…');
    const mode = await connect();
    if (mode === 'direct') {
      setStatus('Connected (direct).', 'is-ok');
      setHint('Using saved Direct Hub profile.', 'is-ok');
      showToast('Connected (direct)', 'success');
    } else {
      setStatus('Connection failed.', 'is-error');
    }
    await refreshCheck();
  }

  async function doStop() {
    if (bridgeDirectHub()) {
      setStatus('Stopping embedded Hub…');
      try { await bridgeDirectHub().stop(); }
      catch (e) {
        setStatus(`Stop failed: ${e?.message || e}`, 'is-error');
        return;
      }
      saveConfig({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
      if (typeof setProfileKind === 'function') setProfileKind(null);
      setStatus('Stopped.', 'is-ok');
      setHint('');
      await connect();
      await refreshCheck();
      return;
    }

    saveConfig({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
    if (typeof setProfileKind === 'function') setProfileKind(null);
    api.configure({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
    state.set('connectionMode', 'disconnected');
    setStatus('Direct profile cleared.', 'is-ok');
    setHint('');
    await refreshCheck();
  }

  async function doRestart() {
    if (bridgeDirectHub()) {
      setStatus('Restarting Direct connection…');
      setBadge('starting');
      try {
        const res = await bridgeDirectHub().restart();
        if (!res || res.ok !== true || !res.apiUrl || !res.apiToken) {
          const msg = (res && res.error) || 'Restart failed.';
          setStatus(msg, 'is-error');
        } else {
          saveConfig({
            serverUrl: res.apiUrl,
            token: res.apiToken,
            relayUrl: '',
            relayToken: '',
          });
          if (typeof setProfileKind === 'function') setProfileKind('direct');
          const mode = await connect();
          if (mode !== 'disconnected') {
            setStatus(`Reconnected (${mode}).`, 'is-ok');
            showToast('Direct connection restarted', 'success');
          } else {
            setStatus('Restarted Hub but reconnect failed.', 'is-error');
          }
        }
      } catch (e) {
        setStatus(`Restart failed: ${e?.message || e}`, 'is-error');
      }
      await refreshCheck();
      return;
    }

    setStatus('Reconnecting…');
    const mode = await connect();
    if (mode === 'direct') {
      setStatus('Reconnected (direct).', 'is-ok');
      showToast('Reconnected (direct)', 'success');
    } else {
      setStatus('Reconnect failed.', 'is-error');
    }
    await refreshCheck();
  }

  async function refreshLogs() {
    if (!diagLogs) return;
    const hub = bridgeDirectHub();
    if (!hub || typeof hub.logs !== 'function') {
      diagLogs.textContent = '(Hub logs require embedded Hub — enable Direct on mobile or Desktop)';
      return;
    }
    let out;
    try { out = await hub.logs(); }
    catch (e) { diagLogs.textContent = `(logs unavailable: ${e?.message || e})`; return; }
    const lines = (out && out.server) || [];
    diagLogs.textContent =
      `=== embedded Hub (${lines.length} lines) ===\n` +
      lines.map((l) => {
        let ts = '';
        if (l.ts) {
          try { ts = new Date(l.ts).toISOString().slice(11, 23) + ' '; } catch { /* noop */ }
        }
        return `${ts}[${l.kind}] ${l.text}`;
      }).join('\n');
  }

  if (checkBtn) checkBtn.textContent = 'Enable';
  checkBtn && checkBtn.addEventListener('click', doEnable);
  startBtn && startBtn.addEventListener('click', doStart);
  stopBtn && stopBtn.addEventListener('click', doStop);
  restartBt && restartBt.addEventListener('click', doRestart);
  diagRefr && diagRefr.addEventListener('click', refreshLogs);

  const cfg = typeof readConfig === 'function' ? readConfig() : {};
  if (bridgeDirectHub()) {
    setBadge('stopped');
    if (detailEl) detailEl.textContent = 'Tap Enable to turn on Direct, or Start to launch the embedded Hub.';
  } else if (cfg.serverUrl && cfg.token) {
    void refreshCheck();
  } else {
    setBadge('stopped');
    if (detailEl) detailEl.textContent =
      'Direct stores Hub URL and token internally (same as Desktop). Use Relay on mobile, or Connect after Desktop seeds a profile.';
    applyButtonState({ summary: 'stopped' }, cfg);
  }
}
