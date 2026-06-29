import { api, state } from './app.js';
import { renderMachines } from './views/machines.js';

/**
 * Relay Nodes — frozen v2.2.0 implementation.
 * Direct mode uses nodes-direct.js via nodes-page.js.
 */
export function renderNodes(container) {
  container.innerHTML = `
    <div class="page-header"><h2>Nodes</h2></div>
    <details class="form-advanced" id="add-host-panel" style="margin:0 0 12px">
      <summary>Add SSH host / context</summary>
      <form id="add-host-form" class="form" style="margin-top:8px">
        <div class="form-group"><label>Name</label><input id="nh-name" class="form-input" required placeholder="my-server"></div>
        <div class="form-group"><label>Working directory</label><input id="nh-path" class="form-input" required placeholder="/home/user/project"></div>
        <div class="form-row">
          <div class="form-group"><label>Host</label><input id="nh-host" class="form-input" required></div>
          <div class="form-group"><label>User</label><input id="nh-user" class="form-input"></div>
        </div>
        <div class="form-group"><label>Port</label><input id="nh-port" class="form-input" type="number" value="22"></div>
        <button type="submit" class="btn-primary btn-full">Add Context</button>
      </form>
    </details>
    <div id="nodes-list-host"></div>`;

  const listHost = container.querySelector('#nodes-list-host');
  const listCleanup = renderMachines(listHost, { skipHeader: true });

  container.querySelector('#add-host-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      name: container.querySelector('#nh-name').value.trim(),
      path: container.querySelector('#nh-path').value.trim(),
      host: container.querySelector('#nh-host').value.trim(),
      user: container.querySelector('#nh-user').value.trim() || undefined,
      port: parseInt(container.querySelector('#nh-port').value, 10) || 22,
    };
    try {
      await api.createContext(body);
      state.toast('Context created', 'success');
      const resp = await api.listContexts();
      state.set('contexts', resp.contexts || []);
      container.querySelector('#add-host-form').reset();
    } catch (err) {
      state.toast(err.message || 'Failed', 'error');
    }
  });

  return () => { if (listCleanup) listCleanup(); };
}
