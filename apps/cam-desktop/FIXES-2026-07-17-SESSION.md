# CAM Desktop — 2026-07-17 session: full change log

Branch: `camui-desktop-v2` (uncommitted working-tree changes; no commits
were made). Everything below shipped in the rebuilt
`dist/CAM-Desktop-0.2.0.msi` (build 23:11).

Detailed docs (this directory):

- `FIXES-UNFINISHED-UI.md` — hidden unfinished surfaces + `[hidden]` CSS
  guard + logo resolution.
- `LOCAL-NODE-DATAPATH.md` — local-node runtime design, WSL bootstrap,
  preflight, env gate, terminal attach, resize.
- `FIXES-ATTACH-LATENCY.md` — remote attach 10s diagnosis, keepalive,
  tab-switch churn, screen-size guarantees, live latency numbers.

This file is the session index and covers the pieces those docs don't.

## 1. MSI datapath preparation

- Audited the electron-builder `extraResources` inputs (`web/`,
  `src/camc` → `resources/camc/camc`, `dist/skillm` →
  `resources/skillm/skillm`) against the runtime resolution in
  `embedded-hub.cjs` — mapping confirmed consistent.
- **`src/camc` was stale** (Jul-13 dirty build `f74db89-dirty` vs
  `dist/camc` `9909fb1`): refreshed by copying `dist/camc` over it, per
  the CLAUDE.md convention.
- **Do NOT run `python build_camc.py` on Windows**: the build writes
  with locale encoding/CRLF and embeds resource keys with backslashes
  (`reference\heal-and-monitor.md`), producing a broken polyglot. The
  Linux-built committed `dist/camc` is the correct artifact. (WSL or CI
  can rebuild safely.)
- `package-lock.json` was out of sync with `package.json`
  (`@xterm/addon-serialize` missing) — `npm ci` failed both locally and
  in CI. Regenerated via `npm install`.
- No system Node.js existed on this machine: portable Node 20.18.1
  lives in repo-root `.tools/` (gitignored). Use
  `export PATH="$PWD/.tools/node:$PATH"` for npm/electron-builder.
- mac datapath prepared: `apps/cam-desktop/build/icon.png` +
  `icon.ico`; `package.json` `build.win.icon` → `build/icon.ico` (moved
  out of the legacy `src-tauri/` tree); `build.mac` gained `icon`,
  `category: public.app-category.developer-tools`, and
  `artifactName: CAM-Desktop-${version}-${arch}.${ext}`. DMG builds on
  the mac CI runner pick these up automatically.

## 2. Hidden unfinished UI (pre-release)

Workflow tab (agent settings), Bots + Todos nav modes, and the logo
placeholder were hidden (not removed) with `UNFINISHED-HIDDEN` markers;
`HIDDEN_MODES` in `web/js/desktop/app.js` blocks stale persisted modes.
Follow-up fix the same day: the `hidden` attribute was defeated by
component `display` rules → global `[hidden] { display: none
!important; }` guard in `web/css/desktop.css`. Details:
`FIXES-UNFINISHED-UI.md`.

## 3. Logo

Final: **"F3 relay hub"** — thin-line blue ring (manager/control plane)
+ green ring (running agent) + white hub node linking both ring centers
(the CAM architecture in one glyph), on `#0d1117`. Generator scripts in
`.tools/logo/` (gitignored, iterations A/B/C/D/E kept for reference).
Shipped: `web/assets/icon-512.png`, `icon-192.png`, multi-size
`build/icon.ico`; sidebar header un-hidden with the new mark. macOS
`.icns` is produced from `build/icon.png` on the mac runner.

## 4. Local-node datapath (WSL2 / native)

Local machine works as an agent node without SSH: Windows executes via
`wsl.exe [-d distro] --exec <home>/.cam/camc`, macOS/Linux natively.
New `electron/local-runtime.cjs` (execCamc / ensureCamc bootstrap /
checkEnvironment preflight / winToWslPath), hub rewiring of all five
local exec sites, local branches for capture/stop/rm/edit/key/cron and
`send --stdin`, `GET /api/local/runtime` route, Start-form readiness
hint. Start-time **environment gate**: missing python3/tmux/tool/auth →
HTTP 400 `local_env_not_ready` with the failing checks listed.
Terminal attach via `script(1)` PTY (no node-pty dependency); resize
implemented as transparent reopen (verified `list-clients` 100x30 →
140x40). Full design + error codes: `LOCAL-NODE-DATAPATH.md`.

## 5. Remote attach latency + tab semantics

- Attach was ~10s: two housekeeping SSH execs blocked the critical path
  (commit `3e8d351`) and pooled connections had keepalive disabled
  (silent NAT drops → random detaches). Fixed: parallel baseline probe,
  deferred size repair, `keepaliveInterval: 15000`, `_ensureRemoteCamc`
  probe skipped on ready-cache hit.
- Live numbers (this machine → prgn.nvidia.com): link floor ~1s/round
  trip; attach now `open_ms≈1.5s, first_byte≈1.8s` (was ~4.5s warm,
  10s+ on stalls).
- Tab semantics (each agent = a terminal tab): redundant resize
  notifies eliminated on both renderer and main sides (same-size switch
  = zero network); open size is always computed from the container rect
  + measured cell size — no 80x24-style defaults anywhere (three
  default-floor sites removed); only the anti-poison floor
  (≥40 cols / ≥4 rows) remains.
- Details + numbers: `FIXES-ATTACH-LATENCY.md`.

## 6. Incident: package.json truncation

Mid-session, `apps/cam-desktop/package.json` was truncated by an
external actor (lost `scripts`/`build`/`devDependencies`; cause
unknown — possibly an editor or another tool). Restored from git HEAD
and reapplied the day's edits (icon paths, mac section, `test:local`,
`lint:electron` including `local-runtime.cjs`). Verified the final diff
contains only intended changes. If the truncation was intentional,
revisit.

## Verification status

- `lint:electron` + all suites green: hub 66, term 80+6, start 6,
  local-runtime unit (incl. three-platform attach argv + reopen).
- Live WSL smoke: camc bootstrap into Ubuntu, `camc version/list` via
  `wsl.exe --exec`, cursor-agent e2e (prompt delivery + capture OK; the
  tool itself errored on an account/team setting, unrelated to the
  datapath), terminal attach with TUI byte flow, resize reopen.
- Live SSH probe to prgn.nvidia.com through the app's own
  `ssh-transport.cjs` (timings quoted above).
- MSI (23:11 build) asar verified to contain every change.

## Known remaining gaps (tracked, not regressions)

- tmux window size = min over attached clients: another device attached
  smaller will still clamp the pane (tmux semantics).
- Local terminal resize is a reopen (brief flicker), not an ioctl.
- tmux window controls (action bar) remain SSH-only.
- Windows-local workspace browse (`\\wsl$` mapping) deferred.
- `docs/windows-installer.md` and the CI workflows still describe the
  legacy Tauri build — tracked as cleanup (see the review list in the
  2026-07-17 conversation; `build:windows-msi` script name vs
  `build:win-msi`, artifact path, Rust steps).
