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
  const agentSystemPromptTarget = document.getElementById('agent-system-prompt-target');
  const agentSystemPromptReload = document.getElementById('agent-system-prompt-reload');
  const agentSystemPromptText = document.getElementById('agent-system-prompt-text');
  const agentSystemPromptSave = document.getElementById('agent-system-prompt-save');
  const agentSystemPromptReset = document.getElementById('agent-system-prompt-reset');
  const agentSystemPromptClear = document.getElementById('agent-system-prompt-clear');
  const agentSystemPromptStatus = document.getElementById('agent-system-prompt-status');
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
  const agentWorkflowTarget = document.getElementById('agent-workflow-target');
  const agentWorkflowRefresh = document.getElementById('agent-workflow-refresh');
  const agentWorkflowValidate = document.getElementById('agent-workflow-validate');
  const agentWorkflowAdd = document.getElementById('agent-workflow-add');
  const agentWorkflowSave = document.getElementById('agent-workflow-save');
  const agentWorkflowExpand = document.getElementById('agent-workflow-expand');
  const agentWorkflowCollapse = document.getElementById('agent-workflow-collapse');
  const agentWorkflowStatus = document.getElementById('agent-workflow-status');
  const agentWorkflowVisual = document.getElementById('agent-workflow-visual');
  const agentWorkflowSummary = document.getElementById('agent-workflow-summary');
  const agentWorkflowCards = document.getElementById('agent-workflow-cards');
  const agentWorkflowInspector = document.getElementById('agent-workflow-inspector');
  const agentWorkflowRaw = document.getElementById('agent-workflow-raw');
  const agentWorkflowViewBtns = Array.from(document.querySelectorAll('[data-agent-workflow-view]'));

  let _agentActionMenuEl = null;
  let _agentActionDocClick = null;
  let _agentActionEsc = null;
  let _agentSettingsAgentId = null;
  let _agentSettingsTab = 'attributes';
  let _agentSystemPromptLoadedFor = null;
  let _agentSystemPromptInitial = '';
  let _agentSystemPromptLoading = false;
  let _agentCronLoadedFor = null;
  let _agentCronCwdFor = null;
  let _agentCronPayload = null;
  let _agentCronLoading = false;
  let _agentWorkflowLoadedFor = null;
  let _agentWorkflowLoading = false;
  let _agentWorkflowPayload = null;
  let _agentWorkflowSelectedNodeId = '';
  let _agentWorkflowView = 'visual';
  let _agentWorkflowExpanded = new Set();
  let _agentWorkflowDirty = false;

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

  function systemPromptFileName(agent = activeSettingsAgent()) {
    const tool = String(agent?.tool || agent?.adapter || agent?.task_tool || agent?.task?.tool || agent?.task?.adapter || '').toLowerCase();
    if (tool === 'claude') return 'CLAUDE.md';
    if (tool === 'codex' || tool === 'cursor') return 'AGENTS.md';
    return '';
  }

  function systemPromptBlockMarkers(agent = activeSettingsAgent()) {
    const id = String(agent?.id || _agentSettingsAgentId || '');
    return {
      begin: `<!-- camc:${id} begin -->`,
      end:   `<!-- camc:${id} end -->`,
    };
  }

  function extractSystemPromptBlock(text, agent = activeSettingsAgent()) {
    const { begin, end } = systemPromptBlockMarkers(agent);
    const raw = String(text || '');
    const start = raw.indexOf(begin);
    if (start < 0) return '';
    const bodyStart = start + begin.length;
    const stop = raw.indexOf(end, bodyStart);
    if (stop < 0) return '';
    return raw.slice(bodyStart, stop).replace(/^\r?\n/, '').replace(/\r?\n$/, '');
  }

  function agentSystemPromptMeta(agent = activeSettingsAgent()) {
    return String(agent?.system_prompt || agent?.task?.system_prompt || '');
  }

  function setAgentSystemPromptStatus(text, cls = '') {
    if (!agentSystemPromptStatus) return;
    agentSystemPromptStatus.textContent = text || '';
    agentSystemPromptStatus.classList.remove('is-error', 'is-ok');
    if (cls) agentSystemPromptStatus.classList.add(cls);
  }

  function updateSystemPromptTarget(agent = activeSettingsAgent()) {
    const file = systemPromptFileName(agent);
    const root = agentWorkspacePath(agent);
    if (agentSystemPromptTarget) {
      agentSystemPromptTarget.textContent = agent
        ? (file ? `Edits the camc marker block in ${root ? `${root}/` : ''}${file}.` : 'This agent tool has no AGENTS.md / CLAUDE.md mapping yet.')
        : 'Select an agent to load its AGENTS.md / CLAUDE.md prompt block.';
    }
    const enabled = !!agent && !!file;
    [agentSystemPromptReload, agentSystemPromptSave, agentSystemPromptReset, agentSystemPromptClear].forEach(btn => {
      if (btn) btn.disabled = !enabled;
    });
    if (agentSystemPromptText) agentSystemPromptText.disabled = !enabled;
  }

  function resetSystemPromptForAgent(agent = activeSettingsAgent()) {
    _agentSystemPromptLoadedFor = null;
    _agentSystemPromptInitial = '';
    if (agentSystemPromptText) agentSystemPromptText.value = agentSystemPromptMeta(agent);
    setAgentSystemPromptStatus('');
    updateSystemPromptTarget(agent);
  }

  async function loadAgentSystemPrompt({ force = false } = {}) {
    const agent = activeSettingsAgent();
    if (!agent) {
      resetSystemPromptForAgent(null);
      return;
    }
    const file = systemPromptFileName(agent);
    updateSystemPromptTarget(agent);
    if (!file) {
      setAgentSystemPromptStatus('No system prompt file mapping for this agent tool.', 'is-error');
      return;
    }
    if (!force && _agentSystemPromptLoadedFor === agent.id) return;
    _agentSystemPromptLoading = true;
    [agentSystemPromptReload, agentSystemPromptSave, agentSystemPromptReset, agentSystemPromptClear].forEach(btn => { if (btn) btn.disabled = true; });
    setAgentSystemPromptStatus(`Loading ${file}...`);
    try {
      await requireAgentSettingsConnected('System Prompt');
      let prompt = agentSystemPromptMeta(agent);
      try {
        const resp = await api.agentReadWorkspaceFile(agent.id, file);
        if (resp && !resp.binary) {
          const fromFile = extractSystemPromptBlock(resp.content || '', agent);
          if (fromFile || !prompt) prompt = fromFile;
        }
      } catch (e) {
        // A missing prompt file is fine; use metadata or empty text.
        const msg = String(e?.message || e || '');
        if (!/not_found|404/i.test(msg)) throw e;
      }
      _agentSystemPromptLoadedFor = agent.id;
      _agentSystemPromptInitial = prompt;
      if (agentSystemPromptText) agentSystemPromptText.value = prompt;
      setAgentSystemPromptStatus(prompt ? `Loaded prompt block from ${file}.` : `No prompt block in ${file}.`, 'is-ok');
    } catch (err) {
      const fallback = agentSystemPromptMeta(agent);
      _agentSystemPromptInitial = fallback;
      if (agentSystemPromptText) agentSystemPromptText.value = fallback;
      setAgentSystemPromptStatus(`Load failed: ${err?.message || err}`, 'is-error');
    } finally {
      _agentSystemPromptLoading = false;
      updateSystemPromptTarget(agent);
    }
  }

  async function saveAgentSystemPrompt() {
    const agent = activeSettingsAgent();
    if (!agent || !agentSystemPromptText) return;
    const file = systemPromptFileName(agent);
    if (!file) {
      setAgentSystemPromptStatus('No system prompt file mapping for this agent tool.', 'is-error');
      return;
    }
    const prompt = String(agentSystemPromptText.value || '');
    if (prompt === _agentSystemPromptInitial) {
      setAgentSystemPromptStatus('No changes.');
      return;
    }
    [agentSystemPromptSave, agentSystemPromptReload, agentSystemPromptReset, agentSystemPromptClear].forEach(btn => { if (btn) btn.disabled = true; });
    setAgentSystemPromptStatus('Saving prompt...');
    try {
      await requireAgentSettingsConnected('System Prompt save');
      const resp = await api.updateAgent(agent.id, { system_prompt: prompt });
      bumpUserActivity(agent.id);
      _agentSystemPromptInitial = prompt;
      _agentSystemPromptLoadedFor = agent.id;
      const current = state.get('agents') || [];
      const updated = resp && resp.agent && typeof resp.agent === 'object'
        ? resp.agent
        : { ...agent, system_prompt: prompt, task: { ...(agent.task || {}), system_prompt: prompt } };
      state.set('agents', current.map(a => a.id === agent.id ? { ...a, ...updated } : a));
      setAgentSystemPromptStatus(prompt ? `Saved prompt to ${file}.` : `Removed prompt block from ${file}.`, 'is-ok');
    } catch (err) {
      setAgentSystemPromptStatus(`Save failed: ${err?.message || err}`, 'is-error');
    } finally {
      updateSystemPromptTarget(activeSettingsAgent());
    }
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

  function workflowYamlPath() {
    return 'workflow.yaml';
  }

  function setAgentWorkflowStatus(text, cls = '') {
    if (!agentWorkflowStatus) return;
    agentWorkflowStatus.textContent = text || '';
    agentWorkflowStatus.classList.remove('is-error', 'is-ok');
    if (cls) agentWorkflowStatus.classList.add(cls);
  }

  function yamlScalar(raw) {
    let v = String(raw == null ? '' : raw).trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    return v;
  }

  function parseWorkflowYamlV0(text) {
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    const parsed = { workflow: '', version: '', goal: '', nodes: [] };
    let inNodes = false;
    let node = null;
    let section = '';
    function pushNode() {
      if (!node) return;
      if (!Array.isArray(node.needs)) node.needs = [];
      if (!Array.isArray(node.steps)) node.steps = [];
      if (!node.run) node.run = {};
      if (!node.output_schema) node.output_schema = {};
      if (!node.verify) node.verify = {};
      parsed.nodes.push(node);
    }
    for (const line of lines) {
      let m;
      if (!inNodes) {
        if ((m = line.match(/^workflow:\s*(.*)$/))) parsed.workflow = yamlScalar(m[1]);
        else if ((m = line.match(/^version:\s*(.*)$/))) parsed.version = yamlScalar(m[1]);
        else if ((m = line.match(/^goal:\s*(.*)$/))) parsed.goal = yamlScalar(m[1]);
        else if (line.match(/^nodes:\s*$/)) inNodes = true;
        continue;
      }
      if ((m = line.match(/^  - id:\s*(.*)$/))) {
        pushNode();
        node = { id: yamlScalar(m[1]), goal: '', needs: [], run: {}, steps: [], output_schema: {}, verify: {}, retry: null };
        section = '';
        continue;
      }
      if (!node) continue;
      if ((m = line.match(/^    goal:\s*(.*)$/))) { node.goal = yamlScalar(m[1]); section = ''; continue; }
      if ((m = line.match(/^    retry:\s*(.*)$/))) { node.retry = yamlScalar(m[1]); section = ''; continue; }
      if ((m = line.match(/^    ([A-Za-z0-9_-]+):\s*$/))) { section = m[1]; continue; }
      if (section === 'needs' && (m = line.match(/^      -\s*(.*)$/))) { node.needs.push(yamlScalar(m[1])); continue; }
      if (section === 'steps' && (m = line.match(/^      -\s*(.*)$/))) { node.steps.push(yamlScalar(m[1])); continue; }
      if (section === 'run' && (m = line.match(/^      ([A-Za-z0-9_-]+):\s*(.*)$/))) { node.run[m[1]] = yamlScalar(m[2]); continue; }
      if (section === 'output_schema' && (m = line.match(/^      ([A-Za-z0-9_.-]+):\s*(.*)$/))) { node.output_schema[m[1]] = yamlScalar(m[2]); continue; }
      if (section === 'verify' && (m = line.match(/^      ([A-Za-z0-9_.-]+):\s*(.*)$/))) { node.verify[m[1]] = yamlScalar(m[2]); continue; }
    }
    pushNode();
    parsed.edges = parsed.nodes.flatMap(n => (n.needs || []).map(dep => ({ from: dep, to: n.id })));
    return parsed;
  }

  function workflowVerifyLabel(node) {
    const v = node?.verify || {};
    if (v.command) return 'command';
    if (v.human) return 'human';
    if (v.criterion) return 'evaluator';
    return 'auto';
  }

  function workflowRunLabel(node) {
    const run = node?.run || {};
    if (run.skill) return `skill: ${run.skill}`;
    if (run.command) return `cmd: ${run.command}`;
    return 'run: unset';
  }

  function workflowResetForAgent(agent = activeSettingsAgent()) {
    _agentWorkflowLoadedFor = null;
    _agentWorkflowLoading = false;
    _agentWorkflowPayload = null;
    _agentWorkflowSelectedNodeId = '';
    _agentWorkflowExpanded = new Set();
    _agentWorkflowDirty = false;
    const root = agentWorkspacePath(agent);
    if (agentWorkflowTarget) {
      agentWorkflowTarget.textContent = agent
        ? `Edits ${root ? `${root}/` : ''}${workflowYamlPath()} in this agent workspace.`
        : 'Select an agent to load workflow.yaml from its workspace.';
    }
    [agentWorkflowRefresh, agentWorkflowValidate, agentWorkflowAdd, agentWorkflowSave, agentWorkflowExpand, agentWorkflowCollapse].forEach(btn => {
      if (btn) btn.disabled = !agent;
    });
    setAgentWorkflowStatus('');
    renderAgentWorkflow(agent);
  }

  function workflowLinesFromTextarea(value) {
    return String(value || '').replace(/\r\n?/g, '\n').split('\n').map(s => s.trim()).filter(Boolean);
  }

  function workflowObjectFromLines(value) {
    const obj = {};
    for (const line of workflowLinesFromTextarea(value)) {
      const idx = line.indexOf(':');
      if (idx <= 0) continue;
      const key = line.slice(0, idx).trim();
      const val = line.slice(idx + 1).trim();
      if (key) obj[key] = val;
    }
    return obj;
  }

  function workflowYamlValue(v) {
    const s = String(v == null ? '' : v);
    if (!s) return '""';
    if (/^[A-Za-z0-9_.@/:+\-]+$/.test(s)) return s;
    return JSON.stringify(s);
  }

  function workflowObjectYaml(obj, indent = '      ') {
    const keys = Object.keys(obj || {}).filter(k => String(k || '').trim());
    if (!keys.length) return [];
    return keys.map(k => `${indent}${k}: ${workflowYamlValue(obj[k])}`);
  }

  function serializeWorkflowYaml(parsed) {
    const p = parsed || { nodes: [] };
    const out = [];
    out.push(`workflow: ${workflowYamlValue(p.workflow || 'workflow')}`);
    if (p.version) out.push(`version: ${workflowYamlValue(p.version)}`);
    if (p.goal) out.push(`goal: ${workflowYamlValue(p.goal)}`);
    out.push('nodes:');
    for (const node of (p.nodes || [])) {
      out.push(`  - id: ${workflowYamlValue(node.id || 'node')}`);
      if (node.goal) out.push(`    goal: ${workflowYamlValue(node.goal)}`);
      const needs = Array.isArray(node.needs) ? node.needs.filter(Boolean) : [];
      if (needs.length) {
        out.push('    needs:');
        needs.forEach(n => out.push(`      - ${workflowYamlValue(n)}`));
      }
      const run = node.run || {};
      if (Object.keys(run).length) {
        out.push('    run:');
        out.push(...workflowObjectYaml(run));
      }
      const steps = Array.isArray(node.steps) ? node.steps.filter(Boolean) : [];
      if (steps.length) {
        out.push('    steps:');
        steps.forEach(step => out.push(`      - ${workflowYamlValue(step)}`));
      }
      const output = node.output_schema || {};
      if (Object.keys(output).length) {
        out.push('    output_schema:');
        out.push(...workflowObjectYaml(output));
      }
      const verify = node.verify || {};
      if (Object.keys(verify).length) {
        out.push('    verify:');
        out.push(...workflowObjectYaml(verify));
      }
      if (node.retry != null && String(node.retry).trim() !== '') out.push(`    retry: ${workflowYamlValue(node.retry)}`);
    }
    return out.join('\n') + '\n';
  }

  function syncWorkflowRawFromParsed() {
    if (!_agentWorkflowPayload) return;
    _agentWorkflowPayload.text = serializeWorkflowYaml(_agentWorkflowPayload.parsed);
    if (agentWorkflowRaw) agentWorkflowRaw.value = _agentWorkflowPayload.text;
  }

  function validateWorkflow(parsed) {
    const errors = [];
    const ids = new Set();
    for (const [idx, node] of (parsed?.nodes || []).entries()) {
      const where = node.id || `node ${idx + 1}`;
      if (!node.id) errors.push(`${where}: id is required`);
      if (node.id && ids.has(node.id)) errors.push(`${where}: duplicate id`);
      if (node.id) ids.add(node.id);
      if (!node.goal) errors.push(`${where}: goal is recommended`);
      if (!node.run || (!node.run.skill && !node.run.command)) errors.push(`${where}: run.skill or run.command is required`);
      const v = node.verify || {};
      const modes = ['criterion', 'command', 'human'].filter(k => v[k]);
      if (modes.length > 1) errors.push(`${where}: verify can use only one of criterion, command, human`);
    }
    for (const node of (parsed?.nodes || [])) {
      for (const dep of (node.needs || [])) {
        if (!ids.has(dep)) errors.push(`${node.id}: missing dependency ${dep}`);
      }
    }
    return errors;
  }

  function workflowMarkDirty() {
    _agentWorkflowDirty = true;
    syncWorkflowRawFromParsed();
    setAgentWorkflowStatus('Unsaved workflow edits. Click Save to write workflow.yaml.');
  }

  function workflowNodeById(id) {
    const nodes = _agentWorkflowPayload?.parsed?.nodes || [];
    return nodes.find(n => n.id === id) || null;
  }

  function renderWorkflowInspector(_node) {
    // Workflow V1 edits inline in cards; the old side inspector is intentionally retired.
  }


  function nextWorkflowNodeId(prefix = 'node') {
    const nodes = _agentWorkflowPayload?.parsed?.nodes || [];
    let n = nodes.length + 1;
    let id = `${prefix}-${n}`;
    while (nodes.some(x => x.id === id)) id = `${prefix}-${++n}`;
    return id;
  }

  function addWorkflowNode(afterId = '') {
    if (!_agentWorkflowPayload) {
      _agentWorkflowPayload = {
        text: '',
        parsed: { workflow: 'workflow', version: '1', goal: '', nodes: [], edges: [] },
        path: workflowYamlPath(),
        root: agentWorkspacePath(activeSettingsAgent()),
      };
      _agentWorkflowLoadedFor = activeSettingsAgent()?.id || null;
    }
    const nodes = _agentWorkflowPayload.parsed.nodes || (_agentWorkflowPayload.parsed.nodes = []);
    const id = nextWorkflowNodeId('node');
    const node = { id, goal: '', needs: afterId ? [afterId] : [], run: {}, steps: [], output_schema: {}, verify: {}, retry: null };
    const idx = afterId ? nodes.findIndex(n => n.id === afterId) : -1;
    nodes.splice(idx >= 0 ? idx + 1 : nodes.length, 0, node);
    _agentWorkflowExpanded.add(id);
    _agentWorkflowSelectedNodeId = id;
    workflowMarkDirty();
    renderAgentWorkflow(activeSettingsAgent());
  }

  function renderAgentWorkflow(agent = activeSettingsAgent()) {
    if (!agentWorkflowCards || !agentWorkflowSummary || !agentWorkflowRaw || !agentWorkflowVisual) return;
    const showRaw = _agentWorkflowView === 'raw';
    agentWorkflowVisual.hidden = showRaw;
    agentWorkflowRaw.hidden = !showRaw;
    for (const btn of agentWorkflowViewBtns) {
      const on = btn.dataset.agentWorkflowView === _agentWorkflowView;
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    if (!agent) {
      agentWorkflowSummary.innerHTML = '<div class="empty-state">Select an agent to load workflow.yaml.</div>';
      agentWorkflowCards.innerHTML = '';
      renderWorkflowInspector(null);
      agentWorkflowRaw.value = '';
      return;
    }
    if (_agentWorkflowLoading) {
      agentWorkflowSummary.innerHTML = '<div class="empty-state">Loading workflow.yaml...</div>';
      agentWorkflowCards.innerHTML = '';
      renderWorkflowInspector(null);
      return;
    }
    const payload = _agentWorkflowPayload;
    if (!payload) {
      agentWorkflowSummary.innerHTML = '<div class="empty-state">Click Refresh to read workflow.yaml from the selected agent workspace.</div>';
      agentWorkflowCards.innerHTML = '';
      renderWorkflowInspector(null);
      agentWorkflowRaw.value = '';
      return;
    }
    agentWorkflowRaw.value = payload.text || '';
    const parsed = payload.parsed || { nodes: [], edges: [] };
    const nodes = parsed.nodes || [];
    const edges = parsed.edges || [];
    agentWorkflowSummary.innerHTML = `
      <div class="agent-workflow-summary-main">
        <div>
          <div class="agent-workflow-title">${escapeHtml(parsed.workflow || 'workflow.yaml')}${_agentWorkflowDirty ? ' <span class="agent-workflow-dirty">unsaved</span>' : ''}</div>
          <div class="agent-workflow-goal">${escapeHtml(parsed.goal || 'No top-level goal declared.')}</div>
        </div>
        <div class="agent-workflow-stat-row">
          <span class="agent-workflow-pill">${nodes.length} nodes</span>
          <span class="agent-workflow-pill">${edges.length} edges</span>
          ${parsed.version ? `<span class="agent-workflow-pill">v${escapeHtml(parsed.version)}</span>` : ''}
        </div>
      </div>
    `;
    agentWorkflowCards.innerHTML = nodes.length ? nodes.map((n, idx) => {
      const expanded = _agentWorkflowExpanded.has(n.id);
      const needs = (n.needs || []).length ? (n.needs || []).join(', ') : 'entry';
      const verify = workflowVerifyLabel(n);
      const outputCount = Object.keys(n.output_schema || {}).length;
      const selectedClass = n.id === _agentWorkflowSelectedNodeId ? ' is-selected' : '';
      const outputText = Object.keys(n.output_schema || {}).map(k => `${k}: ${n.output_schema[k]}`).join('\n');
      const verifyText = Object.keys(n.verify || {}).map(k => `${k}: ${n.verify[k]}`).join('\n');
      return `
        <article class="agent-workflow-node-card${selectedClass}${expanded ? ' is-expanded' : ''}" data-workflow-node="${escapeAttr(n.id)}">
          <button type="button" class="agent-workflow-node-head" data-workflow-toggle="${escapeAttr(n.id)}" aria-expanded="${expanded ? 'true' : 'false'}">
            <span class="agent-workflow-node-index">${idx + 1}</span>
            <span class="agent-workflow-node-id">${escapeHtml(n.id || `node-${idx + 1}`)}</span>
            <span class="agent-workflow-node-run">${escapeHtml(workflowRunLabel(n))}</span>
            <span class="agent-workflow-node-meta">needs ${escapeHtml(needs)} · ${n.steps.length} checks · ${outputCount} outputs · verify ${escapeHtml(verify)}</span>
            <span class="agent-workflow-fold">${expanded ? 'Collapse' : 'Edit'}</span>
          </button>
          ${expanded ? `
            <div class="agent-workflow-node-brief">
              <div class="agent-workflow-node-goal">${escapeHtml(n.goal || 'No goal')}</div>
              <div class="agent-workflow-node-foot">
                <span class="agent-workflow-pill">retry ${escapeHtml(n.retry ?? 'default')}</span>
                ${(n.needs || []).map(dep => `<span class="agent-workflow-chip">${escapeHtml(dep)}</span>`).join('')}
              </div>
            </div>
            <div class="agent-workflow-editor-sections">
              <section class="agent-workflow-editor-section">
                <h4>Identity</h4>
                <div class="agent-workflow-editor-grid compact">
                  <label class="agent-workflow-field"><span>ID</span><input data-wf-field="id" value="${escapeAttr(n.id || '')}"></label>
                  <label class="agent-workflow-field"><span>Needs</span><input data-wf-field="needs" value="${escapeAttr((n.needs || []).join(', '))}" placeholder="node-a, node-b"></label>
                  <label class="agent-workflow-field"><span>Retry</span><input data-wf-field="retry" value="${escapeAttr(n.retry ?? '')}" placeholder="default"></label>
                </div>
              </section>
              <section class="agent-workflow-editor-section">
                <h4>Goal</h4>
                <label class="agent-workflow-field wide"><textarea data-wf-field="goal" rows="3">${escapeHtml(n.goal || '')}</textarea></label>
              </section>
              <section class="agent-workflow-editor-section">
                <h4>Run</h4>
                <div class="agent-workflow-editor-grid compact">
                  <label class="agent-workflow-field"><span>Skill</span><input data-wf-field="run.skill" value="${escapeAttr(n.run?.skill || '')}" placeholder="analyzer / evaluator / codex"></label>
                  <label class="agent-workflow-field wide"><span>Command</span><input data-wf-field="run.command" value="${escapeAttr(n.run?.command || '')}" placeholder="pytest -q"></label>
                </div>
              </section>
              <section class="agent-workflow-editor-section">
                <h4>Checklist</h4>
                <label class="agent-workflow-field wide"><textarea data-wf-field="steps" rows="5">${escapeHtml((n.steps || []).join('\n'))}</textarea></label>
              </section>
              <section class="agent-workflow-editor-section split">
                <div>
                  <h4>Expected Output</h4>
                  <label class="agent-workflow-field wide"><textarea data-wf-field="output_schema" rows="5">${escapeHtml(outputText)}</textarea></label>
                </div>
                <div>
                  <h4>Verify</h4>
                  <label class="agent-workflow-field wide"><textarea data-wf-field="verify" rows="5" placeholder="criterion: ...\ncommand: ...\nhuman: true">${escapeHtml(verifyText)}</textarea></label>
                </div>
              </section>
            </div>
            <div class="agent-workflow-node-actions">
              <button type="button" class="btn-secondary" data-wf-insert-after="${escapeAttr(n.id)}">Insert after</button>
              <button type="button" class="btn-secondary" data-wf-duplicate="${escapeAttr(n.id)}">Duplicate</button>
              <button type="button" class="btn-danger" data-wf-delete="${escapeAttr(n.id)}">Delete</button>
            </div>
          ` : ''}
        </article>`;
    }).join('') : '<div class="empty-state">workflow.yaml loaded, but no nodes were found. Click Add node to start.</div>';
    renderWorkflowInspector(null);
  }

  async function loadAgentWorkflow({ force = false } = {}) {
    const agent = activeSettingsAgent();
    if (!agent) {
      workflowResetForAgent(null);
      return;
    }
    if (!force && _agentWorkflowLoadedFor === agent.id && _agentWorkflowPayload) {
      renderAgentWorkflow(agent);
      return;
    }
    _agentWorkflowLoading = true;
    renderAgentWorkflow(agent);
    setAgentWorkflowStatus('Loading workflow.yaml...');
    if (agentWorkflowRefresh) agentWorkflowRefresh.disabled = true;
    try {
      await requireAgentSettingsConnected('Workflow');
      const path = workflowYamlPath();
      const resp = await api.agentReadWorkspaceFile(agent.id, path);
      if (resp && resp.binary) throw new Error('workflow.yaml is binary and cannot be rendered');
      const text = String(resp?.content || '');
      const parsed = parseWorkflowYamlV0(text);
      _agentWorkflowPayload = { text, parsed, path, root: resp?.root || agentWorkspacePath(agent) };
      _agentWorkflowLoadedFor = agent.id;
      _agentWorkflowExpanded = new Set(parsed.nodes[0]?.id ? [parsed.nodes[0].id] : []);
      _agentWorkflowSelectedNodeId = parsed.nodes[0]?.id || '';
      _agentWorkflowDirty = false;
      const root = _agentWorkflowPayload.root || agentWorkspacePath(agent);
      setAgentWorkflowStatus(`Loaded ${parsed.nodes.length} node(s) from ${root ? `${root}/` : ''}${path}.`, 'is-ok');
    } catch (err) {
      _agentWorkflowPayload = null;
      _agentWorkflowLoadedFor = null;
      _agentWorkflowDirty = false;
      const root = agentWorkspacePath(agent);
      setAgentWorkflowStatus(`Workflow load failed from ${root ? `${root}/` : ''}${workflowYamlPath()}: ${err?.message || err}`, 'is-error');
    } finally {
      _agentWorkflowLoading = false;
      if (agentWorkflowRefresh) agentWorkflowRefresh.disabled = false;
      renderAgentWorkflow(agent);
    }
  }

  function setAgentWorkflowView(view) {
    _agentWorkflowView = view === 'raw' ? 'raw' : 'visual';
    renderAgentWorkflow(activeSettingsAgent());
  }

  function setAgentSettingsTab(tab) {
    _agentSettingsTab = ['attributes', 'system-prompt', 'automation', 'workflow'].includes(tab) ? tab : 'attributes';
    for (const btn of agentSettingsTabs) {
      const on = btn.dataset.agentSettingsTab === _agentSettingsTab;
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    for (const panel of agentSettingsPanels) {
      panel.hidden = panel.dataset.agentSettingsPanel !== _agentSettingsTab;
    }
    if (_agentSettingsTab === 'system-prompt' && agentSettingsPanel && !agentSettingsPanel.hidden) {
      void loadAgentSystemPrompt();
    }
    if (_agentSettingsTab === 'automation' && agentSettingsPanel && !agentSettingsPanel.hidden) {
      void loadAgentCron();
    }
    if (_agentSettingsTab === 'workflow' && agentSettingsPanel && !agentSettingsPanel.hidden) {
      void loadAgentWorkflow();
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
      resetSystemPromptForAgent(null);
      if (agentCronTarget) agentCronTarget.textContent = 'Select an agent to manage automation.';
      if (agentCronAdd) agentCronAdd.disabled = true;
      _agentCronLoadedFor = null;
      _agentCronPayload = null;
      renderAgentCronJobs(null);
      setAgentCronStatus('');
      workflowResetForAgent(null);
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
    if (_agentSystemPromptLoadedFor && _agentSystemPromptLoadedFor !== agent.id) {
      resetSystemPromptForAgent(agent);
    } else {
      updateSystemPromptTarget(agent);
    }
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
    if (_agentWorkflowLoadedFor && _agentWorkflowLoadedFor !== agent.id) {
      workflowResetForAgent(agent);
    } else if (agentWorkflowTarget) {
      const root = agentWorkspacePath(agent);
      agentWorkflowTarget.textContent = `Loads ${root ? `${root}/` : ''}${workflowYamlPath()} from this agent workspace.`;
      if (agentWorkflowRefresh) agentWorkflowRefresh.disabled = false;
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
    setAgentSystemPromptStatus('');
    setAgentWorkflowStatus('');
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
  agentSystemPromptReload?.addEventListener('click', () => { void loadAgentSystemPrompt({ force: true }); });
  agentSystemPromptSave?.addEventListener('click', () => { void saveAgentSystemPrompt(); });
  agentSystemPromptReset?.addEventListener('click', () => {
    if (agentSystemPromptText) agentSystemPromptText.value = _agentSystemPromptInitial;
    setAgentSystemPromptStatus('Reset to loaded prompt.');
  });
  agentSystemPromptClear?.addEventListener('click', () => {
    if (agentSystemPromptText) agentSystemPromptText.value = '';
    setAgentSystemPromptStatus('Prompt will be removed when saved.');
  });
  agentCronScheduleType?.addEventListener('change', updateCronSchedulePlaceholder);
  agentCronTypeInputs.forEach(input => input.addEventListener('change', updateAutomationTypeUi));
  agentCronRefresh?.addEventListener('click', () => { void loadAgentCron({ force: true }); });
  agentCronAdd?.addEventListener('click', () => { void addAgentCronJob(); });
  agentWorkflowRefresh?.addEventListener('click', () => { void loadAgentWorkflow({ force: true }); });
  agentWorkflowValidate?.addEventListener('click', () => {
    const errors = validateWorkflow(_agentWorkflowPayload?.parsed);
    setAgentWorkflowStatus(errors.length ? `Validation: ${errors.slice(0, 3).join('; ')}${errors.length > 3 ? ` (+${errors.length - 3} more)` : ''}` : 'Workflow validates.', errors.length ? 'is-error' : 'is-ok');
  });
  agentWorkflowAdd?.addEventListener('click', () => { addWorkflowNode(_agentWorkflowSelectedNodeId); });
  agentWorkflowSave?.addEventListener('click', async () => {
    const agent = activeSettingsAgent();
    if (!agent || !_agentWorkflowPayload) return;
    try {
      await requireAgentSettingsConnected('Workflow save');
      if (_agentWorkflowView === 'raw' && agentWorkflowRaw) {
        _agentWorkflowPayload.text = agentWorkflowRaw.value || '';
        _agentWorkflowPayload.parsed = parseWorkflowYamlV0(_agentWorkflowPayload.text);
      } else {
        syncWorkflowRawFromParsed();
      }
      const errors = validateWorkflow(_agentWorkflowPayload.parsed);
      if (errors.length) throw new Error(errors.slice(0, 3).join('; '));
      await api.agentWriteWorkspaceFile(agent.id, workflowYamlPath(), _agentWorkflowPayload.text || '');
      _agentWorkflowDirty = false;
      setAgentWorkflowStatus('Saved workflow.yaml.', 'is-ok');
      renderAgentWorkflow(agent);
    } catch (err) {
      setAgentWorkflowStatus(`Save failed: ${err?.message || err}`, 'is-error');
    }
  });
  agentWorkflowExpand?.addEventListener('click', () => {
    _agentWorkflowExpanded = new Set((_agentWorkflowPayload?.parsed?.nodes || []).map(n => n.id).filter(Boolean));
    renderAgentWorkflow(activeSettingsAgent());
  });
  agentWorkflowCollapse?.addEventListener('click', () => {
    _agentWorkflowExpanded = new Set();
    renderAgentWorkflow(activeSettingsAgent());
  });
  agentWorkflowViewBtns.forEach(btn => btn.addEventListener('click', () => setAgentWorkflowView(btn.dataset.agentWorkflowView)));
  agentWorkflowRaw?.addEventListener('input', () => {
    if (!_agentWorkflowPayload) return;
    _agentWorkflowPayload.text = agentWorkflowRaw.value || '';
    _agentWorkflowPayload.parsed = parseWorkflowYamlV0(_agentWorkflowPayload.text);
    _agentWorkflowDirty = true;
    setAgentWorkflowStatus('Unsaved raw YAML edits. Click Save to write workflow.yaml.');
  });
  agentWorkflowCards?.addEventListener('click', (ev) => {
    const toggle = ev.target.closest('[data-workflow-toggle]');
    if (toggle) {
      const id = toggle.dataset.workflowToggle || '';
      _agentWorkflowSelectedNodeId = id;
      if (_agentWorkflowExpanded.has(id)) _agentWorkflowExpanded.delete(id); else _agentWorkflowExpanded.add(id);
      renderAgentWorkflow(activeSettingsAgent());
      return;
    }
    const card = ev.target.closest('[data-workflow-node]');
    if (card) _agentWorkflowSelectedNodeId = card.dataset.workflowNode || '';
    const insertAfter = ev.target.closest('[data-wf-insert-after]');
    const duplicate = ev.target.closest('[data-wf-duplicate]');
    const del = ev.target.closest('[data-wf-delete]');
    const nodes = _agentWorkflowPayload?.parsed?.nodes || [];
    if (insertAfter || duplicate) {
      const baseId = (insertAfter && insertAfter.dataset.wfInsertAfter) || (duplicate && duplicate.dataset.wfDuplicate) || '';
      const idx = nodes.findIndex(n => n.id === baseId);
      const base = nodes[idx] || {};
      let id = `${baseId || 'node'}-next`;
      let n = 2;
      while (nodes.some(x => x.id === id)) id = `${baseId || 'node'}-${n++}`;
      const node = duplicate ? JSON.parse(JSON.stringify(base)) : { id, goal: '', needs: baseId ? [baseId] : [], run: {}, steps: [], output_schema: {}, verify: {}, retry: null };
      node.id = id;
      nodes.splice(idx >= 0 ? idx + 1 : nodes.length, 0, node);
      _agentWorkflowExpanded.add(id);
      _agentWorkflowSelectedNodeId = id;
      workflowMarkDirty();
      renderAgentWorkflow(activeSettingsAgent());
      return;
    }
    if (del) {
      const id = del.dataset.wfDelete || '';
      const dependents = nodes.filter(n => (n.needs || []).includes(id)).map(n => n.id);
      if (dependents.length && !confirm(`Delete ${id}? Dependents still reference it: ${dependents.join(', ')}`)) return;
      const idx = nodes.findIndex(n => n.id === id);
      if (idx >= 0) nodes.splice(idx, 1);
      _agentWorkflowExpanded.delete(id);
      _agentWorkflowSelectedNodeId = nodes[0]?.id || '';
      workflowMarkDirty();
      renderAgentWorkflow(activeSettingsAgent());
    }
  });
  agentWorkflowCards?.addEventListener('input', (ev) => {
    const field = ev.target?.dataset?.wfField;
    if (!field) return;
    const card = ev.target.closest('[data-workflow-node]');
    const node = workflowNodeById(card?.dataset?.workflowNode || '');
    if (!node) return;
    const val = ev.target.value;
    if (field === 'id') {
      const old = node.id;
      node.id = val.trim();
      if (old && old !== node.id) {
        for (const n of (_agentWorkflowPayload?.parsed?.nodes || [])) n.needs = (n.needs || []).map(dep => dep === old ? node.id : dep);
        if (_agentWorkflowExpanded.delete(old)) _agentWorkflowExpanded.add(node.id);
        _agentWorkflowSelectedNodeId = node.id;
      }
    } else if (field === 'goal') node.goal = val;
    else if (field === 'needs') node.needs = val.split(',').map(s => s.trim()).filter(Boolean);
    else if (field === 'run.skill') { node.run = node.run || {}; if (val.trim()) node.run.skill = val.trim(); else delete node.run.skill; }
    else if (field === 'run.command') { node.run = node.run || {}; if (val.trim()) node.run.command = val.trim(); else delete node.run.command; }
    else if (field === 'retry') node.retry = val.trim() || null;
    else if (field === 'steps') node.steps = workflowLinesFromTextarea(val);
    else if (field === 'output_schema') node.output_schema = workflowObjectFromLines(val);
    else if (field === 'verify') node.verify = workflowObjectFromLines(val);
    workflowMarkDirty();
  });
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
        if (_agentSettingsTab === 'system-prompt' && _agentSettingsAgentId && _agentSettingsAgentId !== prevSettingsAgent) {
          void loadAgentSystemPrompt({ force: true });
        }
        if (_agentSettingsTab === 'automation' && _agentSettingsAgentId && _agentSettingsAgentId !== prevSettingsAgent) {
          void loadAgentCron({ force: true });
        }
        if (_agentSettingsTab === 'workflow' && _agentSettingsAgentId && _agentSettingsAgentId !== prevSettingsAgent) {
          void loadAgentWorkflow({ force: true });
        }
      }
    }
  });
}
