# CAM Desktop Skillm Integration

Status: implementation v0

## Goal

Add a Desktop UI wrapper for `skillm` so a user can manage the shared skill
library from CAM Desktop without opening a terminal on each node.

The workflow is split across exactly two tabs — **Repositories** and
**Install**. There is no standalone Sync tab; the backend pulls
repositories automatically on every list/install path and on every
repo write (CAM-DESK-SKILLM-015 / -016).

1. Pick one SSH context/node as the Skillm setup node.
2. On the **Repositories** tab, **Add Repository** (name + Git URL
   + one-shot token). Per-repo rows expose **Edit** (rename + change
   URL/token), **Refresh** (`skillm pull <name>`), and **Remove**
   (`skillm repo rm <name>`). The setup-header **Refresh** action refreshes the active tab; per-repo **Refresh** runs
   `skillm pull <name>`.
3. On the **Install** tab, optionally narrow with the repository
   filter dropdown or the search box. Select skills, choose a node
   scope (Setup node / All SSH / Selected), pick Workspace path or
   Global default, choose the target agent CLIs, then click
   **Install selected skills**.

## Boundaries

`skillm` remains the source of truth for skill storage. Desktop does not create
its own skill database. On each node, `skillm` owns:

- `~/.skillm/config.toml`
- `~/.skillm/library.db`
- `~/.skillm/repos/...`
- global installs under `~/.claude/skills`, `~/.codex/skills`,
  `~/.openclaw/skills`, or `~/.cursor/skills`
- workspace installs under `<workspace>/.<agent>/skills`

Desktop packages the standalone Python 3.6-compatible `skillm` artifact at `dist/skillm` and includes it in Electron `extraResources` as `resources/skillm/skillm`. The embedded Hub manages the remote copy at `~/.cam/skillm`, the same way it manages `~/.cam/camc`. Desktop only calls that managed node-local binary through the embedded Hub.

## API Contract

The embedded Hub exposes a small `/api/skillm/...` surface:

- `GET /api/skillm/status?context=<name>`
  deploys or upgrades `~/.cam/skillm` when needed, then reports the bundled Skillm version available on that SSH node.
- `GET /api/skillm/list?context=<name>&repo=<repoName>&sync=1`
  returns parsed skills from `skillm list`. `repo` filters to a single
  repository. `sync=1` pulls all repos (or just `repo` when set)
  before listing. When a future `skillm --json` surface is available,
  the Hub should prefer it; v0 falls back to parsing the current
  Rich table output.
- `GET    /api/skillm/repos?context=<name>` — list repos
  (`skillm repo list`, parsed; URLs and `oauth2:...@` credential
  fragments redacted).
- `POST   /api/skillm/repos`       — add a repo
  (`skillm repo add <name> <url>` + `skillm pull <name>`).
- `PATCH  /api/skillm/repos`       — rename and/or change URL/token
  (implemented as `skillm repo rm` + `skillm repo add` + `skillm pull`).
- `DELETE /api/skillm/repos`       — remove a repo (`skillm repo rm`).
- `POST   /api/skillm/repos/refresh` — `skillm pull` one repo
  (when `repoName` is set) or every non-local-only repo.
- `POST   /api/skillm/repo-connect` — compatibility alias for
  `POST /api/skillm/repos` (kept so older renderers / smokes keep
  working during the v1 → v2 transition).
- `POST   /api/skillm/sync`        — compatibility alias for
  `POST /api/skillm/repos/refresh` (kept for the same reason).
- `POST   /api/skillm/install`     — pulls the selected repo/all repos on each
  target node, then runs `skillm install <skill> -a <agent>` with either
  `--global` or `--project-root <workspace>`.

All routes accept context names, not host passwords. SSH credentials stay inside
Electron main and reuse the existing Direct SSH transport pool.

## Token Handling

The Git token is a one-shot form value. Desktop does not store it in
`localStorage` and the Hub does not persist it. For HTTPS GitLab URLs, the Hub
passes the token to `skillm repo add` as `https://oauth2:<token>@host/path.git`.

Hub responses redact:

- the raw token,
- URL-encoded token bytes,
- `oauth2:<anything>@` credentials in command output.

## Node Scope

v0 supports SSH contexts only. Local contexts return `not_ssh` because Direct
Hub must not run arbitrary local shell commands from the renderer.

The user can install to multiple nodes, but consistency is still Git-based: each
target node pulls the same repository during backend refresh/install. There is no
always-on shared database or SSH tunnel gateway in this slice.

## Future Work

- Replace table parsing with required stable `skillm --json` output.
- Add a shared service gateway for future common tools that need one endpoint
  instead of per-node replicated state.
- Add default-skill policy for new agents once `skillm` exposes a stable
  programmatic install/inject contract for that flow.
