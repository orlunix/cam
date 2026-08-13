/* agent-doctor-mode.js — native Agent Doctor page (skills-style).
 *
 * The agent-doctor EXTENSION (extensions/packages/agent-doctor) carries
 * only the remote collector (main.py, a SPEC v2 type-B tool). This
 * native page is its view — same layout language as the Skills page,
 * no iframe. Header title + one-liner come from the extension package
 * (manifest title / description via /api/extensions), with the static
 * HTML text as fallback.
 *
 * Entry points (both end in setMode('agent-doctor') by the caller):
 *   - agent console Ext menu → setDoctorAgent(agent, { returnTo: 'agents' })
 *     (agent preselected, check auto-runs once)
 *   - Extensions → Open      → setDoctorAgent(null,  { returnTo: 'extensions' })
 *     (user picks an agent, then Run check)
 *
 * IMPORTANT: every importer must use the same ?v= query so the browser
 * loads ONE module instance — a versionless duplicate import silently
 * breaks the setDoctorAgent handoff (two private states).
 */

const WS_PROMPT_CANDIDATES = ['CLAUDE.md', 'AGENTS.md'];
const KB = 1024;
const PROMPT_WARN_BYTES = 8 * KB;

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

/* ── handoff state (module-private) ── */
let _pendingAgentId = null;
let _returnTo = 'agents';
let _autoRun = false;
let _applyHandoff = null; // bound by mount once the DOM exists

export function setDoctorAgent(agent, opts = {}) {
  _pendingAgentId = agent && agent.id ? String(agent.id) : null;
  _returnTo = (opts && opts.returnTo) || 'agents';
  _autoRun = !!agent;
  if (_applyHandoff) _applyHandoff();
}

export function mountAgentDoctorMode({ api, state, showToast, setMode }) {
  const panel = document.getElementById('mode-agent-doctor');
  if (!panel) return;

  const backBtn     = panel.querySelector('#doctor-back');
  const runBtn      = panel.querySelector('#doctor-run');
  const agentSel    = panel.querySelector('#doctor-agent');
  const statusEl    = panel.querySelector('#doctor-status');
  const titleEl     = panel.querySelector('#doctor-title');
  const subtitleEl  = panel.querySelector('#doctor-subtitle');
  const reportEl    = panel.querySelector('#doctor-report');

  let running = false;
  let lastOptionKey = '';

  function agents() { return state.get('agents') || []; }

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function populateAgents() {
    const list = agents();
    const key = list.map(a => a && a.id).join('|');
    const cur = agentSel.value;
    // Rebuild options only when the id set actually changed — the agent
    // list refreshes on a poll cadence and must not reset the user's
    // selection (or scroll) mid-use.
    if (key !== lastOptionKey) {
      lastOptionKey = key;
      agentSel.innerHTML = list.length
        ? list.map(a => `<option value="${esc(a.id)}">${esc(a.task_name || a.id)} · ${esc(a.context_name || 'local')}</option>`).join('')
        : '<option value="">(no agents)</option>';
    }
    if (_pendingAgentId && list.some(a => a && a.id === _pendingAgentId)) {
      agentSel.value = _pendingAgentId;
    } else if (cur && list.some(a => a && a.id === cur)) {
      agentSel.value = cur;
    }
  }

  _applyHandoff = () => {
    populateAgents();
    if (_autoRun) {
      _autoRun = false;
      runCheck();
    }
  };

  // Keep the picker fresh while the page is open (cheap: keyed rebuild).
  state.subscribe(() => {
    if (state.get('mode') === 'agent-doctor') populateAgents();
  });

  if (backBtn) backBtn.addEventListener('click', () => {
    if (typeof setMode === 'function') setMode(_returnTo || 'agents');
  });
  if (runBtn) runBtn.addEventListener('click', runCheck);

  // Header from the extension package (manifest title / description).
  // Falls back to the static HTML text when unavailable.
  (async () => {
    try {
      const r = await api.listExtensions();
      const meta = ((r && r.extensions) || []).find(x => x && x.name === 'agent-doctor');
      if (!meta) return;
      if (titleEl && meta.title) titleEl.textContent = meta.title;
      if (subtitleEl && meta.description) subtitleEl.textContent = meta.description;
    } catch (_) { /* keep static fallback */ }
  })();

  /* ── data collection ── */

  async function readWorkspacePrompt(agentId) {
    for (const f of WS_PROMPT_CANDIDATES) {
      try {
        const r = await api.agentReadWorkspaceFile(agentId, f);
        if (r && r.content) return { file: f, size: r.content.length };
      } catch (_) { /* file absent — try next candidate */ }
    }
    return null;
  }

  /* ── rendering (rules ported from the retired iframe view) ── */

  function row(level, text, suggestion) {
    const icon = level === 'ok' ? '✓' : level === 'warn' ? '⚠' : '✗';
    return `<div class="doctor-row is-${level}"><span class="doctor-row-icon">${icon}</span><span>${esc(text)}</span></div>`
      + (suggestion ? `<div class="doctor-sug">→ ${esc(suggestion)}</div>` : '');
  }

  function section(title, rows) {
    return `<section class="skills-card"><h3 class="doctor-card-title">${esc(title)}</h3>${rows.join('')}</section>`;
  }

  function renderReport({ tool, facts, factsError, wsPrompt, cron }) {
    const out = [];

    // ── system prompt ──
    {
      const rows = [];
      if (wsPrompt) {
        rows.push(row('ok', `${wsPrompt.file} present (${(wsPrompt.size / KB).toFixed(1)}KB)`));
        if (wsPrompt.size > PROMPT_WARN_BYTES) {
          rows.push(row('warn',
            `system prompt is ${(wsPrompt.size / KB).toFixed(1)}KB — heavy token tax on every call`,
            'trim to < 8KB; move reference material to files the agent reads on demand'));
        }
      } else {
        rows.push(row('warn', 'no CLAUDE.md / AGENTS.md in workspace',
          'add one — a short system prompt anchors the agent'));
      }
      out.push(section('System prompt', rows));
    }

    // ── host facts (remote collector) ──
    if (factsError) {
      out.push(section('Host facts', [row('bad', `collector: ${factsError}`)]));
    } else if (facts && facts.error) {
      out.push(section('Host facts', [row('bad',
        `collector: ${facts.error}${facts.detail ? ' — ' + facts.detail : ''}`,
        facts.error === 'no_adapter'
          ? `no adapter for "${tool}" — install one via install_adapter (see extensions GUIDE)`
          : '')]));
    } else if (facts) {
      const hp = facts.home_paths || {};
      {
        const rows = [];
        const mem = hp.global_memory;
        if (mem) {
          rows.push(mem.exists
            ? row('ok', `global memory present (${(mem.size / KB).toFixed(1)}KB)`)
            : row('warn', 'no global memory file',
                'create one so the agent keeps durable preferences across sessions'));
        }
        const sess = facts.sessions || {};
        rows.push(row('ok', `sessions: ${sess.count || 0}${sess.newest_mtime ? ' (latest ' + new Date(sess.newest_mtime * 1000).toISOString().slice(0, 10) + ')' : ''}`));
        out.push(section('Memory & sessions', rows));
      }
      {
        const rows = [];
        const sk = facts.skills || [];
        rows.push(sk.length
          ? row('ok', `${sk.length} skill(s): ${sk.slice(0, 6).join(', ')}${sk.length > 6 ? '…' : ''}`)
          : row('warn', 'no skills installed', 'install skills via the Skills page'));
        out.push(section('Skills', rows));
      }
      {
        const rows = [];
        const servers = facts.mcp_servers || [];
        if (facts.mcp_error) rows.push(row('warn', `MCP config unreadable: ${facts.mcp_error}`));
        else rows.push(servers.length
          ? row('ok', `${servers.length} MCP server(s): ${servers.join(', ')}`)
          : row('ok', 'no MCP servers configured'));
        out.push(section('MCP', rows));
      }
    }

    // ── cron / loops ──
    {
      const rows = [];
      const jobs = (cron && (cron.jobs || cron)) || [];
      if (!jobs.length) rows.push(row('ok', 'no cron jobs'));
      for (const j of jobs.slice(0, 10)) {
        const noTimeout = !j.timeout_seconds && !j.timeout;
        rows.push(row(noTimeout ? 'warn' : 'ok',
          `${j.name || j.id || 'job'} — ${j.cron || j.schedule || ''}${noTimeout ? ' (no timeout)' : ''}`,
          noTimeout ? 'set a timeout so a hung loop does not pile up' : ''));
      }
      out.push(section('Cron / loops', rows));
    }

    reportEl.innerHTML = out.join('');
  }

  /* ── run ── */

  async function runCheck() {
    if (running) return;
    const a = agents().find(x => x && x.id === agentSel.value);
    if (!a) {
      setStatus('Select an agent first.', 'is-error');
      return;
    }
    running = true;
    if (runBtn) runBtn.disabled = true;
    const tool = String(a.tool || '').toLowerCase();
    const target = `${a.task_name || a.id} · tool: ${tool || '?'} · host: ${a.context_name || '?'}`;
    setStatus(`Checking ${target} …`);
    reportEl.innerHTML = '';

    const [factsR, wsPromptR, cronR] = await Promise.allSettled([
      api.extCall('agent-doctor', a.context_name || '', 'collect', { tool }),
      readWorkspacePrompt(a.id),
      api.agentCronJobs(a.id),
    ]);

    renderReport({
      tool,
      facts:      factsR.status === 'fulfilled' ? factsR.value : null,
      factsError: factsR.status === 'rejected' ? (factsR.reason && factsR.reason.message || 'ext_call failed') : '',
      wsPrompt:   wsPromptR.status === 'fulfilled' ? wsPromptR.value : null,
      cron:       cronR.status === 'fulfilled' ? cronR.value : null,
    });
    setStatus(`Done — ${target}.`, 'is-ok');
    running = false;
    if (runBtn) runBtn.disabled = false;
  }

  // Initial paint (cold entry straight into the mode is coerced away by
  // app.js — doctor is not a persisted mode — so this is mainly for the
  // handoff paths above).
  populateAgents();
}
