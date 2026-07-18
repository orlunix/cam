# Fast-Switch Implementation Briefing — camui-fast-switch

Manager → fix agent. Read this whole file before doing anything.

## Your job (one sentence)
Implement the agent fast-switch feature for CAM-Desktop, build the
MSI on WSL, grep-verify the fix is bundled, release it as a **test
build** to Nutstore, commit on prgn, then **STOP and report**.

**Do NOT push to GitLab** — that's hren's gate, after install smoke.
**Do NOT run install smoke** — needs a Windows display only hren has.

## Read these FIRST (in order)
1. `docs/desktop/agent-fast-switch-plan.md` — **YOUR IMPLEMENTATION
   PLAN.** Tasks 1–4 have the EXACT code to write (no guessing — copy
   it). Task 5 is the acceptance smoke (deferred to hren). Task 6 is
   the MSI build + release flow (summarized below).
2. `docs/desktop/agent-fast-switch-spec.md` — the WHY (root causes,
   design rationale, the 5 de-risked facts).
3. `~/notes/msi-develop-flow.md` — the FULL build & release mechanics
   (WSL gotchas, patch transfer, naming). Read this before the build
   step.

The plan is the source of truth for code. The build flow below is
extracted from the plan + the develop-flow doc so you can execute
without re-reading, but when in doubt, defer to those docs.

## What to implement (Tasks 1–4 from the plan — exact code is in the plan)

- **Task 1 — Ship the SerializeAddon UMD.** Pin
  `@xterm/addon-serialize@0.14.0` (exact, NO `^`, NO beta) in
  `apps/cam-desktop/package.json`. `npm pack`/curl the UMD to
  `web/vendor/xterm/addon-serialize.js`. Add
  `<script src="vendor/xterm/addon-serialize.js?v=0.64.0">` to
  `web/desktop.html` right after the `addon-fit.js` line (match the
  `?v=0.64.0` cache-bust EXACTLY; CSP is `script-src 'self'`).
- **Task 2 — Load the addon onto each new terminal.** In
  `createTerminalEntry` (`web/js/shared/terminal-mount.js:538-543`),
  AFTER `entry.term.open(container)`, instantiate
  `window.SerializeAddon` (the constructor IS `window.SerializeAddon`,
  **NOT** `window.SerializeAddon.SerializeAddon` — getting this wrong
  throws "not a constructor") and `entry.term.loadAddon(entry.serialize)`.
  Store as `entry.serialize`.
- **Task 3 — Change 1 (the fast path).** In `remountTerminalContainer`
  (`:379-394`), DELETE the orphan-sibling cleanup loop (lines 381–383 —
  under pre-mount those siblings ARE the other cached tabs; leaving it
  in destroys them on every show). In `showTerminalEntry`
  (`:396-418`), guard the remount call:
  `if (hostEl && !hostEl.contains(ent.container)) remountTerminalContainer(...)`.
  Cached switches skip the remount → visibility toggle + one `fit()` =
  0-latency.
- **Task 4 — Change 2 (serialize on evict).** Add
  `const _parkedSnapshots = new Map();` at module top next to
  `terminalSessions`. `evictTerminalCacheIfNeeded` calls
  `disposeTerminalForAgent(id, { evict: true })`. Make
  `disposeTerminalForAgent(agentId, opts = {})` snapshot via
  `ent.serialize.serialize({excludeAltBuffer:true, excludeModes:true, scrollback:5000})`
  to `_parkedSnapshots` BEFORE `closeTerminalSession` + `term.dispose()`
  — only when `opts.evict`. The explicit-close path (no `opts.evict`,
  used by `closeAllTerminalSessions`) is UNCHANGED — shutdown still
  discards. In `createTerminalEntry`'s fresh-create branch, after
  loading the serialize addon:
  `if (_parkedSnapshots.has(agent.id)) { try { entry.term.write(_parkedSnapshots.get(agent.id)); } catch {}; _parkedSnapshots.delete(agent.id); }`.

The plan has the full code for each step. Follow it exactly.

## Constraints (from the plan's Global Constraints — all mandatory)
- **Pin `@xterm/addon-serialize@0.14.0`** (NOT `^`, NOT a `0.15.0-beta.*`).
  Version-coupled to `@xterm/xterm@^6.0.0` (same monorepo commit
  `f447274f`, 2023-11-01). A mismatched version throws at `loadAddon`
  or corrupts the buffer.
- **No layout change, no tab strip.** Sidebar-select UI stays.
  `TERMINAL_CACHE_LIMIT` stays 6.
- **No change to `apps/cam-desktop/electron/ssh-transport.cjs`** or
  `main.cjs`. The SSH pool is already correct: `term:close` closes the
  PTY channel + decrements `inflight`; `_dropEntry` (the only
  `ssh2.Client` destroyer) is NOT called; the control connection
  survives 600s idle. Do not touch it.
- **Cache-bust `?v=0.64.0`** on the new `<script>` must match the
  existing two xterm scripts in `desktop.html`.
- **Commit AFTER MSI grep-verify, NOT before** (per
  `msi-verify-before-commit`). This is hren's hard rule — do not
  commit code that hasn't been built + grep-verified in the MSI.
- **Branch:** `camui-desktop-v2`.

## Build & release flow — hren wants you to OWN this part

### A. After implementing Tasks 1–4, before building: local checks
```bash
cd ~/gitlab/cam
node --check web/js/shared/terminal-mount.js          # the file you touched — must parse
grep -n "_parkedSnapshots\|excludeAltBuffer\|SerializeAddon\|addon-serialize" \
  web/js/shared/terminal-mount.js web/desktop.html \
  apps/cam-desktop/package.json web/vendor/xterm/addon-serialize.js
```
Expected: `node --check` silent (exit 0); grep shows your new symbols
across the four files. If `node --check` fails, fix syntax before
building — don't ship a parse error to the MSI build.

### B. Transfer to the WSL build box (DO NOT push unverified)
The WSL build box is at `:2222` (laptop omni-wfa-q8o5s; repo at
`C:\Users\hren\gitlab\cam` = `/mnt/c/Users/hren/gitlab/cam`). Do NOT
push unverified commits to GitLab — transfer as a patch.

```bash
cd ~/gitlab/cam
# From your local working tree (whether or not you committed locally):
git diff camui-desktop-v2 -- \
  apps/cam-desktop/package.json \
  web/desktop.html \
  web/vendor/xterm/addon-serialize.js \
  web/js/shared/terminal-mount.js > /tmp/fast-switch.patch
# (If you already committed locally, use:
#  git format-patch -1 HEAD --output /tmp/fast-switch.patch -- \
#    apps/cam-desktop/package.json web/desktop.html \
#    web/vendor/xterm/addon-serialize.js web/js/shared/terminal-mount.js)

scp -P 2222 /tmp/fast-switch.patch hren@127.0.0.1:/tmp/fast-switch.patch
ssh -p 2222 hren@127.0.0.1 "cd /mnt/c/Users/hren/gitlab/cam && \
  git apply --whitespace=nowarn /tmp/fast-switch.patch && \
  grep -c SerializeAddon web/js/shared/terminal-mount.js && \
  grep -c addon-serialize web/desktop.html"
```
Expected: non-zero counts. Use `git apply`, **NOT `git am`** — WSL1
can't chmod `/mnt/c`, `git am` fails on `.git/config.lock`. The build
reads the **working tree**, not git state, so an uncommitted `git
apply` is fine.

### C. Build the MSI on WSL
```bash
ssh -p 2222 hren@127.0.0.1 'bash ~/cam-build-from-wsl.sh'
```
This triggers a detached Windows `cam-build.bat` (the
`Start-Process -WindowStyle Hidden` fire-and-forget that defeats the
WSL1→Windows stdio EISDIR bug). It polls `C:\Users\hren\cam-build.log`
for `BUILD_DONE_SENTINEL` and returns when done. Subcommands:
`--status` (is a build running?), `--tail [N]` (tail the log).

MSI lands at `apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi` (the
`dist/` is relative to `apps/cam-desktop/` — easy to miss).
Expected in log: `NPM_INSTALL_EXIT=0`, `MSI_BUILD_EXIT=0`.

**`npm ci` FAILS** (stale lockfile, `@emnapi/wasi-threads@1.2.1`
missing) — the build uses `npm install`. Do NOT commit the regenerated
`package-lock.json` with your fix commit.

### D. Verify the fix is bundled (the build-verify gate — REQUIRED before commit)
```bash
# Extract the MSI (administrative install, no install needed)
ssh -p 2222 hren@127.0.0.1 "/mnt/c/Windows/System32/msiexec.exe /a \
  'C:\Users\hren\gitlab\cam\apps\cam-desktop\dist\CAM-Desktop-0.2.0.msi' \
  /qn TARGETDIR='C:\Users\hren\msi-extract'"
# grep the app.asar (it's grep-able raw — concatenation archive)
ssh -p 2222 hren@127.0.0.1 "grep -a -c SerializeAddon \
  /mnt/c/Users/hren/msi-extract/cam-desktop/resources/app.asar && \
  grep -a -c _parkedSnapshots \
  /mnt/c/Users/hren/msi-extract/cam-desktop/resources/app.asar && \
  grep -a -c excludeAltBuffer \
  /mnt/c/Users/hren/msi-extract/cam-desktop/resources/app.asar"
```
Expected: all three counts non-zero. `SerializeAddon` (addon + load
block), `_parkedSnapshots` (Change 2 state), `excludeAltBuffer`
(serialize options). **If any is 0, the patch didn't apply or the
build cached an old asar — clean `apps/cam-desktop/dist/` and
rebuild. Do NOT commit until all three are non-zero.**

Also grab the MSI sha256 (for the report):
```bash
ssh -p 2222 hren@127.0.0.1 "sha256sum \
  /mnt/c/Users/hren/gitlab/cam/apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi"
```

### E. Release to Nutstore (test build, `-2`)
```bash
ssh -p 2222 hren@127.0.0.1 "cp \
  /mnt/c/Users/hren/gitlab/cam/apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi \
  '/mnt/c/Users/hren/Nutstore/1/Nutstore/app/CAM-Desktop-0.2.0-2-fast-switch-$(date +%Y%m%d).msi'"
```
Release location: `C:\Users\hren\Nutstore\1\Nutstore\app\` (Nutstore
syncs to cloud — canonical release channel). Naming:
`CAM-Desktop-<baseVer>-<n>-<desc>-<YYYYMMDD>.msi`. `-2` = test build
(in-flight, not install-smoke'd). `<desc>` = `fast-switch`. A version
with NO `-n` is the **workable** release — hren drops the `-n` AFTER
install smoke passes. You are releasing a TEST build only.

### F. Commit on prgn (AFTER grep-verify — satisfies msi-verify-before-commit)
```bash
cd ~/gitlab/cam
git add apps/cam-desktop/package.json \
  web/desktop.html \
  web/vendor/xterm/addon-serialize.js \
  web/js/shared/terminal-mount.js
git commit -m "cam-desktop: 0-latency agent fast-switch + serialize-on-eviction

Change 1: stop re-parenting the terminal container on show; cached
switches are now a visibility toggle + one deferred fit() (no reflow).
Drops remountTerminalContainer's orphan-sibling cleanup (under
pre-mount those siblings ARE the other cached tabs).

Change 2: serialize scrollback via @xterm/addon-serialize@0.14.0 on
LRU eviction to a _parkedSnapshots Map; restore on reopen before the
fresh camc-attach channel flows live output. Explicit-close path
unchanged — shutdown discards as before.

Pinned 0.14.0 (version-coupled to @xterm/xterm@^6.0.0, monorepo
commit f447274f). UMD shipped under web/vendor/xterm/ per
CAM-DESK-TERM-002 CSP-self pattern.

MSI build-verified: SerializeAddon/_parkedSnapshots/excludeAltBuffer
all grep-found in app.asar. Released as test build
CAM-Desktop-0.2.0-2-fast-switch-<date>.msi to Nutstore. Install
smoke pending (hren).

Refs: docs/desktop/agent-fast-switch-spec.md,
      docs/desktop/agent-fast-switch-plan.md"
```
**Do NOT commit `package-lock.json`** (regenerated by `npm install`,
tangential). **Do NOT commit `apps/cam-desktop/CLAUDE.md`** if you
created one (untracked briefing artifact — delete it). **Do NOT push**
— hren pushes after install smoke.

### G. STOP and report
Report to hren in your final message:
- The commit hash(es) on `camui-desktop-v2`.
- The MSI file name + sha256.
- The three grep-verify counts (`SerializeAddon`, `_parkedSnapshots`,
  `excludeAltBuffer`).
- The Nutstore release path.
- Confirm `node --check` passed and `package-lock.json` was NOT
  committed.

Then stop. hren will: install the MSI on a Windows box, run the
acceptance smoke (Task 5 of the plan: 6 agents 0-latency switch, 7th
evicts, reopen restores scrollback, hidden tabs don't resize), and
if it passes, push the commit + drop the `-n` for a workable release.

## Git/WSL gotchas (full detail in ~/notes/msi-develop-flow.md)
- **`git am` fails on WSL1** with `chmod on .git/config.lock failed:
  Operation not permitted`. Use `git apply` (no commit, no config
  write). The build reads the working tree, not git state.
- **`npm ci` fails** (stale lockfile). The build uses `npm install`.
  Don't commit the regenerated lockfile.
- **MSI not in `dist/`?** Check `apps/cam-desktop/dist/` —
  electron-builder runs from `apps/cam-desktop/`.
- **MSI is per-user** (`perMachine: false`) — installs into
  `%LOCALAPPDATA%\Programs\cam-desktop` of the installing user.
- **Don't push unverified commits to GitLab** — patch transfer only.
- **glm-5.2 Bash classifier may flap** ("temporarily unavailable, so
  auto mode cannot determine the safety of Bash") — retry Bash ops
  after a brief wait; read-only ops (Read/Grep/Glob) still work.

## When to stop and escalate (don't thrash)
- MSI build fails twice with the same error → stop, report the error.
- grep-verify shows 0 for any of the three symbols after a clean
  rebuild → stop, report (the patch didn't land).
- Bash classifier blocking your shell ops for >5 min → report and
  wait.
- Anything in the plan or spec that seems contradictory → stop, ask
  hren via your final message. Do not improvise past the plan.
