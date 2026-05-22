/**
 * Desktop Settings mode — full main-pane configuration view that
 * replaces the Agent Console when mode = 'settings'. Reuses the same
 * localStorage keys as the WebUI (cam_server_url, cam_token,
 * cam_relay_url, cam_relay_token), then re-runs `api.connect()`.
 *
 * Phase 2A additions:
 *  - Connection health summary tied to AppState.connectionMode.
 *  - Local backend readiness section (Check / Start) driven by
 *    window.CamBridge.checkBackendReadiness() and startLocalBackend().
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function bridge() {
  return typeof window !== 'undefined' ? window.CamBridge : null;
}

function bridgeSupportsReadiness() {
  const b = bridge();
  return !!(b && typeof b.checkBackendReadiness === 'function');
}

function bridgeSupportsStart() {
  const b = bridge();
  return !!(b && typeof b.startLocalBackend === 'function');
}

const DEFAULT_LOCAL_URL = 'http://127.0.0.1:8420';

export function mountSettingsMode({ state, showToast, readConfig, saveConfig, connect }) {
  const panel = document.getElementById('mode-settings');
  if (!panel) return;

  const form = panel.querySelector('#settings-form');
  const serverUrl = panel.querySelector('#set-server-url');
  const token = panel.querySelector('#set-token');
  const relayUrl = panel.querySelector('#set-relay-url');
  const relayToken = panel.querySelector('#set-relay-token');
  const statusEl = panel.querySelector('#settings-status');
  const testBtn = panel.querySelector('#settings-test');

  const healthDot = panel.querySelector('#health-dot');
  const healthLabel = panel.querySelector('#health-label');
  const healthDetail = panel.querySelector('#health-detail');

  const readinessGrid = panel.querySelector('#readiness-grid');
  const readinessHint = panel.querySelector('#readiness-hint');
  const readinessCmd = panel.querySelector('#readiness-cmd');
  const checkBtn = panel.querySelector('#readiness-check');
  const startBtn = panel.querySelector('#readiness-start');

  let lastReadiness = null;

  /* ── Form helpers ── */

  function loadIntoForm() {
    const cfg = readConfig();
    serverUrl.value = cfg.serverUrl;
    token.value = cfg.token;
    relayUrl.value = cfg.relayUrl;
    relayToken.value = cfg.relayToken;
  }

  function readForm() {
    return {
      serverUrl: serverUrl.value.trim(),
      token: token.value.trim(),
      relayUrl: relayUrl.value.trim(),
      relayToken: relayToken.value.trim(),
    };
  }

  function setStatus(text, cls = '') {
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  /* ── Connection health summary ── */

  function renderHealth() {
    const mode = state.get('connectionMode') || 'disconnected';
    healthDot.className = 'conn-dot ' + mode;
    if (mode === 'direct') {
      healthLabel.textContent = 'Connected (direct).';
      healthDetail.innerHTML =
        `Using <strong>${escapeHtml(readConfig().serverUrl || '(unset)')}</strong>. ` +
        'Switch to <strong>Agents</strong> to use the connection.';
    } else if (mode === 'relay') {
      healthLabel.textContent = 'Connected (via relay).';
      healthDetail.innerHTML =
        `Using relay <strong>${escapeHtml(readConfig().relayUrl || '(unset)')}</strong>. ` +
        'Switch to <strong>Agents</strong> to use the connection.';
    } else if (mode === 'checking') {
      healthLabel.textContent = 'Checking endpoint…';
      healthDetail.textContent = 'Waiting for direct or relay to respond.';
    } else {
      healthLabel.textContent = 'Not connected.';
      healthDetail.innerHTML =
        'Configure a direct or relay endpoint below, then click ' +
        '<strong>Save &amp; Connect</strong>. If you have CAM installed ' +
        'locally, you can use <strong>Check backend</strong> to detect or ' +
        'start a local server.';
    }
  }

  /* ── Readiness rendering ── */

  function renderReadinessRows(r) {
    if (!r) {
      readinessGrid.innerHTML =
        '<div class="readiness-empty">Click <strong>Check backend</strong> to scan the local environment.</div>';
      return;
    }
    const rows = [];
    rows.push({
      key: 'Platform',
      val: r.platform || 'unknown',
      cls: 'is-info',
      mark: '·',
    });
    if (r.platform === 'win32') {
      rows.push({
        key: 'WSL',
        val: r.hasWsl
          ? (r.wslDistros && r.wslDistros.length
              ? `available · ${r.wslDistros.length} distro${r.wslDistros.length === 1 ? '' : 's'} · selected: ${r.selectedDistro || '—'}`
              : 'available · no distro detected')
          : 'not available',
        cls: r.hasWsl ? 'is-ok' : 'is-err',
        mark: r.hasWsl ? '✓' : '✗',
      });
    }
    rows.push({
      key: 'Python',
      val: r.hasPython ? 'detected' : 'not detected',
      cls: r.hasPython ? 'is-ok' : 'is-err',
      mark: r.hasPython ? '✓' : '✗',
    });
    rows.push({
      key: 'CAM CLI',
      val: r.hasCam ? 'detected' : 'not detected',
      cls: r.hasCam ? 'is-ok' : 'is-err',
      mark: r.hasCam ? '✓' : '✗',
    });
    let serverVal;
    let serverCls;
    let serverMark;
    if (r.localServerRunning) {
      serverVal = 'CAM listening on 127.0.0.1:8420';
      serverCls = 'is-ok';
      serverMark = '✓';
    } else if (r.localPortOccupiedByOther) {
      serverVal = 'port 8420 occupied by non-CAM service';
      serverCls = 'is-err';
      serverMark = '!';
    } else {
      serverVal = 'not running';
      serverCls = 'is-err';
      serverMark = '✗';
    }
    rows.push({ key: 'Local server', val: serverVal, cls: serverCls, mark: serverMark });
    readinessGrid.innerHTML = rows
      .map((row) =>
        `<div class="readiness-row ${row.cls}">` +
          `<span class="mark">${escapeHtml(row.mark)}</span>` +
          `<span class="key">${escapeHtml(row.key)}</span>` +
          `<span class="val">${escapeHtml(row.val)}</span>` +
        '</div>',
      )
      .join('');
  }

  function setHint(text, cls = '') {
    readinessHint.textContent = text || '';
    readinessHint.classList.remove('is-error', 'is-ok');
    if (cls) readinessHint.classList.add(cls);
  }

  function setCmd(text) {
    if (text) {
      readinessCmd.textContent = text;
      readinessCmd.hidden = false;
    } else {
      readinessCmd.textContent = '';
      readinessCmd.hidden = true;
    }
  }

  function updateStartEnabled() {
    const r = lastReadiness;
    const canStart =
      bridgeSupportsStart() &&
      !!r &&
      r.hasCam &&
      !r.localServerRunning &&
      !r.localPortOccupiedByOther;
    startBtn.disabled = !canStart;
    let title;
    if (canStart) {
      title = 'Launch cam serve and reconnect.';
    } else if (!bridgeSupportsStart()) {
      title = 'Start requires the Electron desktop app.';
    } else if (r && r.localPortOccupiedByOther) {
      title = 'Port 8420 is occupied by a non-CAM service. Free the port and try again.';
    } else if (r && r.localServerRunning) {
      title = 'A CAM server is already running. Enter its token below or stop it first.';
    } else if (r && !r.hasCam) {
      title = 'Backend cannot be started until CAM is installed locally.';
    } else {
      title = 'Run Check backend first to detect the local environment.';
    }
    startBtn.title = title;
  }

  /* ── Actions ── */

  async function runCheck() {
    if (!bridgeSupportsReadiness()) {
      renderReadinessRows(null);
      setHint('Backend readiness checks require the Electron desktop app.', 'is-error');
      setCmd('');
      lastReadiness = null;
      updateStartEnabled();
      return;
    }
    checkBtn.disabled = true;
    const orig = checkBtn.textContent;
    checkBtn.textContent = 'Checking…';
    setHint('Scanning local environment…');
    setCmd('');
    try {
      const r = await bridge().checkBackendReadiness();
      lastReadiness = r || null;
      renderReadinessRows(lastReadiness);
      if (lastReadiness?.message) {
        const cls = lastReadiness.localServerRunning
          ? 'is-ok'
          : (lastReadiness.hasCam ? '' : 'is-error');
        setHint(lastReadiness.message, cls);
      } else {
        setHint('', '');
      }
      setCmd(lastReadiness?.suggestedCommand || '');
    } catch (e) {
      lastReadiness = null;
      renderReadinessRows(null);
      setHint(`Check failed: ${e?.message || e}`, 'is-error');
      setCmd('');
    } finally {
      checkBtn.disabled = false;
      checkBtn.textContent = orig;
      updateStartEnabled();
    }
  }

  async function runStart() {
    if (!bridgeSupportsStart()) {
      setHint('Start requires the Electron desktop app.', 'is-error');
      return;
    }
    startBtn.disabled = true;
    const orig = startBtn.textContent;
    startBtn.textContent = 'Starting…';
    setHint('Starting local backend…');
    try {
      const res = await bridge().startLocalBackend();
      if (res?.ok) {
        // Phase 2A one-click path: main process generated a fresh token
        // and started cam serve with it. Fill direct-connection fields and
        // reconnect automatically.
        serverUrl.value = res.url || DEFAULT_LOCAL_URL;
        if (res.token) token.value = res.token;
        saveConfig(readForm());
        setHint(res.message || 'Backend started.', 'is-ok');
        const mode = await connect();
        if (mode !== 'disconnected') {
          showToast(`Connected (${mode})`, 'success');
        } else {
          showToast('Backend started but client could not connect.', 'warning', 6000);
        }
        // Refresh readiness view to reflect the now-running server.
        await runCheck();
      } else {
        setHint(res?.message || 'Start failed.', 'is-error');
        // If main reported a known URL (e.g. an existing CAM server) but
        // refused to start because we don't know its token, fill the URL
        // so the user only needs to paste a token to connect.
        if (res?.url && !readConfig().serverUrl) {
          serverUrl.value = res.url;
        }
      }
    } catch (e) {
      setHint(`Start failed: ${e?.message || e}`, 'is-error');
    } finally {
      startBtn.textContent = orig;
      updateStartEnabled();
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const cfg = readForm();
    if (!cfg.serverUrl && !cfg.relayUrl) {
      setStatus('Set a server URL or relay URL.', 'is-error');
      return;
    }
    saveConfig(cfg);
    setStatus('Connecting…');
    const mode = await connect();
    if (mode !== 'disconnected') {
      setStatus(`Connected (${mode}).`, 'is-ok');
      showToast(`Connected (${mode})`, 'success');
    } else {
      setStatus('Connection failed — check URL and token.', 'is-error');
      showToast('Connection failed', 'error', 5000);
    }
  });

  testBtn.addEventListener('click', async () => {
    const cfg = readForm();
    if (!cfg.serverUrl) {
      setStatus('Set a server URL to test direct.', 'is-error');
      return;
    }
    testBtn.disabled = true;
    const originalText = testBtn.textContent;
    testBtn.textContent = 'Testing…';
    setStatus('Testing direct connection…');
    try {
      const headers = {};
      if (cfg.token) headers['Authorization'] = `Bearer ${cfg.token}`;
      const resp = await fetch(`${cfg.serverUrl.replace(/\/$/, '')}/api/contexts`, {
        headers,
        signal: AbortSignal.timeout(10000),
      });
      if (resp.ok) {
        setStatus('Direct OK — server reachable and token accepted.', 'is-ok');
      } else if (resp.status === 401) {
        setStatus('Server reachable, but the token was rejected.', 'is-error');
      } else {
        setStatus(`Server responded with HTTP ${resp.status}.`, 'is-error');
      }
    } catch (e) {
      setStatus(`Direct test failed: ${e.message}`, 'is-error');
    } finally {
      testBtn.disabled = false;
      testBtn.textContent = originalText;
    }
  });

  checkBtn.addEventListener('click', runCheck);
  startBtn.addEventListener('click', runStart);

  if (!bridgeSupportsReadiness()) {
    checkBtn.disabled = true;
    checkBtn.title = 'Backend readiness checks require the Electron desktop app.';
  }
  updateStartEnabled();

  // Re-render form + health whenever Settings becomes the active mode.
  let prevMode = state.get('mode');
  let prevConn = state.get('connectionMode');
  loadIntoForm();
  renderHealth();
  renderReadinessRows(null);

  state.subscribe(() => {
    const m = state.get('mode');
    const c = state.get('connectionMode');
    if (m !== prevMode) {
      prevMode = m;
      if (m === 'settings') loadIntoForm();
    }
    if (c !== prevConn) {
      prevConn = c;
      renderHealth();
    }
  });
}
