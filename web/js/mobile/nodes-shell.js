/** Nodes panel markup — same IDs/structure as `web/desktop.html` `#mode-nodes`. */
export const MOBILE_NODES_HTML = `
<div id="mobile-nodes" class="mobile-nodes-panel">
  <div class="settings-header">
    <h2>Nodes</h2>
    <p class="settings-help">
      Hosts the embedded Hub knows about. Each node is a card with
      its own boundary. Tap a card to expand actions and contexts.
    </p>
  </div>

  <div id="nodes-readonly-banner" class="nodes-readonly-banner" hidden>
    <p class="settings-help">
      Connected via <strong>Relay</strong> — read-only on this device.
      SSH credentials and node registry live on the workstation Hub;
      manage hosts and contexts from Desktop or <code>camui start</code>.
      Switch to <strong>Direct</strong> in Settings to add or edit nodes from the phone.
    </p>
  </div>

  <div class="nodes-actions" id="nodes-actions">
    <button type="button" class="btn-primary" id="nodes-add-toggle">Add Host</button>
  </div>

  <div id="nodes-manage-panel" class="nodes-manage-panel" hidden>
    <div class="nodes-manage-tabs" role="tablist" aria-label="Add host source">
      <button type="button" class="nodes-manage-tab" data-subtab="manual"
        role="tab" aria-pressed="true">Add manually</button>
      <button type="button" class="nodes-manage-tab" data-subtab="import"
        role="tab" aria-pressed="false">Import from SSH config</button>
      <button type="button" class="btn-secondary nodes-manage-close"
        id="nodes-manage-close" title="Close">Close</button>
    </div>

    <form id="nodes-add-form" class="nodes-add-form settings-form-pane" data-subtab="manual">
      <div class="nodes-add-scroll">
      <div class="settings-section">
        <h3 id="nodes-add-heading">Add host manually</h3>
        <p class="settings-help nodes-add-intro">
          Register a remote SSH node locally. Does not require Connect.
          Connect in Settings when you need agents.
        </p>
        <label data-add-ctx-hide="1" data-ctx-edit-hide="1">Node name
          <input type="text" id="nodes-add-nodename" placeholder="Build server" autocomplete="off">
        </label>
        <label data-host-edit-hide="1">Context name
          <input type="text" id="nodes-add-name" required placeholder="my-workspace" autocomplete="off"
            pattern="[A-Za-z0-9_-]{1,64}" title="Letters, digits, underscore, hyphen (1–64 chars)">
        </label>
        <label data-ctx-edit-readonly="1" data-add-ctx-hide="1">Host
          <input type="text" id="nodes-add-host" required placeholder="host.example.com" autocomplete="off">
        </label>
        <label data-ctx-edit-readonly="1" data-add-ctx-hide="1">User
          <input type="text" id="nodes-add-user" required placeholder="hren" autocomplete="off">
        </label>
        <label data-ctx-edit-readonly="1" data-add-ctx-hide="1">Port
          <input type="number" id="nodes-add-port" min="1" max="65535" value="22">
        </label>
        <label data-host-edit-hide="1">Remote path
          <input type="text" id="nodes-add-path" required placeholder="/home/&lt;user&gt;" autocomplete="off">
        </label>
        <label data-ctx-edit-readonly="1" data-add-ctx-hide="1">Auth method
          <select id="nodes-add-auth">
            <option value="key">SSH key</option>
            <option value="password">Password</option>
            <option value="agent">SSH agent / default</option>
          </select>
        </label>

        <div class="nodes-auth-section" id="nodes-auth-key" data-auth="key" data-ctx-edit-hide="1" data-add-ctx-hide="1">
          <label>Private key file
            <div class="nodes-auth-keyfile-row">
              <input type="text" id="nodes-add-keyfile" placeholder="~/.ssh/id_ed25519" autocomplete="off">
              <button type="button" class="btn-secondary" id="nodes-add-browse">Browse&hellip;</button>
            </div>
          </label>
          <label>Passphrase (optional)
            <input type="password" id="nodes-add-passphrase" autocomplete="new-password"
              placeholder="leave blank if key is unencrypted">
          </label>
          <label class="form-checkbox">
            <input type="checkbox" id="nodes-add-remember-passphrase">
            <span>Remember passphrase</span>
            <span class="form-hint">Encrypted with the OS keychain (Electron safeStorage). Never stored in plaintext.</span>
          </label>
        </div>

        <div class="nodes-auth-section" id="nodes-auth-password" data-auth="password" data-ctx-edit-hide="1" hidden>
          <label>Password
            <input type="password" id="nodes-add-password" autocomplete="new-password">
          </label>
          <label class="form-checkbox">
            <input type="checkbox" id="nodes-add-remember-password">
            <span>Remember password</span>
            <span class="form-hint">Encrypted with the OS keychain (Electron safeStorage). Never stored in plaintext.</span>
          </label>
        </div>

        <div class="nodes-auth-section" id="nodes-auth-agent" data-auth="agent" data-ctx-edit-hide="1" data-add-ctx-hide="1" hidden>
          <p class="form-hint">
            Use the running SSH agent or the default key search
            (<code>~/.ssh/id_*</code>). No key file or password
            is stored by Desktop in this mode.
          </p>
        </div>

        <label data-host-edit-hide="1">Env setup (optional)
          <input type="text" id="nodes-add-env" placeholder="source ~/.bashrc" autocomplete="off">
        </label>
        <p class="form-hint" data-host-edit-only="1" hidden>
          Editing host fields applies to <strong id="nodes-host-edit-scope-count">all</strong>
          context(s) on this host. Workspace fields (name,
          remote path, env setup) are managed per context and are
          not changed by this form.
        </p>
        <p class="form-hint" data-ctx-edit-only="1" hidden>
          Editing context "<strong id="nodes-ctx-edit-scope-name">…</strong>".
          Context name is read-only. Remote path and env setup are
          changed here. Host fields are shown locked/grey because
          contexts are bound to their host; use
          <strong>Edit Host</strong> to change host settings.
        </p>
        <p class="form-hint" data-add-ctx-only="1" hidden>
          Adding a new context under host
          "<strong id="nodes-add-ctx-scope-host">…</strong>".
          Host fields (host, user, port, auth, key file) are
          inherited from the existing host. For password-auth
          hosts you must enter the password again — the embedded
          Hub does not auto-clone credential references to new
          contexts.
        </p>
      </div>
      <div class="settings-status nodes-add-status" id="nodes-add-status" aria-live="polite"></div>
      </div>
      <div class="nodes-add-footer">
        <div class="settings-actions">
          <button type="button" class="btn-secondary" id="nodes-add-cancel">Cancel</button>
          <button type="submit" class="btn-primary" id="nodes-add-submit">Save Node</button>
        </div>
      </div>
    </form>

    <div id="nodes-import-pane" class="nodes-import-pane settings-form-pane" data-subtab="import" hidden>
      <div class="settings-section">
        <h3>Import from SSH config</h3>
        <p class="settings-help" id="nodes-import-source">
          Reading <code>~/.ssh/config</code>&hellip;
        </p>
        <div id="nodes-import-list" class="nodes-import-list">
          <div class="empty-state">Loading&hellip;</div>
        </div>
        <div class="settings-status" id="nodes-import-status" aria-live="polite"></div>
      </div>
    </div>
  </div>

  <div id="nodes-list-wrap" class="nodes-list-wrap">
    <div class="empty-state">Loading hosts&hellip;</div>
  </div>
  <div class="settings-status" id="nodes-status" aria-live="polite"></div>
</div>`;
