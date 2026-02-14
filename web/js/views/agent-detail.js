import { api, state, navigate } from '../app.js';

export function renderAgentDetail(container, agentId) {
  let outputTimer = null;
  let outputOffset = 0;
  let useFullOutput = true; // default to full output mode
  let agent = (state.get('agents') || []).find(a => a.id === agentId);

  const render = async () => {
    // Refresh agent data
    try {
      agent = await api.getAgent(agentId);
    } catch (e) {
      container.innerHTML = `<div class="error-state">Agent not found</div>`;
      return;
    }

    const isActive = ['running', 'starting', 'pending'].includes(agent.status);

    container.innerHTML = `
      <div class="detail-header">
        <button class="back-btn" id="back-btn">&larr;</button>
        <div class="detail-title">
          <h2>${agent.task_name || agent.id.slice(0, 8)}</h2>
          <span class="badge badge-${agent.status}">${agent.status}</span>
        </div>
      </div>

      <div class="detail-meta">
        <div class="meta-row">
          <span class="meta-label">Tool</span>
          <span class="meta-value">${agent.tool}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Context</span>
          <span class="meta-value">${agent.context_name || 'none'}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">State</span>
          <span class="meta-value">${agent.state || '-'}</span>
        </div>
        ${agent.exit_reason ? `
        <div class="meta-row">
          <span class="meta-label">Exit</span>
          <span class="meta-value">${agent.exit_reason}</span>
        </div>` : ''}
      </div>

      ${agent.prompt ? `
      <div class="detail-section">
        <div class="section-label">Prompt</div>
        <div class="prompt-text">${escapeHtml(agent.prompt)}</div>
      </div>` : ''}

      <div class="detail-section">
        <div class="section-header">
          <span class="section-label">Output</span>
          <div style="display:flex;gap:6px;align-items:center">
            <button class="btn-sm ${useFullOutput ? 'btn-primary' : ''}" id="toggle-full">Full</button>
            <button class="btn-sm ${!useFullOutput ? 'btn-primary' : ''}" id="toggle-live">Live</button>
            <button class="btn-sm" id="refresh-output">↻</button>
          </div>
        </div>
        <pre class="output-pane" id="output-pane">Loading...</pre>
      </div>

      ${isActive ? `
      <div class="input-bar">
        <input type="text" id="input-text" class="input-field" placeholder="Send input to agent...">
        <button class="btn-primary btn-sm" id="send-btn">Send</button>
      </div>` : ''}

      <div class="detail-actions">
        ${isActive ? `
          <button class="btn-danger" id="stop-btn">Stop</button>
          <button class="btn-danger" id="kill-btn">Kill</button>
        ` : `
          <button class="btn-danger" id="delete-btn">Delete</button>
        `}
      </div>

      <div class="detail-section">
        <div class="section-label">Logs</div>
        <div class="log-entries" id="log-entries">Loading...</div>
      </div>
    `;

    // Wire events
    container.querySelector('#back-btn').addEventListener('click', () => navigate('/'));

    const refreshBtn = container.querySelector('#refresh-output');
    if (refreshBtn) refreshBtn.addEventListener('click', () => {
      outputOffset = 0;
      const pane = container.querySelector('#output-pane');
      if (pane) pane.textContent = '';
      loadOutput();
    });

    container.querySelector('#toggle-full').addEventListener('click', () => {
      if (useFullOutput) return;
      useFullOutput = true;
      outputOffset = 0;
      const pane = container.querySelector('#output-pane');
      if (pane) pane.textContent = '';
      render();
    });

    container.querySelector('#toggle-live').addEventListener('click', () => {
      if (!useFullOutput) return;
      useFullOutput = false;
      render();
    });

    const stopBtn = container.querySelector('#stop-btn');
    if (stopBtn) stopBtn.addEventListener('click', async () => {
      try { await api.stopAgent(agentId); state.toast('Agent stopped', 'success'); }
      catch (e) { state.toast(e.message, 'error'); }
    });

    const killBtn = container.querySelector('#kill-btn');
    if (killBtn) killBtn.addEventListener('click', async () => {
      try { await api.stopAgent(agentId, true); state.toast('Agent killed', 'success'); }
      catch (e) { state.toast(e.message, 'error'); }
    });

    const deleteBtn = container.querySelector('#delete-btn');
    if (deleteBtn) deleteBtn.addEventListener('click', async () => {
      if (!confirm('Delete this agent from history?')) return;
      try {
        await api.deleteAgentHistory(agentId);
        state.toast('Agent deleted', 'success');
        const resp = await api.listAgents({ limit: 50 });
        state.set('agents', resp.agents || []);
        navigate('/');
      } catch (e) { state.toast(e.message, 'error'); }
    });

    const inputText = container.querySelector('#input-text');
    const sendBtn = container.querySelector('#send-btn');
    if (sendBtn && inputText) {
      const doSend = async () => {
        const text = inputText.value.trim();
        if (!text) return;
        try {
          await api.sendInput(agentId, text);
          inputText.value = '';
        } catch (e) { state.toast(e.message, 'error'); }
      };
      sendBtn.addEventListener('click', doSend);
      inputText.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSend(); });
    }

    // Load output and logs
    loadOutput();
    loadLogs();
  };

  async function loadOutput() {
    const pane = container.querySelector('#output-pane');
    if (!pane) return;

    if (useFullOutput) {
      // Incremental full output — append new data
      try {
        const data = await api.agentFullOutput(agentId, outputOffset);
        if (data.output) {
          if (outputOffset === 0) {
            pane.textContent = data.output;
          } else {
            pane.textContent += data.output;
          }
          pane.scrollTop = pane.scrollHeight;
        } else if (outputOffset === 0) {
          pane.textContent = '(no output yet)';
        }
        outputOffset = data.next_offset || outputOffset;
      } catch {
        if (outputOffset === 0) pane.textContent = '(output unavailable)';
      }
    } else {
      // Live mode — last N lines from capture-pane
      try {
        const data = await api.agentOutput(agentId, 80);
        pane.textContent = data.output || '(no output)';
        pane.scrollTop = pane.scrollHeight;
      } catch {
        pane.textContent = '(output unavailable)';
      }
    }
  }

  async function loadLogs() {
    const el = container.querySelector('#log-entries');
    if (!el) return;
    try {
      const data = await api.agentLogs(agentId, 50);
      const entries = data.entries || [];
      if (entries.length === 0) {
        el.innerHTML = '<div class="empty-state">No logs yet</div>';
        return;
      }
      el.innerHTML = entries.map(e => {
        const ts = e.ts ? new Date(e.ts).toLocaleTimeString() : '';
        const type = e.type || '';
        return `<div class="log-entry log-${type}"><span class="log-ts">${ts}</span> <span class="log-type">[${type}]</span> ${escapeHtml(e.output || e.state || JSON.stringify(e.data || ''))}</div>`;
      }).join('');
    } catch {
      el.innerHTML = '<div class="empty-state">Logs unavailable</div>';
    }
  }

  // Auto-refresh output for active agents
  render().then(() => {
    if (['running', 'starting', 'pending'].includes(agent?.status)) {
      outputTimer = setInterval(loadOutput, 3000);
    }
  });

  // Re-render on status changes
  const unsub = state.subscribe((data) => {
    const updated = (data.agents || []).find(a => a.id === agentId);
    if (updated && updated.status !== agent?.status) {
      agent = updated;
      render();
    }
  });

  // Return cleanup function for router
  return () => { clearInterval(outputTimer); unsub(); };
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
