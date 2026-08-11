# CAM Desktop Extensions — SPEC v2

Extensions are the unified "tool" surface of CAM Desktop. v2 adds the
third extension type (**local agents**) and the evolution discipline.

Design drivers (unchanged):

- **python3.6 stdlib-only remote part** (camc's compatibility bar)
- **Sandboxed UI** (iframe + narrow postMessage bridge)
- **No downloaded code executing locally** (MAS review commitment —
  installs are user-gesture, local-folder only)
- **The hub never runs extension code** (no local Python/Node execution
  of extension logic; the ONLY local execution is the user's own
  agent binaries in non-MAS builds — see §4 type C)

## 0. Repository layout

```
extensions/
  SPEC.md / README.md / GUIDE.md
  host/
    registry.cjs          # manifest parse/validate/install/registry
    tool-proxy.cjs        # remote tool deploy + call
    ext-client.js         # view bridge client (served at /ext/client.js)
  packages/               # built-in extensions (app bundle content)
  examples/hello-ext/     # minimal sample (used by tests)

web/js/shared/ext-bridge.js       # parent-side bridge + capability gate
web/js/shared/ext-view-host.js    # iframe container
web/js/desktop/extensions-mode.js # the Extensions page
```

## 1. Extension types

| type | package carries | runs where | capability |
|---|---|---|---|
| **A. view tool** | `index.html` (+assets) | sandboxed iframe in the app | bridge APIs |
| **B. remote tool** | `main.py` (python3.6 stdlib) | remote SSH hosts via exec pool | `exec` |
| **C. local agent** | `bin/<platform>/<binary>` | **local process** (non-MAS builds) | `local-exec` |
| **N. native page** | no view; optional `main.py` | view is a **built-in app mode page** (Skills-style) | per manifest |

A package may combine A+B freely. Type C is standalone (an agent is its
own UI via the terminal). Type N is for **first-party built-ins**: the
view ships as app code (a mode page like Skills), so changing it needs
an app release — the package only registers the extension and may carry
a remote tool. agent-doctor is the reference: package = `main.py`
collector + `native: agent-doctor`; view = `mode-agent-doctor` in
`web/desktop.html` + `web/js/desktop/agent-doctor-mode.js`.

## 2. Package format

```
my-ext/
  manifest.yaml
  index.html        # type A entry (resolution rules below)
  main.py           # type B entry (also allowed for type N)
  bin/              # type C: per-platform binaries
    windows-x64/my-agent.exe
    darwin-arm64/my-agent
    linux-x64/my-agent
```

### Entry resolution

| part | rule |
|---|---|
| view | `index.html` wins → else exactly one `*.html` → else `view_ambiguous` error → else no view |
| tool | `main.py` wins → else exactly one `*.py` → else `tool_ambiguous` error → else no tool |
| agent | `bin/<current-platform>/<file>` exists → that binary; else `agent_platform_missing` |

**Native packages (`native:` in the manifest) must not carry a view** —
any `.html` entry is rejected with `invalid_native`. Their tool entry
follows the normal rule above.

## 3. manifest.yaml

```yaml
name: my-ext              # [a-z0-9-]{1,32}
version: 0.1.0
title: My Tool
kind: tool | agent        # tool (A/B) default; agent = type C
native: my-mode           # type N: built-in app mode page name (no .html)
mounts:
  - agent                 # also surface in the agent console Ext▾ menu
capabilities:
  - exec                  # B: run main.py on remote hosts
  - files:read            # bridge: read remote files
  - files:write           # bridge: write remote files
  - local-exec            # C: spawn a local process (non-MAS only)
  - agents.start          # bridge: start a camc agent (confirm-gated)
```

Flat YAML subset only (top-level `key: value` and `  - item` lists).

`mounts: [agent]` marks the extension as per-agent: it appears in the
agent console Ext▾ menu, and opening it from there binds the selected
agent (native pages receive it via a module handoff; iframe views read
it via `app.context`).

## 4. Execution model

**A. view** — sandboxed iframe (`sandbox="allow-scripts"`), served by the
loopback hub at `/ext/<name>/<file>?token=…`, postMessage bridge only.
CSP: `default-src 'self'` — no external network from views.

**B. remote tool** — deployed to `$HOME/.cam/extensions/<name>/` via the
hardened transport (chunked SFTP, `$HOME`-anchored install, package-hash
incremental). Invocation: `python3 main.py <method> '<json>'` → one JSON
object on stdout; 30s op budget default. The hub never runs Python.

**C. local agent** — main process spawns `bin/<platform>/<binary>` under
**node-pty**; the renderer attaches an xterm view over a dedicated IPC
channel (separate from the SSH datapath — no hub/transport change).
Availability:

- DMG/MSI builds: yes.
- MAS build: **no** (`process.mas` gate; sandbox forbids spawning).
  The Extensions page shows such entries as "requires the non-App-Store
  build".
- Binary provenance: shipped in the package (built-in) or downloaded
  on first use from a pinned release URL (+ SHA256 check), or a
  user-supplied mirror URL. Never auto-fetched silently.

**N. native page** — no iframe: the Extensions page and the agent
console Ext▾ menu navigate to the named app mode (`setMode(native)`).
The page talks to the hub directly (same APIs as the rest of the app,
including `extCall` for the package's remote tool).

## 5. Bridge API (v2)

View → app (postMessage, capability-gated):

- `agents.list` / `contexts.list` / `agents.capture` — open (read-only)
- `app.context` — the bound `{ agentId, contextName }` for per-agent
  mounts (empty for global opens)
- `agents.cronJobs` / `agents.workspaceList` / `agents.workspaceRead` —
  read-only; per-agent mounts only (need an agent binding)
- `ext.call(context, method, args)` — needs `exec`
- `files.read` / `files.write` — needs `files:read` / `files:write`
- `agents.start(context, body)` — needs `agents.start`; **always
  shows a user confirmation** naming the extension and the agent

App → view: responses + `theme` push.

## 6. Install / manage

Extensions page: list (built-in + user), Install from folder…,
open / disable / remove. Install = validate (manifest, entries, size
caps) + copy to `userData/extensions/<name>/`. Same name = update.
Removal while open kicks back to Agents. Disable keeps files.

Built-ins ship in the app bundle (`extensions/packages/`): they are app
content, not downloads. A user install with the same `name` shadows the
built-in (shown as "built-in · updated by user copy") — that is how
built-in remote tools get updated without an app release.

## 7. MAS stance

- No store, no auto-download, installs are user-folder gestures.
- No extension code executes locally **except** type-C agent binaries
  in non-MAS builds (explicitly user-installed, explicitly started).
  The MAS build omits that channel entirely — the review-notes sentence
  stays literally true for the MAS binary.

## 8. Evolution discipline

Every new extension kind = **one new explicit channel/capability**,
added deliberately and gated in the manifest. Never open a generic
hole. Queued examples:

- `net.connect` — view-side network via hub relay (enables noVNC-style
  remote-desktop extensions)
- browser-automation — a local-exec agent driving a bundled browser
  (heavy; only when a real use case lands)

## 9. Current status

- v1 (shipped in 0.2.4): types A + B, registry, Extensions page,
  hello-ext sample, bridge read APIs + ext.call.
- Post-0.2.4 (in tree): native built-in entries (skills / todos /
  agent-doctor), `mounts: [agent]` + agent console Ext▾ menu,
  per-agent bridge reads (`app.context`, cron, workspace), tar/tgz
  package install, built-in shadowing.
- v2 (this spec): type C channel + `agents.start` — **pending
  implementation** (next mainline task).
