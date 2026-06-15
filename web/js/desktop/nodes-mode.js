/**
 * Desktop Nodes mode (CAM-DESK-NODEUI-010..017, DIRECT-015..017).
 *
 * Host-card workspace built from `state.contexts` + `state.agents`.
 * Each host (endpoint identity = type | user | host | port) renders
 * one card. The card has two collapse levels:
 *
 *   - **Card level**: collapsed shows host identity + counts; click
 *     the header to expand and reveal the host-action stack
 *     (Filter Agents, Edit Host, Sync Host, Delete Host, New Context)
 *     plus the contexts list.
 *   - **Context-row level**: collapsed shows context name + remote
 *     path + small [edit] / [delete] buttons; click the row header
 *     to reveal env_setup and the last-sync diagnostic detail.
 *
 * Action ownership:
 *   - Host-level:   Filter Agents, Edit Host, Sync Host, Delete
 *                   Host, New Context. Host actions never appear
 *                   on context rows.
 *   - Context-level: Edit Context (name read-only, remote path,
 *                   env_setup) and Delete Context
 *                   only. No per-context Sync; Sync Host covers
 *                   the host and surfaces the result back on each
 *                   row's last-sync badge.
 *
 * New Context: opens the manage panel in add-context mode with host
 * fields hidden (inherited from the parent host) and workspace
 * fields visible. For password-auth hosts the password sub-form is
 * shown and Remember password is required — the embedded Hub does
 * not auto-clone credential references to new contexts, so the user
 * must re-enter the password for the new record. Renderer never
 * touches existing credential bytes.
 *
 * The embedded Hub currently persists `machine` fields per context.
 * The renderer treats that as an internal compatibility detail and
 * presents Host vs Context as the user-facing boundary; a
 * normalized `/api/nodes` backend remains future work.
 *
 * Mobile/PWA byte-equal: this module never imports any file under
 * `web/js/views/*` or `web/js/app.js` / `state.js`.
 */

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

export function mountNodesMode({
  api,
  state,
  showToast,
  setMode,
  loadContextsAndAdapters,
  loadAgents,
  connect,
}) {
  const panel = document.getElementById('mode-nodes');
  if (!panel) return;
  const listEl   = panel.querySelector('#nodes-list-wrap');
  const statusEl = panel.querySelector('#nodes-status');
  if (!listEl) return;

  // Card-level fold: at most one host card is expanded at a time.
  // `undefined` means first render should open the first host; `null`
  // is an intentional all-collapsed state after the user folds it.
  let expandedHostKey = undefined;
  // Context-row fold: a Set of context names that are expanded.
  // Independent per row, so multiple rows can stay expanded across
  // re-renders.
  const expandedCtxNames = new Set();

  // Per-context last-sync diagnostics. Survives re-renders so the
  // user can read why agents didn't appear long after the warning
  // toast disappeared.
  const lastSync = new Map();

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }


  async function ensureNodesConnected() {
    if (api && api.mode !== 'disconnected') return true;
    if (typeof connect === 'function') {
      const mode = await connect();
      return mode && mode !== 'disconnected' && api && api.mode !== 'disconnected';
    }
    return false;
  }

  async function requireNodesConnected(label = 'Action') {
    const ok = await ensureNodesConnected();
    if (!ok) throw new Error(`${label} requires an active Direct or Relay connection`);
  }

  function fmtAgo(ts) {
    if (!ts) return '';
    const s = Math.floor((Date.now() - ts) / 1000);
    if (s < 5)    return 'just now';
    if (s < 60)   return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function renderLastSyncInline(ctxName) {
    const e = lastSync.get(ctxName);
    if (!e) return `<span class="ctx-last-sync state-never">never synced</span>`;
    const ago = fmtAgo(e.ts);
    if (e.status === 'running') {
      return `<span class="ctx-last-sync state-checking">syncing…</span>`;
    }
    if (e.status === 'success') {
      const imp = (typeof e.imported === 'number') ? `${e.imported} imported` : 'updated';
      return `<span class="ctx-last-sync state-running">${esc(imp)}</span>` +
             `<span class="ctx-last-sync-time">${esc(ago)}</span>`;
    }
    const code   = e.code || 'failed';
    const detail = (e.detail || e.exception || '').replace(/\s+/g, ' ').trim();
    const short  = detail.length > 200 ? detail.slice(0, 200) + '…' : detail;
    const cls    = e.status === 'warning' ? 'state-port-conflict' : 'state-error';
    return `<span class="ctx-last-sync ${cls}">${esc(code)}</span>` +
           (short ? `<span class="ctx-last-sync-detail">${esc(short)}</span>` : '') +
           `<span class="ctx-last-sync-time">${esc(ago)}</span>`;
  }

  function lastSyncOneLine(ctxName) {
    const e = lastSync.get(ctxName);
    if (!e) return 'never synced';
    const ago = fmtAgo(e.ts);
    if (e.status === 'running') return 'syncing…';
    if (e.status === 'success') {
      const imp = (typeof e.imported === 'number') ? `${e.imported} imported` : 'updated';
      return `${imp}, ${ago}`;
    }
    const code   = e.code || 'failed';
    const detail = (e.detail || e.exception || '').replace(/\s+/g, ' ').trim();
    const short  = detail.length > 80 ? detail.slice(0, 80) + '…' : detail;
    return short ? `${code} — ${short}, ${ago}` : `${code}, ${ago}`;
  }

  function contextClipboardText(ctx) {
    const m = (ctx && ctx.machine) || {};
    const lines = [
      `Context: ${ctx.name || ''}`,
      `Path: ${ctx.path || ''}`,
    ];
    if (m.env_setup) lines.push(`Env: ${m.env_setup}`);
    if (m.host || m.user || m.port) {
      const host = `${m.user || ''}@${m.host || 'local'}:${m.port || 22}`;
      lines.push(`Host: ${host}`);
    }
    if (m.auth_method) lines.push(`Auth: ${m.auth_method}`);
    return lines.join('\n');
  }

  async function copyText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(text);
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
    } finally {
      document.body.removeChild(ta);
    }
  }

  function normalizePort(port, isSSH = false) {
    const n = Number.parseInt(port, 10);
    if (Number.isFinite(n) && n >= 1 && n <= 65535) return n;
    return isSSH ? 22 : null;
  }

  // Host key — transport identity. SSH endpoints include user, so
  // hren@h:22 and vmhren@h:22 are two distinct host cards.
  function hostKeyForMachine({ type, host, user, port }) {
    const h = host || 'local';
    const isSSH = !!(type === 'ssh' || (h && h !== 'local'));
    if (!isSSH) return 'local';
    return `${user || ''}@${h}:${normalizePort(port, true)}`;
  }

  function agentMatchesHost(agent, node) {
    const aHost = agent.machine_host || 'local';
    const aType = agent.machine_type || (aHost === 'local' ? 'local' : 'ssh');
    const isSSH = !!(aType === 'ssh' || (aHost && aHost !== 'local'));
    if (!node.isSSH) return !isSSH || aHost === 'local';
    return aHost === node.host &&
      (agent.machine_user || '') === (node.user || '') &&
      normalizePort(agent.machine_port, true) === normalizePort(node.port, true);
  }

  function buildHosts() {
    const contexts = state.get('contexts') || [];
    const agents = state.get('agents') || [];
    const map = new Map();

    for (const c of contexts) {
      const m = c.machine || {};
      const host = m.host || 'local';
      const isSSH = !!(m.type === 'ssh' || (m.host && m.host !== 'local'));
      const user = m.user || '';
      const port = normalizePort(m.port, isSSH);
      const key = hostKeyForMachine({ type: m.type, host, user, port });
      if (!map.has(key)) {
        map.set(key, { key, host, user, port, isSSH, contexts: [] });
      }
      map.get(key).contexts.push(c);
    }

    // Orphan agents (no matching context machine) still contribute
    // a host card with zero contexts.
    for (const a of agents) {
      const host = a.machine_host || 'local';
      const isSSH = !!(a.machine_type === 'ssh' || (host && host !== 'local'));
      const user = a.machine_user || '';
      const port = normalizePort(a.machine_port, isSSH);
      const key = hostKeyForMachine({ type: a.machine_type, host, user, port });
      if (!map.has(key)) {
        map.set(key, { key, host, user, port, isSSH, contexts: [] });
      }
    }

    const hosts = [...map.values()].map(n => {
      const nodeAgents = agents.filter(a => agentMatchesHost(a, n));
      return {
        ...n,
        agents: nodeAgents,
        agentCount: nodeAgents.length,
        runningCount: nodeAgents.filter(a => a.status === 'running').length,
      };
    });

    hosts.sort((a, b) => {
      if (a.isSSH !== b.isSSH) return a.isSSH ? -1 : 1;
      const ah = a.host.localeCompare(b.host);
      if (ah !== 0) return ah;
      return (a.user || '').localeCompare(b.user || '');
    });

    return hosts;
  }

  /** Sync one context. Updates lastSync entry and re-renders. */
  async function syncOneContext(ctx) {
    lastSync.set(ctx.name, { ts: Date.now(), status: 'running' });
    render();
    let resp = null;
    try {
      await requireNodesConnected('Sync');
      resp = await api.syncContext(ctx.name);
    } catch (err) {
      const msg  = (err && err.message) || String(err);
      const code = err && err.status === 501 ? 'context_sync_501_legacy' : 'exception';
      lastSync.set(ctx.name, {
        ts: Date.now(), status: 'error', code, detail: msg, exception: msg,
      });
      render();
      return { ok: false, error: code, detail: msg };
    }

    const results = (resp && resp.results) || {};
    const values  = Object.values(results);
    const fileFailed    = values.filter(s => s === 'failed').length;
    const fileUpdated   = values.filter(s => s === 'deployed' || s === 'updated').length;
    const fileUnchanged = values.filter(s => s === 'unchanged').length;

    if (resp && resp.ok === false) {
      lastSync.set(ctx.name, {
        ts: Date.now(), status: 'error',
        code: resp.error || 'failed', detail: resp.detail || '',
        imported: 0, total: resp.total || 0,
      });
    } else if (fileFailed > 0) {
      lastSync.set(ctx.name, {
        ts: Date.now(), status: 'warning', code: 'partial_failed',
        detail: `${fileFailed} file(s) failed: ` + Object.entries(results)
                  .filter(([, s]) => s === 'failed').map(([k]) => k).join(', '),
        imported: resp && typeof resp.imported === 'number' ? resp.imported : 0,
        total:    resp && resp.total || 0,
      });
    } else {
      lastSync.set(ctx.name, {
        ts: Date.now(), status: 'success',
        code:   values.length ? (values[0] || 'updated') : 'updated',
        detail: '',
        imported: resp && typeof resp.imported === 'number' ? resp.imported : 0,
        total:    resp && resp.total || 0,
      });
    }
    render();
    return Object.assign({}, resp, {
      _client: { fileFailed, fileUpdated, fileUnchanged },
    });
  }

  /* ─────────────────────── Card rendering ─────────────────────── */

  function renderHostHeader(node) {
    const shortHost = node.host === 'local' ? 'local' : node.host.split('.')[0];
    const typeBadge = node.isSSH
      ? `<span class="context-type-badge ssh">SSH</span>`
      : `<span class="context-type-badge local">local</span>`;
    const id = node.isSSH
      ? `${esc(node.user || '')}@${esc(node.host)}:${esc(String(node.port || 22))}`
      : '';
    const ctxCount = node.contexts.length > 0
      ? `<span class="host-ctx-chip">${node.contexts.length} ctx</span>` : '';
    const agentBadge = node.runningCount > 0
      ? `<span class="machine-agent-badge running">${node.runningCount} running</span>`
      : node.agentCount > 0
        ? `<span class="machine-agent-badge">${node.agentCount} agents</span>`
        : '';
    return `
      <div class="host-card-header" data-key="${esc(node.key)}">
        <span class="host-chevron">&#9656;</span>
        <span class="host-short">${esc(shortHost)}</span>
        ${typeBadge}
        ${id ? `<span class="host-id">${id}</span>` : ''}
        ${ctxCount}
        ${agentBadge}
      </div>`;
  }

  function renderHostActions(node) {
    const acts = [
      `<button type="button" class="btn-sm filter-agents-btn" data-key="${esc(node.key)}">Filter Agents</button>`,
    ];
    if (node.isSSH && node.contexts.length > 0) {
      acts.push(
        `<button type="button" class="btn-sm host-edit-btn"  data-key="${esc(node.key)}">Edit Host</button>`,
        `<button type="button" class="btn-sm sync-host-btn"  data-key="${esc(node.key)}">Sync Host</button>`,
        `<button type="button" class="btn-sm host-add-ctx-btn" data-key="${esc(node.key)}">New Context</button>`,
        `<button type="button" class="btn-sm btn-danger host-delete-btn" data-key="${esc(node.key)}">Delete Host</button>`,
      );
    }
    return `<div class="host-card-actions">${acts.join('')}</div>`;
  }

  function renderContextRow(ctx) {
    const expanded = expandedCtxNames.has(ctx.name);
    const env = (ctx.machine && ctx.machine.env_setup) || '';
    const lastOneLine = lastSyncOneLine(ctx.name);
    const body = expanded ? `
      <div class="ctx-row-body">
        ${env
          ? `<div class="ctx-row-meta"><span class="ctx-meta-label">env</span> <span class="ctx-meta-val">${esc(env)}</span></div>`
          : `<div class="ctx-row-meta dim">env: (none)</div>`}
        <div class="ctx-row-meta">
          <span class="ctx-meta-label">last sync</span>
          ${renderLastSyncInline(ctx.name)}
        </div>
      </div>` : '';
    return `
      <div class="ctx-row${expanded ? ' expanded' : ''}" data-name="${esc(ctx.name)}">
        <div class="ctx-row-header">
          <span class="ctx-row-chevron">&#9656;</span>
          <div class="ctx-row-main">
            <div class="ctx-row-title">
              <span class="ctx-name">${esc(ctx.name)}</span>
              <span class="ctx-path">${esc(ctx.path || '')}</span>
            </div>
            <div class="ctx-row-sub dim">last sync: ${esc(lastOneLine)}</div>
          </div>
          <div class="ctx-row-actions">
            <button type="button" class="btn-xs ctx-copy-context-btn"   data-name="${esc(ctx.name)}">copy</button>
            <button type="button" class="btn-xs ctx-edit-context-btn"   data-name="${esc(ctx.name)}">edit</button>
            <button type="button" class="btn-xs btn-xs-danger ctx-delete-context-btn" data-name="${esc(ctx.name)}">delete</button>
          </div>
        </div>
        ${body}
      </div>`;
  }

  function renderCard(node) {
    const isExpanded = expandedHostKey === node.key;
    const body = isExpanded ? `
      <div class="host-card-body">
        ${renderHostActions(node)}
        ${node.agentCount > 0
          ? `<div class="host-card-agents dim">${node.runningCount} running / ${node.agentCount} total agent(s)</div>`
          : ''}
        ${node.contexts.length === 0
          ? `<div class="host-card-contexts">
              <div class="host-card-ctx-head">Contexts (0)</div>
              <div class="empty-state">No contexts on this host yet. Use <strong>New Context</strong> to add one.</div>
            </div>`
          : `<div class="host-card-contexts">
              <div class="host-card-ctx-head">Contexts (${node.contexts.length})</div>
              ${node.contexts.map(renderContextRow).join('')}
            </div>`}
      </div>` : '';
    return `
      <div class="host-card${isExpanded ? ' expanded' : ''}" data-key="${esc(node.key)}">
        ${renderHostHeader(node)}
        ${body}
      </div>`;
  }

  function render() {
    const hosts = buildHosts();
    if (hosts.length === 0) {
      const haveAnything =
        (state.get('contexts') || []).length || (state.get('agents') || []).length;
      const connected = state.get('connectionMode') !== 'disconnected'
        && (!api || api.mode !== 'disconnected');
      listEl.innerHTML = haveAnything
        ? `<div class="empty-state">No hosts resolved from current contexts/agents.</div>`
        : !connected
          ? `<div class="empty-state">Not connected to Direct or Relay. Open <strong>Settings</strong>, start Direct, then return to Nodes.</div>`
        : `<div class="empty-state">Embedded Hub is running. No hosts yet. Click <strong>Add Host</strong>.</div>`;
      return;
    }

    // Default expanded: the first host only on first render. After
    // the user folds the open card, `expandedHostKey === null` is
    // preserved as an intentional all-collapsed state.
    if (expandedHostKey === undefined) {
      expandedHostKey = hosts[0].key;
    } else if (expandedHostKey !== null && !hosts.find(h => h.key === expandedHostKey)) {
      expandedHostKey = hosts[0].key;
    }

    listEl.innerHTML = `<div class="host-card-list">${hosts.map(renderCard).join('')}</div>`;
    wireHandlers(hosts);
  }

  function wireHandlers(hosts) {
    // ─ Card-level fold/unfold ────────────────────────────────────
    listEl.querySelectorAll('.host-card-header').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const key = hdr.dataset.key;
        expandedHostKey = (expandedHostKey === key) ? null : key;
        render();
      });
    });

    // ─ Context-row fold/unfold ───────────────────────────────────
    listEl.querySelectorAll('.ctx-row-header').forEach(hdr => {
      hdr.addEventListener('click', (e) => {
        if (e.target.closest('.ctx-row-actions')) return;
        const row = hdr.closest('.ctx-row');
        const name = row && row.dataset.name;
        if (!name) return;
        if (expandedCtxNames.has(name)) expandedCtxNames.delete(name);
        else expandedCtxNames.add(name);
        render();
      });
    });

    // ─ Host-level actions ────────────────────────────────────────

    listEl.querySelectorAll('.filter-agents-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const node = hosts.find(n => n.key === btn.dataset.key);
        if (!node) return;
        const prev = state.get('filters') || {};
        state.set('filters', { ...prev, machine: node.host === 'local' ? 'local' : node.host });
        if (typeof setMode === 'function') setMode('agents');
      });
    });

    listEl.querySelectorAll('.host-edit-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const node = hosts.find(n => n.key === btn.dataset.key);
        if (!node) return;
        if (typeof panel._openEditHost === 'function') panel._openEditHost(node);
      });
    });

    listEl.querySelectorAll('.host-delete-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const node = hosts.find(n => n.key === btn.dataset.key);
        if (!node) return;
        const names   = node.contexts.map(c => c.name);
        const nameStr = names.length ? names.join(', ') : '(none)';
        const epLabel = node.isSSH ? `${node.user}@${node.host}:${node.port}` : node.host;
        const ok = window.confirm(
          `Delete host "${epLabel}"?\n\n` +
          `This will remove ${names.length} context record(s):\n  ${nameStr}\n\n` +
          `Agents already imported into this Hub will remain in the ` +
          `agent list. No remote cleanup is performed.`
        );
        if (!ok) return;

        btn.disabled = true;
        const originalText = btn.textContent;
        btn.textContent = 'Deleting…';
        setStatus(`Deleting host ${epLabel} (${names.length} context(s))…`);
        let removed = 0;
        const failures = [];
        try { await requireNodesConnected('Delete host'); }
        catch (err) {
          btn.disabled = false;
          btn.textContent = originalText;
          setStatus(`Delete host failed: ${err.message}`, 'is-error');
          showToast(`Delete host failed: ${err.message}`, 'error', 5000);
          return;
        }
        for (const name of names) {
          try {
            await api.deleteContext(name);
            lastSync.delete(name);
            expandedCtxNames.delete(name);
            removed++;
          } catch (err) {
            failures.push(`${name}:${(err && err.message) || 'failed'}`);
          }
        }
        if (expandedHostKey === node.key) expandedHostKey = null;
        try { await loadContextsAndAdapters(); } catch (_) {}
        if (typeof loadAgents === 'function') {
          try { await loadAgents(); } catch (_) {}
        }
        if (failures.length === 0) {
          showToast(`Deleted host "${epLabel}" (${removed} context(s))`, 'success');
          setStatus(`Deleted host "${epLabel}" (${removed} context(s)).`, 'is-ok');
        } else {
          const head = failures.slice(0, 4).join(', ');
          const tail = failures.length > 4 ? ', …' : '';
          showToast(
            `Delete host "${epLabel}": ${removed} removed, ${failures.length} failed (${head}${tail})`,
            'warning', 6000,
          );
          setStatus(
            `Delete host "${epLabel}": ${removed} removed, ${failures.length} failed (${head}${tail}).`,
            'is-error',
          );
          btn.disabled = false;
          btn.textContent = originalText;
        }
      });
    });

    listEl.querySelectorAll('.sync-host-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const node = hosts.find(n => n.key === btn.dataset.key);
        if (!node || node.contexts.length === 0) return;
        const primary = node.contexts[0];
        const others  = node.contexts.slice(1);
        btn.disabled = true;
        const originalText = btn.textContent;
        btn.textContent = 'Syncing…';
        const epLabel = `${node.user}@${node.host}:${node.port}`;
        setStatus(`Syncing host ${epLabel} (representative: ${primary.name})…`);

        const resp = await syncOneContext(primary);

        if (resp && resp.ok === false) {
          for (const ctx of others) {
            lastSync.set(ctx.name, {
              ts: Date.now(), status: 'error', code: 'covered_failed',
              detail: `covered by primary sync of "${primary.name}" — see that row`,
            });
          }
          render();
          const code   = resp.error  || 'failed';
          const detail = resp.detail || '';
          const summary = `Sync host "${epLabel}": ${primary.name}:${code}` +
            (detail ? ` — ${detail.slice(0, 200)}` : '');
          showToast(summary, 'warning', 6000);
          setStatus(summary, 'is-error');
          btn.disabled = false;
          btn.textContent = originalText;
          return;
        }

        for (const ctx of others) {
          lastSync.set(ctx.name, {
            ts: Date.now(), status: 'success', code: 'covered',
            detail:   `covered by primary sync of "${primary.name}"`,
            imported: 0,
          });
        }
        render();

        try { await loadContextsAndAdapters(); } catch (_) {}
        if (typeof loadAgents === 'function') {
          try { await loadAgents(); } catch (_) {}
        }

        const fc = (resp && resp._client) || {};
        const parts = [];
        if (fc.fileUpdated)   parts.push(`${fc.fileUpdated} updated`);
        if (fc.fileUnchanged) parts.push(`${fc.fileUnchanged} unchanged`);
        if (typeof resp.imported === 'number' && resp.imported > 0) {
          parts.push(`${resp.imported} agent(s) imported`);
        }
        if (fc.fileFailed) parts.push(`${fc.fileFailed} file(s) failed`);
        const coveredNote = others.length
          ? ` (covers ${others.length + 1} context(s) on this host)` : '';
        const summary =
          `Synced host "${epLabel}"` +
          (parts.length ? `: ${parts.join(', ')}` : '') +
          coveredNote;
        showToast(summary, fc.fileFailed ? 'warning' : 'success');
        setStatus(summary, fc.fileFailed ? 'is-error' : 'is-ok');
        btn.disabled = false;
        btn.textContent = originalText;
      });
    });

    // New Context — open the manage panel in add-context mode for
    // this host. Host fields are hidden; workspace fields are
    // entered fresh. For password-auth hosts, Remember password is
    // required.
    listEl.querySelectorAll('.host-add-ctx-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const node = hosts.find(n => n.key === btn.dataset.key);
        if (!node) return;
        if (typeof panel._openAddContext === 'function') panel._openAddContext(node);
      });
    });

    // ─ Context-level actions ─────────────────────────────────────

    listEl.querySelectorAll('.ctx-copy-context-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = btn.dataset.name;
        const ctx = (state.get('contexts') || []).find(c => c.name === name);
        if (!ctx) return;
        const oldText = btn.textContent;
        btn.disabled = true;
        try {
          await copyText(contextClipboardText(ctx));
          btn.textContent = 'copied';
          showToast(`Copied context "${name}"`, 'success');
          setTimeout(() => { btn.textContent = oldText; btn.disabled = false; }, 1000);
        } catch (err) {
          btn.textContent = oldText;
          btn.disabled = false;
          showToast(`Copy ${name} failed: ${err?.message || err}`, 'error', 5000);
        }
      });
    });

    listEl.querySelectorAll('.ctx-edit-context-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const name = btn.dataset.name;
        const ctx  = (state.get('contexts') || []).find(c => c.name === name);
        if (!ctx) return;
        if (typeof panel._openEditContext === 'function') panel._openEditContext(ctx);
      });
    });

    listEl.querySelectorAll('.ctx-delete-context-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = btn.dataset.name;
        if (!name) return;
        const ok = window.confirm(
          `Delete context "${name}"?\n\n` +
          `Only this context's workspace record is removed. Other ` +
          `contexts on the same host are untouched. Agents already ` +
          `imported into this Hub remain in the agent list. No ` +
          `remote cleanup is performed.`
        );
        if (!ok) return;
        btn.disabled = true;
        const originalText = btn.textContent;
        btn.textContent = '…';
        try {
          await requireNodesConnected('Delete context');
          await api.deleteContext(name);
          lastSync.delete(name);
          expandedCtxNames.delete(name);
          try { await loadContextsAndAdapters(); } catch (_) {}
          if (typeof loadAgents === 'function') {
            try { await loadAgents(); } catch (_) {}
          }
          showToast(`Deleted context "${name}"`, 'success');
          setStatus(`Deleted context "${name}".`, 'is-ok');
        } catch (err) {
          btn.disabled = false;
          btn.textContent = originalText;
          showToast(`Delete ${name} failed: ${err.message}`, 'error', 5000);
          setStatus(`Delete "${name}" failed: ${err.message}`, 'is-error');
        }
      });
    });
  }

  render();

  let prevContexts = state.get('contexts');
  let prevAgents = state.get('agents');
  state.subscribe(() => {
    const c = state.get('contexts');
    const a = state.get('agents');
    if (c !== prevContexts || a !== prevAgents) {
      prevContexts = c;
      prevAgents = a;
      render();
    }
  });

  mountNodesActions({
    panel, api, state, showToast, setStatus,
    loadContextsAndAdapters, loadAgents, requireNodesConnected,
  });
}

/* ─────────── Add Host manage panel (CAM-DESK-DIRECT-014..018) ───────────
 * One inline panel hosts four flows, differentiated by which
 * `data-*-hide` / `data-*-only` attributes are toggled:
 *   - Add Host:       host + workspace fields visible (create new host).
 *   - Edit Host:      host fields visible; workspace fields hidden;
 *                     submit fan-outs to every context on the host.
 *   - Edit Context:   name is visible/read-only; remote path and
 *                     env setup are editable; host fields are visible
 *                     but disabled/grey; submit PUTs path/env only.
 *   - Add Context:    workspace fields visible; host fields hidden
 *                     (inherited from the parent host); submit POSTs
 *                     a new context that copies the host's machine
 *                     fields. For password-auth hosts the password
 *                     sub-form is shown and Remember is required —
 *                     the embedded Hub does not auto-clone credential
 *                     references to new contexts.
 *
 * The renderer never touches private-key contents. Key file is a
 * path reference only (with a real Electron file picker behind
 * Browse). Optional Remember stores password/passphrase via
 * Electron safeStorage in main; the renderer never persists secrets
 * to localStorage. */

function bridgeFiles() {
  const b = typeof window !== 'undefined' ? window.CamBridge : null;
  return (b && b.files) || null;
}

function mountNodesActions({ panel, api, state, showToast, setStatus, loadContextsAndAdapters, loadAgents, requireNodesConnected }) {
  const addToggle    = panel.querySelector('#nodes-add-toggle');
  const managePanel  = panel.querySelector('#nodes-manage-panel');
  const manageClose  = panel.querySelector('#nodes-manage-close');
  const addForm      = panel.querySelector('#nodes-add-form');
  const importPane   = panel.querySelector('#nodes-import-pane');
  const addStatusEl  = panel.querySelector('#nodes-add-status');
  const addCancel    = panel.querySelector('#nodes-add-cancel');
  const importList   = panel.querySelector('#nodes-import-list');
  const importSrcEl  = panel.querySelector('#nodes-import-source');
  const importStat   = panel.querySelector('#nodes-import-status');
  const addHeadingEl = panel.querySelector('#nodes-add-heading');
  const addSubmitBtn = panel.querySelector('#nodes-add-submit');

  // Form modes are mutually exclusive. At most one of these is set.
  let editHostNode      = null;     // Edit Host
  let editContextTarget = null;     // Edit Context: ctx.name
  let addContextNode    = null;     // Add Context (new ctx under host)
  const hostEditHideEls = panel.querySelectorAll('[data-host-edit-hide="1"]');
  const hostEditOnlyEls = panel.querySelectorAll('[data-host-edit-only="1"]');
  const ctxEditHideEls     = panel.querySelectorAll('[data-ctx-edit-hide="1"]');
  const ctxEditOnlyEls     = panel.querySelectorAll('[data-ctx-edit-only="1"]');
  const ctxEditReadonlyEls = panel.querySelectorAll('[data-ctx-edit-readonly="1"]');
  const addCtxHideEls   = panel.querySelectorAll('[data-add-ctx-hide="1"]');
  const addCtxOnlyEls   = panel.querySelectorAll('[data-add-ctx-only="1"]');
  const scopeCountEl    = panel.querySelector('#nodes-host-edit-scope-count');
  const ctxScopeNameEl  = panel.querySelector('#nodes-ctx-edit-scope-name');
  const addCtxScopeEl   = panel.querySelector('#nodes-add-ctx-scope-host');

  function setHostEditMode(on) {
    hostEditHideEls.forEach(el => el.toggleAttribute('hidden', !!on));
    hostEditOnlyEls.forEach(el => el.toggleAttribute('hidden', !on));
  }
  function setContextEditMode(on) {
    ctxEditHideEls.forEach(el => el.toggleAttribute('hidden', !!on));
    ctxEditOnlyEls.forEach(el => el.toggleAttribute('hidden', !on));
    ctxEditReadonlyEls.forEach(el => {
      el.classList.toggle('is-context-locked', !!on);
      el.querySelectorAll('input, select, textarea, button').forEach(ctrl => {
        ctrl.disabled = !!on;
        ctrl.setAttribute('aria-disabled', on ? 'true' : 'false');
      });
    });
  }
  function setAddContextMode(on) {
    addCtxHideEls.forEach(el => el.toggleAttribute('hidden', !!on));
    addCtxOnlyEls.forEach(el => el.toggleAttribute('hidden', !on));
  }

  const fName       = panel.querySelector('#nodes-add-name');
  const fHost       = panel.querySelector('#nodes-add-host');
  const fUser       = panel.querySelector('#nodes-add-user');
  const fPort       = panel.querySelector('#nodes-add-port');
  const fPath       = panel.querySelector('#nodes-add-path');
  const fAuth       = panel.querySelector('#nodes-add-auth');
  const fKey        = panel.querySelector('#nodes-add-keyfile');
  const fBrowse     = panel.querySelector('#nodes-add-browse');
  const fPassphrase = panel.querySelector('#nodes-add-passphrase');
  const fRemPassph  = panel.querySelector('#nodes-add-remember-passphrase');
  const fPassword   = panel.querySelector('#nodes-add-password');
  const fRemPasswd  = panel.querySelector('#nodes-add-remember-password');
  const fEnv        = panel.querySelector('#nodes-add-env');
  const authSections = panel.querySelectorAll('.nodes-auth-section[data-auth]');

  function setAddStatus(text, cls = '') {
    if (!addStatusEl) return;
    addStatusEl.textContent = text || '';
    addStatusEl.classList.remove('is-error', 'is-ok');
    if (cls) addStatusEl.classList.add(cls);
  }
  function setImportStatus(text, cls = '') {
    if (!importStat) return;
    importStat.textContent = text || '';
    importStat.classList.remove('is-error', 'is-ok');
    if (cls) importStat.classList.add(cls);
  }

  function clearInactiveAuthFields(mode) {
    // Hidden auth sections must not leak stale values into later Add/Edit
    // submissions. This matters most when Edit Host switches key ->
    // password: the old key-file path should disappear immediately.
    if (mode !== 'key') {
      if (fKey) fKey.value = '';
      if (fPassphrase) fPassphrase.value = '';
      if (fRemPassph) fRemPassph.checked = false;
    }
    if (mode !== 'password') {
      if (fPassword) fPassword.value = '';
      if (fRemPasswd) fRemPasswd.checked = false;
    }
  }

  function applyAuthSection({ clearInactive = false } = {}) {
    const mode = (fAuth && fAuth.value) || 'key';
    if (clearInactive) clearInactiveAuthFields(mode);
    authSections.forEach(sec => {
      const active = sec.dataset.auth === mode;
      // In context-edit mode every host/auth field stays hidden.
      if (editContextTarget) {
        sec.setAttribute('hidden', '');
        return;
      }
      // In add-context mode only the password sub-form should
      // appear, and only if the host's auth_method is password.
      // Key/agent sub-forms are inherited from the host record.
      if (addContextNode) {
        if (sec.dataset.auth === 'password' && active) {
          sec.removeAttribute('hidden');
        } else {
          sec.setAttribute('hidden', '');
        }
        return;
      }
      if (active) sec.removeAttribute('hidden');
      else sec.setAttribute('hidden', '');
    });
  }
  if (fAuth) {
    fAuth.addEventListener('change', () => {
      applyAuthSection({ clearInactive: true });
      setAddStatus('');
    });
    applyAuthSection();
  }

  if (fUser && fPath) {
    fUser.addEventListener('input', () => {
      if (!fPath.value || fPath.dataset.autofill === '1') {
        const u = fUser.value.trim();
        fPath.value = u ? `/home/${u}` : '';
        fPath.dataset.autofill = '1';
      }
    });
    fPath.addEventListener('input', () => { fPath.dataset.autofill = ''; });
  }

  if (fBrowse && fKey) {
    fBrowse.addEventListener('click', async () => {
      const f = bridgeFiles();
      if (!f || typeof f.pickPrivateKey !== 'function') {
        setAddStatus('File picker is only available in the Electron app.', 'is-error');
        return;
      }
      try {
        const res = await f.pickPrivateKey();
        if (res && res.path) { fKey.value = res.path; setAddStatus(''); }
      } catch (e) {
        setAddStatus(`File picker failed: ${e?.message || e}`, 'is-error');
      }
    });
  }

  const subtabBtns = panel.querySelectorAll('.nodes-manage-tab[data-subtab]');
  function applySubtab(name) {
    if (name !== 'manual' && name !== 'import') name = 'manual';
    subtabBtns.forEach(b => {
      const active = b.dataset.subtab === name;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (addForm)    addForm.toggleAttribute('hidden',    name !== 'manual');
    if (importPane) importPane.toggleAttribute('hidden', name !== 'import');
    if (name === 'import') refreshImportList();
  }
  subtabBtns.forEach(b => b.addEventListener('click', () => applySubtab(b.dataset.subtab)));

  function openManage(initialSubtab = 'manual') {
    if (!managePanel) return;
    managePanel.hidden = false;
    applySubtab(initialSubtab);
    setAddStatus('');
    setImportStatus('');
    setTimeout(() => { if (initialSubtab === 'manual' && fName) fName.focus(); }, 0);
  }
  function closeManage() {
    if (!managePanel) return;
    managePanel.hidden = true;
    if (addForm && addForm.reset) addForm.reset();
    // Reviewer fix: clear all edit-mode state BEFORE applyAuthSection
    // so closing Edit Context / Add Context doesn't leave a stale
    // auth section visible when Add Host opens next.
    editHostNode = null;
    editContextTarget = null;
    addContextNode = null;
    setHostEditMode(false);
    setContextEditMode(false);
    setAddContextMode(false);
    if (fAuth) { fAuth.value = 'key'; applyAuthSection(); }
    if (fPath) fPath.dataset.autofill = '';
    if (fName) {
      fName.readOnly = false;
      fName.removeAttribute('aria-readonly');
    }
    if (addHeadingEl) addHeadingEl.textContent = 'Add host manually';
    if (addSubmitBtn) addSubmitBtn.textContent = 'Add';
    setAddStatus('');
    setImportStatus('');
  }

  /** Open the manage panel in host-edit mode. */
  panel._openEditHost = function openEditHost(node) {
    if (!node || !node.contexts || node.contexts.length === 0) return;
    openManage('manual');
    editHostNode = node;
    editContextTarget = null;
    addContextNode = null;
    setHostEditMode(true);
    setContextEditMode(false);
    setAddContextMode(false);

    const primary = node.contexts[0];
    const m = (primary && primary.machine) || {};
    if (scopeCountEl) scopeCountEl.textContent = String(node.contexts.length);
    if (addHeadingEl) {
      const shortHost = node.host === 'local' ? 'local' : node.host.split('.')[0];
      addHeadingEl.textContent =
        `Edit host: ${shortHost} (${node.contexts.length} context(s))`;
    }
    if (addSubmitBtn) addSubmitBtn.textContent = 'Save Host';

    if (fName) {
      fName.value    = primary.name || '';
      fName.readOnly = true;
      fName.setAttribute('aria-readonly', 'true');
    }
    if (fHost) fHost.value = m.host || '';
    if (fUser) fUser.value = m.user || '';
    if (fPort) fPort.value = String(m.port || 22);
    if (fPath) {
      fPath.value = primary.path || (m.user ? `/home/${m.user}` : '');
      fPath.dataset.autofill = '';
    }
    if (fAuth) fAuth.value = m.auth_method || (m.key_file ? 'key' : 'agent');
    if (fKey) fKey.value = fAuth && fAuth.value === 'key' ? (m.key_file || '') : '';
    if (fEnv) fEnv.value = m.env_setup || '';
    if (fPassphrase) fPassphrase.value = '';
    if (fPassword)   fPassword.value   = '';
    if (fRemPassph)  fRemPassph.checked = false;
    if (fRemPasswd)  fRemPasswd.checked = false;
    applyAuthSection();
    setAddStatus('');
  };

  /** Open the manage panel in context-edit (workspace-only) mode. */
  panel._openEditContext = function openEditContext(ctx) {
    if (!ctx) return;
    openManage('manual');
    editContextTarget = ctx.name;
    editHostNode = null;
    addContextNode = null;
    setHostEditMode(false);
    setAddContextMode(false);
    setContextEditMode(true);

    if (addHeadingEl) addHeadingEl.textContent = `Edit context: ${ctx.name}`;
    if (addSubmitBtn) addSubmitBtn.textContent = 'Save Context';
    if (ctxScopeNameEl) ctxScopeNameEl.textContent = ctx.name;

    const m = (ctx && ctx.machine) || {};
    if (fName) {
      fName.value    = ctx.name || '';
      fName.readOnly = true;
      fName.setAttribute('aria-readonly', 'true');
    }
    if (fPath) {
      fPath.value = ctx.path || (m.user ? `/home/${m.user}` : '');
      fPath.dataset.autofill = '';
    }
    if (fEnv) fEnv.value = m.env_setup || '';
    if (fHost) fHost.value = m.host || '';
    if (fUser) fUser.value = m.user || '';
    if (fPort) fPort.value = String(m.port || 22);
    if (fAuth) fAuth.value = m.auth_method || (m.key_file ? 'key' : 'agent');
    if (fKey)  fKey.value  = fAuth && fAuth.value === 'key' ? (m.key_file || '') : '';
    if (fPassphrase) fPassphrase.value = '';
    if (fPassword)   fPassword.value   = '';
    if (fRemPassph)  fRemPassph.checked = false;
    if (fRemPasswd)  fRemPasswd.checked = false;
    setAddStatus('');
  };

  /** Open the manage panel to add a NEW context under an existing
   *  host. Host fields are hidden (inherited from the parent host);
   *  workspace fields are entered fresh. For password-auth hosts the
   *  password sub-form is shown and Remember password is required. */
  panel._openAddContext = function openAddContext(node) {
    if (!node || !node.contexts || node.contexts.length === 0) return;
    openManage('manual');
    addContextNode = node;
    editHostNode = null;
    editContextTarget = null;
    setHostEditMode(false);
    setContextEditMode(false);
    setAddContextMode(true);

    const primary = node.contexts[0];
    const m = (primary && primary.machine) || {};
    const epLabel = `${m.user || ''}@${m.host || ''}:${m.port || 22}`;
    if (addHeadingEl) addHeadingEl.textContent = `Add context to host: ${epLabel}`;
    if (addSubmitBtn) addSubmitBtn.textContent = 'Add Context';
    if (addCtxScopeEl) addCtxScopeEl.textContent = epLabel;

    if (fName) {
      fName.value = '';
      fName.readOnly = false;
      fName.removeAttribute('aria-readonly');
    }
    if (fHost) fHost.value = m.host || '';
    if (fUser) fUser.value = m.user || '';
    if (fPort) fPort.value = String(m.port || 22);
    if (fPath) {
      fPath.value = m.user ? `/home/${m.user}` : '';
      fPath.dataset.autofill = '1';
    }
    if (fAuth) fAuth.value = m.auth_method || (m.key_file ? 'key' : 'agent');
    if (fKey)  fKey.value  = fAuth && fAuth.value === 'key' ? (m.key_file || '') : '';
    if (fEnv)  fEnv.value  = '';
    if (fPassphrase) fPassphrase.value = '';
    if (fPassword)   fPassword.value   = '';
    if (fRemPassph)  fRemPassph.checked = false;
    if (fRemPasswd)  fRemPasswd.checked = false;
    applyAuthSection();
    setAddStatus('');
  };

  addToggle && addToggle.addEventListener('click', () => {
    // Reviewer fix: reset any active edit/add-ctx mode before
    // reopening Add Host, so the form starts as a clean Add.
    closeManage();
    openManage('manual');
  });
  manageClose && manageClose.addEventListener('click', closeManage);
  addCancel   && addCancel.addEventListener('click',   closeManage);

  addForm && addForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const isHostEdit    = !!editHostNode;
    const isContextEdit = !!editContextTarget;
    const isAddContext  = !!addContextNode;

    // ── Context-edit: context name read-only; path/env only. ─────
    if (isContextEdit) {
      const ctxPath = (fPath && fPath.value.trim()) || '';
      if (!ctxPath) { setAddStatus('Remote path is required.', 'is-error'); return; }
      const body = { path: ctxPath, env_setup: fEnv ? fEnv.value.trim() : '' };
      setAddStatus(`Saving context "${editContextTarget}"…`);
      try {
        await requireNodesConnected('Save context');
        await api.updateContext(editContextTarget, body);
        setAddStatus(`Saved context "${editContextTarget}".`, 'is-ok');
        showToast(`Context "${editContextTarget}" updated`, 'success');
        try { await loadContextsAndAdapters(); } catch (_) {}
        if (typeof loadAgents === 'function') { try { await loadAgents(); } catch (_) {} }
        closeManage();
      } catch (err) {
        setAddStatus(`Save context failed: ${err.message}`, 'is-error');
      }
      return;
    }

    // ── Add-context: workspace fields + (for password) credential.
    if (isAddContext) {
      const node    = addContextNode;
      const primary = node.contexts[0] || {};
      const m       = (primary && primary.machine) || {};
      const name    = fName ? fName.value.trim() : '';
      const ctxPath = (fPath && fPath.value.trim()) || (m.user ? `/home/${m.user}` : '');
      if (!name || !ctxPath) {
        setAddStatus('Name and remote path are required.', 'is-error');
        return;
      }
      const authMethod = m.auth_method || (m.key_file ? 'key' : 'agent');
      const body = {
        name,
        path: ctxPath,
        env_setup: fEnv ? fEnv.value.trim() : '',
        host: m.host,
        user: m.user,
        port: m.port || 22,
        auth_method: authMethod,
      };
      if (authMethod === 'key') {
        body.key_file = m.key_file || '';
        if (fRemPassph && fRemPassph.checked) {
          const pp = (fPassphrase && fPassphrase.value) || '';
          if (!pp) {
            setAddStatus('Enter a passphrase or uncheck Remember passphrase.', 'is-error');
            return;
          }
          body.passphrase = pp;
          body.remember_passphrase = true;
        }
      } else if (authMethod === 'password') {
        const pw  = fPassword ? fPassword.value : '';
        const rem = !!(fRemPasswd && fRemPasswd.checked);
        if (!rem) {
          setAddStatus(
            'Password auth requires Remember password — the embedded Hub does not auto-clone credentials to new contexts.',
            'is-error',
          );
          return;
        }
        if (!pw) {
          setAddStatus('Enter a password to remember.', 'is-error');
          return;
        }
        body.password = pw;
        body.remember_password = true;
      }
      // agent auth carries no secret.

      setAddStatus(`Adding context "${name}" to host…`);
      try {
        await requireNodesConnected('Add context');
        await api.createContext(body);
        setAddStatus(`Added context "${name}".`, 'is-ok');
        showToast(
          `Context "${name}" added to ${m.user}@${m.host}:${m.port || 22}`,
          'success',
        );
        try { await loadContextsAndAdapters(); } catch (_) {}
        if (typeof loadAgents === 'function') { try { await loadAgents(); } catch (_) {} }
        closeManage();
      } catch (err) {
        setAddStatus(`Add context failed: ${err.message}`, 'is-error');
      }
      return;
    }

    // ── Add / Edit Host path: validate host fields. ─────────────
    const host = fHost.value.trim();
    const user = fUser.value.trim();
    const port = Number.parseInt(fPort.value, 10) || 22;
    const authMethod = (fAuth && fAuth.value) || 'key';

    if (!host || !user) {
      setAddStatus('Host and user are required.', 'is-error');
      return;
    }
    if (!isHostEdit) {
      const name = fName.value.trim();
      const ctxPath = fPath.value.trim() || (user ? `/home/${user}` : '');
      if (!name || !ctxPath) {
        setAddStatus('Name and remote path are required.', 'is-error');
        return;
      }
    }

    const hostBody = { host, user, port, auth_method: authMethod };
    if (authMethod === 'key') {
      hostBody.key_file = fKey ? fKey.value.trim() : '';
      const pass = fPassphrase ? fPassphrase.value : '';
      const rem  = !!(fRemPassph && fRemPassph.checked);
      if (rem) {
        if (!pass) {
          setAddStatus('Enter a passphrase or uncheck Remember passphrase.', 'is-error');
          return;
        }
        hostBody.passphrase = pass;
        hostBody.remember_passphrase = true;
      }
    } else if (authMethod === 'password') {
      hostBody.key_file = '';
      const pw  = fPassword ? fPassword.value : '';
      const rem = !!(fRemPasswd && fRemPasswd.checked);
      const keepExistingPassword =
        isHostEdit && editHostNode &&
        editHostNode.contexts.every(ctx => {
          const m = (ctx && ctx.machine) || {};
          return (m.auth_method || '') === 'password' && !!m.credential_saved;
        }) && !rem;
      if (!rem && !keepExistingPassword) {
        setAddStatus('Password auth requires Remember password (until a per-sync password prompt is added).', 'is-error');
        return;
      }
      if (rem && !pw) {
        setAddStatus('Enter a password to remember.', 'is-error');
        return;
      }
      if (rem) {
        hostBody.password = pw;
        hostBody.remember_password = true;
      }
    } else if (authMethod === 'agent') {
      hostBody.key_file = '';
    }

    if (isHostEdit) {
      const node = editHostNode;
      const total = node.contexts.length;
      setAddStatus(`Saving host fields to ${total} context(s)…`);
      let saved = 0;
      const failures = [];
      try {
        await requireNodesConnected('Save host');
      } catch (err) {
        setAddStatus(`Save host failed: ${err.message}`, 'is-error');
        showToast(`Host update failed: ${err.message}`, 'error', 5000);
        return;
      }
      for (const ctx of node.contexts) {
        const body = { ...hostBody };
        body.path      = ctx.path || (user ? `/home/${user}` : '');
        body.env_setup = (ctx.machine && ctx.machine.env_setup) || '';
        try {
          await api.updateContext(ctx.name, body);
          saved++;
        } catch (err) {
          failures.push(`${ctx.name}:${(err && err.message) || 'failed'}`);
        }
      }
      try { await loadContextsAndAdapters(); } catch (_) {}
      if (typeof loadAgents === 'function') { try { await loadAgents(); } catch (_) {} }
      if (failures.length === 0) {
        setAddStatus(`Saved host fields to ${saved} context(s).`, 'is-ok');
        showToast(`Host "${node.user}@${node.host}:${node.port}" updated (${saved} context(s))`, 'success');
        closeManage();
      } else {
        const head = failures.slice(0, 4).join(', ');
        const tail = failures.length > 4 ? ', …' : '';
        setAddStatus(`Saved ${saved}/${total}; ${failures.length} failed (${head}${tail})`, 'is-error');
        showToast(`Host update: ${saved}/${total} saved, ${failures.length} failed`, 'warning', 6000);
      }
      return;
    }

    // Add-host path: single POST, then refresh.
    const name = fName.value.trim();
    const ctxPath = fPath.value.trim() || (user ? `/home/${user}` : '');
    const addBody = {
      name,
      path:      ctxPath,
      env_setup: fEnv ? fEnv.value.trim() : '',
      ...hostBody,
    };
    setAddStatus('Adding…');
    try {
      await requireNodesConnected('Add host');
      await api.createContext(addBody);
      setAddStatus(`Added "${name}".`, 'is-ok');
      showToast(`Host "${user}@${host}:${port}" added (initial context "${name}")`, 'success');
      try { await loadContextsAndAdapters(); } catch (_) {}
      if (typeof loadAgents === 'function') { try { await loadAgents(); } catch (_) {} }
      closeManage();
    } catch (err) {
      setAddStatus(`Add failed: ${err.message}`, 'is-error');
    }
  });

  function pickContextName(alias) {
    const MAX = 64, SUFFIX_RES = 4, BASE_MAX = MAX - SUFFIX_RES;
    let base = String(alias || '')
      .replace(/[^A-Za-z0-9_-]+/g, '-')
      .replace(/^-+|-+$/g, '');
    if (!base) base = 'node';
    if (base.length > BASE_MAX) base = base.slice(0, BASE_MAX);
    const existing = new Set((state.get('contexts') || []).map(c => c.name));
    if (!existing.has(base)) return base;
    for (let i = 2; i < 1000; i++) {
      const next = `${base}-${i}`;
      if (!existing.has(next)) return next;
    }
    const tail = String(Date.now()).slice(-6);
    const room = MAX - 1 - tail.length;
    return `${base.slice(0, room)}-${tail}`;
  }

  async function refreshImportList() {
    importList.innerHTML = `<div class="empty-state">Loading…</div>`;
    let resp;
    try {
      await requireNodesConnected('SSH config import');
      resp = await api.sshConfigHosts();
    } catch (err) {
      importList.innerHTML = `<div class="empty-state">SSH config import not available on this Hub.</div>`;
      if (importSrcEl) importSrcEl.textContent = 'This Hub did not return ssh-config suggestions.';
      return;
    }
    if (importSrcEl) {
      importSrcEl.innerHTML = resp.available && resp.source
        ? `Reading <code>${esc(resp.source)}</code>.`
        : `No SSH config found on this machine.`;
    }
    const hosts = (resp && resp.hosts) || [];
    if (hosts.length === 0) {
      importList.innerHTML = `<div class="empty-state">No host entries found. Add hosts manually with <strong>Add Host</strong>.</div>`;
      return;
    }
    importList.innerHTML = hosts.map((h, i) => `
      <div class="nodes-import-row" data-i="${i}">
        <div class="nodes-import-name">${esc(h.alias)}</div>
        <div class="nodes-import-meta">${esc(h.user || '')}@${esc(h.host)}${h.port && h.port !== 22 ? ':' + h.port : ''}${h.identity_file ? ' · key: ' + esc(h.identity_file) : ''}</div>
        <button type="button" class="btn-sm btn-secondary nodes-import-btn" data-i="${i}">Import</button>
      </div>
    `).join('');
    importList.querySelectorAll('.nodes-import-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const i = Number.parseInt(btn.dataset.i, 10);
        const h = hosts[i];
        if (!h) return;
        const name = pickContextName(h.alias);
        const user = h.user || '';
        const body = {
          name,
          path:      user ? `/home/${user}` : '/home',
          host:      h.host,
          user,
          port:      h.port || 22,
          key_file:  h.identity_file || '',
          env_setup: '',
        };
        btn.disabled = true;
        btn.textContent = 'Importing…';
        try {
          await requireNodesConnected('Import host');
          await api.createContext(body);
          btn.textContent = 'Imported';
          setImportStatus(`Imported "${name}".`, 'is-ok');
          showToast(`Host imported as "${name}"`, 'success');
          try { await loadContextsAndAdapters(); } catch (_) {}
        } catch (err) {
          btn.disabled = false;
          btn.textContent = 'Import';
          setImportStatus(`Import failed: ${err.message}`, 'is-error');
        }
      });
    });
  }
}
