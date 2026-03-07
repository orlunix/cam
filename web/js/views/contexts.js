import { api, state, navigate } from '../app.js';

// Track which context is being edited (null = creating new)
let editingId = null;
// Track which card is expanded (by context id)
let expandedId = null;

export function renderContexts(container) {
  editingId = null;

  container.innerHTML = `
    <div class="page-header">
      <h2>Contexts</h2>
    </div>

    <div id="context-list-container"></div>

    <div class="section-divider"></div>

    <div class="page-header">
      <h3 id="form-title">Add Context</h3>
    </div>
    <form id="ctx-form" class="form">
      <div class="form-group">
        <label for="ctx-name">Name</label>
        <input type="text" id="ctx-name" class="form-input" required placeholder="my-project">
      </div>
      <div class="form-group">
        <label for="ctx-path">Working directory</label>
        <input type="text" id="ctx-path" class="form-input" required placeholder="/home/user/project">
      </div>
      <details class="form-advanced" id="ssh-details">
        <summary>SSH (remote context)</summary>
        <div class="form-group">
          <label for="ctx-host">Host</label>
          <input type="text" id="ctx-host" class="form-input" placeholder="server.example.com">
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="ctx-user">User</label>
            <input type="text" id="ctx-user" class="form-input" placeholder="user">
          </div>
          <div class="form-group">
            <label for="ctx-port">Port</label>
            <input type="number" id="ctx-port" class="form-input" value="22">
          </div>
        </div>
      </details>
      <details class="form-advanced" id="env-details">
        <summary>Environment Setup</summary>
        <div class="form-group">
          <label for="ctx-env-setup">Shell commands (run before agent)</label>
          <input type="text" id="ctx-env-setup" class="form-input" placeholder="source ~/.bashrc (default for SSH)">
        </div>
      </details>
      <div class="form-actions" style="flex-direction:row">
        <button type="submit" class="btn-primary" id="ctx-submit-btn" style="flex:1">Add Context</button>
        <button type="button" class="btn-secondary" id="ctx-cancel-btn" style="display:none">Cancel</button>
      </div>
    </form>
  `;

  // --- Context list ---
  function renderList() {
    const contexts = state.get('contexts') || [];
    const listEl = container.querySelector('#context-list-container');
    if (!listEl) return;

    const machine = (c) => c.machine || {};

    listEl.innerHTML = contexts.length === 0
      ? '<div class="empty-state">No contexts. Create one below.</div>'
      : `<div class="context-list">${contexts.map(c => {
          const m = machine(c);
          const isSSH = m.type === 'ssh' || m.host;
          const isExpanded = expandedId === c.id;
          const typeBadge = isSSH
            ? `<span class="context-type-badge ssh">SSH</span>`
            : `<span class="context-type-badge local">local</span>`;

          // Detail rows
          let details = `
            <div class="context-detail-row">
              <span class="context-detail-label">Path</span>
              <span class="context-detail-value">${esc(c.path)}</span>
            </div>`;
          if (isSSH) {
            details += `
            <div class="context-detail-row">
              <span class="context-detail-label">Host</span>
              <span class="context-detail-value host">${esc(m.user || '')}@${esc(m.host)}:${m.port || 22}</span>
            </div>`;
          }
          if (m.env_setup) {
            details += `
            <div class="context-detail-row">
              <span class="context-detail-label">Env</span>
              <span class="context-detail-value">${esc(m.env_setup)}</span>
            </div>`;
          }

          // Action buttons
          let actions = `
            <button class="btn-sm btn-secondary browse-ctx" data-id="${c.id}">Browse</button>
            <button class="btn-sm btn-secondary edit-ctx" data-id="${c.id}">Edit</button>`;
          if (isSSH) {
            actions += `
            <button class="btn-sm btn-secondary sync-ctx" data-name="${c.name}">Sync</button>`;
          }
          actions += `
            <button class="btn-sm btn-secondary copy-ctx" data-name="${c.name}">Duplicate</button>
            <button class="btn-sm btn-danger delete-ctx" data-name="${c.name}">Delete</button>`;

          return `
          <div class="context-card${isExpanded ? ' expanded' : ''}" data-id="${c.id}">
            <div class="context-card-header">
              <div class="context-card-left">
                <span class="context-name">${esc(c.name)}</span>
                ${typeBadge}
              </div>
              <span class="context-chevron">\u25B8</span>
            </div>
            <div class="context-card-body">
              <div class="context-detail-rows">${details}</div>
              <div class="context-actions">${actions}</div>
            </div>
          </div>`;
        }).join('')}</div>`;

    // Toggle expand on header click
    listEl.querySelectorAll('.context-card-header').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const card = hdr.closest('.context-card');
        const id = card.dataset.id;
        if (expandedId === id) {
          expandedId = null;
          card.classList.remove('expanded');
        } else {
          // Collapse previous
          const prev = listEl.querySelector('.context-card.expanded');
          if (prev) prev.classList.remove('expanded');
          expandedId = id;
          card.classList.add('expanded');
        }
      });
    });

    // Browse handlers
    listEl.querySelectorAll('.browse-ctx').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        navigate(`#/context/${btn.dataset.id}/files`);
      });
    });

    // Edit handlers
    listEl.querySelectorAll('.edit-ctx').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const ctx = contexts.find(c => c.id === btn.dataset.id);
        if (ctx) fillFormForEdit(ctx);
      });
    });

    // Sync handlers
    listEl.querySelectorAll('.sync-ctx').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        btn.textContent = 'Syncing...';
        try {
          const resp = await api.syncContext(btn.dataset.name);
          const results = resp.results || {};
          const synced = Object.values(results).filter(s => s === 'deployed' || s === 'updated').length;
          const unchanged = Object.values(results).filter(s => s === 'unchanged').length;
          state.toast(`Synced: ${synced} updated, ${unchanged} unchanged`, 'success');
        } catch (e) { state.toast(e.message, 'error'); }
        btn.disabled = false;
        btn.textContent = 'Sync';
      });
    });

    // Copy handlers
    listEl.querySelectorAll('.copy-ctx').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const newName = prompt(`Duplicate "${btn.dataset.name}" as:`, `${btn.dataset.name}-copy`);
        if (!newName) return;
        try {
          await api.copyContext(btn.dataset.name, newName.trim());
          state.toast('Context duplicated', 'success');
          const resp = await api.listContexts();
          state.set('contexts', resp.contexts || []);
        } catch (e) { state.toast(e.message, 'error'); }
      });
    });

    // Delete handlers
    listEl.querySelectorAll('.delete-ctx').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete context "${btn.dataset.name}"?`)) return;
        try {
          await api.deleteContext(btn.dataset.name);
          state.toast('Context deleted', 'success');
          expandedId = null;
          const resp = await api.listContexts();
          state.set('contexts', resp.contexts || []);
        } catch (e) { state.toast(e.message, 'error'); }
      });
    });
  }

  function fillFormForEdit(ctx) {
    editingId = ctx.id;
    const m = ctx.machine || {};

    container.querySelector('#ctx-name').value = ctx.name;
    container.querySelector('#ctx-path').value = ctx.path;
    container.querySelector('#ctx-host').value = m.host || '';
    container.querySelector('#ctx-user').value = m.user || '';
    container.querySelector('#ctx-port').value = m.port || 22;
    container.querySelector('#ctx-env-setup').value = m.env_setup || '';

    // Open SSH details if it's an SSH context
    if (m.host) {
      container.querySelector('#ssh-details').open = true;
    }
    // Open env details if env_setup is set
    if (m.env_setup) {
      container.querySelector('#env-details').open = true;
    }

    container.querySelector('#form-title').textContent = 'Edit Context';
    container.querySelector('#ctx-submit-btn').textContent = 'Save Changes';
    container.querySelector('#ctx-cancel-btn').style.display = '';

    // Scroll to form
    container.querySelector('#ctx-form').scrollIntoView({ behavior: 'smooth' });
  }

  function resetForm() {
    editingId = null;
    container.querySelector('#ctx-form').reset();
    container.querySelector('#form-title').textContent = 'Add Context';
    container.querySelector('#ctx-submit-btn').textContent = 'Add Context';
    container.querySelector('#ctx-cancel-btn').style.display = 'none';
  }

  renderList();

  // Only re-render list when contexts change
  let prevContexts = state.get('contexts');
  const unsub = state.subscribe(() => {
    const cur = state.get('contexts');
    if (cur !== prevContexts) {
      prevContexts = cur;
      renderList();
    }
  });

  // Cancel button
  container.querySelector('#ctx-cancel-btn').addEventListener('click', resetForm);

  // Submit handler (create or update)
  container.querySelector('#ctx-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      name: container.querySelector('#ctx-name').value.trim(),
      path: container.querySelector('#ctx-path').value.trim(),
    };
    const host = container.querySelector('#ctx-host').value.trim();
    if (host) {
      body.host = host;
      body.user = container.querySelector('#ctx-user').value.trim() || undefined;
      body.port = parseInt(container.querySelector('#ctx-port').value) || 22;
    }
    const envSetup = container.querySelector('#ctx-env-setup').value.trim();
    if (envSetup) body.env_setup = envSetup;
    try {
      if (editingId) {
        await api.updateContext(editingId, body);
        state.toast('Context updated', 'success');
      } else {
        await api.createContext(body);
        state.toast('Context created', 'success');
      }
      resetForm();
      const resp = await api.listContexts();
      state.set('contexts', resp.contexts || []);
    } catch (e) { state.toast(e.message, 'error'); }
  });

  // Return cleanup function for router
  return () => { unsub(); };
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}
