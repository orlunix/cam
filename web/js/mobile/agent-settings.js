import { api, state, navigate } from './app.js';
import {
  agentName, agentTags, agentAutoConfirm, agentWorkspacePath,
  systemPromptFileName, extractSystemPromptBlock, safeTag, escHtml,
} from '../shared/agent-helpers.js';
import {
  parsePromptSections, composePromptSections, renderChecklistPreviewHtml,
} from '../shared/prompt-sections.js?v=2.3.0';
import {
  parseWorkflowYamlV0, serializeWorkflowYaml, validateWorkflow,
  workflowVerifyLabel, workflowRunLabel, nextWorkflowNodeId, applyWorkflowField,
} from '../shared/workflow-yaml.js?v=2.3.0';
import {
  loopTemplateById, buildLoopPrompt, fillLoopTemplateSelect,
  resolvePromptSectionsForAgent, applyLoopTemplateDefaults,
  listLoopTemplates,
  saveCustomLoopTemplate, deleteCustomLoopTemplate,
} from '../shared/loop-templates.js?v=2.3.2';
import {
  fillWorkflowTemplateSelect, applyWorkflowTemplate,
  listCustomWorkflowTemplates, saveCustomWorkflowTemplate,
  deleteCustomWorkflowTemplate,
} from '../shared/workflow-templates.js?v=2.3.2';

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

function cronNextDue(job) {
  const s = job && job.schedule || {};
  const raw = s.next_due_at || job.last_due_at || '';
  return raw || 'not scheduled';
}

function escapeAttr(s) {
  return escHtml(s).replace(/'/g, '&#39;');
}

export function renderAgentSettings(container, agentId, tab = 'attributes') {
  let activeTab = TABS.includes(tab) ? tab : 'attributes';
  let cronPayload = null;
  let promptInitial = '';
  let wfPayload = null;
  let wfView = 'visual';
  let wfExpanded = new Set();
  let wfSelectedId = '';
  let wfDirty = false;

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
    const root = agentWorkspacePath(a);
    p.innerHTML = `
      <p class="form-hint">${file ? `Edits camc block in ${root ? `${root}/` : ''}${file}` : 'No prompt file for this tool.'}</p>
      <div class="prompt-rich-preview" id="prompt-preview"></div>
      <section class="prompt-section-card">
        <h4>Goal</h4>
        <textarea id="prompt-goal" class="form-input form-textarea" rows="3" placeholder="What should this agent optimize for?" ${file ? '' : 'disabled'}></textarea>
      </section>
      <section class="prompt-section-card">
        <h4>Checklist</h4>
        <p class="form-hint">One item per line. Use <code>- [ ]</code> / <code>- [x]</code> for tasks.</p>
        <textarea id="prompt-checklist" class="form-input form-textarea" rows="5" placeholder="- [ ] Run tests&#10;- [ ] Update docs" ${file ? '' : 'disabled'}></textarea>
        <div id="prompt-checklist-preview" class="prompt-checklist-render"></div>
      </section>
      <section class="prompt-section-card">
        <h4>Verify</h4>
        <textarea id="prompt-verify" class="form-input form-textarea" rows="3" placeholder="How to confirm the task is done (command, criterion, or human review)." ${file ? '' : 'disabled'}></textarea>
      </section>
      <details class="form-advanced">
        <summary>Additional prompt text</summary>
        <textarea id="prompt-body" class="form-input form-textarea" rows="4" ${file ? '' : 'disabled'}></textarea>
      </details>
      <details class="form-advanced">
        <summary>Raw combined prompt</summary>
        <textarea id="prompt-raw" class="form-input form-textarea" rows="8" readonly></textarea>
      </details>
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">
        <button type="button" class="btn-secondary btn-sm" id="prompt-load" ${file ? '' : 'disabled'}>Reload</button>
        <button type="button" class="btn-primary btn-sm" id="prompt-save" ${file ? '' : 'disabled'}>Save</button>
        <button type="button" class="btn-secondary btn-sm" id="prompt-clear" ${file ? '' : 'disabled'}>Clear</button>
      </div>
      <div id="prompt-status" class="settings-status"></div>`;

    const st = p.querySelector('#prompt-status');
    const goalEl = p.querySelector('#prompt-goal');
    const checklistEl = p.querySelector('#prompt-checklist');
    const verifyEl = p.querySelector('#prompt-verify');
    const bodyEl = p.querySelector('#prompt-body');
    const rawEl = p.querySelector('#prompt-raw');
    const previewEl = p.querySelector('#prompt-preview');
    const checklistPreviewEl = p.querySelector('#prompt-checklist-preview');

    function syncFromSections() {
      const combined = composePromptSections({
        goal: goalEl.value,
        checklist: checklistEl.value,
        verify: verifyEl.value,
        body: bodyEl.value,
      });
      rawEl.value = combined;
      previewEl.innerHTML = `
        <div class="prompt-rich-card"><span class="prompt-rich-label">Goal</span><p>${escHtml(goalEl.value.trim()) || '<span class="muted">—</span>'}</p></div>
        <div class="prompt-rich-card"><span class="prompt-rich-label">Checklist</span>${renderChecklistPreviewHtml(checklistEl.value, escHtml)}</div>
        <div class="prompt-rich-card"><span class="prompt-rich-label">Verify</span><p>${escHtml(verifyEl.value.trim()) || '<span class="muted">—</span>'}</p></div>`;
      checklistPreviewEl.innerHTML = renderChecklistPreviewHtml(checklistEl.value, escHtml);
    }

    function fillSections(text) {
      const sec = parsePromptSections(text);
      goalEl.value = sec.goal;
      checklistEl.value = sec.checklist;
      verifyEl.value = sec.verify;
      bodyEl.value = sec.body;
      syncFromSections();
    }

    [goalEl, checklistEl, verifyEl, bodyEl].forEach(el => {
      el.addEventListener('input', syncFromSections);
    });

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
      fillSections(prompt);
      setStatus(st, `Loaded from ${file}.`, true);
    }

    p.querySelector('#prompt-load').addEventListener('click', () => load().catch(e => setStatus(st, e.message, false)));
    p.querySelector('#prompt-clear').addEventListener('click', () => {
      goalEl.value = checklistEl.value = verifyEl.value = bodyEl.value = '';
      syncFromSections();
      setStatus(st, 'Cleared — save to persist.');
    });
    p.querySelector('#prompt-save').addEventListener('click', async () => {
      try {
        setStatus(st, 'Saving...');
        const prompt = composePromptSections({
          goal: goalEl.value,
          checklist: checklistEl.value,
          verify: verifyEl.value,
          body: bodyEl.value,
        });
        await api.updateAgent(a.id, { system_prompt: prompt });
        promptInitial = prompt;
        setStatus(st, 'Saved.', true);
      } catch (e) { setStatus(st, e.message, false); }
    });
    try { await load(); } catch (e) { setStatus(st, e.message, false); }
  }

  async function renderAutomation(a) {
    const p = panelEl();
    const host = a.machine_host || a.hostname || 'this node';
    const path = agentWorkspacePath(a);
    p.innerHTML = `
      <div class="mobile-cron-header">
        <p class="form-hint">Loops prompt this agent. Host cron runs shell commands on ${escHtml(host)}${path ? ` in ${escHtml(path)}` : ''}.</p>
        <button type="button" class="btn-secondary btn-sm" id="cron-refresh">Refresh</button>
      </div>
      <div id="cron-list"></div>
      <form id="cron-add" class="form mobile-cron-form">
        <div class="form-section-label">Add automation</div>
        <p class="form-hint">Choose whether to prompt this agent or run a host command.</p>
        <div class="mobile-cron-type" role="group" aria-label="Automation type">
          <label class="mobile-cron-type-opt">
            <input type="radio" name="cron-type" value="loop" checked>
            <span class="mobile-cron-type-body">
              <strong>Agent loop</strong>
              <small>Send prompt text to this agent when idle.</small>
            </span>
          </label>
          <label class="mobile-cron-type-opt">
            <input type="radio" name="cron-type" value="cron">
            <span class="mobile-cron-type-body">
              <strong>Host cron</strong>
              <small>Run any shell command on this node.</small>
            </span>
          </label>
        </div>
        <label id="cron-template-wrap">Loop template<select id="cron-template" class="form-input"></select><span id="cron-template-hint" class="form-hint"></span></label>
        <details class="template-manage" id="loop-tpl-manage">
          <summary>Manage loop templates</summary>
          <p class="form-hint">Built-in templates cannot be deleted. ★ = your custom templates (stored on this device).</p>
          <ul id="loop-tpl-list" class="template-list"></ul>
          <div class="template-add-form">
            <div class="form-section-label">Add custom template</div>
            <label>Label<input id="loop-tpl-label" class="form-input" placeholder="My review nudge" autocomplete="off"></label>
            <label>Hint<textarea id="loop-tpl-hint" class="form-input form-textarea" rows="2" placeholder="Short description"></textarea></label>
            <label>Kind<select id="loop-tpl-kind" class="form-input">
              <option value="static">Static prompt</option>
              <option value="progress-review">Progress review (dynamic)</option>
              <option value="continue-work">Continue work (dynamic)</option>
            </select></label>
            <label id="loop-tpl-body-wrap">Prompt<textarea id="loop-tpl-body" class="form-input form-textarea" rows="4" placeholder="Use {{goal}} {{checklist}} {{verify}} placeholders"></textarea></label>
            <label>Default schedule<input id="loop-tpl-sched" class="form-input" value="10m" placeholder="10m"></label>
            <button type="button" class="btn-secondary btn-sm" id="loop-tpl-save">Save template</button>
          </div>
        </details>
        <div class="mobile-cron-grid">
          <label>Name<input id="cron-name" class="form-input" placeholder="daily-review" autocomplete="off"></label>
          <label>Schedule<select id="cron-sched-type" class="form-input">
            <option value="every">Every</option><option value="daily">Daily</option>
            <option value="in">In</option><option value="at">At</option>
          </select></label>
          <label>Value<input id="cron-sched-val" class="form-input" value="30m" autocomplete="off"></label>
          <label>Timeout (s)<input id="cron-timeout" class="form-input" type="number" min="5" max="3600" value="60"></label>
          <label>Max attempts<input id="cron-attempts" class="form-input" type="number" min="1" max="20" value="3"></label>
          <label>TTL days<input id="cron-ttl" class="form-input" type="number" min="1" max="3650" value="7"></label>
        </div>
        <label id="cron-text-label"><span class="cron-field-label">Prompt</span><textarea id="cron-text" class="form-input form-textarea" rows="4" placeholder="review latest changes and report blockers"></textarea></label>
        <label id="cron-cwd-wrap" hidden>Working directory<input id="cron-cwd" class="form-input" value="${escapeAttr(agentWorkspacePath(a))}"></label>
        <label class="checkbox-row"><input type="checkbox" id="cron-no-expire"> No automatic expiry</label>
        <button type="submit" class="btn-primary btn-full" id="cron-add-btn">Add loop</button>
        <div id="cron-status" class="settings-status"></div>
      </form>`;

    const listEl = p.querySelector('#cron-list');
    const st = p.querySelector('#cron-status');
    const schedType = p.querySelector('#cron-sched-type');
    const schedVal = p.querySelector('#cron-sched-val');
    const cwdWrap = p.querySelector('#cron-cwd-wrap');
    const textLabel = p.querySelector('#cron-text-label');
    const addBtn = p.querySelector('#cron-add-btn');
    const typeRadios = p.querySelectorAll('input[name="cron-type"]');
    const templateWrap = p.querySelector('#cron-template-wrap');
    const templateEl = p.querySelector('#cron-template');
    const templateHint = p.querySelector('#cron-template-hint');
    const cronTextEl = p.querySelector('#cron-text');
    let templateApplying = false;

    function renderLoopTemplateList() {
      const ul = p.querySelector('#loop-tpl-list');
      if (!ul) return;
      ul.innerHTML = listLoopTemplates().filter(t => t.id !== 'custom').map(t => {
        const del = t.builtin ? '' : `<button type="button" class="btn-danger btn-sm" data-loop-tpl-del="${escapeAttr(t.id)}">Delete</button>`;
        const tag = t.builtin ? '<span class="template-tag">default</span>' : '<span class="template-tag">custom</span>';
        return `<li class="template-list-item">${tag}<span>${escHtml(t.label)}</span>${del}</li>`;
      }).join('');
      ul.querySelectorAll('[data-loop-tpl-del]').forEach(btn => {
        btn.addEventListener('click', () => {
          try {
            deleteCustomLoopTemplate(btn.dataset.loopTplDel);
            fillLoopTemplateSelect(templateEl, 'custom');
            renderLoopTemplateList();
            setStatus(st, 'Template deleted.', true);
          } catch (e) { setStatus(st, e.message, false); }
        });
      });
    }

    function syncLoopTplKindUi() {
      const kind = p.querySelector('#loop-tpl-kind')?.value || 'static';
      const wrap = p.querySelector('#loop-tpl-body-wrap');
      if (wrap) wrap.hidden = kind !== 'static';
    }

    fillLoopTemplateSelect(templateEl, 'custom');
    renderLoopTemplateList();
    syncLoopTplKindUi();
    p.querySelector('#loop-tpl-kind')?.addEventListener('change', syncLoopTplKindUi);
    p.querySelector('#loop-tpl-save')?.addEventListener('click', () => {
      try {
        const kind = p.querySelector('#loop-tpl-kind').value;
        const sched = p.querySelector('#loop-tpl-sched').value.trim() || '10m';
        const saved = saveCustomLoopTemplate({
          label: p.querySelector('#loop-tpl-label').value,
          hint: p.querySelector('#loop-tpl-hint').value,
          kind,
          promptBody: p.querySelector('#loop-tpl-body').value,
          defaults: {
            schedule_type: 'every',
            schedule_value: sched,
            no_expire: true,
          },
        });
        fillLoopTemplateSelect(templateEl, saved.id);
        renderLoopTemplateList();
        p.querySelector('#loop-tpl-label').value = '';
        p.querySelector('#loop-tpl-hint').value = '';
        p.querySelector('#loop-tpl-body').value = '';
        setStatus(st, `Saved template "${saved.label}".`, true);
        void applyLoopTemplate(saved.id, { forcePrompt: true });
      } catch (e) { setStatus(st, e.message, false); }
    });

    function updateTemplateHint() {
      if (!templateHint || !templateEl) return;
      const tpl = loopTemplateById(templateEl.value);
      templateHint.textContent = tpl?.hint || '';
    }

    async function applyLoopTemplate(templateId, { forcePrompt = false } = {}) {
      const tpl = loopTemplateById(templateId);
      if (!tpl || tpl.id === 'custom') {
        updateTemplateHint();
        return;
      }
      templateApplying = true;
      applyLoopTemplateDefaults({
        nameEl: p.querySelector('#cron-name'),
        schedTypeEl: schedType,
        schedValEl: schedVal,
        noExpireEl: p.querySelector('#cron-no-expire'),
        onScheduleChange: updateSchedPlaceholder,
      }, templateId);
      if (forcePrompt || !cronTextEl.value.trim() || cronTextEl.dataset.fromTemplate) {
        try {
          setStatus(st, 'Building prompt from system prompt…');
          const sections = await resolvePromptSectionsForAgent(api, a, {
            extractSystemPromptBlock,
            systemPromptFileName,
          });
          const built = buildLoopPrompt(templateId, sections);
          if (built) {
            cronTextEl.value = built;
            cronTextEl.dataset.fromTemplate = templateId;
          }
          setStatus(st, built ? 'Template applied.' : 'Template applied (empty prompt).', !!built);
        } catch (e) {
          setStatus(st, e.message, false);
        }
      } else {
        setStatus(st, 'Schedule defaults applied. Prompt kept (edit or clear to regenerate).', true);
      }
      templateApplying = false;
      updateTemplateHint();
    }

    function updateSchedPlaceholder() {
      const t = schedType.value || 'every';
      const map = { every: '30m or 2h', daily: '09:00', in: '45m or 2h', at: '2026-06-10T09:00:00-07:00' };
      schedVal.placeholder = map[t] || '30m';
      if (!schedVal.value) schedVal.value = t === 'daily' ? '09:00' : (t === 'at' ? '' : '30m');
    }

    function syncTypeUi() {
      const isCron = p.querySelector('input[name="cron-type"]:checked')?.value === 'cron';
      if (templateWrap) templateWrap.hidden = isCron;
      cwdWrap.hidden = !isCron;
      const labelSpan = textLabel.querySelector('.cron-field-label');
      if (labelSpan) labelSpan.textContent = isCron ? 'Command' : 'Prompt';
      p.querySelector('#cron-text').placeholder = isCron
        ? 'camc msg send cam-dev -t "review latest changes" --no-wait'
        : 'review latest changes and report blockers';
      addBtn.textContent = isCron ? 'Add cron job' : 'Add loop';
      p.querySelectorAll('.mobile-cron-type-opt').forEach(lab => {
        const on = lab.querySelector('input')?.checked;
        lab.classList.toggle('is-active', !!on);
      });
    }
    typeRadios.forEach(r => r.addEventListener('change', syncTypeUi));
    schedType.addEventListener('change', updateSchedPlaceholder);
    templateEl?.addEventListener('change', () => {
      void applyLoopTemplate(templateEl.value, { forcePrompt: true });
    });
    cronTextEl?.addEventListener('input', () => {
      if (!templateApplying) delete cronTextEl.dataset.fromTemplate;
    });
    updateSchedPlaceholder();
    updateTemplateHint();
    syncTypeUi();

    function renderList() {
      const jobs = cronPayload?.jobs || [];
      const loops = cronPayload?.loops || [];
      if (!jobs.length && !loops.length) {
        listEl.innerHTML = '<div class="empty-state">No automation attached.</div>';
        return;
      }
      const card = (item, kind) => {
        const keyRaw = item.id || item.name || '';
        const key = escapeAttr(`${kind}:${keyRaw}`);
        const name = escHtml(item.display_name || item.name || item.id || kind);
        const schedule = escHtml(cronScheduleLabel(item));
        const status = escHtml(item.last_status || item.state?.last_status || 'never run');
        const next = escHtml(cronNextDue(item));
        const hostLabel = escHtml(kind === 'loop' ? 'agent monitor' : (item.host || 'this host'));
        return `<article class="mobile-cron-job-card" data-cron-job="${key}">
          <div class="mobile-cron-job-main">
            <div class="mobile-cron-job-title">${name}</div>
            <div class="mobile-cron-job-meta">${schedule} · ${hostLabel}</div>
            <div class="mobile-cron-job-meta">next ${next}</div>
          </div>
          <div class="mobile-cron-job-side">
            <span class="mobile-cron-pill">${status}</span>
            <button type="button" class="btn-secondary btn-sm" data-rm="${key}">Remove</button>
          </div>
        </article>`;
      };
      const parts = [];
      if (loops.length) parts.push(`<div class="mobile-cron-heading">Agent loops</div>${loops.map(x => card(x, 'loop')).join('')}`);
      if (jobs.length) parts.push(`<div class="mobile-cron-heading">Host cron jobs</div>${jobs.map(x => card(x, 'cron')).join('')}`);
      if (cronPayload?.loop_error) parts.push(`<div class="empty-state">${escHtml(cronPayload.loop_error)}</div>`);
      listEl.innerHTML = parts.join('');
      listEl.querySelectorAll('[data-rm]').forEach(btn => {
        btn.addEventListener('click', async () => {
          if (!confirm('Remove this automation item?')) return;
          try {
            await api.deleteAgentCronJob(a.id, btn.dataset.rm);
            cronPayload = await api.agentCronJobs(a.id);
            renderList();
            setStatus(st, 'Removed.', true);
          } catch (e) { setStatus(st, e.message, false); }
        });
      });
    }

    p.querySelector('#cron-refresh').addEventListener('click', async () => {
      try {
        cronPayload = await api.agentCronJobs(a.id);
        renderList();
        setStatus(st, 'Refreshed.', true);
      } catch (e) { setStatus(st, e.message, false); }
    });

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
        schedule_type: schedType.value,
        schedule_value: schedVal.value.trim(),
        timeout_seconds: Number(p.querySelector('#cron-timeout').value) || 60,
        max_attempts: Number(p.querySelector('#cron-attempts').value) || 3,
        ttl_days: Number(p.querySelector('#cron-ttl').value) || 7,
        no_expire: !!p.querySelector('#cron-no-expire').checked,
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
        setStatus(st, type === 'cron' ? 'Cron job added.' : 'Loop added.', true);
        p.querySelector('#cron-name').value = '';
        cronTextEl.value = '';
        delete cronTextEl.dataset.fromTemplate;
        if (templateEl) templateEl.value = 'custom';
        updateTemplateHint();
      } catch (err) { setStatus(st, err.message, false); }
    });
  }

  function renderWorkflowCards(a) {
    const p = panelEl();
    const visualEl = p.querySelector('#wf-visual');
    const rawEl = p.querySelector('#wf-raw');
    const summaryEl = p.querySelector('#wf-summary');
    const st = p.querySelector('#wf-status');

    if (!wfPayload) {
      summaryEl.innerHTML = '<div class="empty-state">Click Refresh to load workflow.yaml.</div>';
      visualEl.innerHTML = '';
      if (rawEl) rawEl.hidden = true;
      return;
    }

    const showRaw = wfView === 'raw';
    visualEl.hidden = showRaw;
    if (rawEl) {
      rawEl.hidden = !showRaw;
      rawEl.value = wfPayload.text || '';
    }
    p.querySelectorAll('[data-wf-view]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.wfView === wfView);
    });

    const parsed = wfPayload.parsed || { nodes: [], edges: [] };
    const nodes = parsed.nodes || [];
    summaryEl.innerHTML = `
      <div class="wf-summary-main">
        <strong>${escHtml(parsed.workflow || 'workflow.yaml')}${wfDirty ? ' <span class="wf-dirty">unsaved</span>' : ''}</strong>
        <p class="muted">${escHtml(parsed.goal || 'No top-level goal.')}</p>
        <div class="wf-pill-row">
          <span class="wf-pill">${nodes.length} nodes</span>
          <span class="wf-pill">${(parsed.edges || []).length} edges</span>
          ${parsed.version ? `<span class="wf-pill">v${escHtml(parsed.version)}</span>` : ''}
        </div>
      </div>`;

    visualEl.innerHTML = nodes.length ? nodes.map((n, idx) => {
      const expanded = wfExpanded.has(n.id);
      const needs = (n.needs || []).length ? (n.needs || []).join(', ') : 'entry';
      const verify = workflowVerifyLabel(n);
      const outputCount = Object.keys(n.output_schema || {}).length;
      const outputText = Object.keys(n.output_schema || {}).map(k => `${k}: ${n.output_schema[k]}`).join('\n');
      const verifyText = Object.keys(n.verify || {}).map(k => `${k}: ${n.verify[k]}`).join('\n');
      const edgeHtml = `<div class="wf-edge" aria-hidden="true"><span>${escHtml(needs)}</span><span class="wf-edge-arrow">→</span><span>${escHtml(n.id)}</span></div>`;
      return `${edgeHtml}
        <article class="wf-node-card${n.id === wfSelectedId ? ' is-selected' : ''}${expanded ? ' is-expanded' : ''}" data-wf-node="${escapeAttr(n.id)}">
          <button type="button" class="wf-node-head" data-wf-toggle="${escapeAttr(n.id)}">
            <span class="wf-node-index">${idx + 1}</span>
            <span class="wf-node-id">${escHtml(n.id)}</span>
            <span class="wf-node-run">${escHtml(workflowRunLabel(n))}</span>
          </button>
          <div class="wf-node-meta muted">${(n.steps || []).length} checks · ${outputCount} outputs · verify ${escHtml(verify)}</div>
          ${expanded ? `
            <div class="wf-node-goal"><strong>Goal</strong><p>${escHtml(n.goal || 'No goal')}</p></div>
            <div class="wf-editor">
              <label>ID<input class="form-input" data-wf-field="id" value="${escapeAttr(n.id || '')}"></label>
              <label>Needs (comma-separated)<input class="form-input" data-wf-field="needs" value="${escapeAttr((n.needs || []).join(', '))}" placeholder="node-a, node-b"></label>
              <label>Goal<textarea class="form-input form-textarea" data-wf-field="goal" rows="2">${escHtml(n.goal || '')}</textarea></label>
              <label>Run · Skill<input class="form-input" data-wf-field="run.skill" value="${escapeAttr(n.run?.skill || '')}"></label>
              <label>Run · Command<input class="form-input" data-wf-field="run.command" value="${escapeAttr(n.run?.command || '')}"></label>
              <label>Checklist<textarea class="form-input form-textarea" data-wf-field="steps" rows="4">${escHtml((n.steps || []).join('\n'))}</textarea></label>
              <label>Expected output<textarea class="form-input form-textarea" data-wf-field="output_schema" rows="3">${escHtml(outputText)}</textarea></label>
              <label>Verify<textarea class="form-input form-textarea" data-wf-field="verify" rows="3" placeholder="criterion: ...&#10;command: ...&#10;human: true">${escHtml(verifyText)}</textarea></label>
              <label>Retry<input class="form-input" data-wf-field="retry" value="${escapeAttr(n.retry ?? '')}"></label>
            </div>
            <div class="wf-node-actions">
              <button type="button" class="btn-secondary btn-sm" data-wf-insert="${escapeAttr(n.id)}">Insert after</button>
              <button type="button" class="btn-secondary btn-sm" data-wf-dup="${escapeAttr(n.id)}">Duplicate</button>
              <button type="button" class="btn-danger btn-sm" data-wf-del="${escapeAttr(n.id)}">Delete</button>
            </div>` : `<button type="button" class="btn-secondary btn-sm wf-expand-btn" data-wf-toggle="${escapeAttr(n.id)}">${expanded ? 'Collapse' : 'Edit'}</button>`}
        </article>`;
    }).join('') : '<div class="empty-state">No nodes — tap Add node.</div>';
  }

  function markWorkflowDirty() {
    wfDirty = true;
    if (wfPayload) {
      wfPayload.text = serializeWorkflowYaml(wfPayload.parsed);
      const rawEl = panelEl()?.querySelector('#wf-raw');
      if (rawEl) rawEl.value = wfPayload.text;
    }
    const st = panelEl()?.querySelector('#wf-status');
    if (st) setStatus(st, 'Unsaved changes.');
  }

  async function renderWorkflow(a) {
    const p = panelEl();
    const root = agentWorkspacePath(a);
    const path = 'workflow.yaml';
    p.innerHTML = `
      <p class="form-hint">${root ? `${root}/` : ''}${path}</p>
      <div class="wf-template-row">
        <label class="wf-template-pick">Workflow template<select id="wf-template" class="form-input"></select></label>
        <button type="button" class="btn-secondary btn-sm" id="wf-apply-template">Apply template</button>
      </div>
      <details class="template-manage" id="wf-tpl-manage">
        <summary>Manage workflow templates</summary>
        <p class="form-hint">Apply replaces the editor content. Save current YAML as a custom template (★ = this device).</p>
        <ul id="wf-tpl-list" class="template-list"></ul>
        <div class="template-add-form">
          <label>Label<input id="wf-tpl-label" class="form-input" placeholder="My pipeline" autocomplete="off"></label>
          <label>Hint<textarea id="wf-tpl-hint" class="form-input form-textarea" rows="2"></textarea></label>
          <button type="button" class="btn-secondary btn-sm" id="wf-tpl-save-current">Save current workflow as template</button>
        </div>
      </details>
      <div class="mobile-tab-bar wf-view-toggle">
        <button type="button" class="mobile-tab active" data-wf-view="visual">Visual</button>
        <button type="button" class="mobile-tab" data-wf-view="raw">Raw YAML</button>
      </div>
      <div class="wf-toolbar">
        <button type="button" class="btn-secondary btn-sm" id="wf-refresh">Refresh</button>
        <button type="button" class="btn-secondary btn-sm" id="wf-validate">Validate</button>
        <button type="button" class="btn-secondary btn-sm" id="wf-add">Add node</button>
        <button type="button" class="btn-primary btn-sm" id="wf-save">Save</button>
      </div>
      <div id="wf-status" class="settings-status"></div>
      <div id="wf-summary" class="wf-summary"></div>
      <div id="wf-visual" class="wf-visual"></div>
      <textarea id="wf-raw" class="form-input form-textarea wf-raw" rows="14" hidden spellcheck="false"></textarea>`;

    const st = p.querySelector('#wf-status');
    const wfTemplateEl = p.querySelector('#wf-template');

    function renderWorkflowTemplateList() {
      const ul = p.querySelector('#wf-tpl-list');
      if (!ul) return;
      ul.innerHTML = listCustomWorkflowTemplates().map(t => `
        <li class="template-list-item">
          <span class="template-tag">custom</span>
          <span>${escHtml(t.label)}</span>
          <button type="button" class="btn-danger btn-sm" data-wf-tpl-del="${escapeAttr(t.id)}">Delete</button>
        </li>`).join('') || '<li class="muted">No custom workflow templates yet.</li>';
      ul.querySelectorAll('[data-wf-tpl-del]').forEach(btn => {
        btn.addEventListener('click', () => {
          try {
            deleteCustomWorkflowTemplate(btn.dataset.wfTplDel);
            fillWorkflowTemplateSelect(wfTemplateEl);
            renderWorkflowTemplateList();
            setStatus(st, 'Workflow template deleted.', true);
          } catch (e) { setStatus(st, e.message, false); }
        });
      });
    }

    fillWorkflowTemplateSelect(wfTemplateEl);
    renderWorkflowTemplateList();

    p.querySelector('#wf-apply-template')?.addEventListener('click', async () => {
      const id = wfTemplateEl?.value;
      if (!id) { setStatus(st, 'Pick a template first.', false); return; }
      if (wfDirty && !confirm('Replace unsaved workflow with this template?')) return;
      try {
        let goal = wfPayload?.parsed?.goal || '';
        if (!goal.trim()) {
          const sections = await resolvePromptSectionsForAgent(api, a, {
            extractSystemPromptBlock, systemPromptFileName,
          });
          goal = sections.goal || '';
        }
        const applied = applyWorkflowTemplate(id, { goal });
        if (!applied) throw new Error('Template not found.');
        wfPayload = {
          text: applied.text,
          parsed: applied.parsed,
          path,
          root: wfPayload?.root || root,
        };
        wfDirty = true;
        wfExpanded = new Set(applied.parsed.nodes[0]?.id ? [applied.parsed.nodes[0].id] : []);
        wfSelectedId = applied.parsed.nodes[0]?.id || '';
        setStatus(st, `Applied template "${applied.template.label}". Save to persist.`, true);
        renderWorkflowCards(a);
      } catch (e) { setStatus(st, e.message, false); }
    });

    p.querySelector('#wf-tpl-save-current')?.addEventListener('click', () => {
      try {
        const text = wfView === 'raw'
          ? (p.querySelector('#wf-raw')?.value || '')
          : (wfPayload?.text || serializeWorkflowYaml(wfPayload?.parsed));
        if (!String(text).trim()) throw new Error('Load or create a workflow first.');
        const saved = saveCustomWorkflowTemplate({
          label: p.querySelector('#wf-tpl-label').value,
          hint: p.querySelector('#wf-tpl-hint').value,
          yaml: text,
        });
        fillWorkflowTemplateSelect(wfTemplateEl, saved.id);
        renderWorkflowTemplateList();
        p.querySelector('#wf-tpl-label').value = '';
        p.querySelector('#wf-tpl-hint').value = '';
        setStatus(st, `Saved workflow template "${saved.label}".`, true);
      } catch (e) { setStatus(st, e.message, false); }
    });

    p.querySelectorAll('[data-wf-view]').forEach(btn => {
      btn.addEventListener('click', () => {
        wfView = btn.dataset.wfView === 'raw' ? 'raw' : 'visual';
        renderWorkflowCards(a);
      });
    });

    async function load() {
      setStatus(st, 'Loading...');
      const resp = await api.agentReadWorkspaceFile(a.id, path);
      if (resp?.binary) throw new Error('workflow.yaml is binary');
      const text = String(resp?.content || '');
      const parsed = parseWorkflowYamlV0(text);
      wfPayload = { text, parsed, path, root: resp?.root || root };
      wfDirty = false;
      wfExpanded = new Set(parsed.nodes[0]?.id ? [parsed.nodes[0].id] : []);
      wfSelectedId = parsed.nodes[0]?.id || '';
      setStatus(st, `Loaded ${parsed.nodes.length} node(s).`, true);
      renderWorkflowCards(a);
    }

    p.querySelector('#wf-refresh').addEventListener('click', () => load().catch(e => setStatus(st, e.message, false)));
    p.querySelector('#wf-validate').addEventListener('click', () => {
      const errors = validateWorkflow(wfPayload?.parsed);
      setStatus(st, errors.length ? errors.slice(0, 2).join('; ') : 'Workflow validates.', !errors.length);
    });
    p.querySelector('#wf-add').addEventListener('click', () => {
      if (!wfPayload) {
        wfPayload = { text: '', parsed: { workflow: 'workflow', version: '1', goal: '', nodes: [], edges: [] }, path, root };
      }
      const nodes = wfPayload.parsed.nodes || (wfPayload.parsed.nodes = []);
      const id = nextWorkflowNodeId(nodes);
      nodes.push({ id, goal: '', needs: wfSelectedId ? [wfSelectedId] : [], run: {}, steps: [], output_schema: {}, verify: {}, retry: null });
      wfExpanded.add(id);
      wfSelectedId = id;
      wfPayload.parsed.edges = nodes.flatMap(n => (n.needs || []).map(dep => ({ from: dep, to: n.id })));
      markWorkflowDirty();
      renderWorkflowCards(a);
    });
    p.querySelector('#wf-save').addEventListener('click', async () => {
      try {
        if (wfView === 'raw') {
          wfPayload.text = p.querySelector('#wf-raw').value || '';
          wfPayload.parsed = parseWorkflowYamlV0(wfPayload.text);
        } else {
          markWorkflowDirty();
        }
        const errors = validateWorkflow(wfPayload.parsed);
        if (errors.length) throw new Error(errors[0]);
        await api.agentWriteWorkspaceFile(a.id, path, wfPayload.text || '');
        wfDirty = false;
        setStatus(st, 'Saved workflow.yaml.', true);
        renderWorkflowCards(a);
      } catch (e) { setStatus(st, e.message, false); }
    });

    const visualEl = p.querySelector('#wf-visual');
    visualEl.addEventListener('click', (ev) => {
      const toggle = ev.target.closest('[data-wf-toggle]');
      if (toggle) {
        const id = toggle.dataset.wfToggle;
        wfSelectedId = id;
        if (wfExpanded.has(id)) wfExpanded.delete(id); else wfExpanded.add(id);
        renderWorkflowCards(a);
        return;
      }
      const insert = ev.target.closest('[data-wf-insert]');
      const dup = ev.target.closest('[data-wf-dup]');
      const del = ev.target.closest('[data-wf-del]');
      const nodes = wfPayload?.parsed?.nodes || [];
      if (insert || dup) {
        const baseId = (insert && insert.dataset.wfInsert) || (dup && dup.dataset.wfDup) || '';
        const idx = nodes.findIndex(n => n.id === baseId);
        const base = nodes[idx] || {};
        const id = nextWorkflowNodeId(nodes);
        const node = dup ? JSON.parse(JSON.stringify(base)) : { id, goal: '', needs: baseId ? [baseId] : [], run: {}, steps: [], output_schema: {}, verify: {}, retry: null };
        node.id = id;
        nodes.splice(idx >= 0 ? idx + 1 : nodes.length, 0, node);
        wfExpanded.add(id);
        wfSelectedId = id;
        wfPayload.parsed.edges = nodes.flatMap(n => (n.needs || []).map(dep => ({ from: dep, to: n.id })));
        markWorkflowDirty();
        renderWorkflowCards(a);
        return;
      }
      if (del) {
        const id = del.dataset.wfDel;
        const idx = nodes.findIndex(n => n.id === id);
        if (idx >= 0) nodes.splice(idx, 1);
        wfExpanded.delete(id);
        wfSelectedId = nodes[0]?.id || '';
        wfPayload.parsed.edges = nodes.flatMap(n => (n.needs || []).map(dep => ({ from: dep, to: n.id })));
        markWorkflowDirty();
        renderWorkflowCards(a);
      }
    });

    visualEl.addEventListener('input', (ev) => {
      const field = ev.target?.dataset?.wfField;
      if (!field || !wfPayload) return;
      const card = ev.target.closest('[data-wf-node]');
      const node = (wfPayload.parsed.nodes || []).find(n => n.id === card?.dataset.wfNode);
      if (!node) return;
      applyWorkflowField(node, field, ev.target.value);
      wfPayload.parsed.edges = (wfPayload.parsed.nodes || []).flatMap(n => (n.needs || []).map(dep => ({ from: dep, to: n.id })));
      markWorkflowDirty();
      const st2 = p.querySelector('#wf-status');
      if (st2) setStatus(st2, 'Unsaved changes.');
    });

    p.querySelector('#wf-raw')?.addEventListener('input', () => {
      if (!wfPayload) return;
      wfPayload.text = p.querySelector('#wf-raw').value || '';
      wfPayload.parsed = parseWorkflowYamlV0(wfPayload.text);
      wfDirty = true;
      setStatus(st, 'Unsaved raw YAML.');
    });

    try {
      await load();
    } catch (e) {
      if (/not_found|404/i.test(String(e.message))) {
        wfPayload = { text: 'workflow: workflow\nversion: 1\nnodes: []\n', parsed: parseWorkflowYamlV0('workflow: workflow\nversion: 1\nnodes: []\n'), path, root };
        wfDirty = false;
        renderWorkflowCards(a);
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
