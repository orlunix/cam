/**
 * Desktop sidebar/list — renders the agent list, status/tool filters,
 * and selection. Selection writes `selectedAgentId` to AppState; the
 * agent console listens for changes.
 */

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function escapeAttr(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;');
}

function timeSince(dateStr) {
  if (!dateStr) return '';
  const s = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (s < 0) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function agentName(agent) {
  return String(agent?.task_name || agent?.task?.name || agent?.id?.slice(0, 8) || '');
}

function _dateMs(raw) {
  if (!raw) return 0;
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : 0;
}

/* ─────────── Local Desktop user-activity overlay (user_activity_at)
 *
 * Remote camc `updated_at` is unreliable as a "user activity"
 * signal — it reflects tmux capture activity, not the user
 * actually interacting with the agent. This overlay is
 * renderer-only:
 *   - `_userActivity` is the in-memory `Map<agentId, ms>`.
 *   - Hydrated from localStorage on module load (best effort;
 *     missing/corrupt → empty map).
 *   - `bumpUserActivity(agentId, ts?)` updates an entry to
 *     `ts` (default Date.now()) and persists.
 *   - `agentUserActivityMs(agent)` reads back the overlay,
 *     falling back to `created_at || started_at` (NOT to remote
 *     `updated_at`) so a brand-new agent has a sensible default.
 *   - Remote `updated_at` is exposed separately as
 *     `agentTmuxActivityMs(agent)` if a caller wants to display
 *     the raw tmux-activity timestamp.
 * Nothing in this overlay is ever sent back to remote camc.
 *
 * The persisted map is capped (oldest entries dropped) so long
 * uptime can't grow localStorage past the browser quota.
 */
const USER_ACTIVITY_KEY = 'cam_desktop_user_activity_at';
const USER_ACTIVITY_CAP = 1000;

const _userActivity = (function _loadUserActivity() {
  const m = new Map();
  try {
    const raw = localStorage.getItem(USER_ACTIVITY_KEY);
    if (raw) {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === 'object') {
        for (const k of Object.keys(obj)) {
          const v = Number(obj[k]);
          if (Number.isFinite(v) && v > 0) m.set(String(k), v);
        }
      }
    }
  } catch { /* corrupt cache → fresh map */ }
  return m;
})();

function _persistUserActivity() {
  try {
    let entries = [..._userActivity.entries()];
    if (entries.length > USER_ACTIVITY_CAP) {
      entries.sort((a, b) => b[1] - a[1]);
      entries = entries.slice(0, USER_ACTIVITY_CAP);
      _userActivity.clear();
      for (const [k, v] of entries) _userActivity.set(k, v);
    }
    const obj = Object.fromEntries(entries);
    localStorage.setItem(USER_ACTIVITY_KEY, JSON.stringify(obj));
  } catch { /* quota / private mode → in-memory only */ }
}

/** Bump local user-activity for an agent. Safe to call from any
 *  module — agent-console.js calls this on send/key/upload, shell.js
 *  calls it on selection / row click. */
export function bumpUserActivity(agentId, ts = Date.now()) {
  if (!agentId) return;
  const v = Number.isFinite(ts) ? ts : Date.now();
  _userActivity.set(String(agentId), v);
  _persistUserActivity();
}

/** Read user activity (renderer-only). Returns 0 if unset and the
 *  agent has no created/started fallback. */
export function agentUserActivityMs(agent) {
  if (!agent) return 0;
  const id = String(agent.id || '');
  if (id) {
    const v = _userActivity.get(id);
    if (Number.isFinite(v) && v > 0) return v;
  }
  return _dateMs(agent.created_at) || _dateMs(agent.started_at);
}

/** Raw remote `updated_at` — surfaced for callers that want
 *  "tmux activity" specifically. Distinct from
 *  `agentUserActivityMs`. */
export function agentTmuxActivityMs(agent) {
  if (!agent) return 0;
  return _dateMs(agent.updated_at);
}

function agentUpdatedAt(agent) {
  // Display string ("3m ago") in the agent row. Prefer the local
  // user-activity overlay first; fall back to remote tmux activity
  // then lifecycle stamps so a freshly-imported agent still shows
  // something.
  if (!agent) return '';
  const id = String(agent.id || '');
  if (id) {
    const v = _userActivity.get(id);
    if (Number.isFinite(v) && v > 0) return new Date(v).toISOString();
  }
  return agent.updated_at || agent.last_active_at || agent.last_seen_at
      || agent.activity_at || agent.modified_at || agent.mtime
      || agent.completed_at || agent.started_at || agent.created_at || '';
}

function agentDateMs(agent) {
  // Legacy: best-available recency stamp. Uses the user-activity
  // overlay.
  return agentUserActivityMs(agent);
}

function agentCreatedMs(agent) {
  return _dateMs(agent?.created_at) || _dateMs(agent?.started_at)
      || _dateMs(agent?.updated_at) || _dateMs(agent?.completed_at);
}

// Sort string is stored in filters.sort as "<field>-<dir>".
//   - 'user_activity' is the renderer-local user-interaction
//     timestamp (see `agentUserActivityMs`).
//   - 'created' is the agent's created_at (or started_at fallback).
//   - 'name' is the task name.
// Legacy values 'date-desc' / 'date-asc' / 'updated-desc' /
// 'updated-asc' migrate transparently to 'user_activity-…' so
// existing AppState snapshots keep working without a flag day.
const SORT_DEFAULT = 'user_activity-desc';
function parseSort(s) {
  let raw = String(s || SORT_DEFAULT);
  if (raw === 'date-desc')    raw = 'user_activity-desc';
  if (raw === 'date-asc')     raw = 'user_activity-asc';
  if (raw === 'updated-desc') raw = 'user_activity-desc';
  if (raw === 'updated-asc')  raw = 'user_activity-asc';
  const idx = raw.lastIndexOf('-');
  const field = idx > 0 ? raw.slice(0, idx) : 'user_activity';
  const dir   = idx > 0 ? raw.slice(idx + 1) : 'desc';
  const f = ['user_activity', 'created', 'name'].includes(field) ? field : 'user_activity';
  const d = dir === 'asc' ? 'asc' : 'desc';
  return { field: f, dir: d, value: `${f}-${d}` };
}

function agentTags(agent) {
  const tags = [];
  if (Array.isArray(agent?.tags)) tags.push(...agent.tags);
  if (Array.isArray(agent?.task?.tags)) tags.push(...agent.task.tags);
  if (Array.isArray(agent?.task_tags)) tags.push(...agent.task_tags);
  return [...new Set(tags.map(t => String(t || '').trim()).filter(Boolean))];
}

export function mountShell({ api, state, connect }) {
  const listWrap = document.getElementById('agent-list-wrap');
  const statusSel = document.getElementById('filter-status');
  const toolSel = document.getElementById('filter-tool');
  const machineSel = document.getElementById('filter-machine');
  const tagSel = document.getElementById('filter-tag');
  const sortFieldSel = document.getElementById('sort-field');
  const sortDirSel = document.getElementById('sort-dir');
  const menuBtn = document.getElementById('agents-menu-btn');
  const popover = document.getElementById('agents-menu-popover');
  const chipsEl = document.getElementById('agents-filter-chips');
  const clearBtn = document.getElementById('agents-menu-clear');

  // Restore filter state if AppState already had a value from prior view.
  // Legacy 'date-desc'/'date-asc' is migrated transparently by parseSort.
  const stored = state.get('filters') || {};
  const initialSort = parseSort(stored.sort).value;
  const filters = {
    status: '', tool: '', machine: '', tag: '',
    ...stored,
    sort: initialSort,
  };
  if (filters.status) statusSel.value = filters.status;
  if (filters.tool) toolSel.value = filters.tool;
  if (sortFieldSel && sortDirSel) {
    const { field, dir } = parseSort(initialSort);
    sortFieldSel.value = field;
    sortDirSel.value = dir;
  }
  // Write back the migrated sort so any other consumer sees the new value.
  if (initialSort !== stored.sort) {
    state.set('filters', { ...stored, sort: initialSort });
  }
  // machine/tag values are restored inside refresh*FilterOptions()
  // after the option lists are populated from the current agent set.

  statusSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), status: statusSel.value });
  });
  toolSel.addEventListener('change', () => {
    state.set('filters', { ...state.get('filters'), tool: toolSel.value });
  });
  if (machineSel) {
    machineSel.addEventListener('change', () => {
      state.set('filters', { ...state.get('filters'), machine: machineSel.value });
    });
  }
  if (tagSel) {
    tagSel.addEventListener('change', () => {
      state.set('filters', { ...state.get('filters'), tag: tagSel.value });
    });
  }
  function commitSort() {
    const field = (sortFieldSel && sortFieldSel.value) || 'user_activity';
    const dir = (sortDirSel && sortDirSel.value) || 'desc';
    state.set('filters', { ...state.get('filters'), sort: `${field}-${dir}` });
  }
  if (sortFieldSel) sortFieldSel.addEventListener('change', commitSort);
  if (sortDirSel) sortDirSel.addEventListener('change', commitSort);

  /* ── Popover open/close ── */
  function openPopover() {
    if (!popover || !menuBtn) return;
    popover.hidden = false;
    menuBtn.setAttribute('aria-expanded', 'true');
  }
  function closePopover() {
    if (!popover || !menuBtn) return;
    popover.hidden = true;
    menuBtn.setAttribute('aria-expanded', 'false');
  }
  function togglePopover() {
    if (!popover) return;
    if (popover.hidden) openPopover(); else closePopover();
  }
  if (menuBtn) {
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePopover();
    });
  }
  if (popover) {
    // Clicks inside the popover should not close it.
    popover.addEventListener('click', (e) => e.stopPropagation());
  }
  // Click outside → close.
  document.addEventListener('click', (e) => {
    if (!popover || popover.hidden) return;
    if (e.target === menuBtn || (menuBtn && menuBtn.contains(e.target))) return;
    if (popover.contains(e.target)) return;
    closePopover();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && popover && !popover.hidden) closePopover();
  });
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      // Clear filter dimensions; keep current sort.
      const cur = state.get('filters') || {};
      state.set('filters', {
        ...cur, status: '', tool: '', machine: '', tag: '',
      });
      statusSel.value = '';
      toolSel.value = '';
      if (machineSel) machineSel.value = '';
      if (tagSel) tagSel.value = '';
    });
  }

  /* ── Active-filter chips ── */
  function chipHtml(key, label, value, display) {
    return (
      '<span class="filter-chip" data-key="' + escapeHtml(key) + '">' +
        '<span class="chip-label">' + escapeHtml(label) + ':</span>' +
        '<span class="chip-value">' + escapeHtml(display || value) + '</span>' +
        '<button type="button" class="chip-clear" aria-label="Clear ' +
          escapeHtml(label) + ' filter">&times;</button>' +
      '</span>'
    );
  }
  function renderChips() {
    if (!chipsEl) return;
    const f = state.get('filters') || {};
    const parts = [];
    if (f.tag)     parts.push(chipHtml('tag',     'Tag',    f.tag,     '#' + f.tag));
    if (f.status)  parts.push(chipHtml('status',  'Status', f.status));
    if (f.tool)    parts.push(chipHtml('tool',    'Tool',   f.tool));
    if (f.machine) {
      const lbl = f.machine === 'local' ? 'local' : String(f.machine).split('.')[0];
      parts.push(chipHtml('machine', 'Node', f.machine, lbl));
    }
    const { field, dir, value } = parseSort(f.sort);
    if (value !== SORT_DEFAULT) {
      const label =
        field === 'name'    ? 'Name'    :
        field === 'created' ? 'Created' :
        'User activity';
      const arrow = dir === 'asc' ? ' ↑' : ' ↓';
      parts.push(chipHtml('sort', 'Sort', value, label + arrow));
    }
    if (parts.length === 0) {
      chipsEl.hidden = true;
      chipsEl.innerHTML = '';
      return;
    }
    chipsEl.hidden = false;
    chipsEl.innerHTML = parts.join('');
    chipsEl.querySelectorAll('.filter-chip').forEach(chip => {
      const key = chip.getAttribute('data-key');
      const clearBtnEl = chip.querySelector('.chip-clear');
      if (!clearBtnEl) return;
      clearBtnEl.addEventListener('click', () => {
        const cur = state.get('filters') || {};
        if (key === 'sort') {
          state.set('filters', { ...cur, sort: SORT_DEFAULT });
          if (sortFieldSel) sortFieldSel.value = 'user_activity';
          if (sortDirSel) sortDirSel.value = 'desc';
        } else {
          state.set('filters', { ...cur, [key]: '' });
          const sel = { status: statusSel, tool: toolSel, machine: machineSel, tag: tagSel }[key];
          if (sel) sel.value = '';
        }
      });
    });
  }

  function applyFilters(agents) {
    const cur = state.get('filters') || {};
    const f = { sort: SORT_DEFAULT, ...cur };
    let out = agents.slice();
    if (f.status) out = out.filter(a => a.status === f.status);
    if (f.tool) out = out.filter(a => a.tool === f.tool);
    if (f.machine) {
      out = out.filter(a => {
        const host = a.machine_host || 'local';
        if (f.machine === 'local') return host === 'local';
        return host === f.machine;
      });
    }
    if (f.tag) out = out.filter(a => agentTags(a).includes(f.tag));

    const { field, dir } = parseSort(f.sort);
    out.sort((a, b) => {
      if (field === 'name') {
        const cmp = agentName(a).localeCompare(agentName(b), undefined, { sensitivity: 'base' });
        if (cmp !== 0) return dir === 'asc' ? cmp : -cmp;
        return agentUserActivityMs(b) - agentUserActivityMs(a);
      }
      const getMs = field === 'created' ? agentCreatedMs : agentUserActivityMs;
      const delta = getMs(a) - getMs(b);
      if (delta !== 0) return dir === 'asc' ? delta : -delta;
      return agentName(a).localeCompare(agentName(b), undefined, { sensitivity: 'base' });
    });
    return out;
  }

  function refreshToolFilterOptions(agents) {
    const tools = [...new Set(agents.map(a => a.tool).filter(Boolean))];
    const cur = toolSel.value;
    toolSel.innerHTML =
      '<option value="">All tools</option>' +
      tools.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
    if (tools.includes(cur)) toolSel.value = cur;
  }

  function refreshMachineFilterOptions(agents) {
    if (!machineSel) return;
    const machines = [...new Set(
      agents.map(a => a.machine_host || 'local')
    )];
    machines.sort((a, b) => {
      if (a === 'local') return 1;
      if (b === 'local') return -1;
      return a.localeCompare(b);
    });
    const desired = (state.get('filters') || {}).machine || '';
    machineSel.innerHTML =
      '<option value="">All nodes</option>' +
      machines.map(m => {
        const label = m === 'local' ? 'local' : m.split('.')[0];
        return `<option value="${escapeHtml(m)}">${escapeHtml(label)}</option>`;
      }).join('');
    if (desired && machines.includes(desired)) {
      machineSel.value = desired;
    } else if (desired) {
      // Filter target is set but no current agent matches that host —
      // keep it as an explicit option so the user can see/clear it.
      const opt = document.createElement('option');
      opt.value = desired;
      opt.textContent = desired === 'local' ? 'local' : desired.split('.')[0];
      machineSel.appendChild(opt);
      machineSel.value = desired;
    }
  }

  function refreshTagFilterOptions(agents) {
    if (!tagSel) return;
    const tags = [...new Set(agents.flatMap(agentTags))].sort((a, b) => a.localeCompare(b));
    const desired = (state.get('filters') || {}).tag || '';
    tagSel.innerHTML =
      '<option value="">All tags</option>' +
      tags.map(t => `<option value="${escapeHtml(t)}">#${escapeHtml(t)}</option>`).join('');
    if (desired && tags.includes(desired)) {
      tagSel.value = desired;
    } else if (desired) {
      const opt = document.createElement('option');
      opt.value = desired;
      opt.textContent = `#${desired}`;
      tagSel.appendChild(opt);
      tagSel.value = desired;
    }
  }

  function render() {
    const agents = state.get('agents') || [];
    const selectedId = state.get('selectedAgentId');
    refreshToolFilterOptions(agents);
    refreshMachineFilterOptions(agents);
    refreshTagFilterOptions(agents);
    renderChips();
    const filtered = applyFilters(agents);

    if (filtered.length === 0) {
      listWrap.innerHTML = `
        <div class="empty-state">
          ${agents.length === 0 ? 'No agents found.' : 'No agents match the current filter.'}
        </div>`;
      return;
    }

    const html = filtered.map(agent => {
      const name = escapeHtml(agentName(agent));
      const tool = escapeHtml(agent.tool || '');
      const ctx = agent.context_name ? ' · ' + escapeHtml(agent.context_name) : '';
      const status = escapeHtml(agent.status || 'unknown');
      const host = agent.machine_host ? ' · ' + escapeHtml(agent.machine_host.split('.')[0]) : '';
      const t = timeSince(agentUpdatedAt(agent));
      const sel = agent.id === selectedId ? ' selected' : '';
      // CAMC task tags — appended inline so the row-meta's
      // text-overflow:ellipsis truncates them naturally when the
      // line is too narrow. Each tag becomes "#tag" text.
      const tags = agentTags(agent);
      const tagText = tags.length
        ? ' · ' + tags.map(tg => '#' + escapeHtml(tg)).join(' ')
        : '';
      return `
        <div class="desktop-agent-row${sel}" data-id="${escapeHtml(agent.id)}">
          <span class="dot ${escapeHtml(agent.status || '')}"></span>
          <div class="row-body">
            <div class="row-name">${name}</div>
            <div class="row-meta">${tool}${ctx}${host} · ${status}${tagText}</div>
          </div>
          <div class="row-time">${t}</div>
          <button type="button" class="row-edit-btn"
                  data-id="${escapeHtml(agent.id)}"
                  aria-haspopup="menu" aria-label="Agent actions"
                  title="Agent actions">&hellip;</button>
        </div>`;
    }).join('');

    listWrap.innerHTML = html;
    listWrap.querySelectorAll('.desktop-agent-row').forEach(el => {
      el.addEventListener('click', (ev) => {
        // Don't treat clicks on the ⋯ button as row selection.
        if (ev.target && ev.target.closest && ev.target.closest('.row-edit-btn')) return;
        const id = el.dataset.id;
        bumpUserActivity(id);                        // user_activity_at
        try { state.set('agents', [...(state.get('agents') || [])]); } catch { /* noop */ }
        state.set('selectedAgentId', id);
      });
    });
    listWrap.querySelectorAll('.row-edit-btn').forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const id = btn.dataset.id;
        const agent = (state.get('agents') || []).find(a => a.id === id);
        if (!agent) return;
        openAgentActionMenu(agent, btn);
      });
    });
  }

  /* ─────────── Agent row actions + Agent Settings
   *
   * The row ⋯ button is intentionally a small action menu only:
   * Settings / Stop / Remove. Settings opens a full Agents-mode
   * settings surface styled like the app Settings page. Attributes
   * are editable now; Automation merges agent-owned prompt loops and
   * advanced host cron jobs in one surface.
   */
  const modeAgentsEl = document.getElementById('mode-agents');
  const agentSettingsPanel = document.getElementById('agent-settings-panel');
  const agentSettingsBack = document.getElementById('agent-settings-back');
  const agentSettingsTitle = document.getElementById('agent-settings-title');
  const agentSettingsMeta = document.getElementById('agent-settings-meta');
  const agentSettingsTabs = Array.from(document.querySelectorAll('[data-agent-settings-tab]'));
  const agentSettingsPanels = Array.from(document.querySelectorAll('[data-agent-settings-panel]'));
  const agentSettingsName = document.getElementById('agent-settings-name');
  const agentSettingsAuto = document.getElementById('agent-settings-auto-confirm');
  const agentSettingsTags = document.getElementById('agent-settings-tags');
  const agentSettingsSave = document.getElementById('agent-settings-save');
  const agentSettingsReset = document.getElementById('agent-settings-reset');
  const agentSettingsStatus = document.getElementById('agent-settings-status');
  const agentCronTarget = document.getElementById('agent-cron-target');
  const agentCronRefresh = document.getElementById('agent-cron-refresh');
  const agentCronName = document.getElementById('agent-cron-name');
  const agentCronScheduleType = document.getElementById('agent-cron-schedule-type');
  const agentCronScheduleValue = document.getElementById('agent-cron-schedule-value');
  const agentCronTimeout = document.getElementById('agent-cron-timeout');
  const agentCronAttempts = document.getElementById('agent-cron-attempts');
  const agentCronTtl = document.getElementById('agent-cron-ttl');
  const agentCronNoExpire = document.getElementById('agent-cron-no-expire');
  const agentCronText = document.getElementById('agent-cron-text');
  const agentCronTextLabel = document.getElementById('agent-cron-text-label');
  const agentCronCwdLabel = document.getElementById('agent-cron-cwd-label');
  const agentCronCwd = document.getElementById('agent-cron-cwd');
  const agentCronTypeInputs = Array.from(document.querySelectorAll('input[name="agent-cron-type"]'));
  const agentCronAdd = document.getElementById('agent-cron-add');
  const agentCronStatus = document.getElementById('agent-cron-status');
  const agentCronList = document.getElementById('agent-cron-list');

  let _agentActionMenuEl = null;
  let _agentActionDocClick = null;
  let _agentActionEsc = null;
  let _agentSettingsAgentId = null;
  let _agentSettingsTab = 'attributes';
  let _agentCronLoadedFor = null;
  let _agentCronCwdFor = null;
  let _agentCronPayload = null;
  let _agentCronLoading = false;

  function _safeTag(s) {
    return /^[A-Za-z0-9_-]{1,32}$/.test(String(s || ''));
  }

  function agentAutoConfirm(agent) {
    for (const v of [agent?.auto_confirm, agent?.task?.auto_confirm, agent?.task_auto_confirm]) {
      if (typeof v === 'boolean') return v;
    }
    return true;
  }

  function closeAgentActionMenu() {
    if (_agentActionMenuEl && _agentActionMenuEl.parentNode) {
      _agentActionMenuEl.parentNode.removeChild(_agentActionMenuEl);
    }
    _agentActionMenuEl = null;
    if (_agentActionDocClick) {
      document.removeEventListener('click', _agentActionDocClick, true);
      _agentActionDocClick = null;
    }
    if (_agentActionEsc) {
      document.removeEventListener('keydown', _agentActionEsc);
      _agentActionEsc = null;
    }
  }

  function activeSettingsAgent() {
    const id = _agentSettingsAgentId || state.get('selectedAgentId');
    if (!id) return null;
    return (state.get('agents') || []).find(a => a.id === id) || null;
  }

  function setAgentSettingsStatus(text, cls = '') {
    if (!agentSettingsStatus) return;
    agentSettingsStatus.textContent = text || '';
    agentSettingsStatus.classList.remove('is-error', 'is-ok');
    if (cls) agentSettingsStatus.classList.add(cls);
  }

  async function ensureAgentSettingsConnected() {
    if (api && api.mode !== 'disconnected') return true;
    if (typeof connect === 'function') {
      const mode = await connect();
      return mode && mode !== 'disconnected' && api && api.mode !== 'disconnected';
    }
    return false;
  }

  async function requireAgentSettingsConnected(label = 'Action') {
    const ok = await ensureAgentSettingsConnected();
    if (!ok) throw new Error(`${label} requires an active Direct or Relay connection`);
  }

  function setAgentCronStatus(text, cls = '') {
    if (!agentCronStatus) return;
    agentCronStatus.textContent = text || '';
    agentCronStatus.classList.remove('is-error', 'is-ok');
    if (cls) agentCronStatus.classList.add(cls);
  }

  function currentAutomationType() {
    const picked = agentCronTypeInputs.find(input => input.checked);
    return picked && picked.value === 'cron' ? 'cron' : 'loop';
  }

  function agentWorkspacePath(agent = activeSettingsAgent()) {
    return String(agent?.context_path || agent?.path || agent?.cwd || '');
  }

  function updateAutomationTypeUi() {
    const type = currentAutomationType();
    const isCron = type === 'cron';
    if (agentCronTextLabel) {
      const child = agentCronTextLabel.querySelector('textarea');
      agentCronTextLabel.firstChild.textContent = isCron ? 'Command\n                    ' : 'Prompt\n                    ';
      if (child) child.placeholder = isCron ? 'camc msg send cam-dev -t "review latest changes" --no-wait' : 'review latest changes and report blockers';
    }
    if (agentCronCwdLabel) agentCronCwdLabel.hidden = !isCron;
    if (agentCronCwd && isCron && !agentCronCwd.value) agentCronCwd.value = agentWorkspacePath();
    if (agentCronAdd) agentCronAdd.textContent = isCron ? 'Add cron job' : 'Add loop';
  }

  function updateCronSchedulePlaceholder() {
    if (!agentCronScheduleType || !agentCronScheduleValue) return;
    const t = agentCronScheduleType.value || 'every';
    const map = {
      every: '30m or 2h',
      daily: '09:00',
      in: '45m or 2h',
      at: '2026-06-10T09:00:00-07:00',
    };
    agentCronScheduleValue.placeholder = map[t] || '30m';
    if (!agentCronScheduleValue.value) {
      agentCronScheduleValue.value = t === 'daily' ? '09:00' : (t === 'at' ? '' : '30m');
    }
  }

  function cronScheduleLabel(job) {
    const s = job && job.schedule || {};
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
    if (!raw) return 'not scheduled';
    const ago = timeSince(raw);
    return ago ? `${raw} (${ago})` : raw;
  }

  function renderAgentCronJobs(agent = activeSettingsAgent()) {
    if (!agentCronList) return;
    if (!agent) {
      agentCronList.innerHTML = '<div class="empty-state">Select an agent to view automation.</div>';
      return;
    }
    if (_agentCronLoading) {
      agentCronList.innerHTML = '<div class="empty-state">Loading automation...</div>';
      return;
    }
    const payload = _agentCronPayload;
    const jobs = payload && Array.isArray(payload.jobs) ? payload.jobs : [];
    const loops = payload && Array.isArray(payload.loops) ? payload.loops : [];
    if (!payload) {
      agentCronList.innerHTML = '<div class="empty-state">Click Refresh to load automation for this agent.</div>';
      return;
    }

    function card(item, kind) {
      const id = escapeHtml(item.id || '');
      const keyRaw = item.id || item.name || '';
      const key = escapeAttr(`${kind}:${keyRaw}`);
      const name = escapeHtml(item.display_name || item.name || item.id || (kind === 'loop' ? 'loop' : 'cron job'));
      const schedule = escapeHtml(cronScheduleLabel(item));
      const status = escapeHtml(item.last_status || item.state?.last_status || 'never run');
      const next = escapeHtml(cronNextDue(item));
      const policy = item.policy || {};
      const attempts = escapeHtml(`${item.attempts ?? item.state?.attempts ?? 0}/${item.max_attempts || policy.max_attempts || '?'}`);
      const expires = escapeHtml(item.expires_at || policy.expires_at || 'no expiry');
      const host = escapeHtml(kind === 'loop' ? 'agent monitor' : (item.host || 'this host'));
      const removeLabel = kind === 'loop' ? 'Remove loop' : 'Remove cron job';
      return `
        <article class="agent-cron-job-card" data-cron-job="${key}">
          <div class="agent-cron-job-main">
            <div class="agent-cron-job-title">${name}</div>
            <div class="agent-cron-job-meta">${schedule} · ${host} · attempts ${attempts}</div>
            <div class="agent-cron-job-meta">next ${next} · expires ${expires}</div>
          </div>
          <div class="agent-cron-job-side">
            <span class="agent-cron-status-pill">${status}</span>
            <button type="button" class="btn-secondary btn-xs" data-cron-remove="${key}" aria-label="${removeLabel} ${name}">Remove</button>
          </div>
          ${id ? `<div class="agent-cron-job-id">${id}</div>` : ''}
        </article>`;
    }

    const sections = [];
    if (loops.length) sections.push(`<div class="agent-cron-list-heading">Agent loops</div>${loops.map(loop => card(loop, 'loop')).join('')}`);
    if (jobs.length) sections.push(`<div class="agent-cron-list-heading">Host cron jobs</div>${jobs.map(job => card(job, 'cron')).join('')}`);
    if (!sections.length) sections.push('<div class="empty-state">No automation is attached to this agent.</div>');
    if (payload.loop_error) sections.push(`<div class="empty-state">Agent loops unavailable: ${escapeHtml(payload.loop_error)}</div>`);
    agentCronList.innerHTML = sections.join('');
  }

  async function loadAgentCron({ force = false } = {}) {
    const agent = activeSettingsAgent();
    if (!agent) {
      _agentCronLoadedFor = null;
      _agentCronCwdFor = null;
      _agentCronPayload = null;
      renderAgentCronJobs(null);
      return;
    }
    if (!force && _agentCronLoadedFor === agent.id && _agentCronPayload) {
      renderAgentCronJobs(agent);
      return;
    }
    _agentCronLoading = true;
    renderAgentCronJobs(agent);
    setAgentCronStatus('Loading automation...');
    try {
      await requireAgentSettingsConnected('Automation');
      const payload = await api.agentCronJobs(agent.id);
      _agentCronLoadedFor = agent.id;
      _agentCronPayload = payload || { jobs: [] };
      const total = (_agentCronPayload.jobs || []).length + (_agentCronPayload.loops || []).length;
      const suffix = _agentCronPayload.loop_error ? ` Loop unavailable: ${_agentCronPayload.loop_error}` : '';
      setAgentCronStatus(`Loaded ${total} automation item(s).${suffix}`, _agentCronPayload.loop_error ? 'is-error' : 'is-ok');
    } catch (err) {
      _agentCronPayload = { jobs: [] };
      setAgentCronStatus(`Automation load failed: ${err?.message || err}`, 'is-error');
    } finally {
      _agentCronLoading = false;
      renderAgentCronJobs(agent);
    }
  }

  function validateCronForm() {
    const name = String(agentCronName?.value || '').trim();
    if (!/^[A-Za-z0-9_.-]{1,48}$/.test(name)) return { error: 'Use a name with letters, digits, dot, underscore, or dash.' };
    const scheduleType = String(agentCronScheduleType?.value || 'every');
    const scheduleValue = String(agentCronScheduleValue?.value || '').trim();
    const text = String(agentCronText?.value || '').trim();
    const type = currentAutomationType();
    if (!text) return { error: type === 'cron' ? 'Command is required.' : 'Prompt is required.' };
    const body = {
      type,
      name,
      schedule_type: scheduleType,
      schedule_value: scheduleValue,
      timeout_seconds: Number(agentCronTimeout?.value || 60),
      max_attempts: Number(agentCronAttempts?.value || 3),
      ttl_days: Number(agentCronTtl?.value || 7),
      no_expire: !!agentCronNoExpire?.checked,
    };
    if (type === 'cron') {
      body.command = text;
      body.cwd = String(agentCronCwd?.value || '').trim() || agentWorkspacePath();
    } else {
      body.text = text;
    }
    return { body };
  }

  async function addAgentCronJob() {
    const agent = activeSettingsAgent();
    if (!agent) return;
    const checked = validateCronForm();
    if (checked.error) {
      setAgentCronStatus(checked.error, 'is-error');
      return;
    }
    if (agentCronAdd) agentCronAdd.disabled = true;
    const type = checked.body.type === 'cron' ? 'cron job' : 'loop';
    setAgentCronStatus(`Adding ${type}...`);
    try {
      await requireAgentSettingsConnected('Automation add');
      await api.createAgentCronJob(agent.id, checked.body);
      setAgentCronStatus(`${type === 'cron job' ? 'Cron job' : 'Loop'} added.`, 'is-ok');
      if (agentCronName) agentCronName.value = '';
      if (agentCronText) agentCronText.value = '';
      await loadAgentCron({ force: true });
    } catch (err) {
      setAgentCronStatus(`Add failed: ${err?.message || err}`, 'is-error');
    } finally {
      if (agentCronAdd) agentCronAdd.disabled = false;
    }
  }

  async function removeAgentCronJob(jobKey) {
    const agent = activeSettingsAgent();
    if (!agent || !jobKey) return;
    const isLoop = String(jobKey || '').startsWith('loop:');
    if (!confirm(`Remove ${isLoop ? 'loop' : 'cron job'} "${jobKey.replace(/^(loop|cron):/, '')}"?`)) return;
    setAgentCronStatus(`Removing ${isLoop ? 'loop' : 'cron job'}...`);
    try {
      await requireAgentSettingsConnected('Automation remove');
      await api.deleteAgentCronJob(agent.id, jobKey);
      setAgentCronStatus(`${isLoop ? 'Loop' : 'Cron job'} removed.`, 'is-ok');
      await loadAgentCron({ force: true });
    } catch (err) {
      setAgentCronStatus(`Remove failed: ${err?.message || err}`, 'is-error');
    }
  }

  function setAgentSettingsTab(tab) {
    _agentSettingsTab = ['attributes', 'automation', 'workflow'].includes(tab) ? tab : 'attributes';
    for (const btn of agentSettingsTabs) {
      const on = btn.dataset.agentSettingsTab === _agentSettingsTab;
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    for (const panel of agentSettingsPanels) {
      panel.hidden = panel.dataset.agentSettingsPanel !== _agentSettingsTab;
    }
    if (_agentSettingsTab === 'automation' && agentSettingsPanel && !agentSettingsPanel.hidden) {
      void loadAgentCron();
    }
  }

  function fillAgentSettings(agent) {
    if (!agent) {
      if (agentSettingsTitle) agentSettingsTitle.textContent = 'Agent Settings';
      if (agentSettingsMeta) agentSettingsMeta.textContent = 'Select an agent to edit settings.';
      if (agentSettingsName) agentSettingsName.value = '';
      if (agentSettingsAuto) agentSettingsAuto.checked = false;
      if (agentSettingsTags) agentSettingsTags.value = '';
      if (agentSettingsSave) agentSettingsSave.disabled = true;
      if (agentSettingsReset) agentSettingsReset.disabled = true;
      if (agentCronTarget) agentCronTarget.textContent = 'Select an agent to manage automation.';
      if (agentCronAdd) agentCronAdd.disabled = true;
      _agentCronLoadedFor = null;
      _agentCronPayload = null;
      renderAgentCronJobs(null);
      setAgentCronStatus('');
      setAgentSettingsStatus('');
      return;
    }
    const name = agentName(agent);
    const tags = agentTags(agent);
    if (agentSettingsTitle) agentSettingsTitle.textContent = `Agent Settings: ${name || agent.id}`;
    if (agentSettingsMeta) {
      const parts = [
        agent.id ? `id ${agent.id}` : '',
        agent.tool || agent.task?.tool || '',
        agent.status || '',
        agent.context_name ? `context ${agent.context_name}` : '',
        agent.machine_host || agent.hostname || '',
      ].filter(Boolean);
      agentSettingsMeta.textContent = parts.join(' · ');
    }
    if (agentSettingsName) agentSettingsName.value = name;
    if (agentSettingsAuto) agentSettingsAuto.checked = agentAutoConfirm(agent);
    if (agentSettingsTags) agentSettingsTags.value = tags.join(', ');
    if (agentSettingsSave) agentSettingsSave.disabled = false;
    if (agentSettingsReset) agentSettingsReset.disabled = false;
    if (agentCronTarget) {
      const host = agent.machine_host || agent.hostname || 'this node';
      const path = agent.context_path || agent.path || '';
      agentCronTarget.textContent = `Loops are owned by this agent. Host cron jobs run on ${host}${path ? ` in ${path}` : ''} by default.`;
    }
    if (agentCronCwd && _agentCronCwdFor !== agent.id) {
      agentCronCwd.value = agentWorkspacePath(agent);
      _agentCronCwdFor = agent.id;
    }
    if (agentCronAdd) agentCronAdd.disabled = false;
    if (_agentCronLoadedFor && _agentCronLoadedFor !== agent.id) {
      _agentCronLoadedFor = null;
      _agentCronPayload = null;
      renderAgentCronJobs(agent);
      setAgentCronStatus('');
    }
    setAgentSettingsStatus('');
  }

  function openAgentSettings(agentId) {
    closeAgentActionMenu();
    const agent = (state.get('agents') || []).find(a => a.id === agentId);
    if (!agent || !agentSettingsPanel || !modeAgentsEl) return;
    _agentSettingsAgentId = agentId;
    bumpUserActivity(agentId);
    try { state.set('selectedAgentId', agentId); } catch { /* noop */ }
    modeAgentsEl.classList.add('agent-settings-open');
    agentSettingsPanel.hidden = false;
    setAgentSettingsTab(_agentSettingsTab || 'attributes');
    fillAgentSettings(agent);
    updateCronSchedulePlaceholder();
    updateAutomationTypeUi();
    try { (_agentSettingsTab === 'automation' ? agentCronName : agentSettingsName)?.focus(); } catch { /* noop */ }
  }

  function closeAgentSettings() {
    if (modeAgentsEl) modeAgentsEl.classList.remove('agent-settings-open');
    if (agentSettingsPanel) agentSettingsPanel.hidden = true;
    _agentSettingsAgentId = null;
    setAgentSettingsStatus('');
  }

  function parseTagInput() {
    const raw = String(agentSettingsTags?.value || '');
    const tags = raw.split(',').map(t => t.trim()).filter(Boolean);
    return [...new Set(tags)];
  }

  async function saveAgentSettingsAttributes() {
    const agent = activeSettingsAgent();
    if (!agent) return;
    const initialName = agentName(agent);
    const initialAuto = agentAutoConfirm(agent);
    const initialTags = agentTags(agent);
    const nextName = String(agentSettingsName?.value || '').trim();
    const nextAuto = !!agentSettingsAuto?.checked;
    const nextTags = parseTagInput();
    for (const t of nextTags) {
      if (!_safeTag(t)) {
        setAgentSettingsStatus(`Invalid tag "${t}". Use letters / digits / _ / -, up to 32 chars.`, 'is-error');
        return;
      }
    }
    const body = {};
    if (nextName && nextName !== initialName) body.name = nextName;
    if (nextAuto !== initialAuto) body.auto_confirm = nextAuto;
    const addT = nextTags.filter(t => !initialTags.includes(t));
    const rmT = initialTags.filter(t => !nextTags.includes(t));
    if (addT.length) body.tags_add = addT;
    if (rmT.length) body.tags_remove = rmT;
    if (Object.keys(body).length === 0) {
      setAgentSettingsStatus('No changes.');
      return;
    }
    if (agentSettingsSave) agentSettingsSave.disabled = true;
    if (agentSettingsReset) agentSettingsReset.disabled = true;
    setAgentSettingsStatus('Saving...');
    try {
      await requireAgentSettingsConnected('Save');
      const resp = await api.updateAgent(agent.id, body);
      bumpUserActivity(agent.id);
      const current = state.get('agents') || [];
      const fallback = {
        ...agent,
        ...(body.name !== undefined ? { task_name: body.name } : {}),
        ...(body.auto_confirm !== undefined ? { auto_confirm: body.auto_confirm } : {}),
        ...(addT.length || rmT.length ? { tags: nextTags.slice() } : {}),
        task: {
          ...(agent.task || {}),
          ...(body.name !== undefined ? { name: body.name } : {}),
          ...(body.auto_confirm !== undefined ? { auto_confirm: body.auto_confirm } : {}),
          ...(addT.length || rmT.length ? { tags: nextTags.slice() } : {}),
        },
      };
      const updated = resp && resp.agent && typeof resp.agent === 'object' ? resp.agent : fallback;
      state.set('agents', current.map(a => a.id === agent.id ? { ...a, ...updated } : a));
      setAgentSettingsStatus('Saved.', 'is-ok');
    } catch (err) {
      const msg = (err && err.message) || String(err || 'save failed');
      setAgentSettingsStatus(`Save failed: ${msg}`, 'is-error');
    } finally {
      if (agentSettingsSave) agentSettingsSave.disabled = false;
      if (agentSettingsReset) agentSettingsReset.disabled = false;
    }
  }

  async function stopAgentFromMenu(agent) {
    closeAgentActionMenu();
    if (!agent) return;
    if (!confirm(`Stop agent "${agentName(agent)}"?`)) return;
    try {
      await requireAgentSettingsConnected('Stop');
      await api.stopAgent(agent.id);
      bumpUserActivity(agent.id);
      state.updateAgent(agent.id, { status: 'stopped' });
      state.toast('Agent stopped', 'success');
    } catch (err) {
      state.toast(`Stop failed: ${err?.message || err}`, 'error', 5000);
    }
  }

  async function removeAgentFromMenu(agent) {
    closeAgentActionMenu();
    if (!agent) return;
    if (!confirm(`Remove agent "${agentName(agent)}" from history?`)) return;
    try {
      await requireAgentSettingsConnected('Remove');
      await api.deleteAgentHistory(agent.id);
      state.removeAgent(agent.id);
      if (state.get('selectedAgentId') === agent.id) state.set('selectedAgentId', null);
      if (_agentSettingsAgentId === agent.id) closeAgentSettings();
      state.toast('Agent removed', 'success');
    } catch (err) {
      state.toast(`Remove failed: ${err?.message || err}`, 'error', 5000);
    }
  }

  function openAgentActionMenu(agent, anchorBtn) {
    closeAgentActionMenu();
    if (!agent) return;
    const menu = document.createElement('div');
    menu.className = 'agent-row-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', `Agent actions for ${agentName(agent)}`);
    menu.innerHTML = `
      <button type="button" class="agent-row-menu-item" data-action="settings" role="menuitem">Settings</button>
      <button type="button" class="agent-row-menu-item" data-action="stop" role="menuitem">Stop</button>
      <button type="button" class="agent-row-menu-item danger" data-action="remove" role="menuitem">Remove</button>
    `;
    document.body.appendChild(menu);
    _agentActionMenuEl = menu;

    function reposition() {
      if (!_agentActionMenuEl || !anchorBtn || !anchorBtn.getBoundingClientRect) return;
      const r = anchorBtn.getBoundingClientRect();
      const mw = _agentActionMenuEl.offsetWidth || 160;
      const mh = _agentActionMenuEl.offsetHeight || 120;
      let top = r.bottom + 4;
      let left = Math.min(r.right - mw, window.innerWidth - mw - 8);
      if (left < 8) left = 8;
      if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
      _agentActionMenuEl.style.position = 'fixed';
      _agentActionMenuEl.style.top = `${top}px`;
      _agentActionMenuEl.style.left = `${left}px`;
    }
    reposition();

    menu.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === 'settings') return openAgentSettings(agent.id);
      if (action === 'stop') return void stopAgentFromMenu(agent);
      if (action === 'remove') return void removeAgentFromMenu(agent);
    });

    function closeViaOutside(ev) {
      if (!_agentActionMenuEl) return;
      if (ev.target === anchorBtn || (anchorBtn && anchorBtn.contains(ev.target))) return;
      if (_agentActionMenuEl.contains(ev.target)) return;
      closeAgentActionMenu();
    }
    function closeViaEsc(ev) {
      if (ev.key === 'Escape') closeAgentActionMenu();
    }
    _agentActionDocClick = closeViaOutside;
    _agentActionEsc = closeViaEsc;
    setTimeout(() => {
      document.addEventListener('click', closeViaOutside, true);
      document.addEventListener('keydown', closeViaEsc);
    }, 0);
    setTimeout(() => { try { menu.querySelector('[data-action="settings"]').focus(); } catch {} }, 0);
  }

  agentSettingsBack?.addEventListener('click', closeAgentSettings);
  agentSettingsTabs.forEach(btn => btn.addEventListener('click', () => setAgentSettingsTab(btn.dataset.agentSettingsTab)));
  agentSettingsSave?.addEventListener('click', () => { void saveAgentSettingsAttributes(); });
  agentSettingsReset?.addEventListener('click', () => fillAgentSettings(activeSettingsAgent()));
  agentCronScheduleType?.addEventListener('change', updateCronSchedulePlaceholder);
  agentCronTypeInputs.forEach(input => input.addEventListener('change', updateAutomationTypeUi));
  agentCronRefresh?.addEventListener('click', () => { void loadAgentCron({ force: true }); });
  agentCronAdd?.addEventListener('click', () => { void addAgentCronJob(); });
  agentCronList?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-cron-remove]');
    if (!btn) return;
    void removeAgentCronJob(btn.dataset.cronRemove || '');
  });

  // Initial render
  render();

  // Re-render when agents, filters, or selection change
  let prevAgents = state.get('agents');
  let prevFilters = state.get('filters');
  let prevSelected = state.get('selectedAgentId');
  state.subscribe(() => {
    const a = state.get('agents');
    const f = state.get('filters');
    const s = state.get('selectedAgentId');
    if (a !== prevAgents || f !== prevFilters || s !== prevSelected) {
      prevAgents = a;
      prevFilters = f;
      prevSelected = s;
      render();
      if (agentSettingsPanel && !agentSettingsPanel.hidden) {
        const prevSettingsAgent = _agentSettingsAgentId;
        if (s && s !== _agentSettingsAgentId) _agentSettingsAgentId = s;
        fillAgentSettings(activeSettingsAgent());
        if (_agentSettingsTab === 'automation' && _agentSettingsAgentId && _agentSettingsAgentId !== prevSettingsAgent) {
          void loadAgentCron({ force: true });
        }
      }
    }
  });
}
