/* extensions registry + hub endpoints tests. Uses the real
 * examples/hello-ext as the fixture (it is the canonical sample). */

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

const helloDir = path.join(root, 'extensions', 'examples', 'hello-ext');

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
const info = registry.inspectExtension(helloDir);
ok('hello-ext inspects clean', info.ok === true, JSON.stringify(info));
ok('hello-ext manifest', info.manifest && info.manifest.name === 'hello-ext'
  && info.manifest.capabilities.includes('exec'));
ok('hello-ext entries resolved', info.entries && info.entries.view === 'index.html' && info.entries.tool === 'main.py');

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
const inst = registry.installExtension(helloDir, extRoot);
ok('install ok', inst.ok === true && inst.name === 'hello-ext');
ok('install copied files', fs.existsSync(path.join(extRoot, 'hello-ext', 'main.py'))
  && fs.existsSync(path.join(extRoot, 'hello-ext', 'index.html'))
  && fs.existsSync(path.join(extRoot, 'hello-ext', 'manifest.yaml')));

// list merges builtin + user with enabled flags
const list = registry.listExtensions({
  packagesDir: path.join(root, 'extensions', 'packages'),
  extRoot,
  storeExts: [{ name: 'hello-ext', enabled: false }],
});
ok('list includes builtin skills/todos + user hello-ext',
  list.some(e => e.name === 'skills' && e.source === 'builtin' && e.native === 'skills')
    && list.some(e => e.name === 'todos' && e.source === 'builtin')
    && list.some(e => e.name === 'hello-ext' && e.source === 'user'));
ok('store disabled flag honored', list.find(e => e.name === 'hello-ext').enabled === false);

console.log(`\n${pass} passed, ${fail} failed`);
process.exitCode = fail ? 1 : 0;
