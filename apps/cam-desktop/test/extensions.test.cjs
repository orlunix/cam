/* extensions registry + hub endpoints tests. Uses the real
 * packages/assistant as the fixture (it is the canonical sample). */

'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..', '..', '..');
const registry = require(path.join(root, 'extensions', 'host', 'registry.cjs'));

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) pass++;
  else { fail++; console.log('FAIL', name, extra || ''); }
}

const sampleDir = path.join(root, 'extensions', 'packages', 'assistant');

// ── manifest parser ──
const m = registry.parseManifest('name: demo\nversion: 1.2.3\ncapabilities:\n  - exec\n  - files:read\n# comment\n');
ok('manifest parses scalars + list', m.name === 'demo' && m.version === '1.2.3'
  && Array.isArray(m.capabilities) && m.capabilities.length === 2 && m.capabilities[0] === 'exec');
ok('manifest quoted value', registry.parseManifest('title: "My Tool"').title === 'My Tool');

// ── entry resolution rules ──
ok('index.html wins', registry.resolveEntries(['index.html', 'a.html', 'main.py']).view === 'index.html');
ok('single html fallback', registry.resolveEntries(['view.html', 'main.py']).view === 'view.html');
ok('multiple html w/o index → ambiguous', registry.resolveEntries(['a.html', 'b.html']).error === 'view_ambiguous');
ok('main.py wins / single py fallback',
  registry.resolveEntries(['x.py', 'index.html']).tool === 'x.py'
  && registry.resolveEntries(['main.py', 'x.py']).tool === 'main.py');
ok('multiple py w/o main → ambiguous', registry.resolveEntries(['a.py', 'b.py', 'index.html']).error === 'tool_ambiguous');
ok('no entries → empty_extension', registry.resolveEntries(['README.md']).error === 'empty_extension');

// ── inspect the real sample ──
const info = registry.inspectExtension(sampleDir);
ok('assistant inspects clean', info.ok === true, JSON.stringify(info));
ok('assistant manifest', info.manifest && info.manifest.name === 'assistant'
  && info.manifest.capabilities.includes('exec'));
ok('assistant entries resolved', info.entries && info.entries.view === 'index.html' && info.entries.tool === 'main.py');

// invalid manifest cases
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-test-'));
fs.writeFileSync(path.join(tmp, 'manifest.yaml'), 'name: BAD NAME\nversion: 1\n');
fs.writeFileSync(path.join(tmp, 'index.html'), '<html></html>');
ok('invalid name rejected', registry.inspectExtension(tmp).error === 'invalid_name');
fs.writeFileSync(path.join(tmp, 'manifest.yaml'), 'name: ok-name\nversion: 1\ncapabilities:\n  - exec\n  - bogus\n');
ok('unknown capability rejected', registry.inspectExtension(tmp).error === 'invalid_capability');
fs.unlinkSync(path.join(tmp, 'index.html'));
fs.writeFileSync(path.join(tmp, 'manifest.yaml'), 'name: ok-name\nversion: 1\n');
ok('missing entries rejected', registry.inspectExtension(tmp).error === 'empty_extension');

// ── install (copy) ──
const extRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-root-'));
const inst = registry.installExtension(sampleDir, extRoot);
ok('install ok', inst.ok === true && inst.name === 'assistant');
ok('install copied files', fs.existsSync(path.join(extRoot, 'assistant', 'main.py'))
  && fs.existsSync(path.join(extRoot, 'assistant', 'index.html'))
  && fs.existsSync(path.join(extRoot, 'assistant', 'manifest.yaml')));

// list merges builtin + user with enabled flags. The installed assistant
// copy is the SAME version as the built-in — a tie goes to the built-in
// (an app reinstall repairs stale same-version shadows), so the row is
// the built-in one, annotated with the ignored user copy's version.
const list = registry.listExtensions({
  packagesDir: path.join(root, 'extensions', 'packages'),
  extRoot,
  storeExts: [{ name: 'assistant', enabled: false }],
});
ok('list includes builtin skills/todos + builtin assistant (tie → built-in wins)',
  list.some(e => e.name === 'skills' && e.source === 'builtin' && e.hasView === true
      && (e.capabilities || []).includes('hub:api'))
    && list.some(e => e.name === 'todos' && e.source === 'builtin' && e.hasView === true)
    && list.some(e => e.name === 'assistant' && e.source === 'builtin'
      && e.shadowed_user === info.manifest.version));
ok('agent-doctor is no longer built-in (moved to examples/)',
  !list.some(e => e.name === 'agent-doctor'));
ok('store disabled flag honored', list.find(e => e.name === 'assistant').enabled === false);

// Every list row carries the single uniform platform attribute
// (show_in_agent_menu, default false — the Ext menu starts empty and the
// user pins entries via Extensions → Settings). Manifest-declared custom
// attributes are parsed but NOT surfaced in the app Settings form.
const skillsRow = list.find(e => e.name === 'skills');
const hello = list.find(e => e.name === 'assistant');
ok('every row carries the uniform platform attribute', skillsRow && hello
  && skillsRow.attributes.length === 1 && hello.attributes.length === 1
  && skillsRow.attributes[0].startsWith('show_in_agent_menu | boolean | false')
  && hello.attributes[0] === skillsRow.attributes[0],
  JSON.stringify(skillsRow && skillsRow.attributes));

// the app-managed chrome's subtitle is the manifest description — every
// built-in must carry one.
ok('built-ins carry a description (chrome subtitle)',
  ['skills', 'todos', 'assistant'].every(n => {
    const e = list.find(x => x.name === n);
    return e && typeof e.description === 'string' && e.description.length > 0;
  }));

// install from a .tar.gz package (single top-level dir unwrap).
const cp = require('node:child_process');
const tgz = path.join(os.tmpdir(), 'assistant-pkg.tar.gz');
cp.execFileSync('tar', ['-czf', tgz, '-C', path.join(root, 'extensions', 'packages'), 'assistant']);
const instPkg = registry.installExtension(tgz, extRoot);
ok('install from .tar.gz package', instPkg.ok === true && instPkg.name === 'assistant', JSON.stringify(instPkg));
ok('.tar.gz landed files incl. subdir unwrap', fs.existsSync(path.join(extRoot, 'assistant', 'main.py')));

// version-gated shadowing (0.2.30): the HIGHER version serves; a tie or
// an older user copy loses to the built-in (app reinstall wins).
const pkgRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-pkg-'));
fs.mkdirSync(path.join(pkgRoot, 'skills'), { recursive: true });
fs.writeFileSync(path.join(pkgRoot, 'skills', 'manifest.yaml'), 'name: skills\nversion: 0.3.0\nnative: skills\n');
fs.mkdirSync(path.join(pkgRoot, 'todos'), { recursive: true });
fs.writeFileSync(path.join(pkgRoot, 'todos', 'manifest.yaml'), 'name: todos\nversion: 9.9.9\nnative: todos\n');
const userSkills = path.join(extRoot, 'skills');
fs.mkdirSync(userSkills, { recursive: true });
fs.writeFileSync(path.join(userSkills, 'manifest.yaml'), 'name: skills\nversion: 9.9.10\n');
fs.writeFileSync(path.join(userSkills, 'index.html'), '<html></html>');
const userTodos = path.join(extRoot, 'todos');
fs.mkdirSync(userTodos, { recursive: true });
fs.writeFileSync(path.join(userTodos, 'manifest.yaml'), 'name: todos\nversion: 0.1.0\n');
fs.writeFileSync(path.join(userTodos, 'index.html'), '<html></html>');
const list2 = registry.listExtensions({ packagesDir: pkgRoot, extRoot, storeExts: [] });
const skillsRows = list2.filter(e => e.name === 'skills');
ok('newer user copy shadows builtin (single row)', skillsRows.length === 1);
ok('shadowed row is the user copy with flag', skillsRows[0].source === 'user'
  && skillsRows[0].version === '9.9.10' && skillsRows[0].shadowing === true);
const todosRows = list2.filter(e => e.name === 'todos');
ok('older user copy LOSES to builtin (single row)', todosRows.length === 1);
ok('winning row is the builtin, user version annotated', todosRows[0].source === 'builtin'
  && todosRows[0].version === '9.9.9' && todosRows[0].shadowed_user === '0.1.0');
ok('compareVersions ordering', registry.compareVersions('0.10.0', '0.9.9') > 0
  && registry.compareVersions('1.0', '1.0.0') === 0 && registry.compareVersions('', '0.1') < 0);

// built-ins carry no privileges: store flags apply to them the same —
// enabled:false disables a built-in, removed:true hides its row (the
// bundle files stay; a same-name install restores it).
const emptyRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-empty-'));
const list3 = registry.listExtensions({
  packagesDir: path.join(root, 'extensions', 'packages'), extRoot: emptyRoot,
  storeExts: [{ name: 'skills', enabled: false }, { name: 'todos', removed: true }],
});
ok('builtin disable honored', (list3.find(e => e.name === 'skills') || {}).enabled === false);
ok('removed builtin hidden', !list3.some(e => e.name === 'todos'));

// …but removed:true on the built-in never removes the USER's own copy:
// when both exist and the (winning) built-in is removed, the user copy
// serves as a plain user row.
const list4 = registry.listExtensions({
  packagesDir: pkgRoot, extRoot,
  storeExts: [{ name: 'todos', removed: true }],
});
const t4 = list4.find(e => e.name === 'todos');
ok('removed builtin with a user copy → user copy serves',
  t4 && t4.source === 'user' && t4.version === '0.1.0');

console.log(`\n${pass} passed, ${fail} failed`);
process.exitCode = fail ? 1 : 0;
