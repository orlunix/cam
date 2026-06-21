import { api, state, navigate } from './app.js';
import {
  agentName, agentTags, agentAutoConfirm, agentWorkspacePath,
  systemPromptFileName, extractSystemPromptBlock, safeTag, escHtml,
} from '../shared/agent-helpers.js';

const TABS = ['attributes', 'prompt', 'automation', 'workflow'];

function findAgent(id) {
  return (state.get('agents') || []).find(a => a.id === id) || null;
}

function cronScheduleLabel(job) {
  const s = (job && job.schedule) || {};
  const t = s.type || job.kind || '';
  if (t === 'interval') {
    const n = Number(s.every_seconds || 0);
    if (n && n % 3600 === 0) return `every ${n / 3600}h`;
    if (n && n % 60 === 0) return `every ${n / 60}m`;
    return 'interval';
  }
  if (t === 'daily') return `daily ${s.time || ''}`.trim();
  if (t === 'once') return `once ${s.run_at || ''}`.trim();
  return t || 'schedule';
}

export function renderAgentSettings(container, agentId, tab = 'attributes') {
  let activeTab = TABS.includes(tab) ? tab : 'attributes';
  let cronPayload = null;
  let promptInitial = '';
  let workflowText = '';

  function agent() { return findAgent(agentId); }

  function setStatus(el, text, ok) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'settings-status' + (ok === true ? ' is-ok' : ok === false ? ' is-error' : '');
  }

  function renderShell() {
    const a = agent();
    if (!a) {
      container.innerHTML = '<div class="empty-state">Agent not found.</div>';
      return;
    }
    container.innerHTML = `
      <div class="page-header mobile-settings-header">
        <button type="button" class="btn-secondary btn-sm" id="settings-back">← Agent</button>
        <div>
          <h2>${escHtml(agentName(a))}</h2>
          <p class="form-hint">${escHtml(a.id)} · ${escHtml(a.tool || '')}</p>
        </div>
      </div>
      <div class="mobile-tab-bar" role="tablist">
        ${TABS.map(t => `
          <button type="button" class="mobile-tab${t === activeTab ? ' active' : ''}" data-tab="${t}">${tabLabel(t)}</button>
        `).join('')}
      </div>
      <div id="settings-panel" class="mobile-settings-panel"></div>
    `;
    container.querySelector('#settings-back').addEventListener('click', () => navigate(`#/agent/${agentId}`));
    container.querySelectorAll('.mobile-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        activeTab = btn.dataset.tab;
        navigate(`#/agent/${encodeURIComponent(agentId)}/settings/${activeTab}`);
        renderShell();
        loadTab();
      });
    });
    loadTab();
  }

  function tabLabel(t) {
    return { attributes: 'Attributes', prompt: 'Prompt', automation: 'Automation', workflow: 'Workflow' }[t] || t;
  }

  function panelEl() { return container.querySelector('#settings-panel'); }

  function loadTab() {
    const a = agent();
    if (!a) return;
    if (activeTab === 'attributes') renderAttributes(a);
    else if (activeTab === 'prompt') void renderPrompt(a);
    else if (activeTab === 'automation') void renderAutomation(a);
    else if (activeTab === 'workflow') void renderWorkflow(a);
  }

  function renderAttributes(a) {
    const p = panelEl();
    p.innerHTML = `
      <form id="attr-form" class="form">
        <div class="form-group"><label>Name</label><input id="attr-name" class="form-input" value="${escHtml(agentName(a))}"></div>
        <div class="form-group form-toggle"><label>Auto confirm</label><input type="checkbox" id="attr-auto" ${agentAutoConfirm(a) ? 'checked' : ''}></div>
        <div class="form-group"><label>Tags (comma-separated)</label><input id="attr-tags" class="form-input" value="${escHtml(agentTags(a).join(', '))}"></div>
        <button type="submit" class="btn-primary btn-full">Save Attributes</button>
        <div id="attr-status" class="settings-status"></div>
      </form>`;
    p.querySelector('#attr-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const st = p.querySelector('#attr-status');
      const nextName = p.querySelector('#attr-name').value.trim();
      const nextAuto = p.querySelector('#attr-auto').checked;
      const nextTags = p.querySelector('#attr-tags').value.split(',').map(t => t.trim()).filter(Boolean);
      for (const t of nextTags) {
        if (!safeTag(t)) { setStatus(st, `Invalid tag: ${t}`, false); return; }
      }
      const body = {};
      if (nextName && nextName !== agentName(a)) body.name = nextName;
      if (nextAuto !== agentAutoConfirm(a)) body.auto_confirm = nextAuto;
      const addT = nextTags.filter(t => !agentTags(a).includes(t));
      const rmT = agentTags(a).filter(t => !nextTags.includes(t));
      if (addT.length) body.tags_add = addT;
      if (rmT.length) body.tags_remove = rmT;
      if (!Object.keys(body).length) { setStatus(st, 'No changes.'); return; }
      try {
        const resp = await api.updateAgent(a.id, body);
        const updated = resp?.agent || { ...a, ...body };
        state.set('agents', (state.get('agents') || []).map(x => x.id === a.id ? { ...x, ...updated } : x));
        setStatus(st, 'Saved.', true);
      } catch (err) {
        setStatus(st, err.message || 'Save failed', false);
      }
    });
  }

  async function renderPrompt(a) {
    const p = panelEl();
    const file = systemPromptFileName(a);
    p.innerHTML = `
      <p class="form-hint">${file ? `Edits camc block in ${file}` : 'No prompt file for this tool.'}</p>
      <textarea id="prompt-text" class="form-input form-textarea" rows="12" ${file ? '' : 'disabled'}></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button type="button" class="btn-secondary" id="prompt-load" ${file ? '' : 'disabled'}>Reload</button>
        <button type="button" class="btn-primary" id="prompt-save" ${file ? '' : 'disabled'}>Save</button>
      </div>
      <div id="prompt-status" class="settings-status"></div>`;
    const ta = p.querySelector('#prompt-text');
    const st = p.querySelector('#prompt-status');
    async function load() {
      if (!file) return;
      setStatus(st, 'Loading...');
      let prompt = String(a.system_prompt || a.task?.system_prompt || '');
      try {
        const resp = await api.agentReadWorkspaceFile(a.id, file);
        if (resp && !resp.binary) {
          const fromFile = extractSystemPromptBlock(resp.content || '', a.id);
          if (fromFile || !prompt) prompt = fromFile;
        }
      } catch (e) {
        if (!/not_found|404/i.test(String(e.message))) throw e;
      }
      promptInitial = prompt;
      ta.value = prompt;
      setStatus(st, `Loaded from ${file}.`, true);
    }
    p.querySelector('#prompt-load').addEventListener('click', () => load().catch(e => setStatus(st, e.message, false)));
    p.querySelector('#prompt-save').addEventListener('click', async () => {
      try {
        setStatus(st, 'Saving...');
        await api.updateAgent(a.id, { system_prompt: ta.value });
        promptInitial = ta.value;
        setStatus(st, 'Saved.', true);
      } catch (e) { setStatus(st, e.message, false); }
    });
    try { await load(); } catch (e) { setStatus(st, e.message, false); }
  }

  async function renderAutomation(a) {
    const p = panelEl();
    p.innerHTML = `
      <div id="cron-list"></div>
      <form id="cron-add" class="form" style="margin-top:16px">
        <div class="form-section-label">Add automation</div>
        <div class="form-group">
          <label><input type="radio" name="cron-type" value="loop" checked> Agent loop</label>
          <label style="margin-left:12px"><input type="radio" name="cron-type" value="cron"> Host cron</label>
        </div>
        <div class="form-group"><label>Name</label><input id="cron-name" class="form-input" placeholder="nightly-review"></div>
        <div class="form-group"><label>Schedule type</label>
          <select id="cron-sched-type" class="form-input"><option value="every">every</option><option value="daily">daily</option><option value="in">in</option><option value="at">at</option></select>
        </div>
        <div class="form-group"><label>Schedule value</label><input id="cron-sched-val" class="form-input" value="30m"></div>
        <div class="form-group" id="cron-cwd-wrap" hidden><label>Working dir</label><input id="cron-cwd" class="form-input" value="${escHtml(agentWorkspacePath(a))}"></div>
        <div class="form-group"><label id="cron-text-label">Prompt</label><textarea id="cron-text" class="form-input form-textarea" rows="3"></textarea></div>
        <button type="submit" class="btn-primary btn-full">Add</button>
        <div id="cron-status" class="settings-status"></div>
      </form>`;
    const listEl = p.querySelector('#cron-list');
    const st = p.querySelector('#cron-status');
    const typeRadios = p.querySelectorAll('input[name="cron-type"]');
    const cwdWrap = p.querySelector('#cron-cwd-wrap');
    const textLabel = p.querySelector('#cron-text-label');

    function syncTypeUi() {
      const isCron = p.querySelector('input[name="cron-type"]:checked')?.value === 'cron';
      cwdWrap.hidden = !isCron;
      textLabel.textContent = isCron ? 'Command' : 'Prompt';
    }
    typeRadios.forEach(r => r.addEventListener('change', syncTypeUi));
    syncTypeUi();

    function renderList() {
      const jobs = cronPayload?.jobs || [];
      const loops = cronPayload?.loops || [];
      if (!jobs.length && !loops.length) {
        listEl.innerHTML = '<div class="empty-state">No automation yet.</div>';
        return;
      }
      const card = (item, kind) => {
        const key = `${kind}:${item.id || item.name || ''}`;
        const name = escHtml(item.display_name || item.name || item.id || kind);
        return `<div class="mobile-cron-card">
          <div><strong>${name}</strong><br><span class="form-hint">${escHtml(cronScheduleLabel(item))}</span></div>
          <button type="button" class="btn-secondary btn-sm" data-rm="${escHtml(key)}">Remove</button>
        </div>`;
      };
      listEl.innerHTML = [
        ...loops.map(x => card(x, 'loop')),
        ...jobs.map(x => card(x, 'cron')),
      ].join('');
      listEl.querySelectorAll('[data-rm]').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (!confirm('Remove this automation item?')) return;
          try {
            await api.deleteAgentCronJob(a.id, btn.dataset.rm);
            cronPayload = await api.agentCronJobs(a.id);
            renderList();
          } catch (e) { setStatus(st, e.message, false); }
        });
      });
    }

    try {
      cronPayload = await api.agentCronJobs(a.id);
      renderList();
    } catch (e) {
      listEl.innerHTML = `<div class="empty-state">${escHtml(e.message)}</div>`;
    }

    p.querySelector('#cron-add').addEventListener('submit', async (e) => {
      e.preventDefault();
      const type = p.querySelector('input[name="cron-type"]:checked')?.value === 'cron' ? 'cron' : 'loop';
      const name = p.querySelector('#cron-name').value.trim();
      const text = p.querySelector('#cron-text').value.trim();
      if (!/^[A-Za-z0-9_.-]{1,48}$/.test(name)) { setStatus(st, 'Invalid name.', false); return; }
      if (!text) { setStatus(st, type === 'cron' ? 'Command required.' : 'Prompt required.', false); return; }
      const body = {
        type,
        name,
        schedule_type: p.querySelector('#cron-sched-type').value,
        schedule_value: p.querySelector('#cron-sched-val').value.trim(),
        timeout_seconds: 60,
        max_attempts: 3,
        ttl_days: 7,
      };
      if (type === 'cron') {
        body.command = text;
        body.cwd = p.querySelector('#cron-cwd').value.trim() || agentWorkspacePath(a);
      } else {
        body.text = text;
      }
      try {
        await api.createAgentCronJob(a.id, body);
        cronPayload = await api.agentCronJobs(a.id);
        renderList();
        setStatus(st, 'Added.', true);
        p.querySelector('#cron-name').value = '';
        p.querySelector('#cron-text').value = '';
      } catch (err) { setStatus(st, err.message, false); }
    });
  }

  async function renderWorkflow(a) {
    const p = panelEl();
    const path = 'workflow.yaml';
    const root = agentWorkspacePath(a);
    p.innerHTML = `
      <p class="form-hint">${root ? `${root}/` : ''}${path}</p>
      <textarea id="wf-text" class="form-input form-textarea" rows="16" style="font-family:monospace;font-size:13px"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button type="button" class="btn-secondary" id="wf-load">Reload</button>
        <button type="button" class="btn-primary" id="wf-save">Save</button>
      </div>
      <div id="wf-status" class="settings-status"></div>`;
    const ta = p.querySelector('#wf-text');
    const st = p.querySelector('#wf-status');
    async function load() {
      setStatus(st, 'Loading...');
      const resp = await api.agentReadWorkspaceFile(a.id, path);
      workflowText = resp?.binary ? '' : String(resp?.content || '');
      ta.value = workflowText;
      setStatus(st, 'Loaded.', true);
    }
    p.querySelector('#wf-load').addEventListener('click', () => load().catch(e => setStatus(st, e.message, false)));
    p.querySelector('#wf-save').addEventListener('click', async () => {
      try {
        setStatus(st, 'Saving...');
        await api.agentWriteWorkspaceFile(a.id, path, ta.value);
        workflowText = ta.value;
        setStatus(st, 'Saved.', true);
      } catch (e) { setStatus(st, e.message, false); }
    });
    try { await load(); } catch (e) {
      if (/not_found|404/i.test(String(e.message))) {
        ta.value = 'workflow: workflow\nversion: 1\nnodes: []\n';
        setStatus(st, 'New workflow.yaml (not on disk yet).');
      } else setStatus(st, e.message, false);
    }
  }

  renderShell();

  const unsub = state.subscribe(() => {
    if (!findAgent(agentId)) renderShell();
  });
  return () => unsub();
}
