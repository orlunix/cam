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

export function mountSettingsMode({ api, state, showToast, readConfig, saveConfig, connect }) {
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

  /* ────────── Settings tabs (Direct / Relay / Appearance) ──────────
   * Active Settings has three tabs. Unknown saved tab values (e.g.
   * legacy 'local') are clamped to 'direct' so existing users do not
   * crash on an unrendered tab. */
  const tabButtons = panel.querySelectorAll('.settings-tab[data-tab]');
  const tabPanels  = panel.querySelectorAll('.settings-tab-panel[data-tab]');
  const TAB_KEY    = 'cam_desktop_settings_tab';
  const VALID_TABS = ['direct', 'relay', 'appearance'];

  function applyTab(name) {
    if (!VALID_TABS.includes(name)) name = 'direct';
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
      if (VALID_TABS.includes(saved)) return saved;
    } catch {}
    if (_profileKind() === 'relay') return 'relay';
    return 'direct';
  }

  tabButtons.forEach((b) => {
    b.addEventListener('click', () => applyTab(b.dataset.tab));
  });
  applyTab(pickInitialTab());

  /* ────────── Direct tab — app-managed Hub lifecycle ────────── */
  mountDirectTab({ panel, api, state, showToast, readConfig, saveConfig, connect });

  /* ────────── Relay tab ──────────
   * The Relay user-mode points at an external relay endpoint that
   * proxies REST traffic to an existing CAM Hub. Because the proxied
   * requests still hit a token-protected /api/* surface, we need
   * THREE inputs: relay URL + relay shared secret + the CAM Hub's API
   * token. Without the third, the websocket connects but every proxied
   * REST call fails 401. */
  mountRelayTab({ panel, readConfig, saveConfig, connect, showToast });

  /* ────────── Appearance tab (CAM-DESK-SET-002) ──────────
   * Theme (dark/light/system) + UI font size + agent-output font
   * size. Stored in localStorage; applied immediately via body
   * `data-theme` + CSS custom properties on `body.desktop`.
   * `applyAppearance()` was already called at module-load time so
   * the initial paint is correct before this mount runs. */
  mountAppearanceTab({ panel, showToast });
}

/* ───────── Direct tab controller (CAM-DESK-DIRECT-010..019) ───────── */

function mountDirectTab({ panel, api, state, showToast, readConfig, saveConfig, connect }) {
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
    const range = r.apiPortRange && r.apiPortRange.start != null && r.apiPortRange.end != null
      ? `${r.apiPortRange.start}..${r.apiPortRange.end}`
      : `${r.apiPort}+`;
    switch (r.summary) {
      case 'running':       return 'Embedded CAM Hub is running on this machine.';
      case 'starting':      return 'Embedded CAM Hub is starting…';
      case 'port-conflict': return `No fixed Direct port in ${range} could be bound, and the OS-assigned fallback also failed. Open Diagnostics and check Port candidates for the real bind error.`;
      case 'stopped':
        if (r.apiPortStatus && r.apiPortStatus.state === 'fallback-free') {
          return `Fixed Direct ports (${range}) are unavailable, but an OS-assigned loopback port is available. Click Restart to recover.`;
        }
        return 'Embedded CAM Hub is stopped. Click Start to launch it.';
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

  function renderDiagnostics(r) {
    if (!r) { diagGrid.textContent = '(no data)'; return; }
    const portState = r.apiPortStatus && r.apiPortStatus.state;
    const cfg = typeof readConfig === 'function' ? readConfig() : {};
    const rows = [
      ['Runtime',        'embedded (Electron Node)'],
      ['Platform',       r.platform || '—'],
      ['API port',       `${r.apiPort}${portState ? ' — ' + portState : ''}`],
      ['Port scan range', r.apiPortRange ? `${r.apiPortRange.start}..${r.apiPortRange.end}` : '—'],
      ['Port candidates', portCandidateSummary(r)],
      ['Profile kind',   _profileKind()],
      ['Saved Direct URL', cfg.serverUrl || '—'],
      ['Renderer API mode', api && api.mode ? api.mode : (state && state.get ? state.get('connectionMode') : '—')],
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
    // start() does its own scan and will pick an alternate candidate if
    // 8420 is busy (within the embedded Hub's configured scan range), so
    // a single foreign listener on 8420 must not block UI.
    startBtn.disabled = !supported || owned || summary === 'port-conflict';
    stopBtn.disabled    = !supported || !owned;
    // Restart doubles as Direct recovery: when disconnected or when the
    // previous loopback URL/token is stale, restart() is stop(no-op)+start
    // and then we persist the fresh URL/token before reconnecting.
    restartBt.disabled  = !supported;
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
      const msg = res && res.error ? res.error : 'Start failed.';
      setStatus(msg, 'is-error');
      setHint(msg, 'is-error');
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
    setStatus('Restarting Direct connection…');
    setHint('Starting a fresh embedded Hub and refreshing the Direct URL/token…');
    setBadge('starting');
    [startBtn, stopBtn, restartBt].forEach((b) => { if (b) b.disabled = true; });
    try {
      const res = await bridgeDirectHub().restart();
      if (!res || res.ok !== true || !res.apiUrl || !res.apiToken) {
        const msg = (res && res.error) || 'Restart failed.';
        setStatus(msg, 'is-error');
        setHint(msg, 'is-error');
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
          setHint('Direct profile was refreshed with a new embedded Hub URL/token.', 'is-ok');
          showToast('Direct connection restarted', 'success');
        } else {
          setStatus('Restarted Hub but reconnect failed.', 'is-error');
          setHint('Open Diagnostics and check Saved Direct URL, Renderer API mode, and Port candidates.', 'is-error');
        }
      }
    } catch (e) {
      setStatus(`Restart failed: ${e?.message || e}`, 'is-error');
      setHint(`Restart failed: ${e?.message || e}`, 'is-error');
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
  const camTokenEl   = panel.querySelector('#set-relay-cam-token');

  // Initial fill from existing localStorage.
  const cfg = readConfig();
  if (relayUrlEl)   relayUrlEl.value   = cfg.relayUrl || '';
  if (relayTokenEl) relayTokenEl.value = cfg.relayToken || '';
  if (camTokenEl)   camTokenEl.value   = cfg.token || '';

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
    const ct = camTokenEl   ? camTokenEl.value.trim()   : '';
    if (!ru || !rt) {
      relaySetStatus('Set both relay URL and relay token.', 'is-error');
      return;
    }
    if (!ct) {
      relaySetStatus(
        'Set the CAM API token of the CAM Hub behind the relay. ' +
        'Without it, proxied /api requests will be unauthenticated.',
        'is-error',
      );
      return;
    }
    // Tab-isolation: Relay clears the Direct serverUrl so CamApi does
    // not race Direct vs Relay, but keeps the CAM token under `token`
    // because CamApi.request() needs it for Authorization on proxied
    // REST calls (web/js/api.js).
    saveConfig({
      serverUrl:  '',
      token:      ct,
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
      relaySetStatus('Connection failed — check relay URL, relay token, and CAM token.', 'is-error');
    }
  });
}

/* ───────── Appearance tab (CAM-DESK-SET-002) ─────────
 *
 * Theme + font-size are renderer-only preferences. Persisted via
 * localStorage; applied immediately by setting `data-theme` on
 * `body` and CSS custom properties (`--ui-font-size`,
 * `--output-font-size`) on `body.desktop`. The light theme is
 * implemented as `body.desktop[data-theme="light"]` overrides in
 * `web/css/desktop.css`; the dark theme is the default (no
 * attribute set).
 *
 * `applyAppearance()` is called once at module-load time so the
 * initial paint is correct before any view mounts. It is also
 * called again from `mountAppearanceTab` whenever a control
 * changes. A `prefers-color-scheme` MediaQueryList listener keeps
 * the "Match system" mode in sync if the OS theme switches.
 */

const APPEARANCE_THEME_KEY     = 'cam_desktop_theme';
const APPEARANCE_UI_FONT_KEY   = 'cam_desktop_font_ui';
const APPEARANCE_OUTPUT_KEY    = 'cam_desktop_font_output';
const APPEARANCE_VALID_THEMES  = ['dark', 'light', 'system'];
const APPEARANCE_UI_MIN        = 11;
const APPEARANCE_UI_MAX        = 17;
const APPEARANCE_UI_DEFAULT    = 13;
const APPEARANCE_OUTPUT_MIN    = 10;
const APPEARANCE_OUTPUT_MAX    = 20;
const APPEARANCE_OUTPUT_DEFAULT = 13;

function _readAppearanceTheme() {
  try {
    const v = localStorage.getItem(APPEARANCE_THEME_KEY);
    if (APPEARANCE_VALID_THEMES.includes(v)) return v;
  } catch {}
  return 'dark';
}
function _readAppearanceFont(key, def, min, max) {
  try {
    const raw = localStorage.getItem(key);
    const n = Number.parseInt(raw, 10);
    if (Number.isFinite(n) && n >= min && n <= max) return n;
  } catch {}
  return def;
}
function _systemPrefersLight() {
  try {
    return !!(typeof window !== 'undefined' &&
              window.matchMedia &&
              window.matchMedia('(prefers-color-scheme: light)').matches);
  } catch { return false; }
}
function _resolveEffectiveTheme(setting) {
  if (setting === 'light') return 'light';
  if (setting === 'dark')  return 'dark';
  return _systemPrefersLight() ? 'light' : 'dark';
}

/** Apply the current saved appearance to the DOM. Idempotent.
 *  Safe to call multiple times; safe to call before any view is
 *  mounted (it only touches `<body>`). */
export function applyAppearance() {
  if (typeof document === 'undefined' || !document.body) return;
  const body = document.body;
  const themeSetting   = _readAppearanceTheme();
  const effectiveTheme = _resolveEffectiveTheme(themeSetting);

  // `data-theme` is only set when light; dark is the unattributed
  // default so existing CSS continues to render as before.
  if (effectiveTheme === 'light') {
    body.setAttribute('data-theme', 'light');
  } else {
    body.removeAttribute('data-theme');
  }
  // Record the explicit user choice too, so the Appearance form can
  // distinguish "system → currently light" from "explicit light".
  body.setAttribute('data-theme-setting', themeSetting);

  const ui = _readAppearanceFont(
    APPEARANCE_UI_FONT_KEY, APPEARANCE_UI_DEFAULT,
    APPEARANCE_UI_MIN, APPEARANCE_UI_MAX,
  );
  const out = _readAppearanceFont(
    APPEARANCE_OUTPUT_KEY, APPEARANCE_OUTPUT_DEFAULT,
    APPEARANCE_OUTPUT_MIN, APPEARANCE_OUTPUT_MAX,
  );
  body.style.setProperty('--ui-font-size', `${ui}px`);
  body.style.setProperty('--output-font-size', `${out}px`);
}

// Side-effect at module load: apply persisted appearance before any
// panel mounts, so the very first paint matches the saved choice.
applyAppearance();

// Keep "Match system" in sync if the OS theme flips while the app
// is running. Best-effort: older browsers without addEventListener
// on MQL fall through gracefully.
try {
  if (typeof window !== 'undefined' && window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => {
      if (_readAppearanceTheme() === 'system') applyAppearance();
    };
    if (typeof mq.addEventListener === 'function') mq.addEventListener('change', onChange);
    else if (typeof mq.addListener === 'function')  mq.addListener(onChange);
  }
} catch {}

function mountAppearanceTab({ panel, showToast }) {
  const tabPanel = panel.querySelector('#settings-tab-appearance');
  if (!tabPanel) return;

  const themeSel       = tabPanel.querySelector('#appearance-theme');
  const uiFontInput    = tabPanel.querySelector('#appearance-ui-font');
  const uiFontValEl    = tabPanel.querySelector('#appearance-ui-font-val');
  const outFontInput   = tabPanel.querySelector('#appearance-output-font');
  const outFontValEl   = tabPanel.querySelector('#appearance-output-font-val');
  const resetBtn       = tabPanel.querySelector('#appearance-reset');
  const statusEl       = tabPanel.querySelector('#settings-status-appearance');

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  // Initialize controls from saved values.
  function syncFromStorage() {
    const theme = _readAppearanceTheme();
    const ui    = _readAppearanceFont(
      APPEARANCE_UI_FONT_KEY, APPEARANCE_UI_DEFAULT,
      APPEARANCE_UI_MIN, APPEARANCE_UI_MAX,
    );
    const out   = _readAppearanceFont(
      APPEARANCE_OUTPUT_KEY, APPEARANCE_OUTPUT_DEFAULT,
      APPEARANCE_OUTPUT_MIN, APPEARANCE_OUTPUT_MAX,
    );
    if (themeSel)     themeSel.value         = theme;
    if (uiFontInput)  uiFontInput.value      = String(ui);
    if (uiFontValEl)  uiFontValEl.textContent = `${ui} px`;
    if (outFontInput) outFontInput.value     = String(out);
    if (outFontValEl) outFontValEl.textContent = `${out} px`;
  }
  syncFromStorage();

  if (themeSel) {
    themeSel.addEventListener('change', () => {
      const v = themeSel.value;
      if (!APPEARANCE_VALID_THEMES.includes(v)) return;
      try { localStorage.setItem(APPEARANCE_THEME_KEY, v); } catch {}
      applyAppearance();
      setStatus(`Theme set to ${v}.`, 'is-ok');
    });
  }

  function commitFont(input, valEl, key, min, max) {
    if (!input) return;
    let n = Number.parseInt(input.value, 10);
    if (!Number.isFinite(n)) n = min;
    if (n < min) n = min;
    if (n > max) n = max;
    input.value = String(n);
    if (valEl) valEl.textContent = `${n} px`;
    try { localStorage.setItem(key, String(n)); } catch {}
    applyAppearance();
  }
  if (uiFontInput) {
    uiFontInput.addEventListener('input', () => {
      commitFont(uiFontInput, uiFontValEl, APPEARANCE_UI_FONT_KEY,
                 APPEARANCE_UI_MIN, APPEARANCE_UI_MAX);
    });
  }
  if (outFontInput) {
    outFontInput.addEventListener('input', () => {
      commitFont(outFontInput, outFontValEl, APPEARANCE_OUTPUT_KEY,
                 APPEARANCE_OUTPUT_MIN, APPEARANCE_OUTPUT_MAX);
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      try {
        localStorage.removeItem(APPEARANCE_THEME_KEY);
        localStorage.removeItem(APPEARANCE_UI_FONT_KEY);
        localStorage.removeItem(APPEARANCE_OUTPUT_KEY);
      } catch {}
      applyAppearance();
      syncFromStorage();
      setStatus('Reset to defaults.', 'is-ok');
      if (typeof showToast === 'function') {
        showToast('Appearance reset to defaults.', 'success');
      }
    });
  }
}
