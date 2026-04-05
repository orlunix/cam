import { api, state, navigate } from '../app.js';

let expandedId = null;

export function renderMachines(container) {
  container.innerHTML = `
    <div class="page-header">
      <h2>Machines</h2>
    </div>
    <div id="machine-list-container"></div>
  `;

  function renderList() {
    const contexts = state.get('contexts') || [];
    const agents = state.get('agents') || [];
    const listEl = container.querySelector('#machine-list-container');
    if (!listEl) return;

    // Build machine list from contexts
    const machineMap = new Map();

    for (const c of contexts) {
      const m = c.machine || {};
      const host = m.host || 'local';
      const port = m.port || null;
      const key = host === 'local' ? 'local' : `${host}:${port || 'default'}`;

      if (!machineMap.has(key)) {
        machineMap.set(key, {
          key, host,
          user: m.user || '',
          port,
          env_setup: m.env_setup || '',
          isSSH: !!(m.type === 'ssh' || m.host),
          contexts: [],
        });
      }
      machineMap.get(key).contexts.push(c);
    }

    // Also pick up machines from agents that have no matching context
    for (const a of agents) {
      const host = a.machine_host || 'local';
      const user = a.machine_user || '';
      const port = a.machine_port || null;
      const key = host === 'local' ? 'local' : `${host}:${port || 'default'}`;
      if (!machineMap.has(key)) {
        machineMap.set(key, {
          key, host, user, port,
          env_setup: '',
          isSSH: host !== 'local',
          contexts: [],
        });
      }
    }

    // Count agents per machine
    const machines = [...machineMap.values()].map(m => {
      const machineAgents = agents.filter(a => {
        const aHost = a.machine_host || 'local';
        if (m.host === 'local') return aHost === 'local';
        return aHost === m.host;
      });
      return {
        ...m,
        agents: machineAgents,
        agentCount: machineAgents.length,
        runningCount: machineAgents.filter(a => a.status === 'running').length,
        completedCount: machineAgents.filter(a => a.status !== 'running').length,
      };
    });

    // Sort: SSH machines first, then by host
    machines.sort((a, b) => {
      if (a.isSSH !== b.isSSH) return a.isSSH ? -1 : 1;
      return a.host.localeCompare(b.host);
    });

    if (machines.length === 0) {
      listEl.innerHTML = '<div class="empty-state">No machines. Add a context with SSH to see remote machines.</div>';
      return;
    }

    listEl.innerHTML = `<div class="context-list">${machines.map(m => {
      const shortHost = m.host === 'local' ? 'local' : m.host.split('.')[0];
      const isExpanded = expandedId === m.key;
      const typeBadge = m.isSSH
        ? `<span class="context-type-badge ssh">SSH</span>`
        : `<span class="context-type-badge local">local</span>`;
      const countBadge = m.runningCount > 0
        ? `<span class="machine-agent-badge running">${m.runningCount} running</span>`
        : m.agentCount > 0
          ? `<span class="machine-agent-badge">${m.agentCount} agents</span>`
          : '';

      let details = '';
      if (m.isSSH) {
        details += `
          <div class="context-detail-row">
            <span class="context-detail-label">Host</span>
            <span class="context-detail-value host">${esc(m.user ? m.user + '@' : '')}${esc(m.host)}${m.port ? ':' + m.port : ''}</span>
          </div>`;
      }
      if (m.contexts.length > 0) {
        details += `
          <div class="context-detail-row">
            <span class="context-detail-label">Ctx</span>
            <span class="context-detail-value">${m.contexts.map(c => esc(c.name)).join(', ')}</span>
          </div>`;
      }
      if (m.agentCount > 0) {
        details += `
          <div class="context-detail-row">
            <span class="context-detail-label">Agents</span>
            <span class="context-detail-value">${m.runningCount} running / ${m.agentCount} total</span>
          </div>`;
      }
      if (m.env_setup) {
        details += `
          <div class="context-detail-row">
            <span class="context-detail-label">Env</span>
            <span class="context-detail-value">${esc(m.env_setup)}</span>
          </div>`;
      }

      // Actions
      let actions = `
        <button class="btn-sm btn-secondary filter-machine" data-host="${esc(m.host)}">Filter Agents</button>`;
      if (m.isSSH && m.contexts.length > 0) {
        actions += `<button class="btn-sm btn-secondary sync-machine" data-key="${esc(m.key)}">Sync</button>`;
      }
      if (m.completedCount > 0) {
        actions += `<button class="btn-sm btn-secondary clean-machine" data-key="${esc(m.key)}">Clean ${m.completedCount}</button>`;
      }
      if (m.contexts.length > 0) {
        actions += `<button class="btn-sm btn-danger delete-machine" data-key="${esc(m.key)}">Delete</button>`;
      }

      return `
        <div class="context-card${isExpanded ? ' expanded' : ''}" data-key="${esc(m.key)}">
          <div class="context-card-header">
            <div class="context-card-left">
              <span class="context-name">${esc(shortHost)}</span>
              ${typeBadge}
              ${countBadge}
            </div>
            <span class="context-chevron">\u25B8</span>
          </div>
          <div class="context-card-body">
            <div class="context-detail-rows">${details}</div>
            <div class="context-actions">${actions}</div>
          </div>
        </div>`;
    }).join('')}</div>`;

    // Toggle expand
    listEl.querySelectorAll('.context-card-header').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const card = hdr.closest('.context-card');
        const key = card.dataset.key;
        if (expandedId === key) {
          expandedId = null;
          card.classList.remove('expanded');
        } else {
          const prev = listEl.querySelector('.context-card.expanded');
          if (prev) prev.classList.remove('expanded');
          expandedId = key;
          card.classList.add('expanded');
        }
      });
    });

    // Filter agents by machine
    listEl.querySelectorAll('.filter-machine').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const host = btn.dataset.host;
        state.set('filters', { ...state.get('filters'), machine: host === 'local' ? 'local' : host });
        navigate('/');
      });
    });

    // Sync all contexts on this machine
    listEl.querySelectorAll('.sync-machine').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const machine = machines.find(m => m.key === btn.dataset.key);
        if (!machine) return;
        btn.disabled = true;
        btn.textContent = 'Syncing...';
        let synced = 0, unchanged = 0;
        for (const ctx of machine.contexts) {
          try {
            const resp = await api.syncContext(ctx.name);
            const results = resp.results || {};
            synced += Object.values(results).filter(s => s === 'deployed' || s === 'updated').length;
            unchanged += Object.values(results).filter(s => s === 'unchanged').length;
          } catch (err) {
            state.toast(`Sync ${ctx.name} failed: ${err.message}`, 'error');
          }
        }
        state.toast(`Synced ${machine.contexts.length} ctx: ${synced} updated, ${unchanged} unchanged`, 'success');
        btn.disabled = false;
        btn.textContent = 'Sync';
      });
    });

    // Clean completed agents on this machine
    listEl.querySelectorAll('.clean-machine').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const machine = machines.find(m => m.key === btn.dataset.key);
        if (!machine) return;
        const completed = machine.agents.filter(a => a.status !== 'running');
        if (!completed.length) return;
        btn.disabled = true;
        btn.textContent = 'Cleaning...';
        let cleaned = 0;
        for (const a of completed) {
          try {
            await api.deleteAgentHistory(a.id);
            cleaned++;
          } catch {}
        }
        // Refresh agent list
        try {
          const resp = await api.listAgents({ limit: 50 });
          state.set('agents', resp.agents || []);
        } catch {}
        state.toast(`Cleaned ${cleaned} completed agents`, 'success');
      });
    });

    // Delete machine (all contexts + agents)
    listEl.querySelectorAll('.delete-machine').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const machine = machines.find(m => m.key === btn.dataset.key);
        if (!machine) return;
        const shortHost = machine.host === 'local' ? 'local' : machine.host.split('.')[0];
        const msg = `Delete machine "${shortHost}"?\n\n` +
          `This will remove ${machine.contexts.length} context(s): ${machine.contexts.map(c => c.name).join(', ')}` +
          (machine.runningCount > 0 ? `\n\nWARNING: ${machine.runningCount} agent(s) still running!` : '');
        if (!confirm(msg)) return;
        btn.disabled = true;
        btn.textContent = 'Deleting...';
        let deleted = 0;
        for (const ctx of machine.contexts) {
          try {
            await api.deleteContext(ctx.name);
            deleted++;
          } catch (err) {
            state.toast(`Delete ${ctx.name} failed: ${err.message}`, 'error');
          }
        }
        // Clean agent history too
        for (const a of machine.agents.filter(a => a.status !== 'running')) {
          try { await api.deleteAgentHistory(a.id); } catch {}
        }
        // Refresh
        try {
          const [ctxResp, agentResp] = await Promise.all([
            api.listContexts(),
            api.listAgents({ limit: 50 }),
          ]);
          state.set('contexts', ctxResp.contexts || []);
          state.set('agents', agentResp.agents || []);
        } catch {}
        expandedId = null;
        state.toast(`Deleted ${deleted} context(s) from ${shortHost}`, 'success');
      });
    });
  }

  renderList();

  let prevContexts = state.get('contexts');
  let prevAgents = state.get('agents');
  const unsub = state.subscribe(() => {
    const curContexts = state.get('contexts');
    const curAgents = state.get('agents');
    if (curContexts !== prevContexts || curAgents !== prevAgents) {
      prevContexts = curContexts;
      prevAgents = curAgents;
      renderList();
    }
  });

  return () => { unsub(); expandedId = null; };
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}
