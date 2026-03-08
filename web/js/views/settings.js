import { api, state } from '../app.js';

export function renderSettings(container) {
  const mode = state.get('connectionMode');

  container.innerHTML = `
    <div class="page-header">
      <h2>Settings</h2>
    </div>

    <div class="connection-status">
      <span class="conn-dot ${mode}"></span>
      <span class="conn-text">
        ${mode === 'direct' ? 'Connected directly' :
          mode === 'relay' ? 'Connected via relay' :
          'Disconnected'}
      </span>
    </div>

    <form id="settings-form" class="form">
      <div class="form-section-label">Direct Connection</div>
      <div class="form-group">
        <label for="server-url">Server URL</label>
        <input type="url" id="server-url" class="form-input"
          value="${localStorage.getItem('cam_server_url') || ''}"
          placeholder="http://localhost:8420">
      </div>
      <div class="form-group">
        <label for="api-token">API Token</label>
        <input type="password" id="api-token" class="form-input"
          value="${localStorage.getItem('cam_token') || ''}"
          placeholder="Bearer token">
      </div>

      <div class="section-divider"></div>

      <div class="form-section-label">Relay Connection</div>
      <div class="form-group">
        <label for="relay-url">Relay URL</label>
        <input type="url" id="relay-url" class="form-input"
          value="${localStorage.getItem('cam_relay_url') || ''}"
          placeholder="ws://relay:8443">
      </div>
      <div class="form-group">
        <label for="relay-token">Relay Token</label>
        <input type="password" id="relay-token" class="form-input"
          value="${localStorage.getItem('cam_relay_token') || ''}"
          placeholder="Shared secret">
      </div>

      <div class="form-actions">
        <button type="submit" class="btn-primary btn-full">Save & Connect</button>
        <button type="button" class="btn-secondary btn-full" id="test-btn">Test Connection</button>
      </div>
    </form>

    <div class="section-divider"></div>
    <div class="form-section-label">App</div>
    <div class="form-group">
      <div class="about-text" style="margin-bottom:8px;">CAM v0.1.0 &mdash; Cache: ${document.querySelector('meta[name="cam-version"]')?.content || 'unknown'}</div>
      <button type="button" class="btn-secondary btn-full" id="update-btn">Update app</button>
    </div>
  `;

  container.querySelector('#settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const serverUrl = container.querySelector('#server-url').value.trim();
    const token = container.querySelector('#api-token').value.trim();
    const relayUrl = container.querySelector('#relay-url').value.trim();
    const relayToken = container.querySelector('#relay-token').value.trim();

    localStorage.setItem('cam_server_url', serverUrl);
    localStorage.setItem('cam_token', token);
    localStorage.setItem('cam_relay_url', relayUrl);
    localStorage.setItem('cam_relay_token', relayToken);

    api.configure({ serverUrl, token, relayUrl, relayToken });
    let mode;
    try {
      mode = await api.connect();
    } catch (err) {
      state.toast(`Connect error: ${err.message}`, 'error');
      mode = 'disconnected';
    }
    state.set('connectionMode', mode);

    if (mode !== 'disconnected') {
      state.toast(`Connected (${mode})`, 'success');
      // Load data
      try {
        const [agentsResp, ctxResp] = await Promise.all([
          api.listAgents({ limit: 50 }),
          api.listContexts(),
        ]);
        state.set('agents', agentsResp.agents || []);
        state.set('contexts', ctxResp.contexts || []);
      } catch (err) {
        state.toast(`Data load error: ${err.message}`, 'error');
      }
      if (mode === 'relay') api._requestRelayEventStream();
    } else {
      state.toast('Connection failed — check URL and token', 'error');
    }

    renderSettings(container); // Re-render with new status
  });

  container.querySelector('#update-btn').addEventListener('click', async () => {
    const btn = container.querySelector('#update-btn');
    btn.disabled = true;
    btn.textContent = 'Updating...';

    // Determine the base HTTP URL for the server
    const relayUrl = localStorage.getItem('cam_relay_url') || '';
    const serverUrl = localStorage.getItem('cam_server_url') || '';
    let baseUrl = '';
    if (relayUrl) baseUrl = relayUrl.replace(/^ws:\/\//, 'http://').replace(/^wss:\/\//, 'https://');
    else if (serverUrl) baseUrl = serverUrl;
    else if (location.protocol !== 'file:') baseUrl = location.origin;

    if (location.protocol === 'file:' && baseUrl) {
      // Running from APK — open APK download in external browser
      const apkUrl = baseUrl + '/assets/cam.apk';
      const a = document.createElement('a');
      a.href = apkUrl;
      a.target = '_blank';
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      state.toast('Opening download in browser...', 'success');
      btn.disabled = false;
      btn.textContent = 'Update app';
    } else if (baseUrl) {
      // Running from HTTP — clear caches and hard reload
      try { const r = await navigator.serviceWorker.getRegistrations(); await Promise.all(r.map(x => x.unregister())); } catch {}
      try { const k = await caches.keys(); await Promise.all(k.map(x => caches.delete(x))); } catch {}
      state.toast('Reloading...', 'success');
      setTimeout(() => { location.href = baseUrl + '/?_=' + Date.now(); }, 500);
    } else {
      btn.disabled = false;
      btn.textContent = 'Update app';
      state.toast('No server URL configured', 'error');
    }
  });

  container.querySelector('#test-btn').addEventListener('click', async () => {
    const serverUrl = container.querySelector('#server-url').value.trim();
    const token = container.querySelector('#api-token').value.trim();

    if (!serverUrl) {
      state.toast('Enter a server URL first', 'error');
      return;
    }

    try {
      // Test with auth to verify token works
      const headers = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;
      const r = await fetch(`${serverUrl}/api/contexts`, {
        headers,
        signal: AbortSignal.timeout(10000),
      });
      if (r.ok) {
        state.toast('Server OK — token valid', 'success');
      } else if (r.status === 401) {
        state.toast('Server reachable but token rejected', 'error');
      } else {
        state.toast(`Server returned ${r.status}`, 'error');
      }
    } catch (e) {
      state.toast(`Connection failed: ${e.message}`, 'error');
    }
  });
}
