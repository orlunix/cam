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
    <div class="form-section-label">About</div>
    <div class="about-text">CAM v0.1.0 &mdash; Coding Agent Manager</div>
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
    const mode = await api.connect();
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
      } catch {}
      if (mode === 'relay') api._requestRelayEventStream();
    } else {
      state.toast('Connection failed', 'error');
    }

    renderSettings(container); // Re-render with new status
  });

  container.querySelector('#test-btn').addEventListener('click', async () => {
    const serverUrl = container.querySelector('#server-url').value.trim();
    const token = container.querySelector('#api-token').value.trim();

    if (!serverUrl) {
      state.toast('Enter a server URL first', 'error');
      return;
    }

    try {
      const r = await fetch(`${serverUrl}/api/system/health`, {
        signal: AbortSignal.timeout(5000),
      });
      if (r.ok) {
        const data = await r.json();
        state.toast(`Server OK: ${data.agents_running} agents running`, 'success');
      } else {
        state.toast(`Server returned ${r.status}`, 'error');
      }
    } catch (e) {
      state.toast(`Connection failed: ${e.message}`, 'error');
    }
  });
}
