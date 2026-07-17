/**
 * CAM Desktop — local-node runtime (LOCAL-DATAPATH milestone).
 *
 * Owns ALL camc execution on the hub's own machine ("local" node):
 *
 *   - POSIX (macOS/Linux): execFile on the bundled/on-PATH camc —
 *     exactly the behavior the hub used to inline (`_localCamcPath` +
 *     execFile), moved here unchanged.
 *   - Windows (win32): the bundled camc is a POSIX sh/python polyglot
 *     that cannot be execFile'd on Windows (no /bin/sh), so execution
 *     is routed into a WSL2 distro:
 *
 *         wsl.exe [-d <distro>] --exec <abs-camc-path> <args...>
 *
 *     argv is passed literally (no shell mangling) — the same shape the
 *     legacy Tauri backend proved out (src-tauri/src/main.rs:40-57).
 *     `wsl.exe --exec` does NOT expand `~`, so the in-distro `$HOME` is
 *     resolved once via `bash -c 'printf %s "$HOME"'` and cached, and
 *     the camc path is built as `<home>/.cam/camc`.
 *
 * The distro must provide python3, tmux, and an authenticated agent
 * CLI; `ensureCamc()` bootstraps `~/.cam/camc` from the bundled copy
 * (md5 short-hash compare + ready-cache, mirroring the hub's
 * `_ensureRemoteCamc`), and `checkEnvironment()` reports precisely
 * which prerequisite is missing.
 *
 * Result convention matches ssh-transport.cjs:
 *   { ok, stdout, stderr, error, detail }
 * with error in:
 *   'camc_missing' | 'timeout' | 'exec_failed' |
 *   'wsl_missing' | 'wsl_distro_missing' | 'camc_bootstrap_failed'
 *
 * Test seam: configure({ execFileImpl, platform, wslDistro, camcPath })
 * — same injection pattern as the hub's configure(). The hub passes the
 * resolved bundled camc path in via `camcPath` (resolution stays in the
 * hub's `_localCamcPath`/`_readBundledCamc`; it is NOT duplicated here)
 * and the distro from the optional store field `hubConfig.wslDistro`.
 */

'use strict';

const { execFile, spawn } = require('node:child_process');

const DEFAULT_TIMEOUT_MS = 15000;
const MAX_BUFFER = 4 * 1024 * 1024;
// In-distro install location, relative to the distro user's $HOME.
// Mirrors the REMOTE_CAMC path used for SSH nodes.
const WSL_CAMC_REL = '.cam/camc';
const WSL_CAMC_TMP_REL = '.cam/camc.tmp';

/* ─────────────── Injectable config ─────────────── */

let _execFileImpl = execFile;
let _spawnImpl = spawn;
let _platform = process.platform;
// '' = WSL default distro. Settable via the store's hubConfig.wslDistro
// (the hub syncs it in through configure()); no UI in this milestone.
let _wslDistro = '';
// Absolute path of the camc binary for the POSIX/native runtime. The
// hub injects its `_localCamcPath()` result (bundled first, then the
// bare name 'camc' for a PATH lookup). Standalone default: PATH lookup.
let _camcPath = 'camc';

// Ready-cache for the WSL bootstrap: "<distro>|<md5-short-hash>" → true.
// Once a hash is verified installed in a distro we skip the probe/upload
// cycle for the rest of the process lifetime (same idea as the hub's
// state.remoteCamcReadyCache).
const _readyCache = new Map();
// Cached in-distro $HOME (per current distro). Reset whenever the
// distro or the exec/platform injection changes.
let _wslHome = null;

/** Injection seam (same pattern as the hub's configure()). Keys are
 *  merged: only provided keys are applied. Passing `execFileImpl` or
 *  `platform` also resets the derived caches (home resolution, ready
 *  cache) so tests start from a clean slate; passing `wslDistro` resets
 *  the cached $HOME (each distro has its own filesystem). */
function configure({ execFileImpl, platform, wslDistro, camcPath, spawnImpl } = {}) {
  if (execFileImpl !== undefined) {
    _execFileImpl = execFileImpl || execFile;
    _wslHome = null;
    _readyCache.clear();
  }
  if (spawnImpl !== undefined) {
    _spawnImpl = spawnImpl || spawn;
  }
  if (platform !== undefined) {
    _platform = platform || process.platform;
    _wslHome = null;
    _readyCache.clear();
  }
  if (wslDistro !== undefined) {
    const next = String(wslDistro || '').trim();
    if (next !== _wslDistro) _wslHome = null;
    _wslDistro = next;
  }
  if (camcPath !== undefined) {
    _camcPath = String(camcPath || '').trim() || 'camc';
  }
}

function getPlatform() { return _platform; }

/** Test helper: drop all injected config + caches back to defaults. */
function _resetForTests() {
  _execFileImpl = execFile;
  _spawnImpl = spawn;
  _platform = process.platform;
  _wslDistro = '';
  _camcPath = 'camc';
  _wslHome = null;
  _readyCache.clear();
}

/* ─────────────── Path helpers ─────────────── */

/** Map a Windows path to its in-distro WSL equivalent:
 *    C:\foo\bar   → /mnt/c/foo/bar
 *    C:/foo       → /mnt/c/foo
 *  Linux-absolute paths (/home/...) pass through untouched, as do
 *  relative paths and UNC paths (\\wsl$\... / \\wsl.localhost\... —
 *  those already name a distro filesystem; mapping them is deferred,
 *  see LOCAL-NODE-DATAPATH.md). */
function winToWslPath(p) {
  const s = String(p == null ? '' : p).trim();
  if (!s) return s;
  const m = /^([A-Za-z]):[\\/](.*)$/.exec(s);
  if (m) {
    const rest = m[2].replace(/\\/g, '/');
    return `/mnt/${m[1].toLowerCase()}/${rest}`;
  }
  return s;
}

/** wsl.exe argv prefix: ['-d', distro] when a non-default distro is
 *  configured, else []. */
function _distroArgs() {
  return _wslDistro ? ['-d', _wslDistro] : [];
}

/* ─────────────── Low-level exec ─────────────── */

/** Decode a wsl.exe stdout/stderr buffer. `wsl.exe -l` and some other
 *  wsl.exe verbs print UTF-16LE on Windows (an inbox-app quirk); in-distro
 *  command output is UTF-8. Detect UTF-16 by NUL bytes in odd positions
 *  near the start of the buffer. Accepts either a Buffer or a string
 *  (strings are returned unchanged). */
function _decodeOut(buf) {
  if (buf == null) return '';
  if (typeof buf === 'string') return buf;
  let nul = 0;
  const probe = Math.min(buf.length, 64);
  for (let i = 1; i < probe; i += 2) { if (buf[i] === 0) nul++; }
  const enc = (probe > 4 && nul > probe / 4) ? 'utf16le' : 'utf8';
  return buf.toString(enc);
}

/** Promise wrapper around the (injectable) execFile. `stdio` opts:
 *  { timeoutMs, stdin (Buffer|string), bufferOut (decode manually,
 *  for wsl.exe's UTF-16 verbs) }. Never rejects: errors are folded
 *  into the result as { ok:false, err, stdout, stderr }. */
function _exec(bin, args, { timeoutMs, stdin, bufferOut = false } = {}) {
  return new Promise((resolve) => {
    const opts = {
      timeout: timeoutMs || DEFAULT_TIMEOUT_MS,
      maxBuffer: MAX_BUFFER,
      windowsHide: true,
    };
    if (bufferOut) opts.encoding = 'buffer';
    let child;
    try {
      child = _execFileImpl(bin, args, opts, (err, stdout, stderr) => {
        resolve({
          ok: !err,
          err: err || null,
          stdout: bufferOut ? _decodeOut(stdout) : String(stdout || ''),
          stderr: bufferOut ? _decodeOut(stderr) : String(stderr || ''),
        });
      });
    } catch (e) {
      // Synchronous throw (bad args, bogus injection) — fold into the
      // same result shape as async spawn failures.
      resolve({ ok: false, err: e, stdout: '', stderr: '' });
      return;
    }
    if (stdin != null && child && child.stdin) {
      try {
        child.stdin.on('error', () => {}); // EPIPE when the child exits early
        child.stdin.write(stdin);
        child.stdin.end();
      } catch (_) {}
    }
  });
}

/** Map a spawn error to the shared error dictionary. Same mapping the
 *  hub's local exec sites used inline: ENOENT → camc_missing (POSIX) /
 *  wsl_missing (win32, wsl.exe itself missing), kill-by-timeout →
 *  timeout, anything else → exec_failed. On win32 a nonzero WSL exit is
 *  inspected: WSL_E_* / "no distribution" stderr → wsl_distro_missing;
 *  "No such file or directory" right after exec'ing camc → camc_missing
 *  (camc not bootstrapped in the distro yet). */
function _mapExecError(res, { win32Target }) {
  const err = res.err;
  const stderr = res.stderr || '';
  let code;
  if (err.code === 'ENOENT') {
    code = win32Target ? 'wsl_missing' : 'camc_missing';
  } else if (err.killed && /TIMEDOUT/i.test(String(err.message || ''))) {
    code = 'timeout';
  } else if (win32Target && /WSL_E_|no distribution|distro.*not.*found|not installed/i.test(stderr)) {
    code = 'wsl_distro_missing';
  } else if (win32Target && /no such file or directory/i.test(stderr)) {
    code = 'camc_missing';
  } else {
    code = 'exec_failed';
  }
  const detail = (stderr || err.message || 'local camc exec failed').trim().slice(0, 400);
  return { ok: false, error: code, detail, stdout: res.stdout || '', stderr };
}

/* ─────────────── WSL $HOME resolution ─────────────── */

/** Resolve the in-distro $HOME (cached). `wsl.exe --exec` does no shell
 *  expansion, so `~/.cam/camc` cannot be used as the exec target; we ask
 *  bash once and build the absolute path. bash is a hard requirement —
 *  the bootstrap path uses it too. */
async function _resolveWslHome() {
  if (_wslHome) return _wslHome;
  const res = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c', 'printf %s "$HOME"'], { timeoutMs: 8000 });
  if (!res.ok) return null;
  const home = String(res.stdout || '').trim();
  if (!home || !home.startsWith('/')) return null;
  _wslHome = home;
  return home;
}

/** Absolute in-distro camc path, or null when $HOME could not be
 *  resolved (callers then run a best-effort bash -c fallback that will
 *  surface the underlying error). */
async function _wslCamcPath() {
  const home = await _resolveWslHome();
  return home ? `${home}/${WSL_CAMC_REL}` : null;
}

/* ─────────────── Public surface ─────────────── */

/** The single entry point for running camc on the local node.
 *  POSIX: execFile(camcPath, args). win32: wsl.exe [-d distro] --exec
 *  <home>/.cam/camc <args>. `stdin` (Buffer|string) pipes into the
 *  child (needed for `camc send --stdin`). Returns
 *  { ok, stdout, stderr } or { ok:false, error, detail, stdout, stderr }. */
async function execCamc(args, { timeoutMs, stdin } = {}) {
  const argv = (args || []).map(a => String(a));
  if (_platform !== 'win32') {
    const res = await _exec(_camcPath, argv, { timeoutMs, stdin });
    if (!res.ok) return _mapExecError(res, { win32Target: false });
    return { ok: true, stdout: res.stdout, stderr: res.stderr };
  }
  // win32 → WSL2. Resolve the absolute in-distro camc path first; when
  // $HOME cannot be resolved (WSL/distro missing) fall back to a bash
  // -c invocation so the user still gets the real stderr back.
  const camcAbs = await _wslCamcPath();
  if (!camcAbs) {
    // Distinguish "no wsl.exe" from "no distro/bash" for the caller.
    const probe = await _exec('wsl.exe', ['--status'], { timeoutMs: 8000, bufferOut: true });
    if (probe.err && probe.err.code === 'ENOENT') {
      return { ok: false, error: 'wsl_missing', detail: 'wsl.exe was not found. Install WSL2 (wsl --install) with a Linux distro to run local agents on Windows.', stdout: '', stderr: '' };
    }
    return { ok: false, error: 'wsl_distro_missing', detail: 'No usable WSL distro (or bash) found. Install a distro (wsl --install -d Ubuntu) or set hubConfig.wslDistro to an existing one.', stdout: '', stderr: '' };
  }
  const res = await _exec('wsl.exe', [..._distroArgs(), '--exec', camcAbs, ...argv], { timeoutMs, stdin });
  if (!res.ok) return _mapExecError(res, { win32Target: true });
  return { ok: true, stdout: res.stdout, stderr: res.stderr };
}

/** Structured preflight for the local runtime. `tool` is the agent CLI
 *  to check readiness for (claude/codex/cursor/...). Returns:
 *    { ok, platform, runtime: 'wsl'|'native', distro,
 *      checks: { python3, tmux, tool, tool_auth }, issues: [{level,message}] }
 *  Never throws; a missing prerequisite is data, not an exception. */
async function checkEnvironment(tool) {
  const t = String(tool || 'claude');
  if (_platform !== 'win32') return _checkNativeEnv(t);
  return _checkWslEnv(t);
}

/** Parse `camc env check --tool <t> --json` output into the shared
 *  checks/issues shape. camc's readiness report carries
 *  { issues:[{level,message}], resolved:{tmux,tool}, warnings } — auth
 *  problems surface as error-level issues mentioning
 *  auth/credential/token/login, which is the best signal available
 *  without tool-specific knowledge. */
function _envCheckFromCamcJson(stdout, checks, issues) {
  let parsed = null;
  try { parsed = JSON.parse(String(stdout || '')); } catch (_) { parsed = null; }
  if (!parsed) {
    issues.push({ level: 'warn', message: 'camc env check did not return JSON' });
    return;
  }
  const resolved = parsed.resolved || {};
  checks.tmux = !!resolved.tmux;
  checks.tool = !!resolved.tool;
  const list = Array.isArray(parsed.issues) ? parsed.issues : [];
  for (const it of list) {
    const level = (it && it.level) === 'error' ? 'error' : 'warn';
    issues.push({ level, message: String((it && it.message) || it) });
  }
  checks.tool_auth = !list.some(it =>
    it && it.level === 'error' && /auth|credential|token|login/i.test(String(it.message || '')));
}

async function _checkNativeEnv(tool) {
  const checks = { python3: false, tmux: false, tool: false, tool_auth: false };
  const issues = [];
  const res = await execCamc(['env', 'check', '--tool', tool, '--json'], { timeoutMs: DEFAULT_TIMEOUT_MS });
  if (!res.ok) {
    issues.push({ level: 'error', message: `local camc could not run (${res.error}): ${res.detail || ''}`.trim() });
    return { ok: false, platform: _platform, runtime: 'native', distro: '', checks, issues };
  }
  // camc itself ran → the POSIX runtime has python3 (camc is python).
  checks.python3 = true;
  _envCheckFromCamcJson(res.stdout, checks, issues);
  const ok = checks.python3 && checks.tmux && checks.tool && checks.tool_auth;
  return { ok, platform: _platform, runtime: 'native', distro: '', checks, issues };
}

async function _checkWslEnv(tool) {
  const checks = { python3: false, tmux: false, tool: false, tool_auth: false };
  const issues = [];
  const fail = (error, message) => ({
    ok: false, platform: 'win32', runtime: 'wsl', distro: _wslDistro || '', checks,
    issues: [...issues, { level: 'error', message }], error,
  });

  // 1) WSL present at all? (ENOENT = no wsl.exe / WSL not installed.)
  const status = await _exec('wsl.exe', ['--status'], { timeoutMs: 8000, bufferOut: true });
  if (status.err && status.err.code === 'ENOENT') {
    return fail('wsl_missing', 'wsl.exe not found — install WSL2 (wsl --install) to run local agents on Windows');
  }

  // 2) Distros. wsl.exe prints this list as UTF-16LE on Windows;
  // _exec(bufferOut) decodes it. Each line is a distro name.
  const list = await _exec('wsl.exe', ['-l', '-q'], { timeoutMs: 8000, bufferOut: true });
  const distros = String(list.stdout || '')
    .split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  if (_wslDistro && !distros.some(d => d.toLowerCase() === _wslDistro.toLowerCase())) {
    return fail('wsl_distro_missing', `Configured WSL distro "${_wslDistro}" not found. Installed: ${distros.join(', ') || '(none)'}`);
  }
  if (!distros.length) {
    return fail('wsl_distro_missing', 'No WSL distro installed — install one with: wsl --install -d Ubuntu');
  }

  // 3) Fast in-distro probes (work even before camc is bootstrapped, so
  //    the report can name the missing piece precisely).
  const py = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'python3', '--version'], { timeoutMs: 8000 });
  checks.python3 = !!py.ok;
  if (!checks.python3) issues.push({ level: 'error', message: 'python3 missing in the WSL distro (e.g. sudo apt install python3)' });
  const tm = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'tmux', '-V'], { timeoutMs: 8000 });
  checks.tmux = !!tm.ok;
  if (!checks.tmux) issues.push({ level: 'error', message: 'tmux missing in the WSL distro (e.g. sudo apt install tmux)' });

  // 4) camc bootstrapped? Then the tool + auth readiness comes from
  //    `camc env check` inside the distro.
  const camcAbs = await _wslCamcPath();
  const probe = camcAbs
    ? await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c', `test -x ~/${WSL_CAMC_REL}`], { timeoutMs: 8000 })
    : { ok: false };
  if (camcAbs && probe.ok) {
    const env = await execCamc(['env', 'check', '--tool', tool, '--json'], { timeoutMs: DEFAULT_TIMEOUT_MS });
    if (env.ok) {
      // tmux keeps the direct-probe result (authoritative, no PATH
      // capture ambiguity); tool + auth come from camc's readiness.
      const camcChecks = { tmux: checks.tmux, tool: false, tool_auth: false };
      _envCheckFromCamcJson(env.stdout, camcChecks, issues);
      checks.tool = camcChecks.tool;
      checks.tool_auth = camcChecks.tool_auth;
    } else {
      issues.push({ level: 'warn', message: `camc env check failed in distro (${env.error})` });
    }
  } else {
    issues.push({ level: 'warn', message: 'camc not bootstrapped in the distro yet — it is installed automatically on the first local agent start' });
  }

  const ok = checks.python3 && checks.tmux &&
    !issues.some(i => i.level === 'error');
  return { ok, platform: 'win32', runtime: 'wsl', distro: _wslDistro || '', checks, issues };
}

/** Mirror of the hub's `_ensureRemoteCamc` for the local WSL runtime:
 *  md5 short-hash compare → upload bundled camc → chmod/mv → verify.
 *  Ready-cache keyed on distro+hash skips the whole cycle once a hash
 *  is verified. `bundled` is the hub's `_readBundledCamc()` result
 *  ({ content, path, hash }) — resolution is NOT duplicated here.
 *  POSIX: no-op (the bundled path executes directly). */
async function ensureCamc(bundled, { force = false } = {}) {
  if (_platform !== 'win32') {
    return { ok: true, skipped: 'native', runtime: 'native' };
  }
  if (!bundled || bundled.error) {
    return { ok: false, error: (bundled && bundled.error) || 'bundled_camc_missing', detail: (bundled && bundled.detail) || 'bundled camc unavailable' };
  }
  const hash = String(bundled.hash || '');
  const readyKey = `${_wslDistro}|${hash}`;
  if (!force && _readyCache.has(readyKey)) {
    return { ok: true, present: true, cached: true, hash, runtime: 'wsl', distro: _wslDistro || '' };
  }

  // Fast path: already installed with the same hash.
  const probe = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c',
    `test -x ~/${WSL_CAMC_REL} && md5sum ~/${WSL_CAMC_REL} 2>/dev/null | cut -c1-12`], { timeoutMs: 8000 });
  if (probe.err && probe.err.code === 'ENOENT') {
    return { ok: false, error: 'wsl_missing', detail: 'wsl.exe was not found. Install WSL2 (wsl --install) with a Linux distro to run local agents on Windows.' };
  }
  const remoteHash = probe.ok ? String(probe.stdout || '').trim() : '';
  if (remoteHash && remoteHash === hash) {
    _readyCache.set(readyKey, true);
    return { ok: true, present: true, hash, runtime: 'wsl', distro: _wslDistro || '' };
  }
  if (!probe.ok && /WSL_E_|no distribution/i.test(probe.stderr || '')) {
    return { ok: false, error: 'wsl_distro_missing', detail: String(probe.stderr || '').trim().slice(0, 300) };
  }

  const boot = (msg) => ({ ok: false, error: 'camc_bootstrap_failed', detail: msg });

  const mkdir = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c', 'mkdir -p ~/.cam'], { timeoutMs: 8000 });
  if (!mkdir.ok) return boot(`mkdir -p ~/.cam failed in distro: ${(mkdir.stderr || (mkdir.err && mkdir.err.message) || '').trim().slice(0, 300)}`);

  // Upload via stdin (no temp file on the Windows side, no quoting
  // pitfalls with the polyglot content).
  const upload = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c', `cat > ~/${WSL_CAMC_TMP_REL}`], { timeoutMs: 30000, stdin: bundled.content });
  if (!upload.ok) return boot(`failed to write ~/${WSL_CAMC_TMP_REL} in distro: ${(upload.stderr || (upload.err && upload.err.message) || '').trim().slice(0, 300)}`);

  const install = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c',
    `chmod 700 ~/${WSL_CAMC_TMP_REL} && mv ~/${WSL_CAMC_TMP_REL} ~/${WSL_CAMC_REL}`], { timeoutMs: 8000 });
  if (!install.ok) return boot(`failed to install ~/${WSL_CAMC_REL} in distro: ${(install.stderr || (install.err && install.err.message) || '').trim().slice(0, 300)}`);

  const verify = await _exec('wsl.exe', [..._distroArgs(), '--exec', 'bash', '-c', `~/${WSL_CAMC_REL} version`], { timeoutMs: 15000 });
  if (!verify.ok) {
    return boot(`~/${WSL_CAMC_REL} failed its post-install version check (python3 present in the distro?): ${(verify.stderr || (verify.err && verify.err.message) || '').trim().slice(0, 300)}`);
  }

  _readyCache.set(readyKey, true);
  return { ok: true, installed: !remoteHash, updated: !!remoteHash, hash, runtime: 'wsl', distro: _wslDistro || '' };
}

/* ─────────────── Terminal attach (script-PTY) ─────────────── */

/** Shell-quote one token for the sh that script(1) spawns. */
function _shq(s) { return `'${String(s).replace(/'/g, `'\\''`)}'`; }

/** Build the spawn spec for a local terminal attach, or null when the
 *  runtime is unavailable (win32: no usable WSL distro — callers then
 *  probe for the precise error, same as execCamc). The command runs
 *  `camc attach <id>` inside a freshly allocated PTY; `stty` sizes the
 *  PTY before exec so tmux sees the renderer's geometry.
 *
 *  Platform shapes:
 *    linux:  script -qec '<sh>' /dev/null          (util-linux: -c sh-string)
 *    darwin: script -q /dev/null sh -c '<sh>'      (BSD: command as argv)
 *    win32:  wsl.exe [-d distro] --exec script -qec '<sh>' /dev/null */
async function _attachSpawnSpec(agentId, cols, rows) {
  const inner = (camcBin) =>
    `stty cols ${cols} rows ${rows}; exec ${_shq(camcBin)} attach ${_shq(agentId)}`;
  if (_platform === 'darwin') {
    return { bin: 'script', args: ['-q', '/dev/null', 'sh', '-c', inner(_camcPath)] };
  }
  if (_platform !== 'win32') {
    return { bin: 'script', args: ['-qec', inner(_camcPath), '/dev/null'] };
  }
  const camcAbs = await _wslCamcPath();
  if (!camcAbs) return null;
  return { bin: 'wsl.exe', args: [..._distroArgs(), '--exec', 'script', '-qec', inner(camcAbs), '/dev/null'] };
}

/** Open a local terminal channel attached to <agentId>'s tmux session.
 *  Node has no built-in PTY and we deliberately avoid a native node-pty
 *  dependency, so the PTY comes from script(1) (present on macOS and in
 *  every mainstream Linux distro / WSL image). The returned channel
 *  mirrors ssh-transport's openTerminalChannel surface:
 *      { ok, write(data), resize(cols, rows), dispose() }
 *  onData delivers raw Buffer chunks (same as the SSH channel).
 *  resize() is a documented no-op: script(1) gives us no ioctl channel
 *  to the PTY — the renderer re-opens the terminal to change size. */
async function openAttachChannel(agentId, { cols = 80, rows = 24, onData, onClose } = {}) {
  // cols/rows are interpolated into the stty command — force integers.
  const c = Math.max(20, Math.min(500, parseInt(cols, 10) || 80));
  const r = Math.max(5, Math.min(200, parseInt(rows, 10) || 24));
  const spec = await _attachSpawnSpec(agentId, c, r);
  if (!spec) {
    const probe = await _exec('wsl.exe', ['--status'], { timeoutMs: 8000, bufferOut: true });
    if (probe.err && probe.err.code === 'ENOENT') {
      return { ok: false, error: 'wsl_missing', detail: 'wsl.exe was not found. Install WSL2 to attach to local agents on Windows.' };
    }
    return { ok: false, error: 'wsl_distro_missing', detail: 'No usable WSL distro (or bash) found to attach a local agent terminal.' };
  }
  let child;
  try {
    child = _spawnImpl(spec.bin, spec.args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (e) {
    return { ok: false, error: 'spawn_failed', detail: String((e && e.message) || e) };
  }
  child.on('error', (e) => {
    if (typeof onClose === 'function') onClose({ code: -1, signal: null, error: String((e && e.message) || e) });
  });
  if (child.stdout && typeof onData === 'function') {
    child.stdout.on('data', (buf) => onData(buf));
  }
  if (child.stderr) child.stderr.resume(); // discard; tmux writes to the PTY (stdout)
  child.on('close', (code, signal) => {
    if (typeof onClose === 'function') onClose({ code, signal });
  });
  return {
    ok: true,
    write(data) {
      try { child.stdin && child.stdin.write(data); } catch (_) {}
    },
    resize() { /* no-op: no ioctl channel through script(1); re-open to resize */ },
    dispose() {
      try { child.kill('SIGTERM'); } catch (_) {}
    },
  };
}

module.exports = {
  configure,
  getPlatform,
  execCamc,
  checkEnvironment,
  ensureCamc,
  winToWslPath,
  openAttachChannel,
  _resetForTests,
};
