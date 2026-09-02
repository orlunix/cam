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
const os   = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');

const NAME_RE = /^[a-z0-9-]{1,32}$/;
// hub:api — the extension's view may drive the app through ext.hubCall
// (any /api/* endpoint, all verbs). This is the same power the app itself
// has, so declaring it in the manifest IS the user-consent surface; every
// call is audit-logged ([ext:<name>] METHOD path).
const KNOWN_CAPS = new Set(['exec', 'files:read', 'files:write', 'hub:api']);
const MAX_FILES = 256;
const MAX_TOTAL_BYTES = 8 * 1024 * 1024;  // 8 MB per extension package
const MAX_FILE_BYTES  = 4 * 1024 * 1024;  // 4 MB per single file

/** Dotted-numeric version compare: '0.5.1' > '0.5.0', '0.10' > '0.9',
 *  '1.0' == '1.0.0' (a missing piece is 0). A PRESENT non-numeric piece
 *  sorts LOW ('' from an absent manifest never beats a versioned one).
 *  Returns >0 / 0 / <0. */
function compareVersions(a, b) {
  const pa = String(a || '').split('.'), pb = String(b || '').split('.');
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const xa = pa[i] === undefined ? 0 : (/^\d+$/.test(pa[i]) ? Number(pa[i]) : -1);
    const xb = pb[i] === undefined ? 0 : (/^\d+$/.test(pb[i]) ? Number(pb[i]) : -1);
    if (xa !== xb) return xa - xb;
  }
  return 0;
}

/** Read just the `version:` of a package dir's manifest ('' when absent
 *  or unparseable — sorts low via compareVersions). */
function manifestVersion(dir) {
  try {
    const m = /^version:\s*(\S+)\s*$/m.exec(fs.readFileSync(path.join(dir, 'manifest.yaml'), 'utf8'));
    return m ? m[1] : '';
  } catch (_) { return ''; }
}

/** Which copy of <name> serves when both a built-in and a user copy
 *  exist: the HIGHER version wins and a TIE GOES TO THE BUILT-IN — an
 *  app reinstall/upgrade must repair stale shadows (same-version user
 *  copies are exactly the "old bits behind a new version number" trap).
 *  A strictly newer user copy still wins, which is how tar.gz updates
 *  keep working without an app release. Returns null when neither
 *  exists; otherwise { dir, source, shadowing?, shadowedUserVersion? }. */
function resolvePackageDir(name, { packagesDir, extRoot }) {
  const userDir = path.join(extRoot, name);
  const builtinDir = path.join(packagesDir, name);
  const hasUser = fs.existsSync(userDir);
  const hasBuiltin = fs.existsSync(builtinDir);
  if (!hasUser && !hasBuiltin) return null;
  if (hasUser && !hasBuiltin) return { dir: userDir, source: 'user' };
  if (!hasUser) return { dir: builtinDir, source: 'builtin' };
  const uv = manifestVersion(userDir), bv = manifestVersion(builtinDir);
  if (compareVersions(uv, bv) > 0) {
    return { dir: userDir, source: 'user', shadowing: true, shadowedBuiltinVersion: bv };
  }
  return { dir: builtinDir, source: 'builtin', shadowedUserVersion: uv };
}

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
  // Native extensions: no iframe VIEW entry (their page is native app
  // code like the skills page), but a TOOL entry (main.py remote
  // collector) is allowed.
  let entries;
  if (native) {
    if (topFiles.some(f => /\.html?$/i.test(f))) {
      return { error: 'invalid_native', detail: 'native extensions must not carry view (.html) entries' };
    }
    entries = { view: null, tool: topFiles.includes('main.py') ? 'main.py' : (topFiles.find(f => /\.py$/i.test(f)) || null) };
  } else {
    entries = resolveEntries(topFiles);
    if (entries.error) return entries;
  }

  return {
    ok: true,
    manifest: {
      name:         String(manifest.name),
      version:      String(manifest.version),
      title:        String(manifest.title || manifest.name),
      description:  String(manifest.description || ''),
      attributes:   Array.isArray(manifest.attributes) ? manifest.attributes.filter(a => typeof a === 'string') : [],
      capabilities: caps,
      native,
      mounts:       Array.isArray(manifest.mounts) ? manifest.mounts.filter(m => typeof m === 'string') : [],
      kind:         String(manifest.kind || 'tool'),
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

/** Minimal tar reader (ustar, 512-byte headers) for .tar.gz install
 *  packages. Returns [{ name, content:Buffer }] for regular files.
 *  Rejects path traversal and odd entry types — we only extract plain
 *  files to a temp dir before validation. */
function _untar(buf) {
  const files = [];
  let off = 0;
  while (off + 512 <= buf.length) {
    const header = buf.slice(off, off + 512);
    if (header.every(b => b === 0)) break; // end-of-archive blocks
    const name = header.slice(0, 100).toString('utf8').replace(/\0.*$/, '');
    const prefix = header.slice(345, 500).toString('utf8').replace(/\0.*$/, '');
    const fullName = prefix ? `${prefix}/${name}` : name;
    const size = parseInt(header.slice(124, 136).toString('utf8').replace(/\0.*$/, '').trim(), 8) || 0;
    const type = String.fromCharCode(header[156]);
    off += 512;
    const content = buf.slice(off, off + size);
    off += Math.ceil(size / 512) * 512;
    if (type !== '0' && type !== '' && type !== '\0') continue; // files only (skip dirs/links)
    if (!fullName || fullName.includes('..') || path.isAbsolute(fullName) || fullName.startsWith('/')) {
      throw new Error(`unsafe tar entry: ${fullName}`);
    }
    files.push({ name: fullName, content });
  }
  return files;
}

/** Extract a .tar.gz/.tgz/.tar package into a temp dir; returns the dir. */
function _extractTarToTemp(pkgPath) {
  let buf = fs.readFileSync(pkgPath);
  if (!/\.tar$/i.test(pkgPath)) buf = zlib.gunzipSync(buf);
  const files = _untar(buf);
  if (!files.length) throw new Error('empty archive');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-tgz-'));
  for (const f of files) {
    const dst = path.join(tmp, f.name);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.writeFileSync(dst, f.content);
  }
  return tmp;
}

/** Install from a folder OR a .tar.gz/.tgz/.tar package. When the
 *  manifest sits in a single top-level subdir (typical archives), that
 *  subdir is used as the package root. Existing same-name extension is
 *  replaced (update semantics). */
function installExtension(srcPath, extRoot) {
  let dir = srcPath;
  let tmpToClean = null;
  if (fs.existsSync(srcPath) && fs.statSync(srcPath).isFile() && /\.(tar\.gz|tgz|tar)$/i.test(srcPath)) {
    try {
      tmpToClean = _extractTarToTemp(srcPath);
      dir = tmpToClean;
    } catch (e) {
      return { error: 'package_extract_failed', detail: e && e.message };
    }
  }
  // Unwrap a single top-level folder (archive-style packages).
  if (dir && !fs.existsSync(path.join(dir, 'manifest.yaml'))) {
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true }).filter(e => e.isDirectory());
      const files = fs.readdirSync(dir, { withFileTypes: true }).filter(e => e.isFile());
      if (entries.length === 1 && files.length === 0
          && fs.existsSync(path.join(dir, entries[0].name, 'manifest.yaml'))) {
        dir = path.join(dir, entries[0].name);
      }
    } catch (_) {}
  }
  const info = inspectExtension(dir);
  if (!info.ok) {
    if (tmpToClean) { try { fs.rmSync(tmpToClean, { recursive: true, force: true }); } catch (_) {} }
    return info;
  }
  const dst = path.join(extRoot, info.manifest.name);
  try {
    fs.rmSync(dst, { recursive: true, force: true });
    copyDir(dir, dst);
  } catch (e) {
    if (tmpToClean) { try { fs.rmSync(tmpToClean, { recursive: true, force: true }); } catch (_) {} }
    return { error: 'install_failed', detail: e && e.message };
  }
  if (tmpToClean) { try { fs.rmSync(tmpToClean, { recursive: true, force: true }); } catch (_) {} }
  return { ok: true, name: info.manifest.name, manifest: info.manifest, entries: info.entries };
}

/** Platform attribute shown on every extension's Settings page. Uniform and
 *  the ONLY form field the app renders: per user direction the Ext menu
 *  starts empty (default false) and the user pins entries explicitly;
 *  saved values live in the per-ext config store and persist across
 *  reinstalls. Custom per-ext config is the extension's own affair —
 *  its view reads ext.config and surfaces it wherever it likes. */
const PLATFORM_ATTRIBUTES = [
  'show_in_agent_menu | boolean | false | Show in agent Ext menu | List this extension in the agent page Ext menu.',
];

/** List extensions: built-ins from packagesDir + user ones from
 *  extRoot, merged with the store's flags. Built-ins carry NO
 *  privileges: the enabled flag applies to them exactly like user
 *  extensions, and a store entry with removed:true hides the built-in
 *  row (its files stay in the read-only app bundle; reinstalling a
 *  same-name folder/package restores it). When BOTH copies exist, the
 *  higher-version one serves (resolvePackageDir): a newer user copy
 *  shadows the built-in (single row, source 'user', shadowing: true) —
 *  reinstall-from-package is how built-ins get updated without an app
 *  release — while an equal/older user copy loses to the built-in, so
 *  an app reinstall/upgrade repairs stale shadows (the row then carries
 *  shadowed_user: <ignored version>). */
function listExtensions({ packagesDir, extRoot, storeExts }) {
  const flags = new Map((Array.isArray(storeExts) ? storeExts : []).map(e => [e.name, e]));
  const out = [];
  const names = new Set();
  for (const dir of [packagesDir, extRoot]) {
    let entries = [];
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (_) { continue; }
    for (const e of entries) if (e.isDirectory()) names.add(e.name);
  }
  for (const dirName of names) {
    let win = resolvePackageDir(dirName, { packagesDir, extRoot });
    if (!win) continue;
    // removed:true hides the built-in; when a user copy exists alongside,
    // it serves instead (removing the built-in never removes the user's
    // own install).
    if (win.source === 'builtin' && (flags.get(dirName) || {}).removed === true
        && fs.existsSync(path.join(extRoot, dirName))) {
      win = { dir: path.join(extRoot, dirName), source: 'user' };
    }
    const info = inspectExtension(win.dir);
    if (!info.ok) {
      out.push({ name: dirName, source: win.source, enabled: false, error: info.error, detail: info.detail });
      continue;
    }
    const row = {
      name:         info.manifest.name,
      title:        info.manifest.title,
      description:  info.manifest.description || '',
      version:      info.manifest.version,
      capabilities: info.manifest.capabilities,
      native:       info.manifest.native || '',
      mounts:       info.manifest.mounts || [],
      attributes:   PLATFORM_ATTRIBUTES.slice(),
      kind:         info.manifest.kind || 'tool',
      viewFile:     info.entries.view || '',
      hasView:      !!info.entries.view,
      hasTool:      !!info.entries.tool,
      source:       win.source,
      enabled:      (flags.get(info.manifest.name) || {}).enabled !== false,
    };
    if (win.shadowing) row.shadowing = true;
    if (win.shadowedUserVersion !== undefined) row.shadowed_user = win.shadowedUserVersion;
    out.push(row);
  }
  // A store entry with removed:true hides the BUILT-IN row (the user-copy
  // case was already swapped to a user row above).
  return out
    .filter(e => !(e.source === 'builtin' && (flags.get(e.name) || {}).removed === true));
}

module.exports = {
  parseManifest,
  resolveEntries,
  inspectExtension,
  installExtension,
  listExtensions,
  copyDir,
  compareVersions,
  resolvePackageDir,
};
