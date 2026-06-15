/**
 * Desktop Skills mode (CAM-DESK-SKILLM-010..014).
 *
 * The renderer manages Skillm repositories and install targets only. Repository
 * refresh happens in the embedded Hub before list/install operations; there is
 * no separate Sync tab/action in the UI.
 */

const AGENT_TOOLS = ['claude', 'codex', 'openclaw', 'cursor'];
const REPO_NAME_KEY = 'cam_desktop_skillm_repo_name';
const REPO_URL_KEY  = 'cam_desktop_skillm_repo_url';

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function machineLabel(ctx) {
  const m = (ctx && ctx.machine) || {};
  if ((m.type || 'local') === 'ssh') {
    const host = m.host || 'ssh-host';
    const user = m.user ? `${m.user}@` : '';
    const port = m.port || 22;
    return `${ctx.name} - ${user}${host}:${port}`;
  }
  return `${ctx && ctx.name || 'local'} - local`;
}

function isSshContext(ctx) {
  const m = (ctx && ctx.machine) || {};
  return (m.type || 'local') === 'ssh';
}

function contextPath(ctx) {
  return String((ctx && ctx.path) || '').trim();
}

function shortSkillName(name) {
  const s = String(name || '');
  return s.includes('/') ? s.split('/').pop() : s;
}

function skillRepoName(skill) {
  return String((skill && skill.repo) || ((skill && skill.name || '').includes('/') ? skill.name.split('/')[0] : ''));
}

function dedupeSkills(items) {
  const seen = new Set();
  const out = [];
  for (const s of Array.isArray(items) ? items : []) {
    const key = [skillRepoName(s), s && s.name, s && s.commit, s && s.updated, s && s.size].join('\u0000');
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(s);
  }
  return out;
}

function fmtResultCount(results) {
  const arr = Array.isArray(results) ? results : [];
  const ok = arr.filter(r => r && r.ok).length;
  return `${ok}/${arr.length} ok`;
}

export function mountSkillsMode({ api, state, showToast }) {
  const panel = document.getElementById('mode-skills');
  if (!panel) return;

  const setupSel       = panel.querySelector('#skillm-setup-context');
  const checkBtn       = panel.querySelector('#skillm-check');
  const refreshBtn     = panel.querySelector('#skillm-refresh');
  const tabBtns        = Array.from(panel.querySelectorAll('[data-skillm-tab]'));
  const reposPanel     = panel.querySelector('#skillm-tab-repos');
  const installPanel   = panel.querySelector('#skillm-tab-install');
  const repoAddBtn     = panel.querySelector('#skillm-repo-add');
  const repoEditor     = panel.querySelector('#skillm-repo-editor');
  const repoNameEl     = panel.querySelector('#skillm-repo-name');
  const repoUrlEl      = panel.querySelector('#skillm-repo-url');
  const repoTokenEl    = panel.querySelector('#skillm-repo-token');
  const repoSaveBtn    = panel.querySelector('#skillm-repo-save');
  const repoCancelBtn  = panel.querySelector('#skillm-repo-cancel');
  const repoListEl     = panel.querySelector('#skillm-repo-list');
  const repoFilterEl   = panel.querySelector('#skillm-repo-filter');
  const nodeListEl     = panel.querySelector('#skillm-node-list');
  const nodeScopeBtns  = Array.from(panel.querySelectorAll('[data-node-scope]'));
  const skillSearchEl  = panel.querySelector('#skillm-skill-search');
  const skillListEl    = panel.querySelector('#skillm-skill-list');
  const selectAllBtn   = panel.querySelector('#skillm-skills-all');
  const selectNoneBtn  = panel.querySelector('#skillm-skills-none');
  const scopeSel       = panel.querySelector('#skillm-install-scope');
  const workspaceEl    = panel.querySelector('#skillm-workspace-path');
  const workspaceRow   = workspaceEl ? workspaceEl.closest('label') : null;
  const installBtn     = panel.querySelector('#skillm-install');
  const statusEl       = panel.querySelector('#skillm-status');

  let activeTab = 'repos';
  let repoEditOldName = '';
  let repos = [];
  let skills = [];
  let selectedSkills = new Set();
  let selectedContexts = new Set();
  let nodeScope = 'setup';
  let loading = false;

  try {
    repoNameEl.value = localStorage.getItem(REPO_NAME_KEY) || 'main';
    repoUrlEl.value  = localStorage.getItem(REPO_URL_KEY) || '';
  } catch {
    repoNameEl.value = 'main';
  }

  function contexts() { return state.get('contexts') || []; }
  function selectedContext() { return contexts().find(c => c.name === setupSel.value) || null; }
  function isConnected() { return (state.get('connectionMode') || 'disconnected') !== 'disconnected'; }

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function setLoading(on) {
    loading = !!on;
    syncButtons();
  }

  function syncButtons() {
    const enabled = isConnected() && !!setupSel.value && !loading;
    [checkBtn, refreshBtn, repoAddBtn, repoSaveBtn, installBtn, selectAllBtn, selectNoneBtn].forEach(btn => {
      if (btn) btn.disabled = !enabled;
    });
  }

  function selectedNodeNames() {
    if (nodeScope === 'setup') return [setupSel.value].filter(Boolean);
    if (nodeScope === 'all') return contexts().filter(isSshContext).map(c => c.name);
    const boxes = Array.from(nodeListEl.querySelectorAll('input[type="checkbox"][data-context]'));
    return boxes.filter(b => b.checked).map(b => b.dataset.context).filter(Boolean);
  }

  function selectedAgentTools() {
    return AGENT_TOOLS.filter(t => {
      const el = panel.querySelector(`#skillm-agent-${t}`);
      return el && el.checked;
    });
  }

  function setTab(tab) {
    activeTab = tab === 'install' ? 'install' : 'repos';
    for (const btn of tabBtns) {
      const on = btn.dataset.skillmTab === activeTab;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    }
    reposPanel.hidden = activeTab !== 'repos';
    installPanel.hidden = activeTab !== 'install';
    if (activeTab === 'install') void loadSkills({ sync: true });
  }

  function renderContextOptions() {
    const ctxs = contexts();
    const prev = setupSel.value;
    setupSel.innerHTML = '<option value="">Select SSH node...</option>' + ctxs.map(c => {
      const disabled = isSshContext(c) ? '' : ' disabled';
      return `<option value="${esc(c.name)}"${disabled}>${esc(machineLabel(c))}</option>`;
    }).join('');
    if (prev && ctxs.some(c => c.name === prev && isSshContext(c))) setupSel.value = prev;
    else {
      const first = ctxs.find(isSshContext);
      if (first) setupSel.value = first.name;
    }
    const ctx = selectedContext();
    if (ctx && !workspaceEl.value) workspaceEl.value = contextPath(ctx);
    if (setupSel.value && !selectedContexts.size) selectedContexts.add(setupSel.value);
    renderNodeList();
    syncButtons();
  }

  function renderRepoFilter() {
    const prev = repoFilterEl.value || 'all';
    repoFilterEl.innerHTML = '<option value="all">All repositories</option>' + repos.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('');
    repoFilterEl.value = repos.some(r => r.name === prev) ? prev : 'all';
  }

  function renderRepos() {
    renderRepoFilter();
    if (!repos.length) {
      repoListEl.innerHTML = '<div class="empty-state">No repositories yet. Add a repository to initialize this node.</div>';
      return;
    }
    repoListEl.innerHTML = repos.map((r) => {
      const active = r.active ? '<span class="skillm-repo-badge">active</span>' : '';
      const local = r.local ? '<span class="skillm-repo-badge">local</span>' : '';
      return `<div class="skillm-repo-row" data-repo="${esc(r.name)}">
        <div class="skillm-repo-main">
          <span class="skillm-repo-name">${esc(r.name)} ${active} ${local}</span>
          <span class="skillm-repo-url">${esc(r.url || '(local repository)')}</span>
        </div>
        <div class="skillm-repo-actions">
          <button type="button" class="btn-secondary" data-repo-action="refresh">Refresh</button>
          <button type="button" class="btn-secondary" data-repo-action="edit">Edit</button>
          <button type="button" class="btn-secondary btn-danger-lite" data-repo-action="remove">Remove</button>
        </div>
      </div>`;
    }).join('');
  }

  function renderNodeList() {
    const ctxs = contexts();
    for (const c of ctxs) if (selectedContexts.size === 0 && c.name === setupSel.value) selectedContexts.add(c.name);
    nodeScopeBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.nodeScope === nodeScope));
    if (nodeScope !== 'selected') {
      const label = nodeScope === 'all' ? 'All SSH nodes will receive the selected skills.' : 'Only the setup node will receive the selected skills.';
      nodeListEl.innerHTML = `<div class="empty-state">${esc(label)}</div>`;
      return;
    }
    if (!ctxs.length) {
      nodeListEl.innerHTML = '<div class="empty-state">No contexts yet. Add hosts in Nodes first.</div>';
      return;
    }
    nodeListEl.innerHTML = ctxs.map(c => {
      const ssh = isSshContext(c);
      const checked = selectedContexts.has(c.name) || c.name === setupSel.value;
      const disabled = ssh ? '' : ' disabled';
      return `<label class="skillm-node-row ${ssh ? '' : 'is-disabled'}">
        <input type="checkbox" data-context="${esc(c.name)}"${checked ? ' checked' : ''}${disabled}>
        <span class="skillm-node-main">
          <span class="skillm-node-name">${esc(c.name)}</span>
          <span class="skillm-node-meta">${esc(machineLabel(c))}</span>
        </span>
        <span class="skillm-node-path">${esc(contextPath(c) || '-')}</span>
      </label>`;
    }).join('');
  }

  function filteredSkills() {
    const q = (skillSearchEl.value || '').trim().toLowerCase();
    const repo = repoFilterEl.value || 'all';
    return skills.filter(s => {
      if (repo !== 'all' && skillRepoName(s) !== repo) return false;
      if (!q) return true;
      const hay = [s.name, skillRepoName(s), s.category, (s.tags || []).join(' '), s.commit].join(' ').toLowerCase();
      return hay.includes(q);
    });
  }

  function syncScopeVisibility() {
    if (workspaceRow) workspaceRow.hidden = scopeSel.value !== 'workspace';
  }

  function renderSkills() {
    const list = filteredSkills();
    if (!skills.length) {
      skillListEl.innerHTML = '<div class="empty-state">Open Install to refresh repositories and load skills.</div>';
      return;
    }
    if (!list.length) {
      skillListEl.innerHTML = '<div class="empty-state">No skills match this filter.</div>';
      return;
    }
    skillListEl.innerHTML = list.map(s => {
      const name = s.name || '';
      const tags = Array.isArray(s.tags) ? s.tags : [];
      const repo = skillRepoName(s) || 'repo';
      return `<label class="skillm-skill-row">
        <input type="checkbox" data-skill="${esc(name)}"${selectedSkills.has(name) ? ' checked' : ''}>
        <span class="skillm-skill-main">
          <span class="skillm-skill-name">${esc(shortSkillName(name))}</span>
          <span class="skillm-skill-full">${esc(name)}</span>
        </span>
        <span class="skillm-skill-tags"><span>${esc(repo)}</span>${tags.map(t => `<span>#${esc(t)}</span>`).join('')}</span>
        <span class="skillm-skill-meta">${esc(s.updated || '')}${s.size ? ' - ' + esc(s.size) : ''}</span>
      </label>`;
    }).join('');
  }

  function showRepoEditor(repo = null) {
    repoEditOldName = repo ? repo.name : '';
    repoNameEl.value = repo ? repo.name : (localStorage.getItem(REPO_NAME_KEY) || 'main');
    repoUrlEl.value = repo ? (repo.url || '') : (localStorage.getItem(REPO_URL_KEY) || '');
    repoTokenEl.value = '';
    repoEditor.hidden = false;
    repoNameEl.focus();
  }

  function hideRepoEditor() {
    repoEditOldName = '';
    repoTokenEl.value = '';
    repoEditor.hidden = true;
  }

  async function loadRepos() {
    const ctx = setupSel.value;
    if (!ctx) return;
    setLoading(true);
    setStatus('Loading repositories...');
    try {
      const res = await api.skillmRepos(ctx);
      repos = Array.isArray(res.repos) ? res.repos : [];
      renderRepos();
      setStatus(`Loaded ${repos.length} repository record(s).`, 'is-ok');
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(`Repository list failed: ${msg}`, 'is-error');
      showToast(`Repository list failed: ${msg}`, 'error', 5000);
    } finally {
      setLoading(false);
    }
  }

  async function loadSkills({ sync = false } = {}) {
    const ctx = setupSel.value;
    if (!ctx) return;
    setLoading(true);
    setStatus(sync ? 'Refreshing repositories and loading skills...' : 'Loading skills...');
    try {
      const repoName = repoFilterEl.value || 'all';
      const res = await api.skillmList(ctx, { repoName, sync });
      skills = dedupeSkills(res.skills);
      selectedSkills = new Set([...selectedSkills].filter(s => skills.some(x => x.name === s)));
      renderSkills();
      setStatus(`Loaded ${skills.length} skill(s) from ${repoName === 'all' ? 'all repositories' : repoName}.`, 'is-ok');
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(`Skill list failed: ${msg}`, 'is-error');
      showToast(`Skill list failed: ${msg}`, 'error', 5000);
    } finally {
      setLoading(false);
    }
  }

  checkBtn.addEventListener('click', async () => {
    if (!setupSel.value) return;
    setLoading(true);
    setStatus('Checking skillm...');
    try {
      const res = await api.skillmStatus(setupSel.value);
      if (res.installed) setStatus(`Skillm ready on ${setupSel.value}${res.version ? ': ' + res.version : ''}.`, 'is-ok');
      else setStatus(`Skillm missing on ${setupSel.value}: ${res.detail || res.error || 'not found'}`, 'is-error');
    } catch (err) {
      setStatus(`Check failed: ${err?.message || err}`, 'is-error');
    } finally {
      setLoading(false);
    }
  });

  refreshBtn.addEventListener('click', async () => {
    if (activeTab === 'repos') await loadRepos();
    else await loadSkills({ sync: true });
  });

  tabBtns.forEach(btn => btn.addEventListener('click', () => setTab(btn.dataset.skillmTab)));
  repoAddBtn.addEventListener('click', () => showRepoEditor(null));
  repoCancelBtn.addEventListener('click', hideRepoEditor);

  repoSaveBtn.addEventListener('click', async () => {
    const body = {
      contextName: setupSel.value,
      oldName: repoEditOldName,
      repoName: (repoNameEl.value || '').trim(),
      repoUrl: (repoUrlEl.value || '').trim(),
      token: repoTokenEl.value || '',
    };
    if (!body.contextName || !body.repoName || !body.repoUrl) {
      setStatus('Pick a node and provide repository name + URL.', 'is-error');
      return;
    }
    setLoading(true);
    setStatus(repoEditOldName ? `Updating repository ${repoEditOldName}...` : `Adding repository ${body.repoName}...`);
    try {
      localStorage.setItem(REPO_NAME_KEY, body.repoName);
      localStorage.setItem(REPO_URL_KEY, body.repoUrl);
      const res = repoEditOldName ? await api.skillmRepoUpdate(body) : await api.skillmRepoAdd(body);
      const ok = res.ok !== false;
      setStatus(ok ? `Repository ${body.repoName} ready.` : `Repository warning: ${res.detail || res.error}`, ok ? 'is-ok' : 'is-error');
      showToast(ok ? 'Repository ready' : 'Repository warning', ok ? 'success' : 'warning');
      hideRepoEditor();
      await loadRepos();
      await loadSkills({ sync: false });
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(`Repository save failed: ${msg}`, 'is-error');
      showToast(`Repository save failed: ${msg}`, 'error', 5000);
    } finally {
      repoTokenEl.value = '';
      setLoading(false);
    }
  });

  repoListEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-repo-action]');
    const row = e.target.closest('[data-repo]');
    if (!btn || !row) return;
    const repo = repos.find(r => r.name === row.dataset.repo);
    if (!repo) return;
    if (btn.dataset.repoAction === 'edit') return showRepoEditor(repo);
    if (btn.dataset.repoAction === 'remove') {
      if (!confirm(`Remove repository "${repo.name}" from ${setupSel.value}?`)) return;
      setLoading(true);
      setStatus(`Removing repository ${repo.name}...`);
      try {
        const res = await api.skillmRepoRemove({ contextName: setupSel.value, repoName: repo.name });
        setStatus(res.ok === false ? `Remove warning: ${res.detail || res.error}` : `Repository ${repo.name} removed.`, res.ok === false ? 'is-error' : 'is-ok');
        await loadRepos();
        await loadSkills({ sync: false });
      } catch (err) {
        setStatus(`Remove failed: ${err?.message || err}`, 'is-error');
      } finally {
        setLoading(false);
      }
      return;
    }
    if (btn.dataset.repoAction === 'refresh') {
      setLoading(true);
      setStatus(`Refreshing ${repo.name}...`);
      try {
        const res = await api.skillmRepoRefresh({ contextName: setupSel.value, repoName: repo.name });
        const summary = fmtResultCount(res.results);
        setStatus(`Refresh complete: ${summary}.`, res.ok === false ? 'is-error' : 'is-ok');
        await loadSkills({ sync: false });
      } catch (err) {
        setStatus(`Refresh failed: ${err?.message || err}`, 'is-error');
      } finally {
        setLoading(false);
      }
    }
  });

  nodeScopeBtns.forEach(btn => btn.addEventListener('click', () => {
    nodeScope = btn.dataset.nodeScope || 'setup';
    renderNodeList();
  }));
  nodeListEl.addEventListener('change', (e) => {
    const box = e.target.closest('input[type="checkbox"][data-context]');
    if (!box) return;
    if (box.checked) selectedContexts.add(box.dataset.context);
    else selectedContexts.delete(box.dataset.context);
  });

  skillSearchEl.addEventListener('input', renderSkills);
  repoFilterEl.addEventListener('change', () => { void loadSkills({ sync: true }); });
  scopeSel.addEventListener('change', syncScopeVisibility);
  skillListEl.addEventListener('change', (e) => {
    const box = e.target.closest('input[type="checkbox"][data-skill]');
    if (!box) return;
    if (box.checked) selectedSkills.add(box.dataset.skill);
    else selectedSkills.delete(box.dataset.skill);
  });
  selectAllBtn.addEventListener('click', () => {
    for (const s of filteredSkills()) selectedSkills.add(s.name);
    renderSkills();
  });
  selectNoneBtn.addEventListener('click', () => {
    selectedSkills.clear();
    renderSkills();
  });

  setupSel.addEventListener('change', async () => {
    const ctx = selectedContext();
    if (ctx) {
      selectedContexts.add(ctx.name);
      workspaceEl.value = contextPath(ctx);
    }
    renderNodeList();
    await loadRepos();
    if (activeTab === 'install') await loadSkills({ sync: true });
    syncButtons();
  });

  installBtn.addEventListener('click', async () => {
    const names = [...selectedSkills];
    const nodes = selectedNodeNames();
    const tools = selectedAgentTools();
    if (!names.length) return setStatus('Select at least one skill.', 'is-error');
    if (!nodes.length) return setStatus('Select at least one SSH node.', 'is-error');
    if (!tools.length) return setStatus('Select at least one agent CLI.', 'is-error');
    const scope = scopeSel.value === 'workspace' ? 'workspace' : 'global';
    const workspacePath = (workspaceEl.value || '').trim();
    if (scope === 'workspace' && !workspacePath) return setStatus('Workspace install requires a workspace path.', 'is-error');
    setLoading(true);
    setStatus(`Refreshing repositories and installing ${names.length} skill(s) on ${nodes.length} node(s)...`);
    try {
      const repoName = repoFilterEl.value || 'all';
      const res = await api.skillmInstall({ contexts: nodes, skills: names, agents: tools, scope, workspacePath, repoName });
      const summary = fmtResultCount(res.results);
      const ok = res.results && res.results.every(r => r.ok);
      setStatus(`Install complete: ${summary}.`, ok ? 'is-ok' : 'is-error');
      showToast(`Skill install: ${summary}`, ok ? 'success' : 'warning');
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(`Install failed: ${msg}`, 'is-error');
      showToast(`Install failed: ${msg}`, 'error', 5000);
    } finally {
      setLoading(false);
    }
  });

  let prevMode = state.get('mode');
  let prevConn = state.get('connectionMode');
  let prevContexts = state.get('contexts');
  renderContextOptions();
  renderRepos();
  renderSkills();
  syncScopeVisibility();
  syncButtons();
  state.subscribe(() => {
    const mode = state.get('mode');
    const conn = state.get('connectionMode');
    const ctxs = state.get('contexts');
    if (mode !== prevMode) {
      prevMode = mode;
      if (mode === 'skills') {
        renderContextOptions();
        void loadRepos();
        if (activeTab === 'install') void loadSkills({ sync: true });
      }
    }
    if (conn !== prevConn) { prevConn = conn; syncButtons(); }
    if (ctxs !== prevContexts) {
      prevContexts = ctxs;
      renderContextOptions();
    }
  });
}
