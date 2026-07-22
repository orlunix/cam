import { api, state, navigate } from '../app.js';

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;');
}

const DEFAULT_TOOLS = ['claude', 'codex', 'cursor', 'others'];
const HIDDEN_TOOLS = new Set(['generic', 'aider']);

function toolOptions() {
  const adapters = state.get('adapters');
  const base = Array.isArray(adapters) && adapters.length ? adapters : DEFAULT_TOOLS;
  const tools = [];
  for (const item of [...base, 'others']) {
    const tool = String(item == null ? '' : item).trim();
    if (!tool || HIDDEN_TOOLS.has(tool)) continue;
    if (!tools.includes(tool)) tools.push(tool);
  }
  return tools.length ? tools : DEFAULT_TOOLS;
}

function hostKeyForMachine(machine) {
  const m = machine || {};
  const type = m.type || (m.host ? 'ssh' : 'local');
  if (type !== 'ssh') return '';
  const user = String(m.user || '').trim();
  const host = String(m.host || '').trim();
  const port = Number(m.port || 22) || 22;
  if (!user || !host) return '';
  const displayHost = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
  return `${user}@${displayHost}:${port}`;
}

function nodeOptions(contexts) {
  const seen = new Map();
  for (const ctx of contexts || []) {
    const machine = ctx && ctx.machine ? ctx.machine : {};
    const key = hostKeyForMachine(machine);
    if (!key || seen.has(key)) continue;
    seen.set(key, key);
  }
  return Array.from(seen.entries()).map(([key, label]) => ({ key, label }));
}

function toggleCustomTool(container) {
  const select = container.querySelector('#tool');
  const group = container.querySelector('#custom-tool-group');
  const input = container.querySelector('#custom-tool');
  if (!select || !group || !input) return;
  const custom = select.value === 'others';
  group.hidden = !custom;
  input.required = custom;
  input.disabled = !custom;
}

function toggleTargetFields(container) {
  const context = container.querySelector('#context');
  const fields = container.querySelector('#node-path-fields');
  const node = container.querySelector('#node');
  const path = container.querySelector('#remote-path');
  if (!context || !fields || !node || !path) return;
  const needsNodePath = !context.value;
  fields.hidden = !needsNodePath;
  node.required = needsNodePath;
  path.required = needsNodePath;
  node.disabled = !needsNodePath;
  path.disabled = !needsNodePath;
}

function selectedTool(container) {
  const select = container.querySelector('#tool');
  if (!select) return '';
  if (select.value !== 'others') return select.value;
  const input = container.querySelector('#custom-tool');
  return input ? input.value.trim() : '';
}

function applyTarget(container, body) {
  const context = container.querySelector('#context').value.trim();
  if (context) {
    body.context = context;
    return true;
  }
  const node = container.querySelector('#node').value.trim();
  const path = container.querySelector('#remote-path').value.trim();
  if (!node || !path) return false;
  body.node = node;
  body.path = path;
  return true;
}

export function renderStartAgent(container) {
  const contexts = state.get('contexts') || [];
  const adapters = toolOptions();
  const nodes = nodeOptions(contexts);

  container.innerHTML = `
    <div class="page-header">
      <h2>Start Agent</h2>
    </div>
    <form id="start-form" class="form">
      <div class="form-group">
        <label for="tool">Tool</label>
        <select id="tool" class="form-input" required>
          ${adapters.map(a => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join('')}
        </select>
      </div>

      <div class="form-group" id="custom-tool-group" hidden>
        <label for="custom-tool">Tool command</label>
        <input type="text" id="custom-tool" class="form-input"
          placeholder="e.g. qwen-code or /path/to/agent" autocomplete="off" disabled>
        <div class="form-hint">Runs via <code>camc run --tool</code>; camc records it as <code>others</code>.</div>
      </div>

      <div class="form-group">
        <label for="context">Context (optional)</label>
        <select id="context" class="form-input">
          <option value="">No context — choose node/path below</option>
          ${contexts.map(c => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}${c.path ? ` (${escapeHtml(c.path)})` : ''}</option>`).join('')}
        </select>
      </div>

      <div class="form-group" id="node-path-fields">
        <label for="node">Node</label>
        <select id="node" class="form-input">
          ${nodes.length ? nodes.map(n => `<option value="${escapeHtml(n.key)}">${escapeHtml(n.label)}</option>`).join('') : '<option value="">No registered SSH nodes</option>'}
        </select>
        <label for="remote-path">Remote path</label>
        <input type="text" id="remote-path" class="form-input" placeholder="/home/user/project" autocomplete="off">
        <div class="form-hint">Required when Context is not selected.</div>
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

      <div class="form-group">
        <label class="toggle-row">
          <span>Auto-exit</span>
          <input type="checkbox" id="autoexit" class="toggle-input">
          <span class="toggle-slider"></span>
        </label>
        <div class="form-hint">Finalize agent when task completes</div>
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

  toggleCustomTool(container);
  toggleTargetFields(container);
  container.querySelector('#tool').addEventListener('change', () => toggleCustomTool(container));
  container.querySelector('#context').addEventListener('change', () => toggleTargetFields(container));

  container.querySelector('#start-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = container.querySelector('#submit-btn');
    btn.disabled = true;
    btn.textContent = 'Starting...';

    const tool = selectedTool(container);
    if (!tool) {
      state.toast('Enter a tool command for Others', 'error');
      btn.disabled = false;
      btn.textContent = 'Start Agent';
      return;
    }

    const body = {
      tool,
      prompt: container.querySelector('#prompt').value || ' ',
      auto_confirm: container.querySelector('#autoconfirm').checked,
      auto_exit: container.querySelector('#autoexit').checked,
      retry: parseInt(container.querySelector('#retry').value) || 0,
    };
    if (!applyTarget(container, body)) {
      state.toast('Select a context, or enter node and remote path', 'error');
      btn.disabled = false;
      btn.textContent = 'Start Agent';
      return;
    }
    const timeout = container.querySelector('#timeout').value.trim();
    if (timeout) body.timeout = timeout;
    const name = container.querySelector('#name').value.trim();
    if (name) body.name = name;

    try {
      console.log('Starting agent with body:', JSON.stringify(body));
      const res = await api.startAgent(body);
      const agent = res && res.agent ? res.agent : res;
      const agentId = agent && (agent.id || agent.agentId) || (res && res.agentId);
      console.log('Agent started:', JSON.stringify(res));
      state.toast('Agent started', 'success');
      const resp = await api.listAgents({ limit: 50 });
      state.set('agents', resp.agents || []);
      if (agentId) navigate(`/agent/${agentId}`);
    } catch (e) {
      console.error('Start agent failed:', e);
      state.toast('Start failed: ' + e.message, 'error');
      btn.disabled = false;
      btn.textContent = 'Start Agent';
    }
  });
}
