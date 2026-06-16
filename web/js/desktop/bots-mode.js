/**
 * Desktop Bots mode (CAM-DESK-BOTS-010..015).
 *
 * V0 is a read-only inventory/editor preview. It renders the bot bundle model
 * and CamFlow-package workflow model before the Hub bot APIs are wired.
 */

const BOT_FIXTURES = [
  {
    id: 'code-reviewer',
    name: 'Code Reviewer',
    version: '0.1.0',
    description: 'Reviews repository changes, runs a preflight checklist, and reports blockers first.',
    source: 'workspace',
    source_path: '<workspace>/.cam/bots/code-reviewer/.bot/bot.yaml',
    updated_at: '2026-06-15T09:20:00-07:00',
    last_used_at: '2026-06-15T10:05:00-07:00',
    tags: ['review', 'coding', 'risk'],
    target: { type: 'agent', default_tool: 'codex', default_workspace: '.' },
    settings: {
      auto_confirm: true,
      tags: ['review'],
      system_prompt_file: 'prompts/AGENTS.md',
    },
    assets: {
      files: [{ from: 'files/review-checklist.md', to: '.cam/bots/code-reviewer/review-checklist.md' }],
      scripts: [{ from: 'scripts/preflight.sh', to: '.cam/bots/code-reviewer/preflight.sh', executable: true }],
      configs: [{ from: 'configs/reviewer.json', to: '.cam/bots/code-reviewer/reviewer.json' }],
    },
    workflow: { file: 'workflows/workflow.yaml' },
    loops: [{ name: 'idle-review', schedule: 'every 30m', prompt_file: 'loops/review-loop.md' }],
    skills: { install: ['managing-cam', 'reviewer'] },
    mcp: { references: [] },
    launch: {
      prompt: 'Review the current workspace and report blockers first.',
      timeout: 3600,
      env_setup: '',
    },
  },
  {
    id: 'nightly-review-flow',
    name: 'Nightly Review Flow',
    version: '0.2.0',
    description: 'Frozen CamFlow package for nightly review, verification, and summary reporting.',
    source: 'camflow',
    source_path: '~/.camflow/packages/nightly_review@0.2.0',
    updated_at: '2026-06-14T21:00:00-07:00',
    last_used_at: '2026-06-14T22:12:00-07:00',
    tags: ['workflow', 'review', 'nightly'],
    target: { type: 'workflow', package: 'nightly_review@0.2.0', package_scope: 'user' },
    settings: { auto_confirm: true, tags: ['nightly'] },
    assets: { files: [], scripts: [], configs: [] },
    workflow: {
      package: 'nightly_review@0.2.0',
      entry: 'workflow.yaml',
      nodes: [
        { id: 'collect', title: 'Collect changes', run: 'skill: managing-cam', verify: 'Expected changed files and active agents are listed.' },
        { id: 'review', title: 'Review blockers', run: 'skill: reviewer', verify: 'Checklist items are complete and blockers are explicit.' },
        { id: 'report', title: 'Write summary', run: 'skill: send-email', verify: 'Summary includes result, risks, and next actions.' },
      ],
    },
    loops: [],
    skills: { install: ['managing-cam', 'reviewer', 'send-email'] },
    mcp: { references: ['mcp/review-tools.json'] },
    launch: { prompt: 'Run the nightly review package.', timeout: 7200, env_setup: '' },
  },
  {
    id: 'rtl-debugger',
    name: 'RTL Debugger',
    version: '0.1.0',
    description: 'Prepared Verilog/SystemVerilog debug agent with waveform and regression helper skills.',
    source: 'git',
    source_path: 'git@example.com:cam/bots.git#rtl-debugger',
    updated_at: '2026-06-12T15:30:00-07:00',
    last_used_at: '',
    tags: ['rtl', 'debug', 'verification'],
    target: { type: 'agent', default_tool: 'claude', default_workspace: '.' },
    settings: {
      auto_confirm: false,
      tags: ['rtl-debug'],
      system_prompt_file: 'prompts/CLAUDE.md',
    },
    assets: {
      files: [{ from: 'files/debug-template.md', to: '.cam/bots/rtl-debugger/debug-template.md' }],
      scripts: [],
      configs: [{ from: 'configs/wave.json', to: '.cam/bots/rtl-debugger/wave.json' }],
    },
    workflow: { file: 'workflows/debug.yaml' },
    loops: [{ name: 'watch-regression', schedule: 'every 15m', prompt_file: 'loops/watch.md' }],
    skills: { install: ['vmod-debug', 'wavedbg', 'run-regression'] },
    mcp: { references: ['mcp/wave-tools.json'] },
    launch: { prompt: 'Debug the selected RTL failure and produce the shortest repro.', timeout: 5400, env_setup: 'source setup.sh' },
  },
];

const SOURCE_LABELS = {
  workspace: 'Workspace',
  node: 'Node',
  camflow: 'CamFlow package',
  git: 'Git library',
};

const TARGET_LABELS = {
  agent: 'Agent',
  workflow: 'Workflow',
};

function esc(value) {
  const d = document.createElement('div');
  d.textContent = String(value == null ? '' : value);
  return d.innerHTML;
}

function dateMs(value) {
  const t = Date.parse(value || '');
  return Number.isFinite(t) ? t : 0;
}

function botSourceLabel(bot) {
  return SOURCE_LABELS[bot.source] || bot.source || 'Unknown';
}

function botTargetLabel(bot) {
  const type = bot && bot.target && bot.target.type;
  return TARGET_LABELS[type] || type || 'Unknown';
}

function botAssetSummary(bot) {
  const assets = bot.assets || {};
  const skills = (bot.skills && bot.skills.install) || [];
  const mcp = (bot.mcp && bot.mcp.references) || [];
  const counts = [
    ['Files', (assets.files || []).length],
    ['Scripts', (assets.scripts || []).length],
    ['Configs', (assets.configs || []).length],
    ['Workflow', bot.workflow ? 1 : 0],
    ['Loops', (bot.loops || []).length],
    ['Skills', skills.length],
    ['MCP', mcp.length],
  ];
  return counts.map(([label, count]) => `${label} ${count}`).join(' · ');
}

function manifestYaml(bot) {
  return `schema: cam-bot/1\nid: ${bot.id}\nname: ${bot.name}\nversion: ${bot.version || '0.1.0'}\ndescription: ${bot.description || ''}\ntags: [${(bot.tags || []).join(', ')}]\nsource:\n  type: ${bot.source}\n  path: ${bot.source_path}\ntarget:\n  type: ${bot.target?.type || 'agent'}${bot.target?.default_tool ? `\n  default_tool: ${bot.target.default_tool}` : ''}${bot.target?.package ? `\n  package: ${bot.target.package}\n  package_scope: ${bot.target.package_scope || 'user'}` : ''}\nsettings:\n  auto_confirm: ${!!bot.settings?.auto_confirm}\n  tags: [${(bot.settings?.tags || []).join(', ')}]\n  system_prompt_file: ${bot.settings?.system_prompt_file || ''}\nworkflow:\n  file: ${bot.workflow?.file || bot.workflow?.entry || ''}\n  package: ${bot.workflow?.package || ''}\nlaunch:\n  prompt: ${bot.launch?.prompt || ''}\n  timeout: ${bot.launch?.timeout || ''}\n`;
}

function detailRows(items) {
  if (!items || !items.length) return '<div class="empty-state bots-empty-mini">None declared.</div>';
  return items.map((item) => `<div class="bots-detail-row">
    <span>${esc(item.from || item.name || item)}</span>
    <code>${esc(item.to || item.schedule || item.prompt_file || item)}</code>
    ${item.executable ? '<span class="bots-pill">executable</span>' : ''}
  </div>`).join('');
}

function botDryRun(bot) {
  const assets = bot.assets || {};
  const skills = (bot.skills && bot.skills.install) || [];
  const lines = [];
  lines.push(`Validate bot manifest: ${bot.id}`);
  lines.push(`Resolve target ${botTargetLabel(bot).toLowerCase()} on selected context/workspace`);
  for (const f of assets.files || []) lines.push(`Copy file ${f.from} -> ${f.to}`);
  for (const s of assets.scripts || []) lines.push(`Copy script ${s.from} -> ${s.to}${s.executable ? ' and chmod +x' : ''}`);
  for (const c of assets.configs || []) lines.push(`Copy config ${c.from} -> ${c.to}`);
  if (bot.settings?.system_prompt_file) lines.push(`Apply system prompt from ${bot.settings.system_prompt_file}`);
  if (bot.workflow?.package) lines.push(`Validate CamFlow package ${bot.workflow.package}`);
  else if (bot.workflow?.file) lines.push(`Attach workflow file ${bot.workflow.file}`);
  for (const loop of bot.loops || []) lines.push(`Create opt-in loop ${loop.name} (${loop.schedule})`);
  if (skills.length) lines.push(`Install skills: ${skills.join(', ')}`);
  if (bot.target?.type === 'workflow' && bot.target?.package) lines.push(`Launch with camflow run --package ${bot.target.package}`);
  else lines.push(`Start ${bot.target?.default_tool || 'agent'} with prepared prompt`);
  return lines;
}

export function mountBotsMode({ state, showToast } = {}) {
  const panel = document.getElementById('mode-bots');
  if (!panel) return;

  const searchEl = panel.querySelector('#bots-search');
  const targetEl = panel.querySelector('#bots-target-filter');
  const sourceEl = panel.querySelector('#bots-source-filter');
  const sortEl = panel.querySelector('#bots-sort');
  const refreshBtn = panel.querySelector('#bots-refresh');
  const importBtn = panel.querySelector('#bots-import');
  const listEl = panel.querySelector('#bots-list');
  const detailEl = panel.querySelector('#bots-detail');
  const statusEl = panel.querySelector('#bots-status');

  let bots = BOT_FIXTURES.slice();
  let selectedId = bots[0]?.id || '';
  let activeTab = 'overview';

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function filteredBots() {
    const q = (searchEl.value || '').trim().toLowerCase();
    const target = targetEl.value || 'all';
    const source = sourceEl.value || 'all';
    const sort = sortEl.value || 'recent';
    const out = bots.filter((bot) => {
      if (target !== 'all' && bot.target?.type !== target) return false;
      if (source !== 'all' && bot.source !== source) return false;
      if (!q) return true;
      const hay = [bot.id, bot.name, bot.description, bot.source_path, bot.source, bot.target?.type, ...(bot.tags || [])]
        .join(' ').toLowerCase();
      return hay.includes(q);
    });
    out.sort((a, b) => {
      if (sort === 'name') return String(a.name || '').localeCompare(String(b.name || ''));
      if (sort === 'updated') return dateMs(b.updated_at) - dateMs(a.updated_at) || String(a.name).localeCompare(String(b.name));
      return dateMs(b.last_used_at || b.updated_at) - dateMs(a.last_used_at || a.updated_at) || String(a.name).localeCompare(String(b.name));
    });
    return out;
  }

  function renderList() {
    const list = filteredBots();
    if (!list.length) {
      listEl.innerHTML = '<div class="empty-state">No bots match this filter.</div>';
      renderDetail();
      return;
    }
    if (!list.some((b) => b.id === selectedId)) selectedId = list[0].id;
    listEl.innerHTML = list.map((bot) => `<article class="bots-row ${bot.id === selectedId ? 'active' : ''}" data-bot-id="${esc(bot.id)}">
      <div class="bots-row-main">
        <div class="bots-row-title">
          <strong>${esc(bot.name)}</strong>
          <span class="bots-pill">${esc(botTargetLabel(bot))}</span>
          <span class="bots-pill muted">${esc(bot.target?.default_tool || bot.target?.package || bot.source)}</span>
        </div>
        <p>${esc(bot.description)}</p>
        <div class="bots-tags">${(bot.tags || []).map((t) => `<span>#${esc(t)}</span>`).join('')}</div>
        <div class="bots-row-meta">${esc(botAssetSummary(bot))}</div>
      </div>
      <div class="bots-row-side">
        <span class="bots-source">${esc(botSourceLabel(bot))}</span>
        <button type="button" class="btn-secondary" data-bots-action="dry-run" data-bot-id="${esc(bot.id)}">Dry run</button>
        <button type="button" class="btn-primary" data-bots-action="launch" data-bot-id="${esc(bot.id)}">Launch</button>
      </div>
    </article>`).join('');
    renderDetail();
  }

  function selectedBot() {
    return bots.find((b) => b.id === selectedId) || null;
  }

  function renderTabContent(bot) {
    if (activeTab === 'settings') {
      return `<div class="bots-detail-grid">
        <div><span>Tool</span><strong>${esc(bot.target?.default_tool || '(workflow package)')}</strong></div>
        <div><span>Auto-confirm</span><strong>${bot.settings?.auto_confirm ? 'Yes' : 'No'}</strong></div>
        <div><span>System prompt</span><code>${esc(bot.settings?.system_prompt_file || 'none')}</code></div>
        <div><span>Launch timeout</span><strong>${esc(bot.launch?.timeout || 'default')}</strong></div>
      </div>
      <section class="bots-subcard"><h4>Launch prompt</h4><pre>${esc(bot.launch?.prompt || '')}</pre></section>`;
    }
    if (activeTab === 'files') {
      return `<section class="bots-subcard"><h4>Files</h4>${detailRows(bot.assets?.files)}</section>
        <section class="bots-subcard"><h4>Scripts</h4>${detailRows(bot.assets?.scripts)}</section>
        <section class="bots-subcard"><h4>Configs</h4>${detailRows(bot.assets?.configs)}</section>`;
    }
    if (activeTab === 'workflow') {
      const nodes = bot.workflow?.nodes || [];
      const nodeHtml = nodes.length ? nodes.map((n, idx) => `<div class="bots-flow-node">
        <span>${idx + 1}</span><div><strong>${esc(n.title || n.id)}</strong><small>${esc(n.run || '')}</small><small>Verify: ${esc(n.verify || 'prompt evaluator')}</small></div>
      </div>`).join('') : '<div class="empty-state bots-empty-mini">Workflow renderer will load the bundle workflow YAML when Hub APIs are wired.</div>';
      return `<section class="bots-subcard"><h4>Workflow source</h4><code>${esc(bot.workflow?.package || bot.workflow?.file || 'none')}</code></section>
        <section class="bots-subcard bots-flow"><h4>Flow preview</h4>${nodeHtml}</section>`;
    }
    if (activeTab === 'loops') {
      return `<section class="bots-subcard"><h4>Loops</h4>${detailRows(bot.loops)}</section>`;
    }
    if (activeTab === 'skills') {
      return `<section class="bots-subcard"><h4>Requested skills</h4>${detailRows(bot.skills?.install || [])}</section>`;
    }
    if (activeTab === 'mcp') {
      return `<section class="bots-subcard"><h4>MCP references</h4>${detailRows(bot.mcp?.references || [])}</section>`;
    }
    if (activeTab === 'raw') {
      return `<section class="bots-subcard"><h4>Raw manifest</h4><pre>${esc(manifestYaml(bot))}</pre></section>`;
    }
    return `<div class="bots-detail-grid">
      <div><span>ID</span><strong>${esc(bot.id)}</strong></div>
      <div><span>Version</span><strong>${esc(bot.version || '-')}</strong></div>
      <div><span>Target</span><strong>${esc(botTargetLabel(bot))}</strong></div>
      <div><span>Source</span><strong>${esc(botSourceLabel(bot))}</strong></div>
      <div class="wide"><span>Source path</span><code>${esc(bot.source_path)}</code></div>
      <div class="wide"><span>Assets</span><strong>${esc(botAssetSummary(bot))}</strong></div>
    </div>`;
  }

  function renderDetail() {
    const bot = selectedBot();
    if (!bot) {
      detailEl.innerHTML = '<div class="empty-state">Select a bot to inspect its launch plan.</div>';
      return;
    }
    const tabs = [
      ['overview', 'Overview'],
      ['settings', 'Settings'],
      ['files', 'Files & Scripts'],
      ['workflow', 'Workflow'],
      ['loops', 'Loops'],
      ['skills', 'Skills'],
      ['mcp', 'MCP'],
      ['raw', 'Raw'],
    ];
    detailEl.innerHTML = `<div class="bots-detail-head">
      <div>
        <h3>${esc(bot.name)}</h3>
        <p>${esc(bot.description)}</p>
      </div>
      <div class="bots-detail-actions">
        <button type="button" class="btn-secondary" data-bots-action="dry-run" data-bot-id="${esc(bot.id)}">Dry run</button>
        <button type="button" class="btn-primary" data-bots-action="launch" data-bot-id="${esc(bot.id)}">Launch</button>
      </div>
    </div>
    <div class="bots-detail-tabs" role="tablist" aria-label="Bot detail tabs">
      ${tabs.map(([key, label]) => `<button type="button" class="bots-tab ${key === activeTab ? 'active' : ''}" data-bots-tab="${key}" aria-selected="${key === activeTab ? 'true' : 'false'}">${esc(label)}</button>`).join('')}
    </div>
    <div class="bots-detail-body">${renderTabContent(bot)}</div>`;
  }

  function showDryRun(bot) {
    activeTab = 'overview';
    renderDetail();
    const lines = botDryRun(bot);
    setStatus(`Dry run preview for ${bot.name}: ${lines.length} action(s).`, 'is-ok');
    detailEl.querySelector('.bots-detail-body').insertAdjacentHTML('afterbegin', `<section class="bots-subcard bots-dryrun"><h4>Dry-run action preview</h4><ol>${lines.map((l) => `<li>${esc(l)}</li>`).join('')}</ol></section>`);
  }

  function handleAction(action, botId) {
    const bot = bots.find((b) => b.id === botId);
    if (!bot) return;
    selectedId = bot.id;
    renderList();
    if (action === 'dry-run') {
      showDryRun(bot);
      return;
    }
    if (action === 'launch') {
      setStatus('Launch is staged for the Hub bot API slice. Use Dry run to inspect the planned actions first.', 'is-error');
      if (showToast) showToast('Bot launch API is not wired yet.', 'error', 3500);
    }
  }

  listEl.addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-bots-action]');
    if (actionBtn) {
      handleAction(actionBtn.dataset.botsAction, actionBtn.dataset.botId);
      return;
    }
    const row = e.target.closest('[data-bot-id]');
    if (!row) return;
    selectedId = row.dataset.botId;
    activeTab = 'overview';
    setStatus('');
    renderList();
  });

  detailEl.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-bots-tab]');
    if (tab) {
      activeTab = tab.dataset.botsTab || 'overview';
      renderDetail();
      return;
    }
    const actionBtn = e.target.closest('[data-bots-action]');
    if (actionBtn) handleAction(actionBtn.dataset.botsAction, actionBtn.dataset.botId);
  });

  [searchEl, targetEl, sourceEl, sortEl].forEach((el) => {
    if (el) el.addEventListener('input', () => { setStatus(''); renderList(); });
  });
  refreshBtn.addEventListener('click', () => {
    bots = BOT_FIXTURES.slice();
    setStatus('Loaded fixture-backed Bots v0. Hub inventory will replace this list in the next backend slice.', 'is-ok');
    renderList();
  });
  importBtn.addEventListener('click', () => {
    setStatus('Import Bot will be wired with repository/folder selection after GET/POST /api/bots lands.', 'is-error');
  });

  state?.subscribe?.(() => {
    if (state.get('mode') === 'bots') renderList();
  });

  renderList();
  setStatus('Bots v0 uses local fixtures to review the UI and manifest model before Hub APIs are connected.');
}
