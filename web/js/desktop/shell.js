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

function agentDateMs(agent) {
  // Legacy: best-available recency stamp.
  return _dateMs(agent?.started_at) || _dateMs(agent?.updated_at)
      || _dateMs(agent?.completed_at) || _dateMs(agent?.created_at);
}

function agentUpdatedMs(agent) {
  return _dateMs(agent?.updated_at) || _dateMs(agent?.completed_at)
      || _dateMs(agent?.started_at) || _dateMs(agent?.created_at);
}

function agentCreatedMs(agent) {
  return _dateMs(agent?.created_at) || _dateMs(agent?.started_at)
      || _dateMs(agent?.updated_at) || _dateMs(agent?.completed_at);
}

// Sort string is stored in filters.sort as "<field>-<dir>". Legacy
// values 'date-desc' / 'date-asc' migrate to 'updated-desc' / 'updated-asc'
// so existing AppState snapshots keep working without a flag day.
const SORT_DEFAULT = 'updated-desc';
function parseSort(s) {
  let raw = String(s || SORT_DEFAULT);
  if (raw === 'date-desc') raw = 'updated-desc';
  if (raw === 'date-asc')  raw = 'updated-asc';
  const idx = raw.lastIndexOf('-');
  const field = idx > 0 ? raw.slice(0, idx) : 'updated';
  const dir   = idx > 0 ? raw.slice(idx + 1) : 'desc';
  const f = ['updated', 'created', 'name'].includes(field) ? field : 'updated';
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

export function mountShell({ state }) {
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
    const field = (sortFieldSel && sortFieldSel.value) || 'updated';
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
      const label = field === 'name' ? 'Name' : (field.charAt(0).toUpperCase() + field.slice(1));
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
          if (sortFieldSel) sortFieldSel.value = 'updated';
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
        return agentUpdatedMs(b) - agentUpdatedMs(a);
      }
      const getMs = field === 'created' ? agentCreatedMs : agentUpdatedMs;
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
      const t = timeSince(agent.started_at);
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
        </div>`;
    }).join('');

    listWrap.innerHTML = html;
    listWrap.querySelectorAll('.desktop-agent-row').forEach(el => {
      el.addEventListener('click', () => {
        state.set('selectedAgentId', el.dataset.id);
      });
    });
  }

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
    }
  });
}
