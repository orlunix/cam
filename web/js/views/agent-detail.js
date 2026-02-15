import { api, state, navigate } from '../app.js';

export function renderAgentDetail(container, agentId) {
  let outputTimer = null;
  let elapsedTimer = null;
  let outputOffset = 0;
  let useFullOutput = true;
  let isFullscreen = false;
  let autoScroll = true;
  let cachedOutput = '';  // preserve output across re-renders
  let agent = (state.get('agents') || []).find(a => a.id === agentId);

  function timeSince(dateStr) {
    if (!dateStr) return '';
    const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d`;
  }

  function renderMeta() {
    const parts = [agent.tool];
    if (agent.context_name) parts.push(agent.context_name);
    parts.push(agent.status);
    if (agent.started_at) parts.push(timeSince(agent.started_at));
    if (agent.exit_reason) parts.push(agent.exit_reason);
    return parts.join(' · ');
  }

  const render = async () => {
    try {
      agent = await api.getAgent(agentId);
    } catch (e) {
      container.innerHTML = `<div class="error-state">Agent not found</div>`;
      return;
    }

    // Save current output before DOM rebuild
    const existingPane = container.querySelector('#output-pane');
    if (existingPane && existingPane.textContent !== 'Loading...') {
      cachedOutput = existingPane.textContent;
    }

    const isActive = ['running', 'starting', 'pending'].includes(agent.status);
    const prompt = agent.prompt || '';

    container.innerHTML = `
      <div class="detail-header">
        <button class="back-btn" id="back-btn">&larr;</button>
        <div class="detail-title">
          <h2>${escapeHtml(agent.task_name || agent.id.slice(0, 8))}</h2>
          <span class="badge badge-${agent.status}">${agent.status}</span>
        </div>
        <div class="detail-actions-inline">
          ${isActive ? `
            <button class="btn-sm btn-danger" id="stop-btn">Stop</button>
          ` : `
            <button class="btn-sm btn-danger" id="delete-btn">&times;</button>
          `}
        </div>
      </div>

      <div class="detail-meta-compact" id="meta-line">${renderMeta()}</div>

      <div class="output-section ${isFullscreen ? 'output-fullscreen' : ''}" id="output-section">
        <div class="output-toolbar">
          <div class="output-mode-btns">
            <button class="btn-sm ${useFullOutput ? 'btn-primary' : ''}" id="toggle-full">Full</button>
            <button class="btn-sm ${!useFullOutput ? 'btn-primary' : ''}" id="toggle-live">Live</button>
          </div>
          <div class="output-mode-btns">
            <button class="btn-sm" id="refresh-output">↻</button>
            <button class="btn-sm" id="toggle-fullscreen">${isFullscreen ? '✕' : '⛶'}</button>
          </div>
        </div>
        <div class="output-wrap">
          <pre class="output-pane ${isFullscreen ? 'output-pane-fullscreen' : ''}" id="output-pane"></pre>
          <button class="jump-bottom-btn hidden" id="jump-bottom">↓ Bottom</button>
        </div>
      </div>

      ${isActive ? `
      <div class="input-section-sticky" id="input-section">
        <div class="quick-actions">
          <button class="btn-quick" data-input="y">y</button>
          <button class="btn-quick" data-input="n">n</button>
          <button class="btn-quick" data-input="1">1</button>
          <button class="btn-quick" data-input="2">2</button>
          <button class="btn-quick" data-input="3">3</button>
        </div>
        <div class="input-bar-sticky">
          <input type="text" id="input-text" class="input-field" placeholder="Send input...">
          <button class="btn-primary btn-sm" id="send-btn">Send</button>
        </div>
      </div>` : ''}

      ${prompt ? `
      <details class="detail-collapse">
        <summary class="collapse-summary">Prompt</summary>
        <div class="prompt-text">${escapeHtml(prompt)}</div>
      </details>` : ''}

      <details class="detail-collapse" id="logs-section">
        <summary class="collapse-summary" id="logs-summary">Logs</summary>
        <div class="log-entries" id="log-entries">Loading...</div>
      </details>

      ${!isActive ? `
      <div class="detail-actions-bottom">
        <button class="btn-danger btn-full" id="delete-btn-bottom">Delete from history</button>
      </div>` : ''}
    `;

    // Restore cached output immediately (no flash)
    const pane = container.querySelector('#output-pane');
    if (pane && cachedOutput) {
      pane.textContent = cachedOutput;
      if (autoScroll) pane.scrollTop = pane.scrollHeight;
    }

    wireEvents(isActive);
    loadOutput();
    loadLogs();
    startElapsedTimer(isActive);
  };

  function wireEvents(isActive) {
    container.querySelector('#back-btn').addEventListener('click', () => navigate('/'));

    // Output controls
    container.querySelector('#refresh-output').addEventListener('click', () => {
      outputOffset = 0;
      cachedOutput = '';
      const pane = container.querySelector('#output-pane');
      if (pane) pane.textContent = '';
      loadOutput();
    });

    container.querySelector('#toggle-full').addEventListener('click', () => {
      if (useFullOutput) return;
      useFullOutput = true;
      outputOffset = 0;
      cachedOutput = '';
      autoScroll = true;
      render();
    });

    container.querySelector('#toggle-live').addEventListener('click', () => {
      if (!useFullOutput) return;
      useFullOutput = false;
      cachedOutput = '';
      autoScroll = true;
      render();
    });

    container.querySelector('#toggle-fullscreen').addEventListener('click', () => {
      isFullscreen = !isFullscreen;
      const section = container.querySelector('#output-section');
      const pane = container.querySelector('#output-pane');
      const btn = container.querySelector('#toggle-fullscreen');
      if (section) section.classList.toggle('output-fullscreen', isFullscreen);
      if (pane) pane.classList.toggle('output-pane-fullscreen', isFullscreen);
      if (btn) btn.textContent = isFullscreen ? '✕' : '⛶';
      if (pane) pane.scrollTop = pane.scrollHeight;
    });

    // Auto-scroll lock
    const pane = container.querySelector('#output-pane');
    if (pane) {
      pane.addEventListener('scroll', () => {
        const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
        autoScroll = atBottom;
        const jumpBtn = container.querySelector('#jump-bottom');
        if (jumpBtn) jumpBtn.classList.toggle('hidden', atBottom);
      });
    }

    const jumpBtn = container.querySelector('#jump-bottom');
    if (jumpBtn) {
      jumpBtn.addEventListener('click', () => {
        autoScroll = true;
        jumpBtn.classList.add('hidden');
        if (pane) pane.scrollTop = pane.scrollHeight;
      });
    }

    // Stop / Kill
    const stopBtn = container.querySelector('#stop-btn');
    if (stopBtn) stopBtn.addEventListener('click', async () => {
      try { await api.stopAgent(agentId); state.toast('Agent stopped', 'success'); }
      catch (e) { state.toast(e.message, 'error'); }
    });

    // Delete buttons (inline header + bottom)
    for (const sel of ['#delete-btn', '#delete-btn-bottom']) {
      const btn = container.querySelector(sel);
      if (btn) btn.addEventListener('click', async () => {
        if (!confirm('Delete this agent from history?')) return;
        try {
          await api.deleteAgentHistory(agentId);
          state.toast('Agent deleted', 'success');
          const resp = await api.listAgents({ limit: 50 });
          state.set('agents', resp.agents || []);
          navigate('/');
        } catch (e) { state.toast(e.message, 'error'); }
      });
    }

    // Input
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

    // Quick action buttons
    container.querySelectorAll('.btn-quick').forEach(btn => {
      btn.addEventListener('click', async () => {
        try {
          await api.sendInput(agentId, btn.dataset.input);
        } catch (e) { state.toast(e.message, 'error'); }
      });
    });
  }

  function startElapsedTimer(isActive) {
    clearInterval(elapsedTimer);
    if (!isActive || !agent.started_at) return;
    elapsedTimer = setInterval(() => {
      const el = container.querySelector('#meta-line');
      if (el) el.textContent = renderMeta();
    }, 1000);
  }

  async function loadOutput() {
    const pane = container.querySelector('#output-pane');
    if (!pane) return;

    if (useFullOutput) {
      try {
        const data = await api.agentFullOutput(agentId, outputOffset);
        if (data.output) {
          if (outputOffset === 0) {
            pane.textContent = data.output;
          } else {
            pane.textContent += data.output;
          }
          cachedOutput = pane.textContent;
          if (autoScroll) pane.scrollTop = pane.scrollHeight;
          outputOffset = data.next_offset || outputOffset;
        }
        // If no data.output, keep existing content (don't flash)
      } catch {
        // Keep existing content on error
      }
    } else {
      try {
        const data = await api.agentOutput(agentId, 80);
        if (data.output) {
          pane.textContent = data.output;
          cachedOutput = data.output;
          if (autoScroll) pane.scrollTop = pane.scrollHeight;
        }
        // If no output, keep existing content
      } catch {
        // Keep existing content on error
      }
    }
  }

  async function loadLogs() {
    const el = container.querySelector('#log-entries');
    const summary = container.querySelector('#logs-summary');
    if (!el) return;
    try {
      const data = await api.agentLogs(agentId, 50);
      const entries = data.entries || [];
      if (summary) summary.textContent = `Logs (${entries.length})`;
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

  // Initial render + auto-refresh
  render().then(() => {
    if (['running', 'starting', 'pending'].includes(agent?.status)) {
      outputTimer = setInterval(loadOutput, 3000);
    }
  });

  // Re-render on status changes only
  const unsub = state.subscribe((data) => {
    const updated = (data.agents || []).find(a => a.id === agentId);
    if (updated && updated.status !== agent?.status) {
      agent = updated;
      render();
    }
  });

  return () => {
    clearInterval(outputTimer);
    clearInterval(elapsedTimer);
    unsub();
  };
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
