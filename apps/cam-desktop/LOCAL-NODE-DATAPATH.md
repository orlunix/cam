# LOCAL-NODE-DATAPATH.md — local agents without SSH (WSL2 on Windows, native on macOS/Linux)

The Desktop embedded hub manages agents on the **local machine as a
node**, without SSH. All local camc execution is owned by one module,
`apps/cam-desktop/electron/local-runtime.cjs`; the hub
(`embedded-hub.cjs`) routes every local camc call through it.

- **macOS / Linux** → the bundled camc (or `camc` on PATH) is executed
  natively via `execFile`. This is the pre-existing behavior, moved
  into the module unchanged (same argv, timeouts, and
  `camc_missing`/`timeout`/`exec_failed` result mapping).
- **Windows** → the bundled camc is a POSIX sh/python polyglot that
  cannot be exec'd on Windows (no `/bin/sh`), so execution is routed
  into a WSL2 distro:

  ```
  wsl.exe [-d <distro>] --exec <home>/.cam/camc <args...>
  ```

  argv is passed literally (no shell mangling) — the shape the legacy
  Tauri backend proved out (`src-tauri/src/main.rs:40-57`).
  `wsl.exe --exec` does **no** shell expansion, so `~` cannot appear in
  the exec target: the in-distro `$HOME` is resolved once via
  `bash -c 'printf %s "$HOME"'` and cached, and the camc path is built
  as `<home>/.cam/camc`.

Machine identity stays `'local'` everywhere — on Windows the local
runtime *is* the WSL distro; there is no second local runtime to
disambiguate, so store records, endpoint keys, and the renderer's node
list are unchanged.

## Requirements (Windows)

- **WSL2** installed (`wsl.exe --status` works; `wsl --install` if not).
- A **distro** (default: Ubuntu; `wsl --install -d Ubuntu`). Inside the
  distro:
  - **python3** (camc is Python; `sudo apt install python3`),
  - **tmux ≥ 2.4** (`sudo apt install tmux`),
  - an **authenticated agent CLI** (e.g. `claude`, `codex`) — same
    requirement as any camc host.
- **bash** in the distro (used for the `$HOME` probe and the bootstrap;
  present on every normal distro).

On macOS/Linux the requirements are unchanged: the bundled camc runs
directly, and `camc env check` reports tmux/tool/auth readiness.

## Bootstrap (Windows)

The first local agent start calls `ensureCamc()` (mirror of the hub's
`_ensureRemoteCamc` for SSH nodes):

1. `test -x ~/.cam/camc && md5sum ~/.cam/camc | cut -c1-12` in the
   distro; if the short hash equals the bundled camc's md5 short hash →
   done.
2. Otherwise: `mkdir -p ~/.cam`, upload the bundled camc via stdin
   (`bash -c 'cat > ~/.cam/camc.tmp'`), `chmod 700 && mv` into place,
   and verify with `~/.cam/camc version`.
3. A ready-cache keyed on `distro|hash` skips the probe/upload cycle
   for the rest of the process lifetime (~800 KB uploaded once per camc
   build per distro).

Bootstrap happens **at agent start only** (`_startLocalAgent`). Read
paths (capture/stop/rm/input) exec the distro camc directly; if it was
never bootstrapped they fail with `camc_missing`, which the agent-list
sync treats as "no local agents".

## Path mapping

Start requests map the workspace path through `winToWslPath()`:

- `C:\proj\foo` → `/mnt/c/proj/foo` (drive-letter regex, either slash),
- already-Linux paths (`/home/u/x`) pass through untouched,
- relative paths and UNC paths (`\\wsl$\...`, `\\wsl.localhost\...`)
  pass through unchanged (mapping those is deferred — they already name
  a distro filesystem).

## Non-default distro

The store field `hubConfig.wslDistro` (in
`<userData>/embedded-hub.json`) selects the distro; `''` (default)
means the WSL default distro. No UI in this milestone — edit the JSON
while the app is closed:

```json
{ "version": 1, "contexts": [], "agents": [], "hubConfig": { "wslDistro": "Ubuntu-22.04" } }
```

The hub syncs it into the local runtime on load; a configured distro
that does not exist fails preflight with `wsl_distro_missing` naming
the installed distros.

## Preflight: `GET /api/local/runtime?tool=<t>`

Returns a structured readiness report (same bearer auth as the rest of
the loopback hub):

```json
{ "ok": false, "platform": "win32", "runtime": "wsl", "distro": "",
  "checks": { "python3": true, "tmux": false, "tool": false, "tool_auth": false },
  "issues": [ { "level": "error", "message": "tmux missing in the WSL distro …" } ] }
```

- Windows: probes `wsl.exe --status`, `wsl.exe -l -q` (UTF-16LE decoded),
  then in-distro `python3 --version` / `tmux -V`, then — once camc is
  bootstrapped — `camc env check --tool <t> --json` for tool + auth.
- POSIX: runs `camc env check --tool <t> --json` natively and folds its
  `{issues, resolved}` into the same shape.

The Start form shows this as a one-line, non-blocking hint when the
node select is `local` (`web/js/desktop/start-agent-mode.js`,
`#start-local-runtime-hint`). Start stays enabled regardless; the hub
returns the same errors authoritatively.

## Error codes

| code | meaning |
|---|---|
| `camc_missing` | POSIX: no bundled/on-PATH camc. WSL: camc not bootstrapped in the distro yet. |
| `timeout` | the local/WSL camc call exceeded its budget |
| `exec_failed` | any other nonzero/failed local exec |
| `wsl_missing` | `wsl.exe` not found — WSL2 not installed |
| `wsl_distro_missing` | no distro installed, or the configured `wslDistro` does not exist |
| `camc_bootstrap_failed` | upload/install/verify of `~/.cam/camc` into the distro failed (detail names the step) |
| `bundled_camc_missing` / `bundled_camc_read_failed` | the hub could not find/read its bundled camc to upload |
| `local_env_not_ready` | start-time environment gate refused: a prerequisite check failed (python3/tmux/tool/tool_auth) or an error-level preflight issue exists; `detail` lists the issues, `checks`/`issues` are included in the response |

## What this unlocks

- `POST /api/agents` with `node=local` works on Windows (the old
  `local_runtime_unsupported` refusal is gone).
- Capture / stop / rm / edit / key / cron / input for **local-context**
  agents now route through the local runtime (`_execCamcOnContext` and
  `_sendAgentInput` grew a local branch; they used to return
  `not_ssh`). `send` pipes text via stdin with the same `--no-enter`
  handling as the SSH path.
- **Terminal attach** for local agents (added after the initial
  milestone): `getAttachConnectOpts` returns `{ ok, local: true }` for
  local-context agents and `main.cjs` opens a channel via
  `localRuntime.openAttachChannel(agentId, {cols, rows})`. No native
  node-pty dependency — the PTY comes from `script(1)`:
  - linux: `script -qec 'stty cols C rows R; exec <camc> attach <id>' /dev/null`
  - darwin: `script -q /dev/null sh -c '…'` (BSD argv form)
  - win32: `wsl.exe [-d distro] --exec script -qec '…' /dev/null`

  Resize on local channels: implemented in `main.cjs`
  (`_resizeLocalTerminal`) as a transparent reopen — dispose the old
  channel and re-attach `camc attach` at the new stty size (tmux
  redraws; sessionId and xterm buffer are preserved; resize storms are
  coalesced single-flight). Verified live: tmux `list-clients` follows
  100x30 → 140x40 exactly. tmux window controls (action bar) remain
  SSH-only for now.

  Reopen lifecycle (fix, same day): the reopen originally disposed the
  old channel first, whose `onClose` then surfaced in the renderer as a
  spurious **"terminal detached"** and nulled the session. Channels now
  carry generation tokens: the replacement opens FIRST, the entry's live
  token flips (retiring the old channel — its events are ignored by
  token mismatch, deterministically), and only then is the old channel
  disposed.

## Deferred (explicitly out of scope)

- **Workspace browse** on Windows-local agents (`\\wsl$` mapping in
  `_browseList` etc.); POSIX local browse already works via Node fs.
- Multi-distro management UI; Settings dropdown for distro selection.
- `\\wsl$` / `\\wsl.localhost` path mapping in `winToWslPath`.
- MAS/sandbox implications of WSL exec (macOS uses the native runtime).
