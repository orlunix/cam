/* extensions-mode.js — the Extensions page (SPEC §5).
 * Lists built-in + user extensions, installs from a user-picked folder,
 * opens views (native mode navigation for built-ins, sandboxed iframe
 * for package views). */

import { installExtBridge } from '../shared/ext-bridge.js';
import { mountExtView } from '../shared/ext-view-host.js?v=0.68.1';
import { setDoctorAgent } from './agent-doctor-mode.js?v=0.68.0';

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

  let unmountView = null;
  // Where Back should return when the view was opened from elsewhere
  // (e.g. the agent page Ext▾ menu). null = opened from the Extensions
  // list → Back returns to the list.
  let returnToMode = null;

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

  function onViewBack() {
    const target = returnToMode;
    closeView();
    if (target && typeof setMode === 'function') setMode(target);
  }

  function openExt(ext, { bindContext = null, returnTo = null } = {}) {
    if (ext.native) {
      // Native extension pages live in the app shell. agent-doctor gets
      // its Back target through the module handoff (no agent bound when
      // opened from the Extensions list).
      if (ext.native === 'agent-doctor') setDoctorAgent(null, { returnTo: 'extensions' });
      if (typeof setMode === 'function') setMode(ext.native);
      return;
    }
    if (!ext.hasView) return;
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
          ${x.source === 'user' ? `<button type="button" class="btn-sm ext-toggle-btn" data-i="${i}">${x.enabled === false ? 'Enable' : 'Disable'}</button>` : ''}
          ${x.source === 'user' ? `<button type="button" class="btn-sm btn-danger ext-remove-btn" data-i="${i}">Remove</button>` : ''}
        </div>
      </div>`).join('');

    listEl.querySelectorAll('.ext-open-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const x = exts[Number(btn.dataset.i)];
        if (x) openExt(x);
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

  refresh();
}
