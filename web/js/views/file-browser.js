import { api, state, navigate } from '../app.js';

export function renderFileBrowser(container, contextId, subpath) {
  // Clean subpath: remove leading slash, default to empty
  let currentPath = (subpath || '').replace(/^\//, '');
  let loading = false;
  let lastFileContent = null; // cached for raw/rendered toggle
  let lastFileName = null;
  let showingRaw = false;

  container.innerHTML = `
    <div class="page-header" style="display:flex;align-items:center;gap:8px">
      <button id="fb-back" class="btn-sm btn-secondary" title="Back">&larr;</button>
      <div id="fb-breadcrumb" class="fb-breadcrumb" style="flex:1;overflow-x:auto;white-space:nowrap"></div>
    </div>
    <div id="fb-list" class="fb-list"></div>
    <div id="fb-viewer" class="fb-viewer" style="display:none">
      <div id="fb-viewer-header" class="fb-viewer-header">
        <span id="fb-viewer-name"></span>
        <button id="fb-viewer-toggle" class="btn-sm btn-secondary" style="display:none">Raw</button>
        <button id="fb-viewer-close" class="btn-sm btn-secondary">Close</button>
      </div>
      <pre id="fb-viewer-content" class="fb-viewer-content"></pre>
      <iframe id="fb-viewer-iframe" class="fb-viewer-content" sandbox="allow-scripts" style="display:none;border:none;background:#fff"></iframe>
    </div>
  `;

  const listEl = container.querySelector('#fb-list');
  const viewerEl = container.querySelector('#fb-viewer');
  const viewerContent = container.querySelector('#fb-viewer-content');
  const viewerIframe = container.querySelector('#fb-viewer-iframe');
  const viewerName = container.querySelector('#fb-viewer-name');
  const toggleBtn = container.querySelector('#fb-viewer-toggle');
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

  // Raw/Rendered toggle
  toggleBtn.addEventListener('click', () => {
    if (!lastFileContent) return;
    showingRaw = !showingRaw;
    toggleBtn.textContent = showingRaw ? 'Rendered' : 'Raw';
    if (showingRaw) {
      showAsCode(true);
      viewerContent.textContent = lastFileContent;
    } else {
      showAsCode(false);
      if (isHtmlFile(lastFileName)) {
        viewerIframe.srcdoc = lastFileContent;
      } else {
        viewerIframe.srcdoc = mdToHtml(lastFileContent);
      }
    }
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

  function isHtmlFile(name) {
    return /\.html?$/i.test(name);
  }

  function isMdFile(name) {
    return /\.md$/i.test(name);
  }

  function isRichFile(name) {
    return isHtmlFile(name) || isMdFile(name);
  }

  function showAsCode(show) {
    viewerContent.style.display = show ? '' : 'none';
    viewerIframe.style.display = show ? 'none' : '';
  }

  async function openFile(path, name) {
    viewerName.textContent = name;
    viewerContent.textContent = 'Loading...';
    showAsCode(true);
    listEl.style.display = 'none';
    viewerEl.style.display = '';
    lastFileContent = null;
    lastFileName = name;
    showingRaw = false;
    toggleBtn.style.display = 'none';
    toggleBtn.textContent = 'Raw';

    try {
      const data = await api.readFile(contextId, path);
      if (data.binary) {
        viewerContent.textContent = `[Binary file, ${formatSize(data.size)}]`;
      } else if (isRichFile(name)) {
        lastFileContent = data.content;
        toggleBtn.style.display = '';
        showAsCode(false);
        if (isHtmlFile(name)) {
          viewerIframe.srcdoc = data.content;
        } else {
          viewerIframe.srcdoc = mdToHtml(data.content);
        }
      } else {
        viewerContent.textContent = data.content;
      }
    } catch (e) {
      showAsCode(true);
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

// Split a YAML front-matter block off the document. Returns simple
// key/value rows (arrays flattened "a, b"); complex YAML stays raw in
// the value cell — good enough for a metadata header without a YAML lib.
function splitFrontMatter(md) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(md);
  if (!m) return { metaRows: [], body: md };
  const rows = [];
  let cur = null;
  for (const line of m[1].split(/\r?\n/)) {
    const kv = /^([A-Za-z0-9_.-]+):\s*(.*)$/.exec(line);
    if (kv) { cur = { k: kv[1], v: kv[2].trim() }; rows.push(cur); continue; }
    const item = /^\s+-\s+(.*)$/.exec(line);
    if (item && cur) { cur.v += (cur.v ? ', ' : '') + item[1].trim(); continue; }
    if (cur) cur.v += ' ' + line.trim();
  }
  return { metaRows: rows, body: md.slice(m[0].length) };
}

// Render markdown inside an iframe. Vendor scripts are bundled with the
// app (web/vendor) — no CDN, works offline and inside the MAS sandbox.
// Front matter renders as a metadata table; output is
// DOMPurify-sanitized and the md payload is <\/script>-proofed.
function mdToHtml(md) {
  const { metaRows, body } = splitFrontMatter(md);
  const metaHtml = metaRows.length
    ? '<table class="md-meta"><tbody>' + metaRows.map(r =>
        `<tr><th>${escapeHtml(r.k)}</th><td>${escapeHtml(r.v)}</td></tr>`).join('') + '</tbody></table>'
    : '';
  const payload = JSON.stringify(body).replace(/<\//g, '<\\/');
  return `<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 18px 20px; line-height: 1.7; color: #24292f; max-width: 100%; word-wrap: break-word; }
h1, h2, h3, h4, h5, h6 { margin: 22px 0 10px; font-weight: 600; }
h1 { font-size: 1.8em; border-bottom: 1px solid #eaecef; padding-bottom: 6px; }
h2 { font-size: 1.5em; border-bottom: 1px solid #eaecef; padding-bottom: 4px; }
h3 { font-size: 1.25em; }
p { margin: 10px 0; }
pre { background: #f6f8fa; border: 1px solid #e8eaed; padding: 12px 14px; border-radius: 8px; overflow-x: auto; font-size: 13px; line-height: 1.55; }
code { background: #eff1f3; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #d0d7de; padding: 2px 14px; color: #57606a; margin: 10px 0; background: #fafbfc; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13.5px; }
th, td { border: 1px solid #e1e4e8; padding: 7px 12px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
tbody tr:nth-child(even) { background: #fafbfc; }
table.md-meta { width: auto; margin: 0 0 16px; font-size: 13px; }
table.md-meta th { white-space: nowrap; vertical-align: top; color: #57606a; }
img { max-width: 100%; }
ul, ol { padding-left: 24px; margin: 8px 0; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #eaecef; margin: 22px 0; }
a { color: #0969da; }
.task-list-item { list-style: none; margin-left: -24px; }
.task-list-item input { margin-right: 6px; }
</style>
<script src="vendor/marked/marked.min.js"><\/script>
<script src="vendor/dompurify/purify.min.js"><\/script>
</head><body>
${metaHtml}
<div id="md-content"></div>
<script>
(async () => {
  const raw = ${payload};
  const html = DOMPurify.sanitize(marked.parse(raw));
  const host = document.getElementById('md-content');
  host.innerHTML = html;
})();
<\/script>
</body></html>`;
}
