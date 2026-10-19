#!/usr/bin/env node
/** Pack the assistant extension as a standalone release tar.gz.
 *
 * The assistant is NOT bundled in the app since cam-desktop 0.2.37 (MAS
 * compliance: MAS builds also refuse /api/extensions/install). Non-MAS
 * users install this package once via Extensions → Install package…; the
 * version-gated shadow rule then loads it (with no built-in package the
 * user copy always wins). Releasing an update = bump the version in
 * extensions/packages/assistant/manifest.yaml, rebuild dist/cam-assist.js
 * (`npm run build` here), then re-run this script.
 *
 * Layout matches the registry's expectations (extensions/host/registry.cjs):
 * a single top-level folder (assistant/) carrying manifest.yaml +
 * index.html + main.py + cam-assist.js, packed with `tar --format=ustar`
 * (the registry's minimal untar skips GNU longname entries).
 *
 * Usage: node pack-ext.mjs
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgDir = path.resolve(here, '..', '..', 'packages', 'assistant');
const bundle = path.join(here, 'dist', 'cam-assist.js');

// registry.cjs caps — a package exceeding them installs nowhere.
const MAX_FILE_BYTES = 4 * 1024 * 1024;
const MAX_TOTAL_BYTES = 8 * 1024 * 1024;

const manifest = fs.readFileSync(path.join(pkgDir, 'manifest.yaml'), 'utf8');
const m = /^version:\s*(\S+)\s*$/m.exec(manifest);
if (!m) { console.error('pack-ext: no version in manifest.yaml'); process.exit(1); }
const version = m[1];

if (!fs.existsSync(bundle)) {
  console.error('pack-ext: dist/cam-assist.js missing — run `npm run build` first');
  process.exit(1);
}

const stageParent = fs.mkdtempSync(path.join(os.tmpdir(), 'cam-assist-pkg-'));
const stage = path.join(stageParent, 'assistant');
fs.mkdirSync(stage, { recursive: true });
let total = 0;
const place = (src, name) => {
  const st = fs.statSync(src);
  if (st.size > MAX_FILE_BYTES) {
    console.error(`pack-ext: ${name} exceeds the 4MB per-file cap (${st.size} bytes)`);
    process.exit(1);
  }
  total += st.size;
  fs.copyFileSync(src, path.join(stage, name));
};
for (const f of ['manifest.yaml', 'index.html', 'main.py']) place(path.join(pkgDir, f), f);
place(bundle, 'cam-assist.js');
if (total > MAX_TOTAL_BYTES) {
  console.error(`pack-ext: package exceeds the 8MB total cap (${total} bytes)`);
  process.exit(1);
}

const out = path.join(here, 'dist', `assistant-${version}.tar.gz`);
execFileSync('tar', ['--format=ustar', '-czf', out, '-C', stageParent, 'assistant'], { stdio: 'inherit' });
fs.rmSync(stageParent, { recursive: true, force: true });

const listing = execFileSync('tar', ['-tzf', out], { encoding: 'utf8' }).trim();
console.log(`packed ${out} (${total} bytes payload)`);
console.log(listing);
