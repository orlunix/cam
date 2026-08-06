/* extensions/host/registry.cjs — extension registry for the embedded Hub.
 *
 * Responsibilities (SPEC §1/§5):
 *  - parse manifest.yaml (flat YAML subset: top-level key: value and
 *    `key:` + `  - item` lists; no nesting, no anchors)
 *  - resolve view/tool entries per the SPEC rules (index.html → single
 *    .html → view_ambiguous; same for main.py / .py)
 *  - validate an extension folder (manifest legal, size caps, entries
 *    unambiguous)
 *  - install = copy folder into <userData>/extensions/<name>/
 *  - list built-in + user extensions with enabled state
 *
 * No Python is executed here (iron rule); no network. All local fs.
 */

'use strict';

const fs   = require('node:fs');
const path = require('node:path');

const NAME_RE = /^[a-z0-9-]{1,32}$/;
const KNOWN_CAPS = new Set(['exec', 'files:read', 'files:write']);
const MAX_FILES = 256;
const MAX_TOTAL_BYTES = 8 * 1024 * 1024;  // 8 MB per extension package
const MAX_FILE_BYTES  = 4 * 1024 * 1024;  // 4 MB per single file

/** Flat-YAML-subset manifest parser.
 *  Supports: `key: value`, `key:` + `  - item` lists, `#` comments,
 *  blank lines. Values are plain strings (quotes stripped). */
function parseManifest(text) {
  const out = {};
  let curListKey = null;
  for (const rawLine of String(text || '').split(/\r?\n/)) {
    const line = rawLine.replace(/\s+$/, '');
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const item = /^\s+-\s+(.*)$/.exec(line);
    if (item && curListKey) {
      out[curListKey].push(_yamlScalar(item[1]));
      continue;
    }
    const kv = /^([A-Za-z0-9_.:-]+):\s*(.*)$/.exec(line);
    if (!kv) return { error: 'manifest_parse', detail: `cannot parse line: ${line.slice(0, 80)}` };
    curListKey = null;
    if (kv[2] === '') {
      out[kv[1]] = [];
      curListKey = kv[1];
    } else {
      out[kv[1]] = _yamlScalar(kv[2]);
    }
  }
  return out;
}

function _yamlScalar(v) {
  const s = String(v || '').trim();
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) return s.slice(1, -1);
  if (s.length >= 2 && s.startsWith("'") && s.endsWith("'")) return s.slice(1, -1);
  return s;
}

/** Entry resolution per SPEC §1. `files` is a basename list. */
function resolveEntries(files) {
  const htmls = files.filter(f => /\.html?$/i.test(f));
  const pys   = files.filter(f => /\.py$/i.test(f));
  const out = { view: null, tool: null };
  if (files.includes('index.html')) out.view = 'index.html';
  else if (htmls.length === 1) out.view = htmls[0];
  else if (htmls.length > 1) return { error: 'view_ambiguous', detail: 'multiple .html files but no index.html' };
  if (files.includes('main.py')) out.tool = 'main.py';
  else if (pys.length === 1) out.tool = pys[0];
  else if (pys.length > 1) return { error: 'tool_ambiguous', detail: 'multiple .py files but no main.py' };
  if (!out.view && !out.tool) return { error: 'empty_extension', detail: 'no view (.html) and no tool (.py) entry found' };
  return out;
}

/** Validate + describe an extension folder. Pure read-only. */
function inspectExtension(dir) {
  const manifestPath = path.join(dir, 'manifest.yaml');
  if (!fs.existsSync(manifestPath)) {
    return { error: 'manifest_missing', detail: 'manifest.yaml not found at the folder root' };
  }
  let manifest;
  try {
    manifest = parseManifest(fs.readFileSync(manifestPath, 'utf8'));
  } catch (e) {
    return { error: 'manifest_read', detail: e && e.message };
  }
  if (manifest.error) return manifest;
  if (!NAME_RE.test(String(manifest.name || ''))) {
    return { error: 'invalid_name', detail: `name must match ${NAME_RE} (got "${manifest.name || ''}")` };
  }
  if (!String(manifest.version || '').trim()) {
    return { error: 'invalid_version', detail: 'version is required' };
  }
  const caps = Array.isArray(manifest.capabilities) ? manifest.capabilities : [];
  const badCap = caps.find(c => !KNOWN_CAPS.has(c));
  if (badCap) {
    return { error: 'invalid_capability', detail: `unknown capability "${badCap}" (known: ${[...KNOWN_CAPS].join(', ')})` };
  }

  // File inventory with size caps (top level only for entries; assets
  // may nest one level — we cap total package size, not depth).
  const files = [];
  let total = 0;
  try {
    const walk = (sub) => {
      for (const ent of fs.readdirSync(path.join(dir, sub), { withFileTypes: true })) {
        const rel = sub ? `${sub}/${ent.name}` : ent.name;
        if (ent.isDirectory()) { walk(rel); continue; }
        const st = fs.statSync(path.join(dir, rel));
        files.push(rel);
        total += st.size;
        if (st.size > MAX_FILE_BYTES) throw new Error(`file too large (>4MB): ${rel}`);
        if (files.length > MAX_FILES) throw new Error(`too many files (>${MAX_FILES})`);
      }
    };
    walk('');
  } catch (e) {
    return { error: 'package_too_large', detail: e && e.message };
  }
  if (total > MAX_TOTAL_BYTES) {
    return { error: 'package_too_large', detail: `extension is ${Math.round(total / 1024)}KB (max ${MAX_TOTAL_BYTES / 1024 / 1024}MB)` };
  }

  const topFiles = files.filter(f => !f.includes('/')).map(f => path.basename(f));
  const native = String(manifest.native || '').trim();
  // Native extensions (built-in aliases for native app pages) carry no
  // view/tool entries — skip entry resolution entirely.
  const entries = native ? { view: null, tool: null } : resolveEntries(topFiles);
  if (entries.error) return entries;
  if (native && (topFiles.some(f => /\.html?$/i.test(f)) || topFiles.some(f => /\.py$/i.test(f)))) {
    return { error: 'invalid_native', detail: 'native extensions must not carry view/tool entries' };
  }

  return {
    ok: true,
    manifest: {
      name:         String(manifest.name),
      version:      String(manifest.version),
      title:        String(manifest.title || manifest.name),
      capabilities: caps,
      native,
    },
    entries,
    sizeBytes: total,
  };
}

/** Recursively copy a folder (no symlinks followed). */
function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isDirectory()) { copyDir(s, d); continue; }
    if (ent.isSymbolicLink()) continue;
    fs.copyFileSync(s, d);
  }
}

/** Install (copy) an extension folder into extRoot/<name>/. Existing
 *  same-name extension is replaced (update semantics). */
function installExtension(srcDir, extRoot) {
  const info = inspectExtension(srcDir);
  if (!info.ok) return info;
  const dst = path.join(extRoot, info.manifest.name);
  try {
    fs.rmSync(dst, { recursive: true, force: true });
    copyDir(srcDir, dst);
  } catch (e) {
    return { error: 'install_failed', detail: e && e.message };
  }
  return { ok: true, name: info.manifest.name, manifest: info.manifest, entries: info.entries };
}

/** List extensions: built-ins from packagesDir + user ones from
 *  extRoot, merged with the store's enabled flags. */
function listExtensions({ packagesDir, extRoot, storeExts }) {
  const flags = new Map((Array.isArray(storeExts) ? storeExts : []).map(e => [e.name, e.enabled !== false]));
  const out = [];
  const scan = (dir, source) => {
    let names = [];
    try { names = fs.readdirSync(dir, { withFileTypes: true }).filter(e => e.isDirectory()).map(e => e.name); }
    catch (_) { return; }
    for (const name of names) {
      const info = inspectExtension(path.join(dir, name));
      if (!info.ok) {
        out.push({ name, source, enabled: false, error: info.error, detail: info.detail });
        continue;
      }
      out.push({
        name:         info.manifest.name,
        title:        info.manifest.title,
        version:      info.manifest.version,
        capabilities: info.manifest.capabilities,
        native:       info.manifest.native || '',
        hasView:      !!info.entries.view,
        hasTool:      !!info.entries.tool,
        source,
        enabled:      source === 'builtin' ? true : flags.get(info.manifest.name) !== false,
      });
    }
  };
  scan(packagesDir, 'builtin');
  scan(extRoot, 'user');
  return out;
}

module.exports = {
  parseManifest,
  resolveEntries,
  inspectExtension,
  installExtension,
  listExtensions,
  copyDir,
};
