import { api, state, navigate } from '../app.js';

export function renderFileBrowser(container, contextId, subpath) {
  // Clean subpath: remove leading slash, default to empty
  let currentPath = (subpath || '').replace(/^\//, '');
  let loading = false;

  container.innerHTML = `
    <div class="page-header" style="display:flex;align-items:center;gap:8px">
      <button id="fb-back" class="btn-sm btn-secondary" title="Back">&larr;</button>
      <div id="fb-breadcrumb" class="fb-breadcrumb" style="flex:1;overflow-x:auto;white-space:nowrap"></div>
    </div>
    <div id="fb-list" class="fb-list"></div>
    <div id="fb-viewer" class="fb-viewer" style="display:none">
      <div id="fb-viewer-header" class="fb-viewer-header">
        <span id="fb-viewer-name"></span>
        <button id="fb-viewer-close" class="btn-sm btn-secondary">Close</button>
      </div>
      <pre id="fb-viewer-content" class="fb-viewer-content"></pre>
    </div>
  `;

  const listEl = container.querySelector('#fb-list');
  const viewerEl = container.querySelector('#fb-viewer');
  const viewerContent = container.querySelector('#fb-viewer-content');
  const viewerName = container.querySelector('#fb-viewer-name');
  const breadcrumbEl = container.querySelector('#fb-breadcrumb');

  // Back button
  container.querySelector('#fb-back').addEventListener('click', () => {
    if (loading) return;
    if (viewerEl.style.display !== 'none') {
      viewerEl.style.display = 'none';
      listEl.style.display = '';
      return;
    }
    if (currentPath) {
      const parts = currentPath.split('/').filter(Boolean);
      parts.pop();
      navigateTo(parts.join('/'));
    } else {
      navigate('#/contexts');
    }
  });

  // Close viewer
  container.querySelector('#fb-viewer-close').addEventListener('click', () => {
    viewerEl.style.display = 'none';
    listEl.style.display = '';
  });

  function navigateTo(path) {
    if (loading) return;
    currentPath = path;
    loadDir();
  }

  function renderBreadcrumb() {
    const contexts = state.get('contexts') || [];
    const ctx = contexts.find(c => c.id === contextId || c.name === contextId);
    const contextName = ctx ? ctx.name : contextId;

    const parts = currentPath ? currentPath.split('/').filter(Boolean) : [];
    let html = `<span class="fb-crumb" data-path="">${contextName}</span>`;
    let accumulated = '';
    for (const part of parts) {
      accumulated += (accumulated ? '/' : '') + part;
      html += ` <span class="fb-sep">/</span> <span class="fb-crumb" data-path="${accumulated}">${part}</span>`;
    }
    breadcrumbEl.innerHTML = html;

    breadcrumbEl.querySelectorAll('.fb-crumb').forEach(el => {
      el.addEventListener('click', () => navigateTo(el.dataset.path));
    });
  }

  async function loadDir() {
    loading = true;
    renderBreadcrumb();
    listEl.innerHTML = '<div class="empty-state">Loading...</div>';
    viewerEl.style.display = 'none';
    listEl.style.display = '';

    try {
      const pathToLoad = currentPath;
      const data = await api.listFiles(contextId, pathToLoad);

      // If path changed while loading, discard stale result
      if (currentPath !== pathToLoad) return;

      if (!data.entries || data.entries.length === 0) {
        listEl.innerHTML = '<div class="empty-state">Empty directory</div>';
        return;
      }

      const entries = data.entries.sort((a, b) => {
        if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

      listEl.innerHTML = entries.map(e => {
        const icon = e.type === 'dir' ? '<span class="fb-icon fb-icon-dir">&#128193;</span>' : '<span class="fb-icon fb-icon-file">&#128196;</span>';
        const size = e.type === 'file' ? formatSize(e.size) : '';
        return `
          <div class="fb-row" data-name="${escapeAttr(e.name)}" data-type="${e.type}">
            ${icon}
            <span class="fb-name">${escapeHtml(e.name)}</span>
            <span class="fb-size">${size}</span>
          </div>`;
      }).join('');

    } catch (e) {
      listEl.innerHTML = `<div class="empty-state" style="color:var(--red)">${escapeHtml(e.message)}</div>`;
    } finally {
      loading = false;
    }
  }

  async function openFile(path, name) {
    viewerName.textContent = name;
    viewerContent.textContent = 'Loading...';
    listEl.style.display = 'none';
    viewerEl.style.display = '';

    try {
      const data = await api.readFile(contextId, path);
      if (data.binary) {
        viewerContent.textContent = `[Binary file, ${formatSize(data.size)}]`;
      } else {
        viewerContent.textContent = data.content;
      }
    } catch (e) {
      viewerContent.textContent = `Error: ${e.message}`;
    }
  }

  // Event delegation for file/dir clicks (survives innerHTML replacement)
  listEl.addEventListener('click', (e) => {
    if (loading) return;
    const row = e.target.closest('.fb-row');
    if (!row) return;
    const name = row.dataset.name;
    const type = row.dataset.type;
    const newPath = currentPath ? `${currentPath}/${name}` : name;
    if (type === 'dir') {
      navigateTo(newPath);
    } else {
      openFile(newPath, name);
    }
  });

  // Initial load
  loadDir();
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttr(s) {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}
