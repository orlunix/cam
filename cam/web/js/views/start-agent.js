import { api, state, navigate } from '../app.js';

export function renderStartAgent(container) {
  const contexts = state.get('contexts') || [];

  container.innerHTML = `
    <div class="page-header">
      <h2>Start Agent</h2>
    </div>
    <form id="start-form" class="form">
      <div class="form-group">
        <label for="tool">Tool</label>
        <select id="tool" class="form-input" required>
          <option value="claude">Claude</option>
          <option value="codex">Codex</option>
          <option value="aider">Aider</option>
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
        <label for="prompt">Prompt</label>
        <textarea id="prompt" class="form-input form-textarea" rows="4" required
          placeholder="Describe the task for the agent..."></textarea>
      </div>

      <details class="form-advanced">
        <summary>Advanced options</summary>
        <div class="form-group">
          <label for="name">Task name (optional)</label>
          <input type="text" id="name" class="form-input" placeholder="auto-generated">
        </div>
        <div class="form-group">
          <label for="timeout">Timeout</label>
          <input type="text" id="timeout" class="form-input" value="30m" placeholder="30m">
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
      prompt: container.querySelector('#prompt').value,
      timeout: container.querySelector('#timeout').value,
      retry: parseInt(container.querySelector('#retry').value) || 0,
    };
    const name = container.querySelector('#name').value.trim();
    if (name) body.name = name;

    try {
      console.log('Starting agent with body:', JSON.stringify(body));
      const agent = await api.startAgent(body);
      console.log('Agent started:', JSON.stringify(agent));
      state.toast('Agent started', 'success');
      // Refresh agents list
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
