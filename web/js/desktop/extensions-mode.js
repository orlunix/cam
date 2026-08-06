/* extensions-mode.js — the Extensions page (SPEC §5).
 * Lists built-in + user extensions, installs from a user-picked folder,
 * opens views (native mode navigation for built-ins, sandboxed iframe
 * for package views). */

import { installExtBridge } from '../shared/ext-bridge.js';
import { mountExtView } from '../shared/ext-view-host.js';

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
  const viewWrap = root.querySelector('#ext-view-wrap');
  const viewTitle = root.querySelector('#ext-view-title');
  const viewHost = root.querySelector('#ext-view-host');
  const viewClose = root.querySelector('#ext-view-close');

  let unmountView = null;

  function setStatus(text, cls = '') {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.remove('is-error', 'is-ok');
    if (cls) statusEl.classList.add(cls);
  }

  function closeView() {
    if (unmountView) { try { unmountView(); } catch (_) {} unmountView = null; }
    if (viewWrap) viewWrap.hidden = true;
  }

  function openExt(ext) {
    if (ext.native) {
      if (typeof setMode === 'function') setMode(ext.native);
      return;
    }
    if (!ext.hasView) return;
    closeView();
    if (viewTitle) viewTitle.textContent = `${ext.title || ext.name}@${ext.version || ''}`;
    if (viewWrap) viewWrap.hidden = false;
    unmountView = mountExtView(viewHost, api, ext);
  }

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
          <span class="ext-meta">${esc(x.name)}${x.version ? '@' + esc(x.version) : ''} · ${x.source}${(x.capabilities || []).length ? ' · ' + esc(x.capabilities.join(', ')) : ''}</span>
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

  if (viewClose) viewClose.addEventListener('click', closeView);

  refresh();
}
