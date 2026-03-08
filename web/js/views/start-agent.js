import { api, state, navigate } from '../app.js';

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function renderStartAgent(container) {
  const contexts = state.get('contexts') || [];
  const adapters = state.get('adapters') || ['claude', 'cursor', 'codex', 'aider'];

  container.innerHTML = `
    <div class="page-header">
      <h2>Start Agent</h2>
    </div>
    <form id="start-form" class="form">
      <div class="form-group">
        <label for="tool">Tool</label>
        <select id="tool" class="form-input" required>
          ${adapters.filter(a => a !== 'generic').map(a => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join('')}
        </select>
      </div>

      <div class="form-group">
        <label for="context">Context</label>
        <select id="context" class="form-input" required>
          <option value="">Select context...</option>
          ${contexts.map(c => `<option value="${c.name}">${c.name} (${c.path})</option>`).join('')}
        </select>
      </div>

      <div class="form-group">
        <label for="prompt">Prompt (optional)</label>
        <textarea id="prompt" class="form-input form-textarea" rows="4"
          placeholder="Describe the task..."></textarea>
      </div>

      <div class="section-divider"></div>

      <div class="form-group">
        <label class="toggle-row">
          <span>Auto-confirm</span>
          <input type="checkbox" id="autoconfirm" class="toggle-input" checked>
          <span class="toggle-slider"></span>
        </label>
        <div class="form-hint">Automatically respond to permission prompts</div>
      </div>

      <details class="form-advanced">
        <summary>Advanced options</summary>
        <div class="form-group">
          <label for="name">Task name (optional)</label>
          <input type="text" id="name" class="form-input" placeholder="auto-generated">
        </div>
        <div class="form-group">
          <label for="timeout">Timeout (optional)</label>
          <input type="text" id="timeout" class="form-input" placeholder="e.g. 30m, 1h">
        </div>
        <div class="form-group">
          <label for="retry">Retries</label>
          <input type="number" id="retry" class="form-input" value="0" min="0" max="5">
        </div>
      </details>

      <button type="submit" class="btn-primary btn-full" id="submit-btn">Start Agent</button>
    </form>
  `;

  container.querySelector('#start-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = container.querySelector('#submit-btn');
    btn.disabled = true;
    btn.textContent = 'Starting...';

    const body = {
      tool: container.querySelector('#tool').value,
      context: container.querySelector('#context').value,
      prompt: container.querySelector('#prompt').value || ' ',
      auto_confirm: container.querySelector('#autoconfirm').checked,
      retry: parseInt(container.querySelector('#retry').value) || 0,
    };
    const timeout = container.querySelector('#timeout').value.trim();
    if (timeout) body.timeout = timeout;
    const name = container.querySelector('#name').value.trim();
    if (name) body.name = name;

    try {
      console.log('Starting agent with body:', JSON.stringify(body));
      const agent = await api.startAgent(body);
      console.log('Agent started:', JSON.stringify(agent));
      state.toast('Agent started', 'success');
      const resp = await api.listAgents({ limit: 50 });
      state.set('agents', resp.agents || []);
      navigate(`/agent/${agent.id}`);
    } catch (e) {
      console.error('Start agent failed:', e);
      state.toast('Start failed: ' + e.message, 'error');
      btn.disabled = false;
      btn.textContent = 'Start Agent';
    }
  });
}
