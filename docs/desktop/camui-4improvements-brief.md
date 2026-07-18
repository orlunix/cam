# CAM-Desktop 4 UI Improvements — Fix Agent Briefing

Manager → fix agent. Read this whole file before doing anything.

## Your job (one sentence)
Implement 4 CAM-Desktop UI improvements per the approved plan, build the MSI on WSL, grep-verify the fix is bundled, release as a **test build** to Nutstore, commit on prgn, then **STOP and report**.

**Do NOT push to GitLab** — that's hren's gate, after install smoke.
**Do NOT run install smoke** — needs a Windows display only hren has.

## Read these FIRST (in order)
1. `/home/hren/.cam/claude-api/plans/piped-churning-pudding.md` — **THE
   APPROVED PLAN.** It has the approach per feature, the exact files,
   the line numbers, and the 4 assumptions (A1–A4) that were approved.
   Follow it. This briefing is just the wrapper + build flow.
2. `~/gitlab/cam/CLAUDE.md` — repo overview, smoke-test rules,
   gotchas (npm install not ci; don't commit package-lock.json with
   the fix; don't commit apps/cam-desktop/CLAUDE.md).
3. `~/notes/msi-develop-flow.md` — the FULL build & release mechanics
   (WSL gotchas, patch transfer, naming). Read before the build step.

## The 4 features (summary — full detail in the plan)

- **F1 — Duplicate context (Nodes).** `web/js/shared/nodes-mode.js`:
  change the `copy` button (line 517) to `duplicate`; rewire the
  handler (line 797) to open a duplicate modal (new
  `panel._openDuplicateContext(ctx)`) that mirrors `_openEditContext`
  (line 1302) BUT with `fName.readOnly = false` + prefilled
  `<orig>-copy` + `editContextTarget = null` (so submit creates a NEW
  context, not updates). The existing add-submit path creates it.
- **F2 — Optional context (Start) + hub start route.** `web/desktop.html`:
  drop `required` on `#start-context` (line 625), add `#start-node` +
  `#start-path` fields shown when context empty. `web/js/desktop/start-agent-mode.js`:
  add `refreshNodeOptions`, toggle field visibility, `readForm` sends
  `context` OR `node`+`path`, replace the `!ctxSel.value` validation
  (line 101). **Hub:** implement a real `POST /api/agents` in
  `apps/cam-desktop/electron/embedded-hub.cjs` (currently 501 at line
  3524) that shells out to `camc run` — local `execFile` for the local
  node (pattern at line 75), SSH+`camc run` for remote (reuse the
  `_runCamcSsh` pattern). For node+path with no context (A2, INLINE):
  resolve the node to its machine and run `camc run --path <path>`
  directly — **NO context record is created**. Map `camc --json run`
  output to the unified agent schema.
- **F3 — API model picker (Start).** **Hub:** new `GET /api/api-models`
  route that runs `camc --json api list` + `camc api default show --json`
  (execFile, same pattern as F2) and returns `{models, defaults,
  toolSupport}` where `toolSupport` is a static map (`{claude:true,
  codex:true, cursor:false, aider:false}`). **Renderer:** `web/js/api.js`
  add `getApiModels()`; `start-agent-mode.js` + `desktop.html` add a
  "List models" button + `<select id="start-api">`; on click populate
  with enabled models, mark the per-tool default; on tool change grey
  the section if `!toolSupport[tool]` (3.3); `readForm` adds `body.api`
  when set + supported.
- **F4 — Terminal ALWAYS the default.** `web/js/desktop/agent-console.js`:
  in `syncModeToggle` (line 1558) REMOVE the silent
  `if (outputMode === 'terminal' && !terminalAllowed) { outputMode =
  'rich'; ... }` auto-flip (1560–1564) ENTIRELY. Terminal mode is
  always the default — never auto-flip to rich, regardless of
  connection state (disconnected, connecting, Relay, Direct). Keep
  the button greying (`b.disabled = !terminalAllowed`) so the user
  sees it's not attachable yet, but DON'T change `outputMode` and
  DON'T hide the terminal pane. The terminal pane shows in every
  connection state (empty/hint when not attachable). Rich/plain/browse
  are manual "assistant" modes the user must explicitly pick.

## Critical implementation facts (from the manager's exploration)
- **The hub `POST /api/agents` is stubbed 501 today** (line 3524).
  F2 + F3 are blocked in Direct mode until you implement a real start
  route. The hub already has the `execFile`-to-local-`camc` pattern
  (line 75, used for Bug-1 local-agent ingestion) — reuse it.
- **The renderer is NOT in `app.asar`** — it ships as loose files
  under `resources/web/`. Grep `resources/web/` for renderer fixes;
  grep `app.asar` only for hub-side routes. (The fast-switch cycle
  learned this the hard way.)
- **`camc --json api list`** — the `--json` is a GLOBAL flag BEFORE the
  subcommand, not after. `camc api list --json` FAILS. `camc --json api
  list` works. Same for `camc --json run`.
- **`camc run` JSON output** — `camc --json run -t <tool> -p <path>
  -n <name> <prompt>` returns the agent record. Map it to the unified
  schema (`src/cam/core/agent_schema.py` for the field names).
- **`toolSupport` is static** — claude + codex support `--api`;
  cursor + aider do NOT (per the `managing-camc` skill). Hardcode this
  map in the hub route; don't try to detect it.
- **`POST /api/contexts` (add context)** — verify it's NOT 501 before
  relying on it for F1's duplicate-create and F2's temp-context. If it
  IS 501, you'll need to implement it too (same `execFile`-to-`camc`
  pattern, `camc context add`). Check first.

## Constraints (all mandatory)
- **Branch:** `camui-desktop-v2`. Don't switch branches.
- **No commit until the MSI is grep-verified** (per `msi-verify-before-commit`).
  The implementation commits land ONLY after the built MSI contains
  the fix. This is hren's hard rule.
- **Don't commit `package-lock.json`** (regenerated by `npm install`,
  tangential). **Don't commit `apps/cam-desktop/CLAUDE.md`** if you
  create one (transient briefing artifact — delete it).
- **`node --check` every modified `.cjs` and `.js`** before building.
- **Match surrounding code style** — naming, idiom, comment density.
- **Don't push unverified commits to GitLab** — patch transfer only.

## Build & release flow — you OWN this part

### A. After implementing F1–F4: local checks
```bash
cd ~/gitlab/cam
node --check apps/cam-desktop/electron/embedded-hub.cjs
node --check web/js/desktop/start-agent-mode.js
node --check web/js/shared/nodes-mode.js
node --check web/js/desktop/agent-console.js
node --check web/js/api.js
grep -n "api-models\|startAgent\|_openDuplicateContext\|start-node\|start-api\|toolSupport" \
  apps/cam-desktop/electron/embedded-hub.cjs web/js/desktop/start-agent-mode.js \
  web/js/shared/nodes-mode.js web/js/desktop/agent-console.js web/js/api.js web/desktop.html
```
Expected: `node --check` silent (exit 0) for each; grep shows your new
symbols across the files.

### B. Transfer to the WSL build box (DO NOT push unverified)
WSL box at `:2222` (laptop omni-wfa-q8o5s; repo at
`/mnt/c/Users/hren/gitlab/cam`).
```bash
cd ~/gitlab/cam
git diff camui-desktop-v2 -- \
  apps/cam-desktop/electron/embedded-hub.cjs \
  web/desktop.html \
  web/js/desktop/start-agent-mode.js \
  web/js/shared/nodes-mode.js \
  web/js/desktop/agent-console.js \
  web/js/api.js > /tmp/camui-4imp.patch
scp -P 2222 /tmp/camui-4imp.patch hren@127.0.0.1:/tmp/camui-4imp.patch
ssh -p 2222 hren@127.0.0.1 "cd /mnt/c/Users/hren/gitlab/cam && \
  git apply --whitespace=nowarn /tmp/camui-4imp.patch && \
  grep -c 'api-models' apps/cam-desktop/electron/embedded-hub.cjs && \
  grep -c '_openDuplicateContext' web/js/shared/nodes-mode.js && \
  grep -c 'start-node' web/desktop.html"
```
Use `git apply`, **NOT `git am`** — WSL1 can't chmod `/mnt/c`, `git am`
fails on `.git/config.lock`. The build reads the working tree.

### C. Build the MSI on WSL
```bash
ssh -p 2222 hren@127.0.0.1 'bash ~/cam-build-from-wsl.sh'
```
Triggers detached `cam-build.bat` (Start-Process -WindowStyle Hidden
defeats the WSL1→Windows stdio EISDIR bug). Polls for
`BUILD_DONE_SENTINEL`. MSI at
`apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi`. Expected in log:
`NPM_INSTALL_EXIT=0`, `MSI_BUILD_EXIT=0`. **`npm ci` FAILS** — the
build uses `npm install`. Don't commit the regenerated lockfile.

### D. Verify the fix is bundled (REQUIRED before commit)
```bash
# Extract the MSI
ssh -p 2222 hren@127.0.0.1 "/mnt/c/Windows/System32/msiexec.exe /a \
  'C:\Users\hren\gitlab\cam\apps\cam-desktop\dist\CAM-Desktop-0.2.0.msi' \
  /qn TARGETDIR='C:\Users\hren\msi-extract-4imp'"
# Grep the RENDERER (un-bundled, under resources/web/) — NOT app.asar
ssh -p 2222 hren@127.0.0.1 "grep -c '_openDuplicateContext' \
  /mnt/c/Users/hren/msi-extract-4imp/cam-desktop/resources/web/js/shared/nodes-mode.js && \
  grep -c 'start-node' \
  /mnt/c/Users/hren/msi-extract-4imp/cam-desktop/resources/web/desktop.html && \
  grep -c 'start-api' \
  /mnt/c/Users/hren/msi-extract-4imp/cam-desktop/resources/web/js/desktop/start-agent-mode.js"
# Grep the HUB routes in app.asar
ssh -p 2222 hren@127.0.0.1 "grep -a -c '/api/api-models' \
  /mnt/c/Users/hren/msi-extract-4imp/cam-desktop/resources/app.asar && \
  grep -a -c 'api-models' \
  /mnt/c/Users/hren/msi-extract-4imp/cam-desktop/resources/app.asar"
# sha256
ssh -p 2222 hren@127.0.0.1 "sha256sum \
  /mnt/c/Users/hren/gitlab/cam/apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi"
```
Expected: all counts non-zero. `_openDuplicateContext` in
`resources/web/js/shared/nodes-mode.js`; `start-node` in
`resources/web/desktop.html`; `start-api` in the start JS; `/api/api-models`
+ `api-models` in `app.asar`. **If any is 0, the patch didn't land —
clean `dist/` and rebuild. Do NOT commit until all are non-zero.**

### E. Release to Nutstore (test build, `-3`)
```bash
ssh -p 2222 hren@127.0.0.1 "cp \
  /mnt/c/Users/hren/gitlab/cam/apps/cam-desktop/dist/CAM-Desktop-0.2.0.msi \
  '/mnt/c/Users/hren/Nutstore/1/Nutstore/app/CAM-Desktop-0.2.0-3-4improvements-$(date +%Y%m%d).msi'"
```
Naming: `CAM-Desktop-<baseVer>-<n>-<desc>-<YYYYMMDD>.msi`. `-3` = test
build. `<desc>` = `4improvements` (or a short list like `dup-ctx-optctx-api-terminal`).

### F. Commit on prgn (AFTER grep-verify)
```bash
cd ~/gitlab/cam
git add apps/cam-desktop/electron/embedded-hub.cjs \
  web/desktop.html \
  web/js/desktop/start-agent-mode.js \
  web/js/shared/nodes-mode.js \
  web/js/desktop/agent-console.js \
  web/js/api.js
git commit -m "cam-desktop: 4 UI improvements — duplicate ctx, optional ctx + start route, API model picker, terminal default

F1: Nodes context 'copy' → 'duplicate' — opens the edit modal with
the name field editable (prefilled <orig>-copy), submit creates a new
context.

F2: Start page context is optional; if empty, user provides node +
path. Hub POST /api/agents (was 501) now shells out to camc run (local
execFile / remote SSH), auto-creating a temp context for node+path.

F3: Start page API model picker. New hub GET /api/api-models wraps
camc --json api list + camc api default show --json (+ static
toolSupport map). UI: List-models button + select; default marked;
greyed for tools without --api (cursor/aider).

F4: Terminal is the default output mode even in connecting/disabled
state; no silent flip to rich when terminal isn't allowed — show the
terminal pane with a 'connect to attach' hint. Rich/plain are manual
'assistant' modes.

MSI build-verified: renderer fixes in resources/web/, hub routes in
app.asar. Released as test build CAM-Desktop-0.2.0-3-4improvements-<date>.msi
to Nutstore. Install smoke pending (hren).

Refs: docs/desktop/agent-fast-switch-spec.md (renderer pattern),
      ~/gitlab/cam/CLAUDE.md (smoke rules)."
```
**Do NOT commit `package-lock.json`.** **Do NOT push.**

### G. STOP and report
Report to hren in your final message:
- The commit hash on `camui-desktop-v2`.
- The MSI file name + sha256.
- The grep-verify counts (renderer + hub).
- The Nutstore release path.
- Confirm `node --check` passed on all files and `package-lock.json`
  was NOT committed.

Then stop. hren will: install the MSI on a Windows box, run the
acceptance smoke (the verification section of the plan), and if it
passes, push the commit + drop the `-3` for a workable release.

## Git/WSL gotchas (full detail in ~/notes/msi-develop-flow.md)
- **`git am` fails on WSL1** (`chmod on .git/config.lock`). Use
  `git apply`.
- **`npm ci` fails** (stale lockfile). The build uses `npm install`.
- **MSI at `apps/cam-desktop/dist/`** — electron-builder runs from
  `apps/cam-desktop/`.
- **Renderer is un-bundled** — grep `resources/web/`, NOT `app.asar`,
  for renderer fixes.
- **glm-5.2 Bash classifier may flap** ("temporarily unavailable") —
  retry Bash ops after a brief wait; read-only ops (Read/Grep/Glob)
  still work.

## When to stop and escalate (don't thrash)
- MSI build fails twice with the same error → stop, report the error.
- grep-verify shows 0 for any symbol after a clean rebuild → stop,
  report (the patch didn't land).
- `POST /api/contexts` is 501 and you need it for F1/F2 → stop,
  report (you may need to implement it too, but ask hren first since
  that enlarges scope).
- Bash classifier blocking your shell ops for >5 min → report and wait.
- Anything in the plan that seems contradictory → stop, ask hren via
  your final message. Do not improvise past the plan.
