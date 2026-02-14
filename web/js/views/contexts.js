import { api, state } from '../app.js';

// Track which context is being edited (null = creating new)
let editingId = null;

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
          return `
          <div class="context-card">
            <div class="context-card-header">
              <span class="context-name">${c.name}</span>
              <span>
                <button class="btn-sm btn-secondary edit-ctx" data-id="${c.id}">Edit</button>
                <button class="btn-sm btn-danger delete-ctx" data-name="${c.name}">Delete</button>
              </span>
            </div>
            <div class="context-card-meta">
              <span class="context-path">${c.path}</span>
              ${isSSH ? `<span class="context-host">${m.user || ''}@${m.host}:${m.port || 22}</span>` : '<span class="context-type">local</span>'}
            </div>
          </div>`;
        }).join('')}</div>`;

    // Edit handlers
    listEl.querySelectorAll('.edit-ctx').forEach(btn => {
      btn.addEventListener('click', () => {
        const ctx = contexts.find(c => c.id === btn.dataset.id);
        if (ctx) fillFormForEdit(ctx);
      });
    });

    // Delete handlers
    listEl.querySelectorAll('.delete-ctx').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm(`Delete context "${btn.dataset.name}"?`)) return;
        try {
          await api.deleteContext(btn.dataset.name);
          state.toast('Context deleted', 'success');
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

    // Open SSH details if it's an SSH context
    if (m.host) {
      container.querySelector('#ssh-details').open = true;
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

  // Cleanup when navigated away
  const observer = new MutationObserver(() => {
    if (!container.isConnected) { unsub(); observer.disconnect(); }
  });
  observer.observe(document.getElementById('content'), { childList: true });
}
