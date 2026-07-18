> **Superseded:** Do not execute this plan. It persisted a transport driver,
> removed the automatic local node, and omitted a Windows local PTY. Use
> `2026-07-16-node-transport-wsl-implementation.md` instead.

# NodeTransport Architecture Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Desktop's scattered SSH-versus-local branches with one `NodeTransport` factory so every agent operation uses the same transport contract.

**Architecture:** `embedded-hub.cjs` resolves an agent or context, asks one factory for a `NodeTransport`, and then calls transport methods without inspecting `machine.type`. `SshNodeTransport` wraps the existing pooled `ssh2` implementation; `LocalProcessNodeTransport` preserves legacy direct-process records behind the same method surface. New nodes, including `127.0.0.1`, are always created as SSH nodes; legacy local records are decoded and normalized once at store load.

**Tech Stack:** Electron 31, Node.js CommonJS, built-in `child_process`/`fs`, existing pooled `ssh2` transport, vanilla browser ES modules, deterministic Node assertion tests.

## Global Constraints

- Transport selection occurs only in `apps/cam-desktop/electron/node-transport.cjs`; no route, feature, renderer, or adapter may branch on `machine.type`, `machine_type`, `transport_type`, `isLocal`, or a node key named `local`.
- The authoritative discriminator is `machine.transport_driver`, with exact values `ssh2` and `local-process`.
- New nodes always use `transport_driver: "ssh2"`, including nodes whose host is `127.0.0.1`, `localhost`, or `::1`.
- `local-process` exists only to load legacy persisted records. The UI does not auto-create it, default to it, or offer it as a new-node type.
- Both adapters expose the same methods and the same `{ ok, error, detail }` failure shape. A capability that cannot be provided by the compatibility adapter returns a normalized method result; callers do not branch on driver.
- Keep renderer processes free of SSH credentials, command strings, local executable paths, and decrypted secrets.
- Preserve the existing long-lived `ssh2` connection pool and its 600-second idle lifetime.
- Preserve existing HTTP response shapes for agent, context, Browse, Skillm, and terminal operations unless a task below explicitly adds a field.
- Do not add `node-pty` or another native dependency. `LocalProcessNodeTransport.openTerminal()` returns `terminal_unavailable` until a separate local PTY design is approved.
- Preserve unrelated dirty-worktree changes. Commit steps are checkpoints in this plan and require explicit user authorization before execution.
- This plan supersedes the “Retain the automatic local Node/context” constraint in `docs/superpowers/plans/2026-07-15-start-local-runtime-implementation.md`.

---

## File Map

### New files

- `apps/cam-desktop/electron/node-transport.cjs` — transport contract, legacy decoder, normalizer, contract validator, and the only adapter factory.
- `apps/cam-desktop/electron/ssh-node-transport.cjs` — SSH implementation backed by `ssh-transport.cjs`.
- `apps/cam-desktop/electron/local-process-node-transport.cjs` — legacy direct `execFile`/`fs` implementation with the same contract.
- `apps/cam-desktop/test/node-transport.test.cjs` — factory, normalization, adapter-contract, quoting, file, and error-shape tests.
- `apps/cam-desktop/test/node-transport-architecture.test.cjs` — source guard that prevents transport branching from returning outside the factory/adapters.

### Existing files to modify

- `apps/cam-desktop/electron/embedded-hub.cjs` — consume `NodeTransport` for sync, start, model lookup, camc/Skillm operations, files, uploads, and terminal attachment; remove direct local and SSH feature branches.
- `apps/cam-desktop/electron/main.cjs` — ask the Hub to open an agent terminal instead of directly decoding SSH options.
- `apps/cam-desktop/electron/ssh-transport.cjs` — remain the low-level pooled SSH primitive; only adjust exported result fields if contract tests reveal an exact mismatch.
- `apps/cam-desktop/test/hub.test.cjs` — inject fake NodeTransport factories and verify command/API behavior independent of real SSH and local camc.
- `apps/cam-desktop/test/terminal-follow.test.cjs` — retain renderer follow behavior and add the new main-process terminal delegation assertion.
- `apps/cam-desktop/package.json` — syntax-check new modules and add transport test scripts.
- `web/js/desktop/start-agent-mode.js` — remove the synthetic local option/default and handle an empty node registry.
- `web/js/desktop/app.js` — stop filtering synchronization by SSH type.
- `web/js/desktop/skills-mode.js` — list node contexts without a local-versus-SSH renderer decision.
- `web/js/shared/node-host-meta.js` — generate endpoint keys without a special `local` key.
- `web/js/shared/nodes-mode.js` — render all configured endpoints as nodes and remove local-only grouping/badges.
- `apps/cam-desktop/FIXES-NODES.md` — mark direct local ingestion as superseded by NodeTransport.
- `docs/desktop/requirements.md` — record the one-factory transport invariant and loopback-SSH semantics.
- `docs/superpowers/plans/2026-07-15-start-local-runtime-implementation.md` — add a superseded notice to the old automatic-local constraint.

---

### Task 1: Define the NodeTransport contract and single decoder

**Files:**
- Create: `apps/cam-desktop/electron/node-transport.cjs`
- Create: `apps/cam-desktop/test/node-transport.test.cjs`
- Modify: `apps/cam-desktop/package.json`

**Interfaces:**
- Produces: `decodeNodeTransport(machine) -> { driver, endpoint }`.
- Produces: `normalizeNodeMachine(machine, { localUser }) -> normalized machine`.
- Produces: `createNodeTransport(context, { factories }) -> { ok, transport } | { ok: false, error, detail }`.
- Produces the exact required method list: `runTool`, `ensureDirectory`, `listFiles`, `readFile`, `writeFile`, and `openTerminal`.

- [ ] **Step 1: Write the failing decoder and factory tests**

Create `apps/cam-desktop/test/node-transport.test.cjs` with this initial contract section:

```js
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const {
  NODE_TRANSPORT_METHODS,
  decodeNodeTransport,
  normalizeNodeMachine,
  createNodeTransport,
} = require(path.join(__dirname, '..', 'electron', 'node-transport.cjs'));

const methods = ['runTool', 'ensureDirectory', 'listFiles', 'readFile', 'writeFile', 'openTerminal'];
assert.deepEqual(NODE_TRANSPORT_METHODS, methods);

assert.deepEqual(
  decodeNodeTransport({ transport_driver: 'ssh2', host: '127.0.0.1', user: 'hren', port: 22 }),
  { driver: 'ssh2', endpoint: 'hren@127.0.0.1:22' },
);
assert.equal(decodeNodeTransport({ type: 'ssh', host: 'pdx098', user: 'hren', port: 3422 }).driver, 'ssh2');
assert.equal(decodeNodeTransport({ type: 'local' }).driver, 'local-process');

const migrated = normalizeNodeMachine({ type: 'local', host: '', user: '', port: null }, { localUser: 'hren' });
assert.deepEqual(migrated, {
  type: 'ssh',
  transport_driver: 'local-process',
  host: '127.0.0.1',
  user: 'hren',
  port: 22,
});

const calls = [];
const fake = Object.fromEntries(methods.map(name => [name, async () => ({ ok: true, name })]));
const built = createNodeTransport(
  { id: 'ctx-1', machine: { transport_driver: 'ssh2', host: 'pdx098', user: 'hren', port: 3422 } },
  { factories: { ssh2: (context) => { calls.push(context.id); return fake; } } },
);
assert.equal(built.ok, true);
assert.equal(built.transport, fake);
assert.deepEqual(calls, ['ctx-1']);

const unknown = createNodeTransport(
  { id: 'ctx-2', machine: { transport_driver: 'serial' } },
  { factories: {} },
);
assert.deepEqual(unknown, {
  ok: false,
  error: 'transport_unsupported',
  detail: 'unsupported node transport driver: serial',
});

console.log('node transport decoder and factory passed');
```

- [ ] **Step 2: Run the test and verify the module is missing**

Run:

```bash
cd apps/cam-desktop
node test/node-transport.test.cjs
```

Expected: FAIL with `Cannot find module .../electron/node-transport.cjs`.

- [ ] **Step 3: Implement the contract and the only driver decoder**

Create `apps/cam-desktop/electron/node-transport.cjs`:

```js
'use strict';

const NODE_TRANSPORT_METHODS = Object.freeze([
  'runTool',
  'ensureDirectory',
  'listFiles',
  'readFile',
  'writeFile',
  'openTerminal',
]);

const LEGACY_DRIVER = Object.freeze({
  ssh: 'ssh2',
  local: 'local-process',
});

function decodeNodeTransport(machine = {}) {
  const driver = String(
    machine.transport_driver || LEGACY_DRIVER[machine.type] || 'ssh2',
  );
  const host = String(machine.host || (driver === 'local-process' ? '127.0.0.1' : ''));
  const user = String(machine.user || '');
  const port = Number(machine.port || 22) || 22;
  return { driver, endpoint: `${user}@${host}:${port}` };
}

function normalizeNodeMachine(machine = {}, { localUser = '' } = {}) {
  const decoded = decodeNodeTransport(machine);
  return {
    ...machine,
    type: 'ssh',
    transport_driver: decoded.driver,
    host: machine.host || (decoded.driver === 'local-process' ? '127.0.0.1' : ''),
    user: machine.user || (decoded.driver === 'local-process' ? localUser : ''),
    port: Number(machine.port || 22) || 22,
  };
}

function validateNodeTransport(transport) {
  const missing = NODE_TRANSPORT_METHODS.filter(name => typeof transport?.[name] !== 'function');
  if (missing.length) {
    return {
      ok: false,
      error: 'transport_contract_invalid',
      detail: `node transport missing methods: ${missing.join(', ')}`,
    };
  }
  return { ok: true, transport };
}

function createNodeTransport(context, { factories = {} } = {}) {
  const { driver } = decodeNodeTransport(context?.machine || {});
  const factory = factories[driver];
  if (typeof factory !== 'function') {
    return {
      ok: false,
      error: 'transport_unsupported',
      detail: `unsupported node transport driver: ${driver}`,
    };
  }
  return validateNodeTransport(factory(context));
}

module.exports = {
  NODE_TRANSPORT_METHODS,
  decodeNodeTransport,
  normalizeNodeMachine,
  validateNodeTransport,
  createNodeTransport,
};
```

The two `driver === 'local-process'` expressions above are normalization defaults inside the decoder module. No other file may decode or branch on the driver.

- [ ] **Step 4: Add package scripts and syntax coverage**

Add these scripts to `apps/cam-desktop/package.json`:

```json
"test:transport": "node test/node-transport.test.cjs",
"lint:electron": "node --check electron/main.cjs && node --check electron/preload.cjs && node --check electron/ssh-transport.cjs && node --check electron/node-transport.cjs && node --check electron/ssh-node-transport.cjs && node --check electron/local-process-node-transport.cjs && node --check electron/embedded-hub.cjs && node --check electron/credential-store.cjs && node --check cli/camui-cli.cjs"
```

Do not run `lint:electron` until Task 2 creates both adapter files.

- [ ] **Step 5: Run the factory test**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs`

Expected: PASS and print `node transport decoder and factory passed`.

- [ ] **Step 6: Commit the contract checkpoint after authorization**

```bash
git add apps/cam-desktop/electron/node-transport.cjs apps/cam-desktop/test/node-transport.test.cjs apps/cam-desktop/package.json
git commit -m "refactor(desktop): define node transport contract"
```

### Task 2: Implement SSH and legacy local-process adapters

**Files:**
- Create: `apps/cam-desktop/electron/ssh-node-transport.cjs`
- Create: `apps/cam-desktop/electron/local-process-node-transport.cjs`
- Modify: `apps/cam-desktop/test/node-transport.test.cjs`

**Interfaces:**
- Consumes: `NODE_TRANSPORT_METHODS` from Task 1.
- Produces: `createSshNodeTransport(deps) -> NodeTransport`.
- Produces: `createLocalProcessNodeTransport(deps) -> NodeTransport`.
- `runTool(tool, argv, options)` accepts `{ stdin, cwd, timeoutMs }` and returns `{ ok, stdout, stderr, error?, detail? }`.
- `openTerminal(options)` returns `{ ok, dispose, write, resize }` or a normalized error.

- [ ] **Step 1: Extend tests with one shared contract suite**

Append this helper and two adapter cases to `node-transport.test.cjs`:

```js
async function verifyContract(name, transport) {
  for (const method of methods) assert.equal(typeof transport[method], 'function', `${name}.${method}`);
  const missing = await transport.readFile('/missing', { maxBytes: 10 });
  assert.equal(typeof missing.ok, 'boolean');
  if (!missing.ok) {
    assert.equal(typeof missing.error, 'string');
    assert.equal(typeof missing.detail, 'string');
  }
}

const { createSshNodeTransport } = require('../electron/ssh-node-transport.cjs');
const { createLocalProcessNodeTransport } = require('../electron/local-process-node-transport.cjs');

const sshCalls = [];
const sshAdapter = createSshNodeTransport({
  context: { name: 'pdx', machine: { host: 'pdx098', user: 'hren', port: 3422 } },
  connectOptions: { host: 'pdx098', user: 'hren', port: 3422 },
  sshTransport: {
    execRemote: async opts => { sshCalls.push(opts); return { ok: true, stdout: 'ok', stderr: '' }; },
    listRemoteFiles: async () => ({ ok: true, entries: [] }),
    readRemoteFile: async () => ({ ok: false, error: 'not_found', detail: 'missing' }),
    writeRemoteFile: async () => ({ ok: true, bytes: 1 }),
    openTerminalChannel: async () => ({ ok: true, dispose() {}, write() {}, resize() {} }),
  },
  resolveTool: async tool => ({ ok: true, path: tool === 'camc' ? '~/.cam/camc' : '~/.cam/skillm' }),
});
await verifyContract('ssh', sshAdapter);
await sshAdapter.runTool('camc', ['status', "a'b"], { stdin: Buffer.from('x'), timeoutMs: 9000 });
assert.match(sshCalls.at(-1).command, /~\/.cam\/camc 'status' 'a'\\''b'/);
assert.equal(sshCalls.at(-1).stdin.toString(), 'x');

const localAdapter = createLocalProcessNodeTransport({
  context: { name: 'legacy-local', machine: { transport_driver: 'local-process' } },
  resolveTool: tool => ({ ok: true, path: `/opt/cam/${tool}` }),
  execFileImpl(program, argv, options, callback) {
    callback(null, `${program}:${argv.join(',')}`, '');
    return { unref() {} };
  },
});
await verifyContract('local-process', localAdapter);
const localRun = await localAdapter.runTool('camc', ['list'], { timeoutMs: 9000 });
assert.equal(localRun.ok, true);
assert.equal(localRun.stdout, '/opt/cam/camc:list');
const localTerm = await localAdapter.openTerminal({ agentId: 'abc' });
assert.equal(localTerm.error, 'terminal_unavailable');
```

Wrap the test body in an async `main()` and end with `main().catch(...)` so the awaited contract cases execute deterministically.

- [ ] **Step 2: Run the test and verify both adapter modules are missing**

Run: `cd apps/cam-desktop && node test/node-transport.test.cjs`

Expected: FAIL on `ssh-node-transport.cjs` missing.

- [ ] **Step 3: Implement the SSH adapter around the existing pool**

Create `apps/cam-desktop/electron/ssh-node-transport.cjs` with these exact rules:

```js
'use strict';

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function fail(error, detail) {
  return { ok: false, error, detail: String(detail || error) };
}

function createSshNodeTransport({
  context,
  connectOptions,
  sshTransport,
  resolveTool,
  prepareTerminal = async () => ({ ok: true }),
}) {
  async function toolPath(tool) {
    const found = await resolveTool(tool, connectOptions, context);
    return found && found.ok ? found : fail(found?.error || 'tool_unavailable', found?.detail || `${tool} unavailable`);
  }

  return Object.freeze({
    driver: 'ssh2',
    context,

    async runTool(tool, argv = [], { stdin = null, cwd = '', timeoutMs = 15000 } = {}) {
      const found = await toolPath(tool);
      if (!found.ok) return found;
      const invocation = [found.path, ...argv].map(shellQuote).join(' ');
      const command = cwd ? `cd ${shellQuote(cwd)} && ${invocation}` : invocation;
      const result = await sshTransport.execRemote({
        ...connectOptions,
        command,
        stdin,
        timeout_ms: timeoutMs,
      });
      return result?.ok ? { ok: true, stdout: String(result.stdout || ''), stderr: String(result.stderr || ''), timings: result.timings }
        : fail(result?.error || 'exec_failed', result?.detail || result?.stderr || 'remote tool execution failed');
    },

    async ensureDirectory(remotePath, { timeoutMs = 15000 } = {}) {
      const result = await sshTransport.execRemote({
        ...connectOptions,
        command: `mkdir -p ${shellQuote(remotePath)}`,
        timeout_ms: timeoutMs,
      });
      return result?.ok ? { ok: true } : fail(result?.error || 'mkdir_failed', result?.detail || result?.stderr);
    },

    async listFiles(remotePath) {
      const result = await sshTransport.listRemoteFiles({ ...connectOptions, remotePath });
      return result?.ok ? { ok: true, entries: result.entries || [] }
        : fail(result?.error || 'list_failed', result?.detail);
    },

    async readFile(remotePath, { maxBytes } = {}) {
      const result = await sshTransport.readRemoteFile({ ...connectOptions, remotePath, maxBytes });
      return result?.ok ? { ok: true, content: result.content, size: result.size, binary: result.binary }
        : fail(result?.error || 'read_failed', result?.detail);
    },

    async writeFile(remotePath, content, { createParents = false, timeoutMs = 60000 } = {}) {
      if (createParents) {
        const parent = String(remotePath).split('/').slice(0, -1).join('/') || '.';
        const made = await this.ensureDirectory(parent, { timeoutMs });
        if (!made.ok) return made;
      }
      const result = await sshTransport.writeRemoteFile({ ...connectOptions, remotePath, content, timeout_ms: timeoutMs });
      return result?.ok ? { ok: true, bytes: result.bytes }
        : fail(result?.error || 'write_failed', result?.detail);
    },

    async openTerminal({ agentId, cols, rows, onData, onClose }) {
      const found = await toolPath('camc');
      if (!found.ok) return found;
      const prepared = await prepareTerminal({ connectOptions, agentId, cols, rows });
      if (prepared && prepared.ok === false) return prepared;
      return sshTransport.openTerminalChannel(
        { ...connectOptions, command: `${shellQuote(found.path)} attach ${shellQuote(agentId)}` },
        { cols, rows, onData, onClose },
      );
    },
  });
}

module.exports = { createSshNodeTransport, shellQuote };
```

- [ ] **Step 4: Implement the compatibility adapter without a shell**

Create `apps/cam-desktop/electron/local-process-node-transport.cjs`. Use `execFile`, `fs`, and `path`; never concatenate a local shell command. Its `runTool` calls `execFile(program, argv, { cwd, timeout, maxBuffer, windowsHide: true }, callback)`. Its file methods use `readdirSync`, `readFileSync`, `mkdirSync`, and `writeFileSync`, returning the same success/error shape as the SSH adapter. Its terminal method is exactly:

```js
async openTerminal() {
  return {
    ok: false,
    error: 'terminal_unavailable',
    detail: 'legacy local-process nodes do not provide a PTY; configure this machine as an SSH node',
  };
}
```

Export `createLocalProcessNodeTransport`. Accept `execFileImpl`, `fsImpl`, and `pathImpl` injections with built-in defaults so tests never execute a real binary or mutate the developer filesystem.

- [ ] **Step 5: Run adapter tests and syntax checks**

Run:

```bash
cd apps/cam-desktop
node test/node-transport.test.cjs
node --check electron/node-transport.cjs
node --check electron/ssh-node-transport.cjs
node --check electron/local-process-node-transport.cjs
```

Expected: all commands exit 0; the test prints the decoder/factory success line and no real SSH connection is attempted.

- [ ] **Step 6: Commit the adapters after authorization**

```bash
git add apps/cam-desktop/electron/ssh-node-transport.cjs apps/cam-desktop/electron/local-process-node-transport.cjs apps/cam-desktop/test/node-transport.test.cjs
git commit -m "refactor(desktop): add node transport adapters"
```

### Task 3: Route camc and Skillm operations through NodeTransport

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `apps/cam-desktop/test/hub.test.cjs`

**Interfaces:**
- Consumes: `createNodeTransport`, `createSshNodeTransport`, and `createLocalProcessNodeTransport`.
- Produces: `_transportForContext(ctx, options) -> factory result`.
- Produces: `_runToolOnContext(ctx, tool, argv, options) -> normalized result`.
- Preserves every current camc and Skillm HTTP response shape.

- [ ] **Step 1: Add failing Hub tests for lifecycle command mapping**

Change the Hub test harness to accept a `nodeTransportFactories` injection and record `runTool` calls. Add these assertions:

```js
const toolCalls = [];
function fakeTransport() {
  return {
    runTool: async (tool, argv, options) => {
      toolCalls.push({ tool, argv, options });
      return { ok: true, stdout: '', stderr: '' };
    },
    ensureDirectory: async () => ({ ok: true }),
    listFiles: async () => ({ ok: true, entries: [] }),
    readFile: async () => ({ ok: false, error: 'not_found', detail: 'missing' }),
    writeFile: async () => ({ ok: true, bytes: 0 }),
    openTerminal: async () => ({ ok: false, error: 'terminal_unavailable', detail: 'test' }),
  };
}

r = await request('DELETE', '/api/agents/abcdef12?force=false');
eq('desktop stop accepted', r.status, 200);
assert.deepEqual(toolCalls.at(-1).argv, ['stop', 'abcdef12']);

r = await request('DELETE', '/api/agents/abcdef12/history');
eq('desktop remove accepted', r.status, 200);
assert.deepEqual(toolCalls.at(-1).argv, ['rm', 'abcdef12', '--kill']);
```

Seed an active agent record before Stop and restore it before Remove so neither path returns its existing terminal-state no-op.

- [ ] **Step 2: Run Hub tests and verify lifecycle still bypasses the fake adapter**

Run: `cd apps/cam-desktop && npm run test:hub`

Expected: FAIL because `configure()` does not accept `nodeTransportFactories` and `_execCamcOnContext()` still calls `_sshTransport` directly.

- [ ] **Step 3: Add the single Hub factory wrapper**

At module configuration scope, import the three Task 1/2 modules and add a test-only factory override:

```js
let _nodeTransportFactoryOverrides = null;

function configure({ credentialStore, sshTransport, nodeTransportFactories } = {}) {
  if (credentialStore !== undefined) _credentialStore = credentialStore || null;
  if (sshTransport !== undefined) _sshTransport = sshTransport || null;
  if (nodeTransportFactories !== undefined) {
    _nodeTransportFactoryOverrides = nodeTransportFactories || null;
  }
}

function _transportForContext(ctx, { overrides = {} } = {}) {
  const factories = _nodeTransportFactoryOverrides || {
    ssh2: context => createSshNodeTransport({
      context,
      connectOptions: _connectOptionsForContext(context, overrides),
      sshTransport: _sshTransport,
      resolveTool: _resolveSshTool,
      prepareTerminal: _prepareSshTerminal,
    }),
    'local-process': context => createLocalProcessNodeTransport({
      context,
      resolveTool: _resolveLocalTool,
    }),
  };
  return createNodeTransport(ctx, { factories });
}

async function _runToolOnContext(ctx, tool, argv, options = {}) {
  const built = _transportForContext(ctx, options);
  if (!built.ok) return built;
  return built.transport.runTool(tool, argv, options);
}
```

Rename `_sshBaseOptsForContext` to `_connectOptionsForContext`. It validates required endpoint/auth fields but does not inspect transport type. Move `_ensureRemoteCamc` and `_ensureRemoteSkillm` selection behind `_resolveSshTool(tool, connectOptions)`; it returns `{ ok: true, path }` with `~/.cam/camc` or `~/.cam/skillm`.

- [ ] **Step 4: Replace all camc execution call sites**

Replace `_execCamcOnContext(ctx, args, options)` with `_runToolOnContext(ctx, 'camc', args, options)` in these exact functions:

```text
_editAgent
_stopAgent
_removeAgent
_listAgentCron
_addAgentLoop
_addAgentCron
_removeAgentCron
_sendAgentKey
_captureAgentOutput
_sendAgentInput
```

For `_sendAgentInput`, pass the message through `options.stdin` and keep `['send', agentId, '--stdin']` plus optional `--no-enter`; remove its duplicate SSH credential and `execRemote` block.

Delete `_execCamcOnContext` after `grep -n "_execCamcOnContext" embedded-hub.cjs` returns no callers.

- [ ] **Step 5: Replace the Skillm executor**

Implement `_execSkillmOnContext` as a thin normalized call:

```js
async function _execSkillmOnContext(ctx, args, { token = '', timeoutMs = SKILLM_DEFAULT_TIMEOUT_MS, cwd = null } = {}) {
  const env = token ? { SKILLM_TOKEN: token } : {};
  const result = await _runToolOnContext(ctx, 'skillm', args, { timeoutMs, cwd: cwd || '', env, token });
  if (!result.ok) {
    return { ...result, detail: _skillmRedact(result.detail || result.stderr || 'skillm failed', token) };
  }
  return { ...result, stdout: _skillmRedact(result.stdout, token), stderr: _skillmRedact(result.stderr, token) };
}
```

Update `runTool` options in both adapters to accept an `env` object. The local adapter passes it through `execFile` as `{ ...process.env, ...env }`; the SSH adapter passes only allowlisted environment names through a shell-quoted `env NAME=value` prefix. Initially allow only `SKILLM_TOKEN` and reject any other key with `invalid_env`.

- [ ] **Step 6: Run focused command-routing tests**

Run:

```bash
cd apps/cam-desktop
npm run test:hub
node test/node-transport.test.cjs
npm run lint:electron
```

Expected: all pass; Stop records `camc stop`, Remove records `camc rm --kill`, and the test fake receives no SSH options.

- [ ] **Step 7: Commit the execution migration after authorization**

```bash
git add apps/cam-desktop/electron/embedded-hub.cjs apps/cam-desktop/test/hub.test.cjs apps/cam-desktop/electron/ssh-node-transport.cjs apps/cam-desktop/electron/local-process-node-transport.cjs apps/cam-desktop/test/node-transport.test.cjs
git commit -m "refactor(hub): route agent commands through node transport"
```

### Task 4: Unify sync, start, and API-model discovery

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `apps/cam-desktop/test/hub.test.cjs`

**Interfaces:**
- Produces: one `_syncContextAgents(ctx, overrides)` for every driver.
- Produces: one `_startAgent(body, ctx, overrides)` for every driver.
- Produces: one `_getApiModels(query)` execution path through `runTool('camc', ...)`.
- Removes: `_ensureLocalContext`, `_runLocalCamcList`, `_syncLocalAgents`, `_localCamcStatus`, `_startLocalAgent`, `_startRemoteAgent`, and `_localCamcJson`.

- [ ] **Step 1: Replace environment-dependent local API-model testing with adapter injection**

Delete the Hub test that accepts either real local camc output or a missing-camc error. Add deterministic fake-adapter responses for:

```js
if (tool === 'camc' && argv.join(' ') === '--json api list --all') {
  return { ok: true, stdout: JSON.stringify([{ name: 'corp', tool: 'claude', enabled: true }]), stderr: '' };
}
if (tool === 'camc' && argv.join(' ') === 'api default show --json') {
  return { ok: true, stdout: JSON.stringify([{ tool: 'claude', api: 'corp', mode: 'api' }]), stderr: '' };
}
```

Assert `GET /api/api-models?context=ren01` returns those exact arrays. Add a `local-process` legacy context fixture and run the same assertion against it, proving the route is driver-independent.

- [ ] **Step 2: Run Hub tests and verify the duplicate paths are still active**

Run: `cd apps/cam-desktop && npm run test:hub`

Expected: FAIL because local API-model requests still call `_localCamcJson` and agent start still chooses `_startRemoteAgent` versus `_startLocalAgent`.

- [ ] **Step 3: Make synchronization driver-neutral**

In `_syncContextAgents`, remove the `ctx.machine.type !== 'ssh'` guard and direct `_sshTransport` command. Execute:

```js
const result = await _runToolOnContext(ctx, 'camc', ['--json', 'list'], {
  overrides,
  timeoutMs: SYNC_DEFAULT_TIMEOUT_MS,
});
```

Keep the current JSON parsing, normalization, diffing, and `_upsertAgentsForContext` behavior. Change `_syncableAgentContexts()` to dedupe all configured contexts by `decodeNodeTransport(ctx.machine).endpoint`; it must not inspect `machine.type`.

Delete the separate local pass from `_syncAllAgentContexts` and `GET /api/agents`. An empty context list returns `{ ok: true, synced: 0, failed: 0, results: [] }` and does not create a context.

- [ ] **Step 4: Make Start driver-neutral**

Replace `_startLocalAgent` and `_startRemoteAgent` with:

```js
async function _startAgent(body, ctx, overrides = {}) {
  const argv = _buildRunArgv(body);
  const run = await _runToolOnContext(ctx, 'camc', argv, {
    overrides,
    timeoutMs: RUN_REMOTE_TIMEOUT_MS,
  });
  if (!run.ok) return run;
  const agentId = _parseRunAgentId(run.stdout);
  if (!agentId) return { ok: false, error: 'no_agent_id', detail: 'camc run did not print an agent ID' };
  const status = await _runToolOnContext(ctx, 'camc', ['--json', 'status', agentId], {
    overrides,
    timeoutMs: 8000,
  });
  let record = null;
  if (status.ok) {
    try { record = JSON.parse(status.stdout || 'null'); } catch { record = null; }
  }
  if (!record) {
    record = {
      id: agentId,
      status: 'running',
      state: 'initializing',
      task: { tool: String(body.tool || 'claude'), name: String(body.name || ''), prompt: String(body.prompt || '') },
      context_path: String(body.path || ''),
    };
  }
  const normalized = _stampStartRequestFields(_normalizeAgent(record, ctx), body);
  if (normalized?.id) _upsertAgentsForContext(ctx, [normalized]);
  return { ok: true, agentId, record: normalized };
}
```

Change `_resolveStartTarget` to return `{ ok, ctx, overrides }`; remove `baseOpts` and the `nodeKey === 'local'` branch. Inline starts must match a registered endpoint key and use one of its contexts.

- [ ] **Step 5: Make API-model discovery driver-neutral**

Resolve the selected context, obtain its transport, then run the two exact camc argv arrays from Step 1. Remove `_localCamcJson` and every error message claiming the bundled POSIX camc should execute on Windows. Preserve secret redaction and the existing `{ models, defaults, toolSupport, source }` response.

- [ ] **Step 6: Prove all duplicate local execution helpers are gone**

Run:

```bash
grep -n -E "_ensureLocalContext|_runLocalCamcList|_syncLocalAgents|_localCamcStatus|_startLocalAgent|_startRemoteAgent|_localCamcJson" apps/cam-desktop/electron/embedded-hub.cjs
```

Expected: no output and exit status 1.

Then run: `cd apps/cam-desktop && npm run test:hub && npm run test:transport && npm run lint:electron`

Expected: all scripts exit 0.

- [ ] **Step 7: Commit unified discovery/start after authorization**

```bash
git add apps/cam-desktop/electron/embedded-hub.cjs apps/cam-desktop/test/hub.test.cjs
git commit -m "refactor(hub): unify node sync and start execution"
```

### Task 5: Move workspace files, uploads, and terminal attachment behind the contract

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `apps/cam-desktop/electron/main.cjs`
- Modify: `apps/cam-desktop/test/hub.test.cjs`
- Modify: `apps/cam-desktop/test/terminal-follow.test.cjs`

**Interfaces:**
- `_browseList`, `_browseRead`, `_browseAgentWrite`, `_writeAgentSystemPrompt`, and `_uploadAgentFile` consume only NodeTransport file methods.
- Produces: `embeddedHub.openAgentTerminal(agentId, options) -> NodeTransport.openTerminal(...)`.
- Removes: `embeddedHub.getAttachConnectOpts()` and main-process direct `sshTransport.openTerminalChannel()` calls.

- [ ] **Step 1: Add file-method routing tests**

Extend the fake transport with per-method call arrays. Drive these existing routes:

```text
GET  /api/agents/:id/files?path=subdir
GET  /api/agents/:id/files/read?path=README.md
PUT  /api/agents/:id/files
POST /api/agents/:id/upload
PATCH /api/agents/:id with system_prompt
```

Assert that list calls `listFiles`, reads call `readFile`, writes and prompts call `writeFile(..., { createParents: true })`, and uploads call `ensureDirectory` followed by `writeFile`. Assert the fake receives the agent workspace's full path and not a Desktop-local filesystem path.

- [ ] **Step 2: Replace every file `isLocal` branch**

At the top of each file helper, resolve the context transport:

```js
const built = _transportForContext(ctx);
if (!built.ok) return { error: built.error, detail: built.detail };
const transport = built.transport;
```

Use these exact calls:

```js
await transport.listFiles(fullPath);
await transport.readFile(fullPath, { maxBytes: WORKSPACE_FILE_MAX_BYTES });
await transport.writeFile(fullPath, Buffer.from(text, 'utf8'), { createParents: true });
await transport.ensureDirectory(uploadDirectory, { timeoutMs: 60000 });
await transport.listFiles(fullPath, { root });
await transport.readFile(fullPath, { root, maxBytes: WORKSPACE_FILE_MAX_BYTES });
await transport.writeFile(fullPath, Buffer.from(text, 'utf8'), { root, createParents: true });
await transport.ensureDirectory(uploadDirectory, { root, timeoutMs: 60000 });
await transport.writeFile(uploadPath, decoded.content, { root, createParents: false, timeoutMs: 60000 });
- Modify: \`apps/cam-desktop/package.json\`
```

Delete `_localPath`, `_safeLocalPathUnderRoot`, local `fs.readdirSync/readFileSync/writeFileSync` feature branches, and direct `_sshTransport.listRemoteFiles/readRemoteFile/writeRemoteFile` feature calls from `embedded-hub.cjs`. Path validation remains transport-independent and must run before adapter calls.

- [ ] **Step 3: Add a failing terminal delegation assertion**

In `terminal-follow.test.cjs`, read `main.cjs` and assert:

```js
ok('terminal delegates to NodeTransport', source.includes('embeddedHub.openAgentTerminal('));
ok('terminal does not open ssh directly', !source.includes('sshTransport.openTerminalChannel('));
```

In `hub.test.cjs`, call exported `HUB.openAgentTerminal('abcdef12', callbacks)` and assert the fake transport's `openTerminal` receives `{ agentId, cols, rows, onData, onClose }`.

- [ ] **Step 4: Move terminal opening into the Hub transport boundary**

Replace `getAttachConnectOpts` with:

```js
async function openAgentTerminal(agentId, options = {}) {
  const resolved = _contextForAgent(agentId);
  if (resolved.error) return { ok: false, error: resolved.error, detail: `agent ${agentId}: ${resolved.error}` };
  const built = _transportForContext(resolved.ctx);
  if (!built.ok) return built;
  return built.transport.openTerminal({
    agentId: resolved.agent?.id || agentId,
    cols: options.cols,
    rows: options.rows,
    onData: options.onData,
    onClose: options.onClose,
  });
}
```

Export `openAgentTerminal`. In `main.cjs`, retain terminal session caching and renderer event routing, but replace connect-option resolution, `_repairRemoteTerminalSize`, and `sshTransport.openTerminalChannel` with one `embeddedHub.openAgentTerminal(...)` call. Store only `{ dispose, write, resize, contentsId, agentId }` in `_terminals`; remove the SSH-only `opts` field.

- [ ] **Step 5: Run file and terminal tests**

Run:

```bash
cd apps/cam-desktop
npm run test:hub
npm run test:term
npm run test:transport
npm run lint:electron
```

Expected: all pass; no test accesses real SSH, SFTP, camc, or the repository filesystem for simulated agent files.

- [ ] **Step 6: Commit file and terminal migration after authorization**

```bash
git add apps/cam-desktop/electron/embedded-hub.cjs apps/cam-desktop/electron/main.cjs apps/cam-desktop/test/hub.test.cjs apps/cam-desktop/test/terminal-follow.test.cjs
git commit -m "refactor(desktop): unify node files and terminal transport"
```

### Task 6: Normalize persisted nodes once and remove special-local UI behavior

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `web/js/desktop/start-agent-mode.js`
- Modify: `web/js/desktop/app.js`
- Modify: `web/js/desktop/skills-mode.js`
- Modify: `web/js/shared/node-host-meta.js`
- Modify: `web/js/shared/nodes-mode.js`
- Create: `apps/cam-desktop/test/node-transport-architecture.test.cjs`
- Modify: `apps/cam-desktop/test/node-transport.test.cjs`

**Interfaces:**
- Store load converts legacy `type: local` to synthetic endpoint `user@127.0.0.1:22` with `transport_driver: local-process`.
- Store load converts legacy `type: ssh` to `transport_driver: ssh2` without changing host/user/port/auth.
- Context creation accepts only host+user SSH nodes and persists `type: ssh`, `transport_driver: ssh2`.
- Node selectors derive exclusively from configured contexts and may be empty.

- [ ] **Step 1: Add store-normalization tests**

Extract a pure `normalizeStoreNodeTransports(store, { localUser })` export from `node-transport.cjs` and test this input:

```js
const store = {
  contexts: [
    { id: 'a', name: 'old-local', machine: { type: 'local', host: '', user: '', port: null } },
    { id: 'b', name: 'pdx', machine: { type: 'ssh', host: 'pdx098', user: 'hren', port: 3422 } },
  ],
  agents: [
    { id: '11111111', context_name: 'old-local', machine_type: 'local', machine_host: '' },
  ],
};
```

Assert context `a` receives the synthetic endpoint and `local-process`, context `b` receives `ssh2`, and the agent receives `machine_type: 'ssh'`, `transport_driver: 'local-process'`, `machine_host: '127.0.0.1'`, `machine_user: localUser`, and `machine_port: 22`.

- [ ] **Step 2: Normalize once during `loadStore`**

After parsing `embedded-hub.json`, call:

```js
const normalized = normalizeStoreNodeTransports(state.store, { localUser: os.userInfo().username });
state.store = normalized.store;
if (normalized.changed) saveStore();
```

Remove `state.localSyncInFlight`, `LOCAL_CONTEXT_NAME`, and `LOCAL_SYNC_TIMEOUT_MS`. Change `buildContextRecord` so an empty host returns `missing_host`, an empty user returns `missing_user`, and every new machine contains:

```js
{
  type: 'ssh',
  transport_driver: 'ssh2',
  host,
  user,
  port: port || 22,
  key_file,
  env_setup: envSetup,
  auth_method: authMethod,
}
```

`applyContextUpdate` keeps the existing `transport_driver`; it never derives transport from whether host is empty.

- [ ] **Step 3: Remove the synthetic local option from Start**

In `refreshNodeOptions`, derive entries only from configured contexts. For an empty registry render:

```js
nodeSel.innerHTML = '<option value="">(no nodes configured)</option>';
nodeSel.value = '';
nodeSel.disabled = true;
setStatus('Add an SSH node on the Nodes page before starting an agent.', 'is-error');
```

For a non-empty registry, enable the select and preserve the current selection or select the first endpoint. Remove every fallback `|| 'local'`. `readForm()` must refuse an inline start when no node is selected instead of sending `node: 'local'`.

- [ ] **Step 4: Remove renderer transport decoding**

Apply these exact rules:

- `node-host-meta.js`: `hostKeyForMachine` always returns `${user}@${host}:${normalizePort(port, true)}` and returns an empty key when host or user is missing.
- `nodes-mode.js`: every registered context creates a normal node card; use a neutral `Node` badge, not `SSH` versus `local`; group by the shared endpoint key.
- `skills-mode.js`: remove `isSshContext`; show every context and let Hub capability/error responses decide availability.
- `app.js`: `representativeSyncContexts` dedupes non-empty endpoint keys and does not filter `m.type`.
- Agent filters use `machine_host` as an endpoint string and do not synthesize the word `local` for missing data.

- [ ] **Step 5: Add a source architecture guard**

Create `apps/cam-desktop/test/node-transport-architecture.test.cjs`:

```js
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..', '..', '..');
const files = [
  'apps/cam-desktop/electron/embedded-hub.cjs',
  'apps/cam-desktop/electron/main.cjs',
  'web/js/desktop/start-agent-mode.js',
  'web/js/desktop/app.js',
  'web/js/desktop/skills-mode.js',
  'web/js/shared/node-host-meta.js',
  'web/js/shared/nodes-mode.js',
];
const forbidden = [
  /machine\.type\s*[!=]==?\s*['"](?:ssh|local)['"]/,
  /\b(?:m|mm)\.type\s*[!=]==?\s*['"](?:ssh|local)['"]/,
  /\((?:m|mm)\.type\s*\|\|[^)]*\)\s*[!=]==?/,
  /machine_type\s*[!=]==?\s*['"](?:ssh|local)['"]/,
  /transport_type\s*[!=]==?\s*['"](?:ssh|local)['"]/,
  /\bisLocal\b/,
  /nodeKey\s*===?\s*['"]local['"]/,
  /seen\.set\(['"]local['"]/,
  /_syncLocalAgents|_startLocalAgent|_localCamcJson/,
];

for (const rel of files) {
  const source = fs.readFileSync(path.join(root, rel), 'utf8');
  for (const pattern of forbidden) {
    assert.equal(pattern.test(source), false, `${rel} violates NodeTransport boundary: ${pattern}`);
  }
}
console.log('node transport architecture guard passed');
```

The only files allowed to mention `local-process` are `node-transport.cjs`, `local-process-node-transport.cjs`, and their tests.

Change the package script to include the guard after the file exists:

\`\`\`json
"test:transport": "node test/node-transport.test.cjs && node test/node-transport-architecture.test.cjs"
\`\`\`

- [ ] **Step 6: Run migration and renderer guards**

Run:

```bash
cd apps/cam-desktop
npm run test:transport
npm run test:hub
npm run lint:electron
```

Expected: all pass. Manually inspect one normalized legacy fixture and confirm it is presented as `hren@127.0.0.1:22`, while a real loopback SSH node has the same endpoint presentation but driver `ssh2` internally.

- [ ] **Step 7: Commit normalization and UI cleanup after authorization**

```bash
git add apps/cam-desktop/electron/embedded-hub.cjs web/js/desktop/start-agent-mode.js web/js/desktop/app.js web/js/desktop/skills-mode.js web/js/shared/node-host-meta.js web/js/shared/nodes-mode.js apps/cam-desktop/test/node-transport.test.cjs apps/cam-desktop/test/node-transport-architecture.test.cjs apps/cam-desktop/package.json
git commit -m "refactor(desktop): remove special local node behavior"
```

### Task 7: Update architecture documentation and run full verification

**Files:**
- Modify: `apps/cam-desktop/FIXES-NODES.md`
- Modify: `docs/desktop/requirements.md`
- Modify: `docs/superpowers/plans/2026-07-15-start-local-runtime-implementation.md`
- Verify: all files changed by Tasks 1–6

**Interfaces:**
- Documents the invariant `Context -> createNodeTransport() -> NodeTransport`.
- Documents loopback nodes as ordinary SSH endpoints.
- Documents `local-process` as load-only compatibility, not a product mode.

- [ ] **Step 1: Add the architecture contract to requirements**

Add an approved requirement with this normative text:

```text
CAM-DESK-NODETRANSPORT-001: Every Desktop agent/context operation resolves one NodeTransport through the central factory. Feature code and renderers do not inspect transport type. New nodes, including loopback endpoints, use SSH. Legacy direct-local records are normalized once and may use the local-process compatibility adapter behind the same contract.
```

Include the method list and normalized error shape from Task 1. State that the existing `ssh2` pool remains the SSH implementation.

- [ ] **Step 2: Mark conflicting local-ingestion documentation as historical**

At the top of the local-ingestion section in `FIXES-NODES.md`, add:

```markdown
> Superseded by CAM-DESK-NODETRANSPORT-001. The automatic non-SSH local context described below is historical and must not be recreated by current Desktop code.
```

At the top of `2026-07-15-start-local-runtime-implementation.md`, add:

```markdown
> Superseded in part by `2026-07-16-node-transport-architecture.md`: do not retain or auto-create a special local Node. Loopback execution is configured as an SSH node; legacy local records are handled only by the NodeTransport compatibility adapter.
```

- [ ] **Step 3: Run every focused Desktop test**

Run:

```bash
cd apps/cam-desktop
npm run test:transport
npm run test:hub
npm run test:term
node test/mobile-terminal-input-routing.test.cjs
npm run lint:electron
```

Expected: every command exits 0.

- [ ] **Step 4: Run repository-level safety checks**

Run from the repository root:

```bash
git diff --check
grep -R -n -E "_syncLocalAgents|_startLocalAgent|_localCamcJson|seen\.set\(['\"]local['\"]" apps/cam-desktop/electron web/js/desktop web/js/shared --exclude-dir=node_modules
git status --short
```

Expected: `git diff --check` exits 0; `grep` prints nothing; `git status` shows only files intentionally changed by this implementation plus pre-existing unrelated user changes.

- [ ] **Step 5: Perform three smoke scenarios without building an MSI**

Use deterministic test nodes or a development launch:

1. Remote SSH node such as `hren@pdx098:3422`: sync, Start, send input, capture output, Stop, Remove, Browse, upload, Skillm list, and terminal attach all succeed.
2. Loopback SSH node such as `hren@127.0.0.1:22`: the same actions follow the SSH adapter and display no special local badge.
3. Migrated legacy local record: it appears as `user@127.0.0.1:22`; sync/start/files use the compatibility adapter; terminal returns the normalized `terminal_unavailable` message without detaching another session or crashing the renderer.

Expected: no feature chooses behavior by checking transport type, and the SSH pool is reused across repeated operations.

- [ ] **Step 6: Commit documentation and final verification after authorization**

```bash
git add apps/cam-desktop/FIXES-NODES.md docs/desktop/requirements.md docs/superpowers/plans/2026-07-15-start-local-runtime-implementation.md
git commit -m "docs(desktop): define unified node transport architecture"
```

Do not build, publish, push, or release an MSI unless the user separately requests it.

## Self-review

- Spec coverage: the plan reduces transport decoding to `node-transport.cjs`, preserves real SSH pooling, keeps a compatibility adapter, makes loopback ordinary SSH, removes automatic local creation, and migrates commands, files, terminal, store, and UI.
- Boundary coverage: camc lifecycle/send/capture/cron, Skillm, sync, Start, API models, Browse, system prompt, uploads, and terminal all have an explicit migration task.
- Regression coverage: deterministic factory/adapter tests, Hub API tests, terminal delegation tests, source architecture guards, syntax checks, and three smoke scenarios are included.
- Placeholder scan: the plan contains no deferred implementation fields or unspecified error handling. The local terminal limitation is an explicit approved constraint with a stable error.
- Type consistency: all tasks use `transport_driver`, `ssh2`, `local-process`, `runTool`, `ensureDirectory`, `listFiles`, `readFile`, `writeFile`, and `openTerminal` with the same spelling and result shape.
- Scope check: this plan changes Desktop's transport architecture only. It does not redesign Mobile, Relay, camc internals, tmux, or tool-runtime APIs.
