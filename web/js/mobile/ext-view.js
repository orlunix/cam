/** Agent Extensions view — list installed extensions, run type-B tools bound to the agent. */
import { api, state } from '../app.js';

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

export function renderAgentExtensions(container, agentId) {
  container.innerHTML = `
    <div class="detail-header">
      <button class="back-btn" id="ext-back">&larr;</button>
      <div class="detail-title"><h2>Extensions</h2><span class="badge">agent ${esc(agentId.slice(0, 8))}</span></div>
    </div>
    <div id="ext-list" class="ext-list"><div class="empty-state">Loading…</div></div>
    <pre id="ext-result" class="ext-result"></pre>`;

  container.querySelector('#ext-back').addEventListener('click', () => history.back());

  const listEl = container.querySelector('#ext-list');
  const resultEl = container.querySelector('#ext-result');

  function currentTool() {
    const a = (state.get('agents') || []).find((x) => x.id === agentId);
    return (a && a.tool) || 'claude';
  }

  (async () => {
    let resp;
    try {
      resp = await api.extList();
    } catch (e) {
      listEl.innerHTML = `<div class="empty-state">Failed to load extensions: ${esc(e.message)}</div>`;
      return;
    }
    const exts = (resp && resp.extensions) || [];
    if (!exts.length) {
      listEl.innerHTML = '<div class="empty-state">No extensions installed.</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const ext of exts) {
      const caps = ext.capabilities || [];
      const row = document.createElement('div');
      row.className = 'ext-row';
      row.innerHTML = `
        <div class="ext-row-main">
          <div class="ext-name">${esc(ext.title || ext.name)}${ext.builtIn ? ' <span class="ext-badge">built-in</span>' : ''}</div>
          <div class="ext-meta">${esc(ext.name)}@${esc(ext.version || '?')} · ${esc(caps.join(', ') || 'view')}</div>
        </div>
        <button class="btn-sm btn-primary ext-run" ${caps.includes('exec') ? '' : 'disabled title="No remote tool"'}>Run</button>`;
      const btn = row.querySelector('.ext-run');
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = 'Running…';
        resultEl.textContent = '';
        try {
          const args = ext.name === 'agent-doctor' ? { tool: currentTool() } : {};
          const method = ext.name === 'agent-doctor' ? 'collect' : 'main';
          const r = await api.extCall(ext.name, method, args, agentId);
          resultEl.textContent = JSON.stringify(r.result ?? r, null, 2);
        } catch (e) {
          resultEl.textContent = `Error: ${e.message}`;
        }
        btn.disabled = false;
        btn.textContent = 'Run';
      });
      listEl.appendChild(row);
    }
  })();

  return () => {};
}
