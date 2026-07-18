# Desktop NodeTransport with Windows WSL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every CAM Desktop node operation through one NodeTransport factory, with SSH on configured endpoints, the default WSL Linux environment for Windows local, and native processes for Linux/macOS local.

**Architecture:** Persist only `machine.type` (`local` or `ssh`). The Electron-main factory is the only execution boundary that maps topology plus `process.platform` to `ssh2`, default WSL, or native-process adapters. All adapters share command, file, and PTY methods; Desktop Start asks CAMC to create a repairable tmux session without treating tool authentication or version probes as blockers.

**Tech Stack:** Electron 31, Node.js CommonJS, `ssh2` pooled connections, `node-pty` 1.1.0/Windows ConPTY, `wsl.exe --exec`, Python 3 inside WSL, tmux, vanilla browser ES modules, pytest, and deterministic Node assertion tests.

## Global Constraints

- Persist `machine.type` only. Exact supported values are `local` and `ssh`; do not add `local: true`, `transport_driver`, or a WSL distro field.
- `machine.type: "ssh"` always uses `ssh2`, including `127.0.0.1`, `localhost`, and `::1`.
- `machine.type: "local"` uses the default WSL environment on Windows and a native process on Linux/macOS.
- Invoke `wsl.exe --exec` without `-d`; the app does not select, install, or manage a distro. WSL2 is expected.
- WSL is Windows-only: require `wsl-node-transport.cjs` lazily only after the factory selects `wsl` on `win32`. Linux/macOS must never probe or invoke `wsl.exe`.
- Linux/macOS local is a thin adapter over the existing native `execFile`/filesystem behavior. Do not add WSL compatibility code or platform setup UI there.
- Keep this migration thin: do not redesign Nodes grouping, host metadata, Relay, Mobile, or platform installation flows unless a focused regression test requires a compatibility fix.
- The automatic `local` context/node remains. Do not remove it, convert it to loopback SSH, or require SSH credentials for it.
- The factory in `electron/node-transport.cjs` is the only execution switch on topology/native OS. Hub routes and Electron terminal code call the contract and do not choose adapters.
- Preserve the `ssh2` 600-second connection pool and existing HTTP/API response shapes.
- Local WSL is a preconfigured Linux environment with Python, tmux, agent tools, and user-owned authentication. Desktop reports missing runtime/executable failures but does not install dependencies or authenticate tools.
- Authentication and licensing are not Start blockers. Desktop passes CAMC's hidden interactive-setup flag; missing binaries still block, while nonzero tool version probes and missing auth files become warnings for that launch only.
- User prompt/input travels through stdin or argv, never interpolated into a shell program. The only shell text allowed is a fixed adapter-owned wrapper plus the existing trusted `env_setup` field.
- Windows-local Terminal uses `node-pty`/ConPTY and must support input, resize, close, and warm-session reuse through the same renderer events as SSH.
- Preserve unrelated dirty-worktree changes. Do not commit or push without explicit user authorization.

---

## File Map

### New files

- `apps/cam-desktop/electron/node-transport.cjs` — contract, legacy topology normalization, OS mapping, and the only adapter factory.
- `apps/cam-desktop/electron/ssh-node-transport.cjs` — contract wrapper over the existing pooled `ssh-transport.cjs`.
- `apps/cam-desktop/electron/wsl-node-transport.cjs` — default-WSL command and Python-backed file operations.
- `apps/cam-desktop/electron/local-process-node-transport.cjs` — Linux/macOS local command and file operations.
- `apps/cam-desktop/electron/pty-terminal.cjs` — injected `node-pty` lifecycle wrapper used by WSL/native local adapters.
- `apps/cam-desktop/test/node-transport.test.cjs` — factory and adapter contract tests.
- `apps/cam-desktop/test/node-transport-architecture.test.cjs` — source boundary guard.

### Existing files to modify

- `src/camc_pkg/runtime_env.py` — advisory readiness mode for interactive setup.
- `src/camc_pkg/cli.py` — hidden `run --allow-interactive-setup` flag.
- `tests/test_runtime_env.py` — strict-default and advisory-mode readiness tests.
- `src/camc` — regenerated tracked standalone CAMC packaged in Desktop.
- `apps/cam-desktop/electron/embedded-hub.cjs` — resolve transport once and migrate commands, sync, Start, models, Skills, files, uploads, and terminal.
- `apps/cam-desktop/electron/main.cjs` — retain session cache/event routing but delegate terminal opening to the Hub.
- `apps/cam-desktop/electron/preload.cjs` — update SSH-only terminal comments; bridge shape stays unchanged.
- `apps/cam-desktop/package.json` and `package-lock.json` — `node-pty@1.1.0`, unpacking, syntax/test scripts.
- `apps/cam-desktop/test/hub.test.cjs` — fake factory routing and API parity tests.
- `apps/cam-desktop/test/terminal-follow.test.cjs` — main-process delegation assertions plus existing renderer behavior.
- `web/js/desktop/start-agent-mode.js` — retain local choice and label Windows local as default WSL; no auth promise.
- `docs/desktop/requirements.md` and `apps/cam-desktop/FIXES-NODES.md` — architecture and migration evidence.

---

### Task 1: Define the single factory and contract

**Files:**
- Create: `apps/cam-desktop/electron/node-transport.cjs`
- Create: `apps/cam-desktop/test/node-transport.test.cjs`
- Modify: `apps/cam-desktop/package.json`

**Interfaces:**
- Produces: `NODE_TRANSPORT_METHODS` with `runTool`, `ensureDirectory`, `listFiles`, `readFile`, `writeFile`, `openTerminal`, `dispose`.
- Produces: `normalizeMachineType(machine) -> { ok, type, machine } | { ok:false, error, detail }`.
- Produces: `resolveAdapterKind(machine, platform) -> { ok, kind } | normalized error`.
- Produces: `createNodeTransport(context, { platform, factories }) -> { ok, kind, transport } | normalized error`.

- [ ] **Step 1: Write the failing factory tests**

Create `apps/cam-desktop/test/node-transport.test.cjs` with:

```js
'use strict';
const assert = require('node:assert/strict');
const {
  NODE_TRANSPORT_METHODS,
  normalizeMachineType,
  resolveAdapterKind,
  createNodeTransport,
} = require('../electron/node-transport.cjs');

const methods = ['runTool', 'ensureDirectory', 'listFiles', 'readFile', 'writeFile', 'openTerminal', 'dispose'];
assert.deepEqual(NODE_TRANSPORT_METHODS, methods);
assert.equal(resolveAdapterKind({ type: 'local' }, 'win32').kind, 'wsl');
assert.equal(resolveAdapterKind({ type: 'local' }, 'linux').kind, 'local-process');
assert.equal(resolveAdapterKind({ type: 'local' }, 'darwin').kind, 'local-process');
assert.equal(resolveAdapterKind({ type: 'ssh', host: '127.0.0.1' }, 'win32').kind, 'ssh2');
assert.equal(resolveAdapterKind({ type: 'ssh', host: 'localhost' }, 'linux').kind, 'ssh2');
assert.equal(normalizeMachineType({ host: 'pdx098', user: 'hren' }).type, 'ssh');
assert.equal(normalizeMachineType({ host: '', user: '' }).type, 'local');
assert.equal(resolveAdapterKind({ type: 'serial' }, 'win32').error, 'transport_unsupported');

const calls = [];
const fake = Object.fromEntries(methods.map(name => [name, async () => ({ ok: true })]));
const made = createNodeTransport(
  { id: 'local', machine: { type: 'local' } },
  { platform: 'win32', factories: { wsl: ctx => { calls.push(ctx.id); return fake; } } },
);
assert.equal(made.ok, true);
assert.equal(made.kind, 'wsl');
assert.deepEqual(calls, ['local']);
assert.equal(Object.hasOwn(made.transport.context || {}, 'transport_driver'), false);
console.log('node transport factory passed');
```

- [ ] **Step 2: Run RED**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs`

Expected: FAIL with `Cannot find module '../electron/node-transport.cjs'`.

- [ ] **Step 3: Implement the pure decoder/factory**

Create `electron/node-transport.cjs`:

```js
'use strict';

const NODE_TRANSPORT_METHODS = Object.freeze([
  'runTool', 'ensureDirectory', 'listFiles', 'readFile',
  'writeFile', 'openTerminal', 'dispose',
]);

function fail(error, detail) {
  return { ok: false, error, detail: String(detail || error) };
}

function normalizeMachineType(machine = {}) {
  const explicit = String(machine.type || '').toLowerCase();
  const type = explicit || (machine.host ? 'ssh' : 'local');
  if (type !== 'local' && type !== 'ssh') {
    return fail('transport_unsupported', `unsupported machine.type: ${type}`);
  }
  return { ok: true, type, machine: { ...machine, type } };
}

function resolveAdapterKind(machine = {}, platform = process.platform) {
  const normalized = normalizeMachineType(machine);
  if (!normalized.ok) return normalized;
  if (normalized.type === 'ssh') return { ok: true, kind: 'ssh2' };
  return { ok: true, kind: platform === 'win32' ? 'wsl' : 'local-process' };
}

function validateTransport(transport) {
  const missing = NODE_TRANSPORT_METHODS.filter(name => typeof transport?.[name] !== 'function');
  return missing.length
    ? fail('transport_contract_invalid', `node transport missing methods: ${missing.join(', ')}`)
    : { ok: true, transport };
}

function createNodeTransport(context, { platform = process.platform, factories = {} } = {}) {
  const resolved = resolveAdapterKind(context?.machine || {}, platform);
  if (!resolved.ok) return resolved;
  const factory = factories[resolved.kind];
  if (typeof factory !== 'function') return fail('transport_unavailable', `${resolved.kind} adapter is unavailable`);
  const checked = validateTransport(factory(context));
  return checked.ok ? { ok: true, kind: resolved.kind, transport: checked.transport } : checked;
}

module.exports = {
  NODE_TRANSPORT_METHODS, normalizeMachineType, resolveAdapterKind,
  validateTransport, createNodeTransport, fail,
};
```

- [ ] **Step 4: Add test/lint scripts**

Add `"test:transport": "node test/node-transport.test.cjs && node test/node-transport-architecture.test.cjs"` after Task 7 creates the guard. For now run the first file directly. Extend `lint:electron` with every new `.cjs` module as it is created.

- [ ] **Step 5: Run GREEN**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs && node --check electron/node-transport.cjs`

Expected: PASS and `node transport factory passed`.

- [ ] **Step 6: Review checkpoint**

Run: `git diff --check` and inspect `git status --short`. Do not commit without authorization.

---

### Task 2: Let CAMC create sessions without authentication probing

**Files:**
- Modify: `src/camc_pkg/runtime_env.py`
- Modify: `src/camc_pkg/cli.py`
- Modify: `tests/test_runtime_env.py`
- Generate: `src/camc`

**Interfaces:**
- `check_tool_readiness(..., allow_interactive_setup=False)` remains strict by default.
- `camc run --allow-interactive-setup` converts only selected-tool version/auth readiness failures to warnings. Missing tool binary, broken/missing tmux, inaccessible workdir, and tmux-session creation remain blocking.

- [ ] **Step 1: Add failing readiness tests**

Add two tests beside the existing F2 version-probe tests in `tests/test_runtime_env.py`:

```python
def test_interactive_setup_makes_tool_probe_and_auth_advisory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    home.mkdir(); bindir.mkdir()
    (bindir / "tmux").write_text("#!/bin/sh\necho 'tmux 3.3'\n")
    (bindir / "claude").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "tmux").chmod(0o755); (bindir / "claude").chmod(0o755)
    monkeypatch.setattr(re_mod, "_GOLDEN_TMUX_PATHS", ())
    monkeypatch.setattr(re_mod, "_GOLDEN_TOOL_PATHS", {})
    rt = re_mod.RuntimeEnv(env={"PATH": str(bindir), "HOME": str(home)},
                           source="explicit", shell="", path=str(bindir))
    result = re_mod.check_tool_readiness(rt, "claude", allow_interactive_setup=True)
    errors = [m for level, m in result["issues"] if level == "error"]
    warnings = [m for level, m in result["issues"] if level == "warn"]
    assert not any("sanity probe" in m or "auth file" in m for m in errors)
    assert any("sanity probe" in m for m in warnings)
    assert any("auth file" in m for m in warnings)

def test_interactive_setup_still_blocks_missing_binary_and_tmux(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setattr(re_mod, "_GOLDEN_TMUX_PATHS", ())
    monkeypatch.setattr(re_mod, "_GOLDEN_TOOL_PATHS", {})
    rt = re_mod.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": str(home)},
                           source="explicit", shell="", path=str(tmp_path))
    result = re_mod.check_tool_readiness(rt, "claude", allow_interactive_setup=True)
    errors = [m for level, m in result["issues"] if level == "error"]
    assert any("tmux not found" in m for m in errors)
    assert any("claude" in m and "not found" in m for m in errors)
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_runtime_env.py -k 'interactive_setup' -q`

Expected: FAIL because `allow_interactive_setup` is not accepted.

- [ ] **Step 3: Implement advisory classification without weakening diagnostics**

Add the keyword parameter to `check_tool_readiness`. At the tool version probe and every required/optional authentication issue, select the level with:

```python
tool_readiness_level = "warn" if allow_interactive_setup else "error"
```

Use `tool_readiness_level` only for the selected tool's nonzero `version_args`, missing/unreadable `auth_files`, and unsatisfied `auth_files_any`/`env_keys`. Keep tool-path resolution and all tmux checks at `error`.

Thread the keyword through `_preflight(..., allow_interactive_setup=False)` in `cli.py` and pass it from `cmd_run`:

```python
allow_interactive_setup = bool(getattr(args, "allow_interactive_setup", False))
issues, resolved = _preflight(
    tool, tool_binary, workdir,
    env_setup=env_setup, runtime=runtime,
    adapter_readiness=adapter_readiness,
    use_env_tool=use_env_tool,
    allow_interactive_setup=allow_interactive_setup,
)
```

Add the hidden parser flag:

```python
r.add_argument("--allow-interactive-setup", action="store_true",
               help=argparse.SUPPRESS)
```

- [ ] **Step 4: Prove strict default compatibility**

Run:

```bash
python3 -m pytest tests/test_runtime_env.py -k 'version_probe or auth or interactive_setup or tmux_probe' -q
```

Expected: existing strict tests and new advisory tests all PASS.

- [ ] **Step 5: Regenerate the packaged standalone CAMC**

Run:

```bash
python3 build_camc.py --verify
cmp dist/camc src/camc || cp dist/camc src/camc
cmp dist/camc src/camc
```

Expected: builder verification passes and the tracked `src/camc` packaged by Electron matches `dist/camc`. Preserve the pre-existing `dist/BUILD_LOG.md` changes and report the newly appended entry separately when reviewing the diff.

- [ ] **Step 6: Verify the generated flag and syntax**

Run: `python3 -m py_compile src/camc_pkg/runtime_env.py src/camc_pkg/cli.py src/camc && src/camc run --help`

Expected: compilation succeeds; the hidden flag is not displayed.

---

### Task 3: Implement SSH/native adapters and shared PTY lifecycle

**Files:**
- Create: `apps/cam-desktop/electron/ssh-node-transport.cjs`
- Create: `apps/cam-desktop/electron/local-process-node-transport.cjs`
- Create: `apps/cam-desktop/electron/pty-terminal.cjs`
- Modify: `apps/cam-desktop/test/node-transport.test.cjs`

**Interfaces:**
- `runTool(tool, argv, { stdin, cwd, env, timeoutMs })` returns normalized stdout/stderr.
- File methods return `{ok:true,...}` or `{ok:false,error,detail}`.
- `openPtyTerminal({ file, args, cwd, env, cols, rows, onData, onClose, pty })` returns `{ok, dispose, write, resize}`.

- [ ] **Step 1: Add shared contract and PTY tests**

Extend `node-transport.test.cjs` with fake SSH and fake PTY objects. Assert every adapter has all contract methods, SSH quotes `a'b` as `'a'\''b'`, stdin is forwarded unchanged, and PTY write/resize/dispose call the fake process exactly once.

Use this fake PTY shape:

```js
const ptyEvents = {};
const fakePtyProcess = {
  write: data => ptyEvents.write = data,
  resize: (cols, rows) => ptyEvents.resize = [cols, rows],
  kill: () => ptyEvents.killed = (ptyEvents.killed || 0) + 1,
  onData: fn => { ptyEvents.onData = fn; return { dispose() {} }; },
  onExit: fn => { ptyEvents.onExit = fn; return { dispose() {} }; },
};
const fakePty = { spawn: (...args) => { ptyEvents.spawn = args; return fakePtyProcess; } };
```

- [ ] **Step 2: Run RED**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs`

Expected: FAIL because adapter modules are absent.

- [ ] **Step 3: Implement `pty-terminal.cjs`**

Normalize sizes to 40..500 columns and 4..500 rows. Call `pty.spawn(file, args, { name:'xterm-256color', cols, rows, cwd, env })`; convert `onData` strings to `Buffer`; map synchronous load/spawn errors to `pty_unavailable`; make dispose idempotent; return boolean from write/resize catches.

The returned object is exactly:

```js
{
  ok: true,
  dispose() { /* dispose listeners, kill once */ },
  write(data) { /* ptyProcess.write(String(data)); boolean */ },
  resize(cols, rows) { /* ptyProcess.resize(clampedCols, clampedRows); boolean */ },
}
```

- [ ] **Step 4: Implement the SSH adapter**

Wrap existing `execRemote`, `listRemoteFiles`, `readRemoteFile`, `writeRemoteFile`, and `openTerminalChannel`. Inject `connectOptions`, `resolveTool`, and `prepareTerminal`. Build remote command text only with:

```js
function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}
```

Allow only `SKILLM_TOKEN` in `options.env`; reject other names with `invalid_env`. Preserve timings returned by `ssh-transport.cjs`. `dispose()` is a no-op because the shared SSH pool is owned globally.

- [ ] **Step 5: Implement native local-process adapter**

Inject `execFileImpl`, `fsImpl`, `pathImpl`, `resolveTool`, and `pty`. Use `execFile` argv directly with `{ cwd, env:{...process.env,...allowlistedEnv}, timeout, maxBuffer, windowsHide:true }`. File methods use `fs` under an optional `root` guard based on `realpath/commonpath` semantics. Terminal resolves CAMC and calls `openPtyTerminal({ file:camcPath, args:['attach',agentId], ... })`.

- [ ] **Step 6: Run contract tests**

Run:

```bash
cd apps/cam-desktop
node test/node-transport.test.cjs
node --check electron/ssh-node-transport.cjs
node --check electron/local-process-node-transport.cjs
node --check electron/pty-terminal.cjs
```

Expected: all PASS without opening real SSH, files, or PTYs.

---

### Task 4: Implement default-WSL command, file, and terminal transport

**Files:**
- Create: `apps/cam-desktop/electron/wsl-node-transport.cjs`
- Modify: `apps/cam-desktop/test/node-transport.test.cjs`
- Modify: `apps/cam-desktop/package.json`
- Modify: `apps/cam-desktop/package-lock.json`

**Interfaces:**
- `createWslNodeTransport({ context, execFileImpl, pty, bundledTool, windowsEnv }) -> NodeTransport`.
- Every process invocation begins `wsl.exe --exec`; no invocation contains `-d`.
- CAMC/Skillm are installed into `$HOME/.cam/<tool>` from packaged bytes using Python stdin and an atomic replace.

- [ ] **Step 1: Add failing WSL invocation tests**

Record all injected `execFileImpl(file,args,options,callback)` calls and assert:

```js
assert.equal(call.file, 'wsl.exe');
assert.equal(call.args[0], '--exec');
assert.equal(call.args.includes('-d'), false);
assert.equal(call.args.includes('Ubuntu'), false);
assert.equal(call.args.includes("user prompt"), true); // argv, never script text
assert.equal(call.args.find(a => a.includes('exec "$HOME/.cam/camc"')), WSL_TOOL_SCRIPT);
```

Also assert a failed `wsl.exe` ENOENT maps to `wsl_unavailable`, file JSON envelopes decode to Buffers, and terminal uses `pty.spawn('wsl.exe', ['--exec', ...])`.

- [ ] **Step 2: Run RED**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs`

Expected: FAIL on missing `wsl-node-transport.cjs`.

- [ ] **Step 3: Implement one binary-safe WSL runner**

Use `execFileImpl('wsl.exe', ['--exec', ...linuxArgv], { timeout, maxBuffer, windowsHide:true, encoding:null }, callback)`. Write optional stdin with `child.stdin.end(buffer)`. Map ENOENT to `wsl_unavailable`, timeout to `timeout`, exit 127 mentioning Python to `python_missing`, and other nonzero exits to `exec_failed` while preserving stdout/stderr.

Use this fixed tool script; append `context.machine.env_setup` before `exec` only when configured:

```sh
cwd=$1
shift
if [ -n "$cwd" ]; then cd -- "$cwd" || exit 73; fi
exec "$HOME/.cam/$1" "${@:2}"
```

Because POSIX `sh` does not guarantee `${@:2}`, implement the actual fixed script as:

```sh
cwd=$1
tool=$2
shift 2
if [ -n "$cwd" ]; then cd -- "$cwd" || exit 73; fi
exec "$HOME/.cam/$tool" "$@"
```

Invoke it as `wsl.exe --exec /bin/sh -lc <fixedScript> cam-desktop <cwd> <tool> ...argv`. Prompt and agent input remain separate argv/stdin bytes.

- [ ] **Step 4: Install packaged tools without a distro path**

Use `wsl.exe --exec python3 -c <fixedPython> <tool> <md5>` with packaged bytes on stdin. The fixed Python expands `~/.cam`, compares the existing executable MD5, writes `<tool>.tmp-<pid>`, chmods `0700`, and `os.replace`s it. Cache `{tool,hash}` after success. Reject tools other than `camc` and `skillm`.

The Python error envelope is JSON on stderr with `error` one of `python_missing`, `tool_install_failed`, or `permission_denied`; do not include packaged content.

- [ ] **Step 5: Implement Python-backed file methods**

Use fixed Python programs with the path only in `sys.argv`:

- `ensureDirectory`: `os.makedirs(path, exist_ok=True)`.
- `listFiles`: `os.scandir`, `stat(follow_symlinks=False)`, emit JSON `{ok:true,entries:[{name,type,size,mtime}]}` sorted directories first.
- `readFile`: reject directories and `size > maxBytes`; emit base64 JSON `{ok:true,size,content}`; Node returns a Buffer.
- `writeFile`: optionally create the parent, read raw stdin, write a temp file, then `os.replace`; emit `{ok:true,bytes}`.

Each Python program catches `FileNotFoundError`, `PermissionError`, `IsADirectoryError`, and generic `OSError`, emitting `{ok:false,error,detail}`. When `options.root` is provided, compare `os.path.commonpath([realRoot, realTarget]) == realRoot` before access and return `path_traversal` otherwise.

- [ ] **Step 6: Implement WSL Terminal**

Resolve/install CAMC, then call `openPtyTerminal` with:

```js
{
  file: 'wsl.exe',
  args: ['--exec', '/bin/sh', '-lc',
    'exec "$HOME/.cam/camc" attach "$1"',
    'cam-desktop-attach', agentId],
  cwd: windowsEnv.USERPROFILE || windowsEnv.HOME || process.cwd(),
  env: { ...windowsEnv, SystemRoot: windowsEnv.SystemRoot || 'C:\\Windows' },
  cols, rows, onData, onClose, pty,
}
```

- [ ] **Step 7: Add and package node-pty**

Run: `cd apps/cam-desktop && npm install --save-exact node-pty@1.1.0`

Add `"**/node_modules/node-pty/**"` to `build.asarUnpack`. Do not add a postinstall script. Electron Builder performs the native dependency rebuild; the Windows MSI task must prove the Electron ABI binary is packaged.

- [ ] **Step 8: Run WSL adapter tests and syntax checks**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs && npm run lint:electron`

Expected: all PASS; no test invokes real WSL or node-pty.

---

### Task 5: Route Hub command, sync, Start, models, and Skillm operations

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `apps/cam-desktop/test/hub.test.cjs`
- Modify: `web/js/desktop/start-agent-mode.js`

**Interfaces:**
- `_transportForContext(ctx,{overrides}) -> factory result` is the sole Hub entry.
- `_runToolOnContext(ctx,tool,argv,options) -> normalized result` replaces local/remote executors.
- The automatic local context remains `{ machine:{type:'local',host:'',user:'',port:null} }`.

- [ ] **Step 1: Add deterministic fake-factory Hub tests**

Extend `configure()` test injection with `nodeTransportFactories` and record calls for `runTool`. Cover local and SSH contexts with the same fake contract. Assert:

- `GET /api/agents` calls `camc --json list` for local and SSH contexts.
- `POST /api/agents` appends `--allow-interactive-setup` to CAMC run argv and accepts an empty prompt.
- Stop maps to `camc stop <id>`; Remove maps to `camc rm <id> --kill`.
- send uses `['send',id,'--stdin']` and passes message bytes as stdin.
- API models and Skillm use `runTool` without direct SSH calls.
- loopback context `{type:'ssh',host:'127.0.0.1'}` selects the SSH fake.

- [ ] **Step 2: Run RED**

Run: `cd apps/cam-desktop && npm run test:hub`

Expected: FAIL because Hub configuration and routes bypass the factory.

- [ ] **Step 3: Configure the three real factories**

Import the factory/adapters. Add `_nodeTransportFactoryOverrides`. `_transportForContext` builds:

```js
const factories = _nodeTransportFactoryOverrides || {
  ssh2: context => createSshNodeTransport({
    context,
    connectOptions: _sshBaseOptsForContext(context, timeoutMs, overrides).opts,
    sshTransport: _sshTransport,
    resolveTool: _resolveSshTool,
    prepareTerminal: _prepareSshTerminal,
  }),
  wsl: context => require('./wsl-node-transport.cjs').createWslNodeTransport({
    context, bundledTool: _readBundledTool, pty: _loadNodePty(),
  }),
  'local-process': context => createLocalProcessNodeTransport({
    context, resolveTool: _resolveLocalTool, pty: _loadNodePty(),
  }),
};
return createNodeTransport(ctx, { platform: process.platform, factories });
```

Handle `_sshBaseOptsForContext` errors before constructing the SSH adapter. Lazy-load `node-pty` so non-terminal command tests and unsupported build hosts do not fail at module import.
Do not import `wsl-node-transport.cjs` at module top level; the inline `require`
above is the platform-isolation boundary.

- [ ] **Step 4: Unify CAMC lifecycle/send/capture/cron and Skillm**

Replace `_execCamcOnContext`, `_sendAgentInput`'s direct SSH block, and `_execSkillmOnContext` transport selection with `_runToolOnContext`. Preserve redaction, timeout, delivered-nonzero send handling, and API status mapping. Pass only `SKILLM_TOKEN` through the adapter env allowlist.

- [ ] **Step 5: Unify synchronization and API model discovery**

Remove the SSH-only guard from `_syncContextAgents`. `_syncableAgentContexts` includes one representative per topology endpoint plus one local context. Delete `_runLocalCamcList` and make `_syncLocalAgents` call `_syncContextAgents(_ensureLocalContext())`, retaining throttling and nonfatal missing-runtime behavior.

Replace `_localCamcJson`/`_remoteCamcJson` selection with two `_runToolOnContext` calls. Remove Windows text claiming users must choose a remote node.

- [ ] **Step 6: Unify Start and make login repairable**

Add `--allow-interactive-setup` to `_buildRunArgv`. Replace `_startLocalAgent` and `_startRemoteAgent` with one `_startAgent(body,ctx,options)` that runs CAMC, parses the ID, fetches status through the same transport, normalizes/upserts, and preserves fallback record fields. `_resolveStartTarget` still accepts node key `local` and returns `_ensureLocalContext()`; SSH node keys still require a registered credential donor.

Do not call tool `--version`, check auth files, or reject missing login state in Desktop. Missing WSL/Python/tmux/CAMC/tool errors come from the adapter/CAMC and remain actionable.

- [ ] **Step 7: Update Start UI wording only**

Keep the synthetic/local option and default. On Windows (`CamBridge.getPlatform() === 'win32'`) label it `local (default WSL)` and explain: `Uses the default WSL2 Linux environment. Python, tmux, and the selected tool must be installed; login can be completed in Terminal after Start.` Do not add a distro selector or auth status.

- [ ] **Step 8: Run command/start regression tests**

Run:

```bash
cd apps/cam-desktop
npm run test:hub
node test/node-transport.test.cjs
npm run lint:electron
```

Expected: all PASS and direct local/SSH command executors have no remaining callers.

---

### Task 6: Route files and Terminal through NodeTransport

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `apps/cam-desktop/electron/main.cjs`
- Modify: `apps/cam-desktop/electron/preload.cjs`
- Modify: `apps/cam-desktop/test/hub.test.cjs`
- Modify: `apps/cam-desktop/test/terminal-follow.test.cjs`

**Interfaces:**
- Browse/upload/system-prompt functions consume only transport file methods.
- `embeddedHub.openAgentTerminal(agentId,{cols,rows,onData,onClose})` returns the selected adapter channel.
- IPC/preload shapes remain unchanged.

- [ ] **Step 1: Add failing file-routing tests**

Drive list/read/write/upload/system-prompt routes through a fake transport. Assert full Linux workspace paths, byte limits, root options, create-parent behavior, and existing HTTP error mapping. No test may read or write the developer's real workspace.

- [ ] **Step 2: Replace local/SFTP feature branches**

After existing lexical subpath validation, resolve `transport = _transportForContext(ctx).transport` and call:

```js
transport.listFiles(fullPath, { root });
transport.readFile(fullPath, { root, maxBytes: WORKSPACE_FILE_MAX_BYTES });
transport.writeFile(fullPath, Buffer.from(text, 'utf8'), { root, createParents: true });
transport.ensureDirectory(uploadDir, { root, timeoutMs: 60000 });
transport.writeFile(uploadPath, decoded.content, { root, timeoutMs: 60000 });
```

Delete feature-level `isLocal`, `_localPath`, direct `fs` Browse writes, and direct SFTP calls after their callers are migrated. Keep bundle reading/deployment helpers in main-process infrastructure.

- [ ] **Step 3: Add failing terminal delegation tests**

In `terminal-follow.test.cjs`, assert `main.cjs` contains `embeddedHub.openAgentTerminal(` and does not call `sshTransport.openTerminalChannel(` from `termOpen`. In `hub.test.cjs`, assert local/SSH agents call the corresponding fake adapter with `{agentId,cols,rows,onData,onClose}`.

- [ ] **Step 4: Move terminal selection into the Hub**

Replace `getAttachConnectOpts` with:

```js
async function openAgentTerminal(agentId, options = {}) {
  const resolved = _contextForAgent(agentId);
  if (resolved.error) return { ok:false, error:resolved.error, detail:`agent ${agentId}: ${resolved.error}` };
  const built = _transportForContext(_attachContextForAgent(resolved.agent, resolved.ctx));
  if (!built.ok) return built;
  return built.transport.openTerminal({
    agentId: resolved.agent?.id || agentId,
    cols: options.cols, rows: options.rows,
    onData: options.onData, onClose: options.onClose,
  });
}
```

Export it. Keep agent machine/context matching but remove the `not_ssh` terminal rejection.

- [ ] **Step 5: Keep main's cache but remove SSH knowledge**

`termOpen` creates the opaque session ID and calls `embeddedHub.openAgentTerminal`. Store only `{dispose,write,resize,contentsId,agentId}`. Cached reuse calls `resize`; it does not run `_repairRemoteTerminalSize` itself because adapter-owned `prepareTerminal` handles SSH and local CAMC attach owns local sizing. Keep renderer `term:data`/`term:status` events identical.

- [ ] **Step 6: Update comments and run focused tests**

Change preload/main comments from SSH-only to NodeTransport wording without changing IPC. Run:

```bash
cd apps/cam-desktop
npm run test:hub
npm run test:term
node test/node-transport.test.cjs
npm run lint:electron
```

Expected: all PASS; local terminal no longer returns `not_ssh`.

---

### Task 7: Enforce the boundary, document it, and verify Windows packaging

**Files:**
- Create: `apps/cam-desktop/test/node-transport-architecture.test.cjs`
- Modify: `apps/cam-desktop/package.json`
- Modify: `docs/desktop/requirements.md`
- Modify: `apps/cam-desktop/FIXES-NODES.md`
- Verify: all implementation files

**Interfaces:**
- Execution modules outside the factory/adapters contain no transport-selection branches.
- Renderer may display `machine.type`, but hostnames never infer transport.

- [ ] **Step 1: Add the architecture guard**

Create a Node test that reads `embedded-hub.cjs` and `main.cjs`. Reject direct execution selection patterns `machine.type ===`, `machine.type !==`, `isLocal`, `not_ssh`, `_startLocalAgent`, `_startRemoteAgent`, `_localCamcJson`, `_remoteCamcJson`, and direct `sshTransport.openTerminalChannel` in main. Allow topology comparisons only in `node-transport.cjs` and record-matching/display helpers. Also assert `node-transport.cjs` contains no `transport_driver` or distro field.

- [ ] **Step 2: Prove WSL isolation without broad UI changes**

Add source assertions that `wsl-node-transport.cjs` is loaded only inside the `wsl` factory callback, and factory tests that Linux/macOS local select `local-process` while Windows local selects `wsl`. Leave `node-host-meta.js` and `nodes-mode.js` unchanged. Existing renderer display logic may inspect explicit `machine.type`, but it must not decide execution.

- [ ] **Step 3: Document the normative invariant**

Add this requirement:

```text
CAM-DESK-NODETRANSPORT-001: Every Desktop command, file, and terminal operation resolves one NodeTransport in Electron main. Persisted machine.type is local or ssh. Windows local uses the default WSL environment, Linux/macOS local uses native processes, and SSH uses the pooled ssh2 adapter even for loopback hosts. Authentication is completed interactively inside the created agent session and is not a Desktop Start preflight.
```

In `FIXES-NODES.md`, record before/after files, local WSL behavior, SSH-pool preservation, and focused test evidence. Do not modify Mobile architecture.

- [ ] **Step 4: Run complete source verification**

Run:

```bash
python3 -m pytest tests/test_runtime_env.py -q
cd apps/cam-desktop
npm run test:transport
npm run test:hub
npm run test:term
node test/mobile-terminal-input-routing.test.cjs
npm run lint:electron
cd ../..
git diff --check
git status --short
```

Expected: all commands exit 0. Status contains only intentional files plus pre-existing unrelated changes.

- [ ] **Step 5: Smoke the three adapter mappings**

Using fakes or available development nodes, verify:

1. `{type:'ssh',host:'pdx098',port:3422}` reuses the pooled SSH connection for sync/send/capture/terminal.
2. `{type:'ssh',host:'127.0.0.1'}` still requests SSH credentials and uses SSH.
3. `{type:'local'}` on Windows invokes default WSL with no `-d`, starts with `--allow-interactive-setup`, and supports Terminal login interaction.

- [ ] **Step 6: Verify native dependency and MSI on the Windows build box**

Transfer only the reviewed patch to the existing Windows checkout at `/mnt/c/Users/hren/gitlab/cam`, then run through the established tunnel:

```bash
ssh -p 2222 hren@127.0.0.1 'bash ~/cam-build-from-wsl.sh'
```

Expected: `apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi` is produced. Extract/inspect `app.asar.unpacked/node_modules/node-pty` and confirm a Windows `.node` binary is present. Launch the installed build, start a local WSL agent, open Terminal, type input, resize, close/reopen, and confirm SSH terminal behavior remains unchanged.

Do not copy the MSI to Nutstore, publish, commit, or push without separate authorization.

## Self-review

- Spec coverage: persisted topology, OS mapping, default WSL, automatic local node, loopback SSH, command/file/terminal parity, authentication-advisory Start, packaging, and MSI checks each have a task.
- Placeholder scan: no deferred implementation markers remain; unsupported states have explicit normalized errors.
- Type consistency: every task uses `machine.type`, adapter kinds `ssh2|wsl|local-process`, and the same seven NodeTransport method names.
- Regression coverage: strict CAMC diagnostics remain default; only Desktop's hidden launch flag is advisory. SSH pooling/API shapes and renderer terminal events are preserved.
- Scope: Desktop NodeTransport plus the minimum CAMC readiness flag. WSL code is Windows-only; Linux/macOS receive only the thin native adapter. Mobile, Relay protocol, Nodes redesign, tmux internals, tool installation, distro management, authentication automation, release, and push are out of scope.
