/* extensions-mode.js — the Extensions page (SPEC §5).
 * Lists built-in + user extensions, installs from a user-picked folder,
 * opens views (native mode navigation for built-ins, sandboxed iframe
 * for package views), and edits per-extension attributes. */

import { installExtBridge } from '../shared/ext-bridge.js';
import { mountExtView } from '../shared/ext-view-host.js?v=0.68.1';

// Cross-mode entry point: other pages (the agent console Ext▾ menu)
// open an extension's dedicated page through this. Set by
// mountExtensionsMode on the first mount.
let _openExtensionView = null;
export function openExtensionView(ext, opts) {
  if (_openExtensionView) _openExtensionView(ext, opts || {});
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

/** Parse a schema-line like `key | boolean | false | Label | hint`. */
function parseAttrSpec(line) {
  const parts = String(line).split('|').map(s => s.trim());
  return {
    key: parts[0] || '',
    type: parts[1] || 'text',
    defaultValue: parts[2] || '',
    label: parts[3] || parts[0] || '',
    hint: parts[4] || '',
  };
}

function parseAttrDefault(v, type) {
  if (type === 'boolean') return String(v).toLowerCase() === 'true';
  if (type === 'number') return Number(v) || 0;
  return v;
}

/** Inject the uniform app-managed chrome into a native extension page.
 *  Replaces the page's baked-in header with a title/description/Back bar
 *  and returns an unmount function. */
export function applyExtChrome({ nativeName, ext, returnTo = null, setMode }) {
  const panel = document.getElementById(`mode-${nativeName}`) || document.getElementById(nativeName);
  if (!panel) return () => {};
  panel.classList.add('ext-chromed');
  let chrome = panel.querySelector(':scope > .ext-chrome');
  if (!chrome) {
    chrome = document.createElement('div');
    chrome.className = 'settings-header ext-chrome';
    panel.insertBefore(chrome, panel.firstChild);
  }
  chrome.innerHTML = `
    <div class="agent-settings-heading">
      <h2>${esc(ext.title || ext.name)}@${esc(ext.version || '')}</h2>
      <p class="settings-help">${esc(ext.description || ext.name)}</p>
    </div>
    <button type="button" class="btn-secondary ext-chrome-back">&larr; Back</button>
  `;
  const backBtn = chrome.querySelector('.ext-chrome-back');
  const onBack = () => {
    panel.classList.remove('ext-chromed');
    if (chrome && chrome.parentNode) chrome.remove();
    if (typeof setMode === 'function') setMode(returnTo || 'extensions');
  };
  backBtn.addEventListener('click', onBack);
  return () => {
    panel.classList.remove('ext-chromed');
    if (chrome && chrome.parentNode) chrome.remove();
    backBtn.removeEventListener('click', onBack);
  };
}

export function mountExtensionsMode({ api, state, showToast, setMode }) {
  const root = document.getElementById('mode-extensions');
  if (!root) return;
  installExtBridge(api);

  const listEl = root.querySelector('#ext-list');
  const statusEl = root.querySelector('#ext-status');
  const installBtn = root.querySelector('#ext-install-btn');
  const installPkgBtn = root.querySelector('#ext-install-pkg-btn');
  const viewWrap = root.querySelector('#ext-view-wrap');
  const viewTitle = root.querySelector('#ext-view-title');
  const viewHost = root.querySelector('#ext-view-host');
  const viewClose = root.querySelector('#ext-view-close');
  const editWrap = root.querySelector('#ext-edit-wrap');
  const editTitle = root.querySelector('#ext-edit-title');
  const editSubtitle = root.querySelector('#ext-edit-subtitle');
  const editForm = root.querySelector('#ext-edit-form');
  const editRaw = root.querySelector('#ext-edit-raw');
  const editSave = root.querySelector('#ext-edit-save');
  const editCancel = root.querySelector('#ext-edit-cancel');
  const editClose = root.querySelector('#ext-edit-close');

  let unmountView = null;
  let unmountChrome = null;
  // Where Back should return when the view was opened from elsewhere
  // (e.g. the agent page Ext▾ menu). null = opened from the Extensions
  // list → Back returns to the list.
  let returnToMode = null;
  // Extension currently being edited in the Settings page.
  let editingExt = null;

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function closeView() {
    if (unmountView) { try { unmountView(); } catch (_) {} unmountView = null; }
    if (viewWrap) viewWrap.hidden = true;
    root.classList.remove('ext-viewing');
    returnToMode = null;
  }

  function closeEdit() {
    editingExt = null;
    if (editWrap) editWrap.hidden = true;
    root.classList.remove('ext-editing');
  }

  function onViewBack() {
    const target = returnToMode;
    closeView();
    if (target && typeof setMode === 'function') setMode(target);
  }

  function openExt(ext, { bindContext = null, returnTo = null } = {}) {
    if (ext.native) {
      // Native extension pages live in the app shell; wrap them in the
      // uniform app-managed chrome.
      if (unmountChrome) { try { unmountChrome(); } catch (_) {} unmountChrome = null; }
      returnToMode = returnTo;
      unmountChrome = applyExtChrome({ nativeName: ext.native, ext, returnTo, setMode });
      if (typeof setMode === 'function') setMode(ext.native);
      return;
    }
    if (!ext.hasView) return;
    closeEdit();
    closeView();
    if (viewTitle) viewTitle.textContent = `${ext.title || ext.name}@${ext.version || ''}`;
    const subtitleEl = root.querySelector('#ext-view-subtitle');
    if (subtitleEl) subtitleEl.textContent = ext.name + ((ext.capabilities || []).length ? ' · ' + ext.capabilities.join(', ') : '');
    // Open = dedicated page (like Skills), not an inline expander:
    // the list hides and the view takes over the whole mode area.
    returnToMode = returnTo;
    root.classList.add('ext-viewing');
    if (viewWrap) viewWrap.hidden = false;
    unmountView = mountExtView(viewHost, api, ext, bindContext);
  }

  function renderEditForm(ext, cfg) {
    if (!editForm || !editRaw) return;
    const attrs = Array.isArray(ext.attributes) ? ext.attributes : [];
    if (attrs.length) {
      editForm.innerHTML = attrs.map((line) => {
        const spec = parseAttrSpec(line);
        const current = cfg[spec.key] !== undefined
          ? cfg[spec.key]
          : parseAttrDefault(spec.defaultValue, spec.type);
        if (spec.type === 'boolean') {
          return `
            <label class="form-checkbox">
              <input type="checkbox" data-attr-key="${esc(spec.key)}" ${current ? 'checked' : ''}>
              <span>${esc(spec.label)}</span>
            </label>
            ${spec.hint ? `<p class="form-hint">${esc(spec.hint)}</p>` : ''}
          `;
        }
        // Non-boolean types fall back to the raw JSON editor for now.
        return '';
      }).join('');
    } else {
      editForm.innerHTML = '';
    }
    editRaw.value = JSON.stringify(cfg, null, 2);
  }

  function collectEditConfig() {
    const cfg = {};
    if (editForm) {
      editForm.querySelectorAll('input[type="checkbox"][data-attr-key]').forEach(cb => {
        cfg[cb.dataset.attrKey] = cb.checked;
      });
    }
    // Merge form values on top of any raw JSON the user may have edited.
    try {
      const raw = JSON.parse((editRaw && editRaw.value) || '{}');
      return { ...raw, ...cfg };
    } catch (e) {
      throw new Error('Invalid raw JSON: ' + e.message);
    }
  }

  async function openEditor(ext) {
    closeView();
    editingExt = ext;
    if (editTitle) editTitle.textContent = `${ext.title || ext.name}@${ext.version || ''}`;
    if (editSubtitle) editSubtitle.textContent = ext.description || ext.name;
    root.classList.add('ext-editing');
    if (editWrap) editWrap.hidden = false;
    try {
      const r = await api.extConfigGet(ext.name);
      renderEditForm(ext, (r && r.config) || {});
    } catch (e) {
      setStatus(`Load config failed: ${e.message}`, 'is-error');
      renderEditForm(ext, {});
    }
  }

  async function onEditSave() {
    if (!editingExt) return;
    try {
      const cfg = collectEditConfig();
      await api.extConfigSet(editingExt.name, cfg);
      showToast(`Settings saved for "${editingExt.name}"`, 'success');
      closeEdit();
    } catch (e) {
      setStatus(`Save failed: ${e.message}`, 'is-error');
    }
  }

  // Open an extension view from OUTSIDE the Extensions page (e.g. the
  // agent console Ext▾ menu): caller switches mode first, we render
  // the dedicated page and remember where Back should return.
  _openExtensionView = (ext, opts = {}) => {
    openExt(ext, opts);
  };

  async function refresh() {
    let r;
    try {
      r = await api.listExtensions();
    } catch (e) {
      listEl.innerHTML = `<div class="empty-state">Extensions unavailable: ${esc(e.message)}</div>`;
      return;
    }
    const exts = (r && r.extensions) || [];
    if (!exts.length) {
      listEl.innerHTML = '<div class="empty-state">No extensions. Install one from a folder.</div>';
      return;
    }
    listEl.innerHTML = exts.map((x, i) => `
      <div class="ext-row${x.enabled === false ? ' is-disabled' : ''}" data-i="${i}">
        <div class="ext-row-main">
          <span class="ext-title">${esc(x.title || x.name)}</span>
          <span class="ext-meta">${esc(x.name)}${x.version ? '@' + esc(x.version) : ''} · ${x.shadowing ? 'built-in · updated by user copy' : x.source}${(x.capabilities || []).length ? ' · ' + esc(x.capabilities.join(', ')) : ''}</span>
          ${x.error ? `<span class="ext-error">${esc(x.error)}: ${esc(x.detail || '')}</span>` : ''}
        </div>
        <div class="ext-row-actions">
          ${x.enabled !== false && (x.hasView || x.native) ? `<button type="button" class="btn-sm ext-open-btn" data-i="${i}">Open</button>` : ''}
          <button type="button" class="btn-sm ext-edit-btn" data-i="${i}">Settings</button>
          <button type="button" class="btn-sm ext-toggle-btn" data-i="${i}">${x.enabled === false ? 'Enable' : 'Disable'}</button>
          <button type="button" class="btn-sm btn-danger ext-remove-btn" data-i="${i}">Remove</button>
        </div>
      </div>`).join('');

    listEl.querySelectorAll('.ext-open-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const x = exts[Number(btn.dataset.i)];
        if (x) openExt(x);
      });
    });
    listEl.querySelectorAll('.ext-edit-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const x = exts[Number(btn.dataset.i)];
        if (x) openEditor(x);
      });
    });
    listEl.querySelectorAll('.ext-toggle-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const x = exts[Number(btn.dataset.i)];
        if (!x) return;
        const target = x.enabled === false ? 'enable' : 'disable';
        try {
          await api.request('POST', `/api/extensions/${x.name}/${target}`);
          if (target === 'disable') closeView();
          await refresh();
        } catch (e) { setStatus(`${target} failed: ${e.message}`, 'is-error'); }
      });
    });
    listEl.querySelectorAll('.ext-remove-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const x = exts[Number(btn.dataset.i)];
        if (!x) return;
        try {
          await api.request('DELETE', `/api/extensions/${x.name}`);
          closeView();
          closeEdit();
          showToast(`Extension "${x.name}" removed`, 'success');
          await refresh();
        } catch (e) { setStatus(`Remove failed: ${e.message}`, 'is-error'); }
      });
    });
  }

  if (installBtn) installBtn.addEventListener('click', async () => {
    const b = typeof window !== 'undefined' ? window.CamBridge : null;
    if (!b || !b.files || typeof b.files.pickFile !== 'function') {
      setStatus('Install requires the Desktop file dialog.', 'is-error');
      return;
    }
    const r = await b.files.pickFile({ title: 'Select extension folder', directory: true });
    if (!r || !r.ok || !r.path) return;
    setStatus('Installing…');
    try {
      const res = await api.request('POST', '/api/extensions/install', { path: r.path });
      setStatus(`Installed "${res.name}@${res.manifest.version}"`, 'is-ok');
      showToast(`Extension "${res.name}" installed`, 'success');
      await refresh();
    } catch (e) {
      setStatus(`Install failed: ${e.message}`, 'is-error');
    }
  });

  if (installPkgBtn) installPkgBtn.addEventListener('click', async () => {
    const b = typeof window !== 'undefined' ? window.CamBridge : null;
    if (!b || !b.files || typeof b.files.pickFile !== 'function') {
      setStatus('Install requires the Desktop file dialog.', 'is-error');
      return;
    }
    const r = await b.files.pickFile({ title: 'Select extension package (.tar.gz / .tgz / .tar)' });
    if (!r || !r.ok || !r.path) return;
    if (!/\.(tar\.gz|tgz|tar)$/i.test(r.path)) {
      setStatus('Please pick a .tar.gz, .tgz or .tar package.', 'is-error');
      return;
    }
    setStatus('Installing…');
    try {
      const res = await api.request('POST', '/api/extensions/install', { path: r.path });
      setStatus(`Installed "${res.name}@${res.manifest.version}"`, 'is-ok');
      showToast(`Extension "${res.name}" installed`, 'success');
      await refresh();
    } catch (e) {
      setStatus(`Install failed: ${e.message}`, 'is-error');
    }
  });

  if (viewClose) viewClose.addEventListener('click', onViewBack);
  if (editClose) editClose.addEventListener('click', closeEdit);
  if (editCancel) editCancel.addEventListener('click', closeEdit);
  if (editSave) editSave.addEventListener('click', onEditSave);

  // The mount-time refresh races autoStartConnection() (app.js mounts all
  // modes BEFORE the first connect), so a boot-time 'Not connected' error
  // would stick for the whole session — setMode only toggles visibility,
  // nothing re-runs refresh. Re-refresh when the connection comes up and
  // whenever the page becomes visible. (state.subscribe fires on every
  // set; track last-seen values so only real transitions trigger.)
  let lastConn = '';
  let lastMode = '';
  state.subscribe(() => {
    const conn = state.get('connectionMode') || '';
    const mode = state.get('mode') || '';
    if (conn !== lastConn) {
      lastConn = conn;
      if (conn === 'direct' || conn === 'relay') refresh();
    }
    if (mode !== lastMode) {
      lastMode = mode;
      if (mode === 'extensions') refresh();
    }
  });

  refresh();
}
