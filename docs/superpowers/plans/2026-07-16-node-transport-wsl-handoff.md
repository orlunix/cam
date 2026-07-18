# NodeTransport WSL Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` task-by-task. Do not run two implementation agents in the same worktree.

**Goal:** Finish and verify the Desktop NodeTransport migration: all Desktop command, workspace-file, upload, and terminal operations use one selected transport while Windows local uses default WSL, Linux/macOS local uses native processes, and explicit SSH remains pooled SSH.

**Working tree:** `/home/hren/gitlab/cam/.worktrees/node-transport-wsl` on `codex/node-transport-wsl`.

**Current state:** Uncommitted only. No push. The previous implementation agent was stopped for this handoff. Do not reset, checkout, clean, or discard the existing diff.

**Architecture:** `machine.type` is persisted as only `local` or `ssh`. Adapter selection is `ssh -> ssh2`, `local + win32 -> wsl`, `local + linux/darwin -> local-process`. The renderer keeps the existing IPC shapes; Electron main owns only terminal session caching and delegates opening to the Hub.

**Tech stack:** Electron CommonJS, ssh2 shared pool, node-pty 1.1.0, `wsl.exe --exec`, Python 3 in WSL, tmux, CAMC.

## Non-negotiable constraints

- Do not change Mobile, browser UI layout, tmux protocol, Relay, or Node schema beyond existing `machine.type`.
- Explicit SSH, including `127.0.0.1`, `localhost`, and `::1`, always uses the SSH adapter. It must never be silently treated as local.
- Windows local uses `wsl.exe --exec` without `-d`. Do not add distro selection, installation, or authentication UI.
- Linux/macOS local uses native processes. Do not import or invoke WSL on those platforms.
- The local node stays. It is not loopback SSH.
- User prompt/input is stdin or argv only. Never interpolate it into a shell command.
- Authentication is performed inside the newly created session. Start passes hidden CAMC `--allow-interactive-setup`; missing executables remain failures, but tool login/version probes are warnings for that launch.
- Do not commit or push unless the user explicitly asks.

## Completed work (reviewed before handoff)

1. **NodeTransport contract and adapters exist.**
   - `apps/cam-desktop/electron/node-transport.cjs`
   - `apps/cam-desktop/electron/ssh-node-transport.cjs`
   - `apps/cam-desktop/electron/local-process-node-transport.cjs`
   - `apps/cam-desktop/electron/wsl-node-transport.cjs`
   - `apps/cam-desktop/electron/pty-terminal.cjs`
   - SSH adapter preserves the existing pooled `ssh-transport.cjs`; WSL has atomic bundled CAMC/Skillm installation and Python-backed file operations; local has guarded native filesystem operations.

2. **CAMC interactive-start support is implemented.**
   - `src/camc_pkg/runtime_env.py`, `src/camc_pkg/cli.py`, tests, and generated `src/camc` / `dist/camc` include hidden `run --allow-interactive-setup`.
   - Existing strict readiness stays the default; only Desktop Start asks for advisory interactive setup.

3. **Hub command migration is implemented.**
   - Sync, Start, send via `--stdin`, capture, stop/remove, cron, API models, and Skillm route through the contract.
   - Empty Start prompt is accepted and Desktop Start wording calls local Windows “default WSL”.
   - SSH readiness/bootstrap remains direct infrastructure around the adapter, not a user-facing command route.

4. **Task 6 migration is implemented but has not received final review.**
   - Workspace list/read/write, upload, and system-prompt writes use transport methods.
   - `embeddedHub.openAgentTerminal(agentId, options)` selects the agent transport and opens it.
   - `main.cjs` keeps the opaque terminal session cache and `term:data` / `term:status`, but opens channels through `embeddedHub.openAgentTerminal`.
   - `preload.cjs` IPC shape is unchanged; comments now describe NodeTransport.

5. **Two P0 issues found during review were fixed and must be retained.**
   - SSH executable paths such as `~/.cam/camc` are now emitted as an expandable `"$HOME/.cam/camc"`; quoting a literal `~` breaks real SSH commands.
   - Remote terminal preparation uses `os.path.expanduser("~/.cam/camc")`, removes malformed tiny tmux clients, and cleans up the tmux control-mode client after `refresh-client -C`.

## Latest known test results

These were run after the P0 fixes, but no complete final verification has yet run:

- `cd apps/cam-desktop && npm run test:hub` -> `93 passed, 0 failed`
- `cd apps/cam-desktop && npm run test:term` -> `20 passed, 0 failed`
- `cd apps/cam-desktop && node test/node-transport.test.cjs` -> `node transport adapters passed`
- Earlier task checks passed: focused Python runtime tests, Electron syntax, lint, and `git diff --check`.

Do **not** claim completion from those results. Re-run the complete verification in Task 3 after the architecture cleanup below.

## Current diff

- Tracked files currently show roughly `805 insertions / 874 deletions`; new adapter files are not included in that stat.
- Most churn in `embedded-hub.cjs` and `main.cjs` is deliberate removal of duplicated local/SSH feature branches.
- The new files plus tests are substantial. Do not add features or widen scope further.

## Task 1: Stabilize and review the existing Task 6 result

**Files:**
- Review: `apps/cam-desktop/electron/embedded-hub.cjs`
- Review: `apps/cam-desktop/electron/main.cjs`
- Review: `apps/cam-desktop/electron/preload.cjs`
- Review: `apps/cam-desktop/test/hub.test.cjs`
- Review: `apps/cam-desktop/test/terminal-follow.test.cjs`
- Create/update: `.superpowers/sdd/task-6-report.md`

- [ ] **Step 1: Ensure only one implementation agent edits the worktree**

Run:

```bash
cd /home/hren/gitlab/cam/.worktrees/node-transport-wsl
camc status 7b5e5a43
git status --short
```

Expected: the previous agent is idle/stopped and the existing uncommitted files remain intact. If it is editing, stop it before modifying anything.

- [ ] **Step 2: Re-run the focused Task 6 tests before changing code**

Run:

```bash
cd apps/cam-desktop
npm run test:hub
npm run test:term
node test/node-transport.test.cjs
```

Expected: `93 passed, 0 failed`; `20 passed, 0 failed`; `node transport adapters passed`.

- [ ] **Step 3: Check the terminal contract rather than rewriting it**

Keep these facts true:

```js
// apps/cam-desktop/electron/main.cjs
const ch = await embeddedHub.openAgentTerminal(agentId, {
  cols, rows, onData, onClose,
});
// cached entry contains only dispose/write/resize/contentsId/agentId
```

`termOpen` must not call `sshTransport.openTerminalChannel`, `getAttachConnectOpts`, or a remote size-repair helper. It must preserve `term:data` and `term:status` payloads exactly. Make the failure guard robust:

```js
if (!ch || !ch.ok) {
  return { ok: false, error: ch?.error || 'terminal_open_failed', detail: ch?.detail || 'terminal open failed' };
}
```

This prevents Electron IPC from throwing “reply was never sent” if a future adapter returns an invalid result.

- [ ] **Step 4: Verify file-operation argument parity**

The Hub must call exactly these transport shapes after lexical subpath validation:

```js
transport.listFiles(fullPath, { root });
transport.readFile(fullPath, { root, maxBytes: WORKSPACE_FILE_MAX_BYTES });
transport.writeFile(fullPath, Buffer.from(text, 'utf8'), { root, createParents: true });
transport.ensureDirectory(uploadDir, { root, timeoutMs: 60000 });
transport.writeFile(uploadPath, decoded.content, { root, timeoutMs: 60000 });
```

System-prompt writes must use `writeFile(fullPath, Buffer.from(nextText, 'utf8'), { root, createParents: true })`. Do not restore `isLocal`, `_localPath`, direct `fs` workspace writes, or direct SFTP feature branches.

- [ ] **Step 5: Keep the two real-SSH fixes covered**

`ssh-node-transport.cjs` must retain an executable renderer that turns only trusted `~/...` or `$HOME/...` tool paths into `"$HOME/..."`; arguments still use single-quote escaping. Test both CAMC and Skillm commands with a `cwd`, because tool resolution must remain correct after `cd <workspace>`.

`_prepareRemoteTerminal` must retain all of the following:

```python
camc = os.path.expanduser('~/.cam/camc')
# list-clients; detach clients with width < 40 or height < 4
# control mode: refresh-client -C <cols>,<rows>
# on timeout: write detach-client, wait, then kill as final cleanup
```

- [ ] **Step 6: Record Task 6 accurately**

Write `.superpowers/sdd/task-6-report.md` with the valid RED history, the three current focused test results, the P0 `~` quoting fix, and a statement that no commit/push happened.

## Task 2: Fix the architecture boundary before adding the guard test

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Create: `apps/cam-desktop/test/node-transport-architecture.test.cjs`
- Modify: `apps/cam-desktop/package.json`

**Problem to fix first:** the current Hub still has direct SSH topology inspection in `_nodeTransportForContext` / `_sshBaseOptsForContext`, and the string/error `not_ssh` remains. A naive guard test will fail. Do not hide this by weakening the guard.

- [ ] **Step 1: Write a failing architecture test**

The test should read `embedded-hub.cjs` and `main.cjs` and reject execution-level patterns:

```js
const prohibitedHub = [
  'machine.type ===', 'machine.type !==', 'isLocal', '_localPath',
  '_safeLocalPathUnderRoot', 'getAttachConnectOpts',
];
const prohibitedMain = [
  'sshTransport.openTerminalChannel(', 'getAttachConnectOpts',
  '_repairRemoteTerminalSize',
];
```

Allow direct `_sshTransport.execRemote` / file calls only in the remote CAMC/Skillm readiness deployment helpers and `_prepareRemoteTerminal`; these are infrastructure, not user command/file routes. Also assert `node-transport.cjs` contains no distro field or `transport_driver`.

Run:

```bash
cd apps/cam-desktop
node test/node-transport-architecture.test.cjs
```

Expected before cleanup: FAIL because Hub still checks `ctx.machine.type === 'ssh'` and contains `not_ssh`.

- [ ] **Step 2: Remove Hub-side selection branches without losing credentials**

Refactor `_nodeTransportForContext` so it never preselects SSH with `ctx.machine.type`. The factory must perform the selection through `createNodeTransport`; the `ssh2` factory callback can build SSH options because it is called only after the contract selected SSH.

Use this shape:

```js
function _defaultNodeTransportFactories(timeoutMs, overrides = {}) {
  return {
    ssh2: context => {
      const built = _sshBaseOptsForContext(context, timeoutMs, overrides);
      if (built.error) return invalidTransport(built); // implements all contract methods returning built
      return createConfiguredSshTransport(context, built.opts);
    },
    wsl: context => createWslNodeTransport(/* existing options */),
    'local-process': context => createLocalProcessNodeTransport(/* existing options */),
  };
}
```

`_sshBaseOptsForContext` is then an SSH-factory credential helper: remove its `machine.type` selection check and replace the exposed `not_ssh` error with a neutral internal error only if it is ever called with malformed data. Update HTTP bad-error lists accordingly. Preserve one-shot password/passphrase forwarding.

Add regression tests proving:

- local factory selection never asks for an SSH credential;
- SSH including loopback receives host/user/port/auth credentials;
- the original 93 Hub assertions still pass.

- [ ] **Step 3: Make the guard green and wire it into package scripts**

Set `test:transport` to run both transport tests:

```json
"test:transport": "node test/node-transport.test.cjs && node test/node-transport-architecture.test.cjs"
```

Run:

```bash
cd apps/cam-desktop
npm run test:transport
npm run test:hub
npm run test:term
```

Expected: all pass. Do not accept a guard that simply excludes the entire Hub source file.

## Task 3: Documentation and full source verification

**Files:**
- Modify: `docs/desktop/requirements.md`
- Modify: `apps/cam-desktop/FIXES-NODES.md`
- Update: `.superpowers/sdd/progress.md`

- [ ] **Step 1: Add the normative requirement verbatim**

Add to `docs/desktop/requirements.md`:

```text
CAM-DESK-NODETRANSPORT-001: Every Desktop command, file, and terminal operation resolves one NodeTransport in Electron main. Persisted machine.type is local or ssh. Windows local uses the default WSL environment, Linux/macOS local uses native processes, and SSH uses the pooled ssh2 adapter even for loopback hosts. Authentication is completed interactively inside the created agent session and is not a Desktop Start preflight.
```

- [ ] **Step 2: Update migration evidence**

In `apps/cam-desktop/FIXES-NODES.md`, document: before/after files, local WSL behavior, SSH pool preservation, loopback SSH behavior, interactive-start flag, and focused test evidence. State that Mobile was not modified.

- [ ] **Step 3: Run the full source gate**

Run exactly:

```bash
cd /home/hren/gitlab/cam/.worktrees/node-transport-wsl
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

Expected: every command exits 0. If full pytest cannot collect because `fastapi` / `websockets` are missing, record that as an environment baseline, then run the collectable runtime subset; do not label unrelated pre-existing test failures as caused by this diff.

## Task 4: Final review and Windows MSI gate

**Do this only after Tasks 1-3 are green.**

- [ ] **Step 1: Final diff review**

Check all user-facing command/file/terminal paths use NodeTransport. Confirm the only direct SSH calls are readiness/deployment and adapter-owned terminal preparation. Confirm no Mobile files changed. Do not accept new features.

- [ ] **Step 2: Windows build verification**

Copy only the reviewed patch to the established Windows checkout `/mnt/c/Users/hren/gitlab/cam`; preserve any unrelated Windows worktree changes. Use the existing build path:

```bash
ssh -p 2222 hren@127.0.0.1 'bash ~/cam-build-from-wsl.sh'
```

Expected artifact: `apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi` (or the script's dated test-build variant). Inspect the produced package and confirm `app.asar.unpacked/node_modules/node-pty` includes a Windows `.node` binary.

Install/smoke only if a displayed Windows session is available:

- local Windows node starts a WSL agent with empty prompt;
- authentication can be completed in Terminal;
- Terminal input, resize, close/reopen work;
- existing SSH terminal still works;
- loopback SSH prompts for SSH credentials and stays SSH.

Do not publish the MSI to Nutstore, commit, or push without explicit user authorization.

## Exact final report format

```text
HANDOFF_STATUS: complete|blocked
TASK_6: pass|fail with test counts
ARCHITECTURE_GUARD: pass|fail
PYTHON_RUNTIME: pass|baseline-blocked|fail
ELECTRON_TESTS: transport/hub/term/lint exact results
MSI: not-run|built|smoked and exact artifact path
UNCOMMITTED_FILES: list
NO_COMMIT_OR_PUSH: yes|no
RISKS_OR_BLOCKERS: concise
```

