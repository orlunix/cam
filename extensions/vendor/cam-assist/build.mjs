// Build cam-assist.js — bundle entry.js + pi SDK into one self-inflating
// CJS file (gzip+base64 wrapper, see the emit step at the bottom).
//
// Two bundling quirks of pi-coding-agent (ESM-only, expects to run from its
// own installed package) handled here:
//  1. import.meta.url — undefined in CJS output; pinned to a harmless constant
//     via --define (see `define` below). config.js derives __dirname from it;
//     getPackageDir() then fails its walk-up, which is fine because of (2).
//  2. config.js:332 does a TOP-LEVEL `JSON.parse(readFileSync(getPackageJsonPath()))`
//     to read its own package.json (APP_NAME/VERSION/CONFIG_DIR_NAME constants).
//     In the bundled layout that file does not exist, so the plugin below wraps
//     it in try/catch with a fallback. Everything we use passes explicit paths
//     (agentDir, session dir), so the fallback constants never reach our code.
import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const PATCH_TARGET = 'const pkg = JSON.parse(readFileSync(getPackageJsonPath(), "utf-8"));';
const PATCH_REPLACEMENT =
  'const pkg = (() => { try { return JSON.parse(readFileSync(getPackageJsonPath(), "utf-8")); } ' +
  'catch { return { name: "cam-assist", version: "0.0.0" }; } })();';

const patchPiConfig = {
  name: 'patch-pi-config',
  setup(build) {
    build.onLoad({ filter: /pi-coding-agent[\\/]dist[\\/]config\.js$/ }, (args) => {
      let source = fs.readFileSync(args.path, 'utf8');
      if (!source.includes(PATCH_TARGET)) {
        throw new Error(
          'patch-pi-config: expected line not found in pi-coding-agent dist/config.js — ' +
          'upstream changed; re-check the patch against the new source.',
        );
      }
      source = source.replace(PATCH_TARGET, PATCH_REPLACEMENT);
      return { contents: source, loader: 'js' };
    });
  },
};

// The pi-coding-agent SDK assumes every assistant message carries a `usage`
// object, but OpenAI-compatible endpoints (e.g. NVIDIA inference API) may omit
// it. Without this guard, `_checkCompaction` crashes with
// "Cannot read properties of undefined (reading 'totalTokens')".
const USAGE_OLD = 'export function calculateContextTokens(usage) {\n    return usage.totalTokens || usage.input + usage.output + usage.cacheRead + usage.cacheWrite;\n}';
const USAGE_NEW = 'export function calculateContextTokens(usage) {\n    if (!usage) return 0;\n    return usage.totalTokens || usage.input + usage.output + usage.cacheRead + usage.cacheWrite;\n}';
const patchPiCompaction = {
  name: 'patch-pi-compaction',
  setup(build) {
    build.onLoad({ filter: /pi-coding-agent[\\/]dist[\\/]core[\\/]compaction[\\/]compaction\.js$/ }, (args) => {
      let source = fs.readFileSync(args.path, 'utf8');
      if (source.includes(USAGE_OLD)) {
        source = source.replace(USAGE_OLD, USAGE_NEW);
      } else if (!source.includes(USAGE_NEW)) {
        throw new Error(
          'patch-pi-compaction: expected calculateContextTokens shape not found — ' +
          'upstream changed; re-check the patch.',
        );
      }
      return { contents: source, loader: 'js' };
    });
  },
};

const entry = process.argv[2] || 'entry.js';
const outfile = process.argv[3] || path.join('dist', 'cam-assist.js');

const result = await esbuild.build({
  entryPoints: [entry],
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node18',
  write: false,
  logLevel: 'warning',
  minify: true,
  plugins: [patchPiConfig, patchPiCompaction],
  define: { 'import.meta.url': '"file:///C:/fake/cam-assist.js"' },
});

for (const warn of result.warnings) console.warn('esbuild:', warn.text);
const raw = result.outputFiles[0].contents;

// Emit a self-inflating single file: the minified bundle is ~6.8MB, but the
// extension registry caps user packages at 4MB per file
// (extensions/host/registry.cjs MAX_FILE_BYTES) — and the whole point of the
// assistant package is shipping view AND agent logic as one shadowing
// tar.gz. gzip+base64 lands at ~2.3MB; the wrapper below inflates and
// evaluates it with the same free variables Node's own module wrapper
// provides, so runtime semantics are unchanged.
const payload = zlib.gzipSync(raw, { level: 9 }).toString('base64');
const wrapper =
  '/* cam-assist.js — self-inflating bundle (built by build.mjs; do not edit).\n' +
  ' * Real CJS bundle is gzip+base64-embedded to stay under the 4MB per-file\n' +
  ' * extension-package cap so user-package shadowing keeps working. */\n' +
  "'use strict';\n" +
  "const zlib = require('node:zlib');\n" +
  "const src = zlib.gunzipSync(Buffer.from(" + JSON.stringify(payload) + ", 'base64')).toString('utf8');\n" +
  "const run = new Function('exports', 'require', 'module', '__filename', '__dirname',\n" +
  "  src + '\\n//# sourceURL=cam-assist-bundle.js');\n" +
  'run.call(exports, exports, require, module, __filename, __dirname);\n';

fs.mkdirSync(path.dirname(outfile), { recursive: true });
fs.writeFileSync(outfile, wrapper);

// Optional debug escape hatch: CAM_ASSIST_RAW_OUT=<path> also writes the
// unwrapped bundle (for diffing raw-vs-wrapped behavior).
if (process.env.CAM_ASSIST_RAW_OUT) {
  fs.writeFileSync(process.env.CAM_ASSIST_RAW_OUT, raw);
}

const kb = Math.round(fs.statSync(outfile).size / 1024);
console.log(`built ${outfile} (${kb} KB wrapped, ${Math.round(raw.length / 1024)} KB raw) from ${entry}`);
