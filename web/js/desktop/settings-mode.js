/**
 * Desktop Settings mode — full main-pane configuration view that
 * replaces the Agent Console when mode = 'settings'.
 *
 * Active Settings exposes exactly two connection modes
 * (CAM-DESK-REMOTE-014):
 *
 *   Direct — app-managed embedded CAM Hub. Electron main runs a
 *            Node HTTP server on 127.0.0.1:8420 (see
 *            apps/cam-desktop/electron/embedded-hub.cjs), generates
 *            an API token, and Desktop connects to it through the
 *            normal CamApi REST surface (CAM-DESK-DIRECT-010..019,
 *            CAM-DESK-HUB-010..012). No host `cam` CLI, Python, or
 *            WSL is involved. The renderer never sees or edits the
 *            Hub URL or API token in the normal path; tokens are
 *            only shown as redacted sha256 fingerprints inside
 *            Advanced / Diagnostics.
 *
 *   Relay  — external relay endpoint that proxies to an existing Hub
 *            (CAM-DESK-REMOTE-012). User-typed relay URL + relay
 *            token + CAM API token.
 *
 * The earlier separate "Local" tab and the user-typed Direct URL/token
 * form are retired. Their safe main-process lifecycle code is reused
 * via CamBridge.directHub.* (IPC handler names on the main side are
 * still `local:*` for internal compatibility).
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

const PROFILE_KIND_KEY = 'cam_profile_kind';

function _profileKind() {
  try {
    const v = localStorage.getItem(PROFILE_KIND_KEY);
    if (v === 'direct' || v === 'relay') return v;
  } catch {}
  try {
    if (localStorage.getItem('cam_relay_url') && localStorage.getItem('cam_relay_token')) return 'relay';
    if (localStorage.getItem('cam_server_url') && localStorage.getItem('cam_token')) return 'direct';
  } catch {}
  return 'unset';
}

function _setProfileKind(kind) {
  try {
    if (kind === 'direct' || kind === 'relay') {
      localStorage.setItem(PROFILE_KIND_KEY, kind);
    } else {
      localStorage.removeItem(PROFILE_KIND_KEY);
    }
  } catch {}
}

function shortRedact(fp) {
  if (!fp) return '(none)';
  if (typeof fp !== 'string') return '(?)';
  return fp.length > 24 ? fp.slice(0, 24) + '…' : fp;
}

/** Argv-only narrow bridge to the Electron-main Direct Hub supervisor.
 *  Exposes only `check / start / stop / restart / logs / getProfile`;
 *  no command/argv/path/env passed from renderer. The renderer cannot
 *  reach this surface in a browser tab — it lives on
 *  `window.CamBridge.directHub`. See preload.cjs. */
function bridgeDirectHub() {
  const b = typeof window !== 'undefined' ? window.CamBridge : null;
  return (b && b.directHub) || null;
}

export function mountSettingsMode({ state, showToast, readConfig, saveConfig, connect }) {
  const panel = document.getElementById('mode-settings');
  if (!panel) return;

  /* ────────── Legacy profile migration ──────────
   * If a previous build left `cam_profile_kind === 'local'` (old Local
   * Node experiment marker), fold it into Direct so existing
   * serverUrl+token state keeps working under the renamed mode. */
  try {
    if (localStorage.getItem(PROFILE_KIND_KEY) === 'local') {
      const cfg = readConfig();
      if (cfg.serverUrl && cfg.token) {
        localStorage.setItem(PROFILE_KIND_KEY, 'direct');
      } else {
        localStorage.removeItem(PROFILE_KIND_KEY);
      }
    }
  } catch {}

  /* ────────── Settings tabs (Direct / Relay only) ──────────
   * CAM-DESK-REMOTE-014: active Settings has exactly two tabs.
   * Unknown saved tab values (e.g. legacy 'local') are clamped to
   * 'direct' so existing users do not crash on an unrendered tab. */
  const tabButtons = panel.querySelectorAll('.settings-tab[data-tab]');
  const tabPanels  = panel.querySelectorAll('.settings-tab-panel[data-tab]');
  const TAB_KEY    = 'cam_desktop_settings_tab';

  function applyTab(name) {
    if (name !== 'direct' && name !== 'relay') name = 'direct';
    tabButtons.forEach((b) => {
      const active = b.dataset.tab === name;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    tabPanels.forEach((p) => {
      const active = p.dataset.tab === name;
      if (active) p.removeAttribute('hidden');
      else p.setAttribute('hidden', '');
    });
    try { localStorage.setItem(TAB_KEY, name); } catch {}
  }

  function pickInitialTab() {
    try {
      const saved = localStorage.getItem(TAB_KEY);
      if (saved === 'direct' || saved === 'relay') return saved;
    } catch {}
    if (_profileKind() === 'relay') return 'relay';
    return 'direct';
  }

  tabButtons.forEach((b) => {
    b.addEventListener('click', () => applyTab(b.dataset.tab));
  });
  applyTab(pickInitialTab());

  /* ────────── Direct tab — app-managed Hub lifecycle ────────── */
  mountDirectTab({ panel, state, showToast, saveConfig, connect });

  /* ────────── Relay tab ──────────
   * The Relay user-mode points at an external relay endpoint that
   * proxies REST traffic to an existing CAM Hub. Because the proxied
   * requests still hit a token-protected /api/* surface, we need
   * THREE inputs: relay URL + relay shared secret + the CAM Hub's API
   * token. Without the third, the websocket connects but every proxied
   * REST call fails 401. */
  mountRelayTab({ panel, readConfig, saveConfig, connect, showToast });
}

/* ───────── Direct tab controller (CAM-DESK-DIRECT-010..019) ───────── */

function mountDirectTab({ panel, state, showToast, saveConfig, connect }) {
  const tabPanel = panel.querySelector('#settings-tab-direct');
  if (!tabPanel) return;

  const badgeEl   = tabPanel.querySelector('#direct-state-badge');
  const detailEl  = tabPanel.querySelector('#direct-state-detail');
  const targetEl  = tabPanel.querySelector('#direct-target');
  const hintEl    = tabPanel.querySelector('#direct-hint');
  const statusEl  = tabPanel.querySelector('#direct-status');
  const checkBtn  = tabPanel.querySelector('#direct-check');
  const startBtn  = tabPanel.querySelector('#direct-start');
  const stopBtn   = tabPanel.querySelector('#direct-stop');
  const restartBt = tabPanel.querySelector('#direct-restart');
  const diagGrid  = tabPanel.querySelector('#direct-diag-grid');
  const diagLogs  = tabPanel.querySelector('#direct-diag-logs');
  const diagRefr  = tabPanel.querySelector('#direct-diag-refresh');

  function setBadge(summary) {
    badgeEl.className = 'direct-state-badge state-' + (summary || 'stopped');
    badgeEl.textContent = (summary || 'stopped').replace('-', ' ');
  }
  function setHint(text, cls = '') {
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
    switch (r.summary) {
      case 'running':       return 'Embedded CAM Hub is running on this machine.';
      case 'starting':      return 'Embedded CAM Hub is starting…';
      case 'port-conflict': return `All candidate loopback ports (${r.apiPort}+) are in use. Free one of them and click Check again.`;
      case 'stopped':       return 'Embedded CAM Hub is stopped. Click Start to launch it.';
      case 'error':         return r.lastError || 'Embedded CAM Hub failed to start.';
      default:              return r.summary || '';
    }
  }

  function renderTarget(r) {
    if (!r) { targetEl.textContent = ''; return; }
    // The embedded Hub runs inside the Electron Node process — no
    // WSL, no host Python, no host `cam` CLI required
    // (CAM-DESK-DIRECT-011 / DIRECT-013).
    const plat = r.platform ? ` · ${escapeHtml(r.platform)}` : '';
    targetEl.innerHTML = `Runtime: <strong>embedded</strong> (Electron Node${plat})`;
  }

  function renderDiagnostics(r) {
    if (!r) { diagGrid.textContent = '(no data)'; return; }
    const portState = r.apiPortStatus && r.apiPortStatus.state;
    const rows = [
      ['Runtime',        'embedded (Electron Node)'],
      ['Platform',       r.platform || '—'],
      ['API port',       `${r.apiPort}${portState ? ' — ' + portState : ''}`],
      ['Profile kind',   _profileKind()],
    ];
    if (r.state && r.state.server) {
      const s = r.state.server;
      rows.push(
        ['Hub owned',          String(!!s.owned)],
        ['Hub PID',            s.pid || '—'],
        ['Hub token (sha256)', shortRedact(s.tokenFingerprint)],
      );
    }
    if (r.lastError) {
      rows.push(['Last error', r.lastError]);
    }
    diagGrid.innerHTML = rows.map(([k, v]) =>
      `<div class="diag-key">${escapeHtml(k)}</div><div class="diag-val">${escapeHtml(String(v))}</div>`,
    ).join('');
  }

  function applyButtonState(r) {
    const supported = !!bridgeDirectHub();
    const owned    = !!(r && r.state && r.state.server && r.state.server.owned);
    const summary  = r && r.summary;
    checkBtn.disabled = !supported;
    // The embedded Hub is built in — there is no local-runtime
    // gate. Start is only blocked when the whole candidate port range
    // is exhausted (summary === 'port-conflict'). The hub-side
    // start() does its own scan and will pick 8421..8429 if 8420 is
    // taken, so a single foreign listener on 8420 must not block UI.
    startBtn.disabled = !supported || owned || summary === 'port-conflict';
    stopBtn.disabled    = !supported || !owned;
    restartBt.disabled  = !supported || !owned;
  }

  async function refreshCheck() {
    if (!bridgeDirectHub()) {
      setBadge('error');
      detailEl.textContent = 'Direct Hub requires CAM Desktop (Electron). Open the installed app to use Direct.';
      checkBtn.disabled = true;
      startBtn.disabled = stopBtn.disabled = restartBt.disabled = true;
      return null;
    }
    setBadge('checking');
    detailEl.textContent = 'Checking embedded Hub state…';
    let r;
    try {
      r = await bridgeDirectHub().check();
    } catch (e) {
      setBadge('error');
      detailEl.textContent = `Check failed: ${e?.message || e}`;
      return null;
    }
    setBadge(r.summary);
    detailEl.textContent = summaryDetail(r);
    renderTarget(r);
    renderDiagnostics(r);
    applyButtonState(r);
    return r;
  }

  async function doStart() {
    if (!bridgeDirectHub()) return;
    setStatus('Starting embedded Hub…');
    setBadge('starting');
    startBtn.disabled = stopBtn.disabled = restartBt.disabled = true;
    let res;
    try {
      res = await bridgeDirectHub().start();
    } catch (e) {
      setStatus(`Start failed: ${e?.message || e}`, 'is-error');
      await refreshCheck();
      return;
    }
    if (!res || res.ok !== true) {
      setStatus(res && res.error ? res.error : 'Start failed.', 'is-error');
      setHint(res && res.error ? res.error : 'See Diagnostics for details.', 'is-error');
      await refreshCheck();
      return;
    }
    // The generated internal profile is Direct-compatible (CAM-DESK-DIRECT-012):
    // store apiUrl + apiToken in the shared localStorage keys, clear the
    // Relay fields, and mark the profile kind. Downstream code uses
    // CamApi.connect via the existing Direct wire shape — no branching
    // on transport. Token never leaves Diagnostics in plaintext.
    saveConfig({
      serverUrl:  res.apiUrl,
      token:      res.apiToken,
      relayUrl:   '',
      relayToken: '',
    });
    _setProfileKind('direct');
    setStatus('Connecting…');
    const mode = await connect();
    if (mode !== 'disconnected') {
      setStatus(`Connected (${mode}).`, 'is-ok');
      setHint('Embedded Hub is running and Desktop is connected.', 'is-ok');
      showToast('Embedded Hub started', 'success');
    } else {
      setStatus('Hub started but client could not connect.', 'is-error');
      setHint('Hub started but client could not connect.', 'is-error');
    }
    await refreshCheck();
  }

  async function doStop() {
    if (!bridgeDirectHub()) return;
    setStatus('Stopping embedded Hub…');
    try {
      await bridgeDirectHub().stop();
    } catch (e) {
      setStatus(`Stop failed: ${e?.message || e}`, 'is-error');
      return;
    }
    // Disable the Direct profile entirely. We clear both the
    // profile-kind marker AND the internal Direct serverUrl/token,
    // then call connect() so AppState transitions to disconnected.
    // Without this, app.js' resolveConnectionConfig() infers a Direct
    // profile from the still-present serverUrl+token and the
    // POLL_INTERVAL reconnect loop hammers a stopped Hub every 5s.
    // The next Start round will generate a fresh token anyway, so
    // there's nothing useful to preserve here.
    saveConfig({ serverUrl: '', token: '', relayUrl: '', relayToken: '' });
    _setProfileKind(null);
    setStatus('Stopped.', 'is-ok');
    setHint('');
    await connect();
    await refreshCheck();
  }

  async function doRestart() {
    if (!bridgeDirectHub()) return;
    setStatus('Restarting embedded Hub…');
    setBadge('starting');
    try {
      const res = await bridgeDirectHub().restart();
      if (!res || res.ok !== true) {
        setStatus((res && res.error) || 'Restart failed.', 'is-error');
      } else {
        saveConfig({
          serverUrl:  res.apiUrl,
          token:      res.apiToken,
          relayUrl:   '',
          relayToken: '',
        });
        _setProfileKind('direct');
        const mode = await connect();
        if (mode !== 'disconnected') {
          setStatus(`Reconnected (${mode}).`, 'is-ok');
        } else {
          setStatus('Restarted but reconnect failed.', 'is-error');
        }
      }
    } catch (e) {
      setStatus(`Restart failed: ${e?.message || e}`, 'is-error');
    }
    await refreshCheck();
  }

  async function refreshLogs() {
    if (!bridgeDirectHub()) return;
    let out;
    try { out = await bridgeDirectHub().logs(); }
    catch (e) { diagLogs.textContent = `(logs unavailable: ${e?.message || e})`; return; }
    const lines = (out && out.server) || [];
    diagLogs.textContent =
      `=== embedded Hub (${lines.length} lines) ===\n` +
      lines.map((l) => `[${l.kind}] ${l.text}`).join('\n');
  }

  checkBtn.addEventListener('click',  refreshCheck);
  startBtn.addEventListener('click',  doStart);
  stopBtn.addEventListener('click',   doStop);
  restartBt.addEventListener('click', doRestart);
  diagRefr.addEventListener('click',  refreshLogs);

  // Initial render — don't auto-check at mount; let the user click
  // Check. But disable the buttons when the bridge isn't available
  // (e.g. opened in a browser tab against the WebUI dev mode).
  if (!bridgeDirectHub()) {
    setBadge('error');
    detailEl.textContent = 'Direct Hub requires CAM Desktop (Electron). Open the installed app to use Direct.';
    [checkBtn, startBtn, stopBtn, restartBt].forEach(b => { b.disabled = true; });
  } else {
    setBadge('stopped');
    detailEl.textContent = 'Click Check to inspect the embedded Hub, or Start to launch it.';
  }
}

/* ───────── Relay tab controller (CAM-DESK-REMOTE-012) ───────── */

function mountRelayTab({ panel, readConfig, saveConfig, connect, showToast }) {
  const relayForm   = panel.querySelector('#settings-form-relay');
  const relayStatus = panel.querySelector('#settings-status-relay');
  if (!relayForm) return;

  const relayUrlEl   = panel.querySelector('#set-relay-url');
  const relayTokenEl = panel.querySelector('#set-relay-token');
  // CAM-DESK-REMOTE-012 (2026-06-12): the CAM API token field is
  // hidden in the Relay form. It is now profile-managed on the
  // source side and injected by the relay; the legacy input
  // (#set-relay-cam-token) stays in the DOM (display:none) so this
  // querySelector still resolves and any stale localStorage value
  // can be cleared without a follow-up cleanup pass.
  const camTokenEl   = panel.querySelector('#set-relay-cam-token');

  // Initial fill from existing localStorage. The hidden CAM token
  // input no longer gets repopulated — leave it empty so a future
  // save() doesn't carry forward a stale bearer.
  const cfg = readConfig();
  if (relayUrlEl)   relayUrlEl.value   = cfg.relayUrl || '';
  if (relayTokenEl) relayTokenEl.value = cfg.relayToken || '';
  if (camTokenEl)   camTokenEl.value   = '';

  function relaySetStatus(text, cls = '') {
    if (!relayStatus) return;
    relayStatus.textContent = text || '';
    relayStatus.classList.remove('is-error', 'is-ok');
    if (cls) relayStatus.classList.add(cls);
  }

  relayForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const ru = relayUrlEl   ? relayUrlEl.value.trim()   : '';
    const rt = relayTokenEl ? relayTokenEl.value.trim() : '';
    if (!ru || !rt) {
      relaySetStatus('Set both Relay URL and Relay token.', 'is-error');
      return;
    }
    // Tab-isolation: Relay clears the Direct serverUrl AND token so
    // CamApi does not race Direct vs Relay, and the relay path no
    // longer needs the CAM API token at the client (relay/relay.py
    // injects it). Keeping token empty also means a later Direct
    // login cannot accidentally inherit a stale relay-side bearer.
    saveConfig({
      serverUrl:  '',
      token:      '',
      relayUrl:   ru,
      relayToken: rt,
    });
    _setProfileKind('relay');
    relaySetStatus('Connecting…');
    const mode = await connect();
    if (mode !== 'disconnected') {
      relaySetStatus(`Connected (${mode}).`, 'is-ok');
      showToast(`Connected (${mode})`, 'success');
    } else {
      relaySetStatus(
        'Connection failed — check Relay URL, Relay token, and source status.',
        'is-error',
      );
    }
  });
}
