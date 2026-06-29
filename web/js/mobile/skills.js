/**
 * CamUI Mobile V2 — Skills (port of desktop skills-mode.js, client-only).
 */
import { api, state } from './app.js';
import { escHtml as esc } from '../shared/agent-helpers.js';

const AGENT_TOOLS = ['claude', 'codex', 'openclaw', 'cursor'];

function machineLabel(ctx) {
  const m = (ctx && ctx.machine) || {};
  if ((m.type || 'local') === 'ssh' || m.host) {
    const user = m.user ? `${m.user}@` : '';
    return `${ctx.name} — ${user}${m.host || 'ssh'}:${m.port || 22}`;
  }
  return `${ctx?.name || 'local'} — local`;
}

function isSshContext(ctx) {
  const m = (ctx && ctx.machine) || {};
  return m.type === 'ssh' || !!m.host;
}

function shortSkillName(name) {
  const s = String(name || '');
  return s.includes('/') ? s.split('/').pop() : s;
}

function skillRepoName(skill) {
  return String(skill?.repo || ((skill?.name || '').includes('/') ? skill.name.split('/')[0] : ''));
}

export function renderSkills(container) {
  let activeTab = 'repos';
  let repos = [];
  let skills = [];
  let selectedSkills = new Set();
  let nodeScope = 'setup';
  let loading = false;
  let repoEditOldName = '';

  container.innerHTML = `
    <div class="page-header"><h2>Skills</h2></div>
    <div class="form-group">
      <label>Setup node</label>
      <select id="skillm-setup" class="form-input"></select>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <button type="button" class="btn-secondary btn-sm" id="skillm-check">Check</button>
      <button type="button" class="btn-secondary btn-sm" id="skillm-refresh">Refresh</button>
    </div>
    <div class="mobile-tab-bar">
      <button type="button" class="mobile-tab active" data-tab="repos">Repositories</button>
      <button type="button" class="mobile-tab" data-tab="install">Install</button>
    </div>
    <div id="skillm-status" class="settings-status"></div>
    <div id="panel-repos"></div>
    <div id="panel-install" hidden></div>
  `;

  const setupSel = container.querySelector('#skillm-setup');
  const statusEl = container.querySelector('#skillm-status');
  const panelRepos = container.querySelector('#panel-repos');
  const panelInstall = container.querySelector('#panel-install');

  function setStatus(text, ok) {
    statusEl.textContent = text || '';
    statusEl.className = 'settings-status' + (ok === true ? ' is-ok' : ok === false ? ' is-error' : '');
  }

  function setLoading(on) { loading = !!on; syncButtons(); }
  function syncButtons() {
    const ok = ['direct', 'relay'].includes(state.get('connectionMode')) && setupSel.value && !loading;
    container.querySelectorAll('#skillm-check,#skillm-refresh,.skillm-action').forEach(b => { if (b) b.disabled = !ok; });
  }

  function renderSetupOptions() {
    const ctxs = state.get('contexts') || [];
    const prev = setupSel.value;
    setupSel.innerHTML = '<option value="">Select SSH node...</option>' +
      ctxs.map(c => `<option value="${esc(c.name)}"${isSshContext(c) ? '' : ' disabled'}>${esc(machineLabel(c))}</option>`).join('');
    if (prev && ctxs.some(c => c.name === prev && isSshContext(c))) setupSel.value = prev;
    else {
      const first = ctxs.find(isSshContext);
      if (first) setupSel.value = first.name;
    }
    syncButtons();
  }

  function renderReposPanel() {
    panelRepos.innerHTML = `
      <button type="button" class="btn-secondary btn-full skillm-action" id="skillm-repo-add" style="margin:12px 0">Add Repository</button>
      <div id="skillm-repo-editor" class="form" hidden style="margin-bottom:12px;padding:12px;border:1px solid var(--border);border-radius:8px">
        <div class="form-group"><label>Name</label><input id="repo-name" class="form-input"></div>
        <div class="form-group"><label>URL</label><input id="repo-url" class="form-input"></div>
        <div class="form-group"><label>Token (one-shot)</label><input id="repo-token" type="password" class="form-input" autocomplete="off"></div>
        <div style="display:flex;gap:8px"><button type="button" class="btn-primary" id="repo-save">Save</button><button type="button" class="btn-secondary" id="repo-cancel">Cancel</button></div>
      </div>
      <div id="skillm-repo-list"></div>`;
    const listEl = panelRepos.querySelector('#skillm-repo-list');
    if (!repos.length) listEl.innerHTML = '<div class="empty-state">No repositories.</div>';
    else {
      listEl.innerHTML = repos.map(r => `
        <div class="skillm-repo-row">
          <div><strong>${esc(r.name)}</strong><br><span class="form-hint">${esc(r.url || '(local)')}</span></div>
          <div style="display:flex;gap:4px;flex-wrap:wrap">
            <button type="button" class="btn-secondary btn-xs skillm-action" data-act="refresh" data-repo="${esc(r.name)}">↻</button>
            <button type="button" class="btn-secondary btn-xs skillm-action" data-act="edit" data-repo="${esc(r.name)}">Edit</button>
            <button type="button" class="btn-secondary btn-xs skillm-action" data-act="remove" data-repo="${esc(r.name)}">×</button>
          </div>
        </div>`).join('');
    }
    panelRepos.querySelector('#skillm-repo-add').addEventListener('click', () => {
      repoEditOldName = '';
      const ed = panelRepos.querySelector('#skillm-repo-editor');
      ed.hidden = false;
      ed.querySelector('#repo-name').value = 'main';
      ed.querySelector('#repo-url').value = '';
      ed.querySelector('#repo-token').value = '';
    });
    panelRepos.querySelector('#repo-cancel').addEventListener('click', () => {
      panelRepos.querySelector('#skillm-repo-editor').hidden = true;
      panelRepos.querySelector('#repo-token').value = '';
    });
    panelRepos.querySelector('#repo-save').addEventListener('click', () => saveRepo());
    listEl.querySelectorAll('[data-act]').forEach(btn => {
      btn.addEventListener('click', () => onRepoAction(btn.dataset.act, btn.dataset.repo));
    });
  }

  function renderInstallPanel() {
    panelInstall.innerHTML = `
      <div class="form-group"><label>Repository filter</label><select id="skillm-repo-filter" class="form-input"><option value="all">All</option></select></div>
      <div class="form-group"><label>Search</label><input id="skillm-search" class="form-input" placeholder="Filter skills"></div>
      <div id="skillm-skill-list" class="skillm-skill-list"></div>
      <div class="form-group"><label>Install scope</label>
        <select id="skillm-scope" class="form-input"><option value="global">Global</option><option value="workspace">Workspace</option></select>
      </div>
      <div class="form-group" id="skillm-ws-row" hidden><label>Workspace path</label><input id="skillm-ws" class="form-input"></div>
      <div class="form-group"><label>Agent CLIs</label>
        ${AGENT_TOOLS.map(t => `<label style="margin-right:10px"><input type="checkbox" id="skillm-${t}" checked> ${t}</label>`).join('')}
      </div>
      <button type="button" class="btn-primary btn-full skillm-action" id="skillm-install">Install selected</button>`;
    const filterEl = panelInstall.querySelector('#skillm-repo-filter');
    filterEl.innerHTML = '<option value="all">All repositories</option>' +
      repos.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('');
    const ctx = (state.get('contexts') || []).find(c => c.name === setupSel.value);
    if (ctx) panelInstall.querySelector('#skillm-ws').value = ctx.path || '';
    panelInstall.querySelector('#skillm-scope').addEventListener('change', (e) => {
      panelInstall.querySelector('#skillm-ws-row').hidden = e.target.value !== 'workspace';
    });
    panelInstall.querySelector('#skillm-search').addEventListener('input', renderSkillList);
    filterEl.addEventListener('change', () => loadSkills(true));
    panelInstall.querySelector('#skillm-install').addEventListener('click', doInstall);
    renderSkillList();
  }

  function renderSkillList() {
    const listEl = panelInstall.querySelector('#skillm-skill-list');
    if (!listEl) return;
    const q = (panelInstall.querySelector('#skillm-search')?.value || '').trim().toLowerCase();
    const repo = panelInstall.querySelector('#skillm-repo-filter')?.value || 'all';
    const list = skills.filter(s => {
      if (repo !== 'all' && skillRepoName(s) !== repo) return false;
      if (!q) return true;
      return [s.name, skillRepoName(s)].join(' ').toLowerCase().includes(q);
    });
    if (!list.length) { listEl.innerHTML = '<div class="empty-state">No skills.</div>'; return; }
    listEl.innerHTML = list.map(s => `
      <label class="skillm-skill-row">
        <input type="checkbox" data-skill="${esc(s.name)}"${selectedSkills.has(s.name) ? ' checked' : ''}>
        <span>${esc(shortSkillName(s.name))} <span class="form-hint">${esc(skillRepoName(s))}</span></span>
      </label>`).join('');
    listEl.querySelectorAll('input[data-skill]').forEach(box => {
      box.addEventListener('change', () => {
        if (box.checked) selectedSkills.add(box.dataset.skill);
        else selectedSkills.delete(box.dataset.skill);
      });
    });
  }

  async function loadRepos() {
    if (!setupSel.value) return;
    setLoading(true);
    try {
      const res = await api.skillmRepos(setupSel.value);
      repos = res.repos || [];
      renderReposPanel();
      if (activeTab === 'install') renderInstallPanel();
      setStatus(`Loaded ${repos.length} repo(s).`, true);
    } catch (e) { setStatus(e.message, false); }
    finally { setLoading(false); }
  }

  async function loadSkills(sync) {
    if (!setupSel.value) return;
    setLoading(true);
    try {
      const repoName = panelInstall.querySelector('#skillm-repo-filter')?.value || 'all';
      const res = await api.skillmList(setupSel.value, { repoName, sync: !!sync });
      skills = res.skills || [];
      renderSkillList();
      setStatus(`Loaded ${skills.length} skill(s).`, true);
    } catch (e) { setStatus(e.message, false); }
    finally { setLoading(false); }
  }

  async function saveRepo() {
    const ed = panelRepos.querySelector('#skillm-repo-editor');
    const body = {
      contextName: setupSel.value,
      oldName: repoEditOldName,
      repoName: ed.querySelector('#repo-name').value.trim(),
      repoUrl: ed.querySelector('#repo-url').value.trim(),
      token: ed.querySelector('#repo-token').value,
    };
    if (!body.repoName || !body.repoUrl) { setStatus('Name and URL required.', false); return; }
    setLoading(true);
    try {
      if (repoEditOldName) await api.skillmRepoUpdate(body);
      else await api.skillmRepoAdd(body);
      ed.hidden = true;
      ed.querySelector('#repo-token').value = '';
      await loadRepos();
    } catch (e) { setStatus(e.message, false); }
    finally { setLoading(false); }
  }

  async function onRepoAction(act, name) {
    const repo = repos.find(r => r.name === name);
    if (!repo) return;
    if (act === 'edit') {
      repoEditOldName = repo.name;
      const ed = panelRepos.querySelector('#skillm-repo-editor');
      ed.hidden = false;
      ed.querySelector('#repo-name').value = repo.name;
      ed.querySelector('#repo-url').value = repo.url || '';
      ed.querySelector('#repo-token').value = '';
      return;
    }
    if (act === 'remove') {
      if (!confirm(`Remove repo ${name}?`)) return;
      setLoading(true);
      try { await api.skillmRepoRemove({ contextName: setupSel.value, repoName: name }); await loadRepos(); }
      catch (e) { setStatus(e.message, false); }
      finally { setLoading(false); }
      return;
    }
    if (act === 'refresh') {
      setLoading(true);
      try { await api.skillmRepoRefresh({ contextName: setupSel.value, repoName: name }); await loadSkills(true); }
      catch (e) { setStatus(e.message, false); }
      finally { setLoading(false); }
    }
  }

  async function doInstall() {
    const names = [...selectedSkills];
    const tools = AGENT_TOOLS.filter(t => panelInstall.querySelector(`#skillm-${t}`)?.checked);
    const scope = panelInstall.querySelector('#skillm-scope').value;
    const workspacePath = panelInstall.querySelector('#skillm-ws').value.trim();
    if (!names.length || !tools.length) { setStatus('Select skills and CLIs.', false); return; }
    setLoading(true);
    try {
      const res = await api.skillmInstall({
        contexts: [setupSel.value],
        skills: names,
        agents: tools,
        scope,
        workspacePath,
        repoName: panelInstall.querySelector('#skillm-repo-filter')?.value || 'all',
      });
      const ok = (res.results || []).filter(r => r.ok).length;
      setStatus(`Install: ${ok}/${(res.results || []).length} ok`, ok === (res.results || []).length);
    } catch (e) { setStatus(e.message, false); }
    finally { setLoading(false); }
  }

  function setTab(tab) {
    activeTab = tab;
    container.querySelectorAll('.mobile-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    panelRepos.hidden = tab !== 'repos';
    panelInstall.hidden = tab !== 'install';
    if (tab === 'install') {
      renderInstallPanel();
      void loadSkills(true);
    }
  }

  container.querySelectorAll('.mobile-tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  container.querySelector('#skillm-check').addEventListener('click', async () => {
    if (!setupSel.value) return;
    try {
      const res = await api.skillmStatus(setupSel.value);
      setStatus(res.installed ? `Skillm OK${res.version ? ': ' + res.version : ''}` : (res.detail || 'Not installed'), !!res.installed);
    } catch (e) { setStatus(e.message, false); }
  });
  container.querySelector('#skillm-refresh').addEventListener('click', () => {
    if (activeTab === 'repos') void loadRepos();
    else void loadSkills(true);
  });
  setupSel.addEventListener('change', () => { void loadRepos(); });

  renderSetupOptions();
  renderReposPanel();
  syncButtons();

  const unsub = state.subscribe(() => {
    renderSetupOptions();
  });

  if (setupSel.value) void loadRepos();

  return () => unsub();
}
