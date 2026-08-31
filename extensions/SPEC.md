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
  packages/               # built-in extensions (app bundle content):
                          #   skills / todos / assistant (minimal sample, used by tests)
  examples/agent-doctor/  # demo A+B extension (installable via dist/ext/ tar.gz)

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
a remote tool. skills / todos are the references (`native: skills`,
`native: todos`). Prefer A+B whenever the bridge APIs suffice — an A+B
extension updates by installing a same-name package, no app release:
agent-doctor is that reference (package = `index.html` review browser +
`main.py` collector, bridge-only data; ships as the `examples/` demo).

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
  - agent                 # per-agent ext: openable with an agent binding
capabilities:
  - exec                  # B: run main.py on remote hosts
  - files:read            # bridge: read remote files
  - files:write           # bridge: write remote files
  - hub:api               # bridge: ext.hubCall — full /api/* passthrough
  - local-exec            # C: spawn a local process (non-MAS only)
  - agents.start          # bridge: start a camc agent (confirm-gated)
```

Flat YAML subset only (top-level `key: value` and `  - item` lists).

`mounts: [agent]` marks the extension as per-agent: opening it from the
agent console binds the selected agent (native pages receive it via a
module handoff; iframe views read it via `app.context`). Visibility in
the agent console Ext▾ menu is NOT gated by mounts — it is purely
config-driven via the platform attribute `show_in_agent_menu` (default
false; see Per-extension attributes below).

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

**App-managed chrome (all types).** Every extension open — iframe view
or native page — renders under one uniform app header: line 1
`title@version`, line 2 the manifest `description`, a Back button
top-right, then a divider; below the divider is the extension's own
scope, width-capped to the app's content rail. Native pages get the
chrome injected (`applyExtChrome`) and their own baked-in header is
hidden. Consequences for authors: **set a manifest `description`** (it
is the chrome's subtitle) and **do not render your own title bar** in a
view — start with content.

## 5. Bridge API (v2)

View → app (postMessage, capability-gated):

- `agents.list` / `contexts.list` / `agents.capture` — open (read-only)
- `app.context` — the bound `{ agentId, contextName }` for per-agent
  mounts (empty for global opens)
- `agents.cronJobs` / `agents.workspaceList` / `agents.workspaceRead` —
  read-only; per-agent mounts only (need an agent binding)
- `ext.call(context, method, args)` — needs `exec`
- `ext.config` — the calling extension's own attributes object (edited
  in the app via Extensions → Settings). Always allowed, read-only.
- `files.read` / `files.write` — needs `files:read` / `files:write`
- `ext.hubCall(method, path, body)` — needs `hub:api`: generic hub
  passthrough, any `/api/*` endpoint, verbs GET/POST/PUT/PATCH/DELETE,
  every call audit-logged `[ext:<name>] METHOD path`. This is the same
  power the app itself has — the manifest declaration IS the consent
  surface. It lets built-in pages (skills/todos since 0.2.31) live as
  self-managed extensions instead of app-shell code.
- `ext.storageGet` / `ext.storageSet(storage)` — the calling ext's own
  durable key/value store (hub-side `ext-data/<name>/storage.json`,
  ≤512KB). Always allowed (its own data only). Sandboxed views run in an
  opaque origin where `localStorage` throws — this is their local-state
  channel; shim `window.localStorage` over it if you port code that
  expects synchronous storage.
- `agents.start(context, body)` — needs `agents.start`; **always
  shows a user confirmation** naming the extension and the agent

App → view: responses + `theme` push.

## 6. Install / manage

Extensions page: list (built-in + user), Install from folder…,
open / settings / disable / remove. Install = validate (manifest,
entries, size caps) + copy to `userData/extensions/<name>/`. Same name =
update. Removal while open kicks back to Agents. Disable keeps files.

**Built-ins carry no privileges.** They ship in the app bundle
(`extensions/packages/`) — app content, not downloads — but the UI and
store semantics are identical to user extensions: Open / Settings /
Enable / Disable / Remove on every row, and the store `enabled` flag
applies to them the same way. The only differences are provenance:
built-in files live in the read-only bundle, so **Remove on a built-in
cannot delete files** — it sets a store `removed` flag that hides the
row; installing a same-name folder/package clears the flag (offline
restore), and an app reinstall/upgrade clears every `removed` flag on
startup (bundle content returns to factory state). A user install with
the same `name` shadows the built-in **only when its version is strictly
newer** (shown as "built-in · updated by user copy") — that is how
built-in remote tools get updated without an app release. An equal or
older user copy LOSES to the built-in: an app reinstall/upgrade repairs
stale shadows, and the list row annotates the ignored copy
(`shadowed_user: <version>`).

Per-extension **attributes**: every extension has a JSON-object config
edited in the app (Extensions → Settings) and stored in
`userData/extension-config.json` — outside the package dir, so package
reinstall/update keeps it. Views read it via `ext.config`; native pages
call `/api/extensions/<name>/config` directly. Writes happen only from
the app (PUT; validated plain object, ≤16KB per extension, `{}` resets).

**Per-extension data dir.** Extensions that need local state beyond the
attribute object keep it under `userData/ext-data/<name>/` (e.g. the
assistant's `config.json` + `events.jsonl` transcript). Like attributes,
it survives reinstall/update/shadowing. When an extension is removed
**entirely** (user ext deleted, or built-in hidden via the `removed`
flag), the app cascades: its attributes entry, its `ext-data/<name>/`
dir, and its credential-store secrets (refs prefixed `<name>:`) are
deleted with it. Removing a *shadowing* user copy is not a removal — it
reverts to the built-in and keeps all config.

**Uniform platform attribute.** The Settings page renders exactly ONE field
for every extension, declared by the platform (not the manifest):

```
show_in_agent_menu | boolean | false | Show in agent Ext menu | …
```

Default is **false**: the agent page Ext menu starts empty, and the user
pins entries explicitly (saved values persist across reinstalls). The
menu is purely config-driven — any enabled ext with a view or native
page whose flag is saved `true` appears there.

Schema-line format (kept for future platform attributes):
`key | type | default | label | hint` (`hint` optional); type is `text`
| `number` | `boolean` | `select:a,b,c`. (A raw JSON textarea remains
in the editor only as a defensive fallback.)

Custom per-ext settings are the extension's own affair: the app does not
render manifest-declared custom attributes. The ext's view reads its
config (`ext.config` / `/api/extensions/<name>/config`) and surfaces
them wherever it likes on its own page — e.g. agent-doctor shows its
effective `prompt_warn_kb` reference next to its review controls.

## 7. MAS stance

- No store, no auto-download, installs are user-folder gestures.
- No extension code executes locally **except** type-C agent binaries
  in non-MAS builds (explicitly user-installed, explicitly started).
  The MAS build omits that channel entirely — the review-notes sentence
  stays literally true for the MAS binary.
- Assistant specifics (0.2.36): on non-MAS builds the assistant child
  (itself a full Node process) runs local shell commands **directly** —
  no companion bridge, no setup. MAS builds compile with
  `CAM_LOCAL_SHELL=false`: the `bash` tool is never registered there.
  The one remaining MAS gate: **bundle shadowing is disabled** on MAS —
  the child only ever runs the signed, in-bundle `cam-assist.js` (Apple
  2.5.2: no executable code from outside the app bundle).

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
- Post-0.2.4 (in tree): native built-in entries (skills / todos),
  agent console Ext▾ menu (opt-in per ext via the
  `show_in_agent_menu` platform attribute; `mounts: [agent]` only marks
  agent-bindable), per-agent bridge reads (`app.context`, cron,
  workspace), tar/tgz package install, built-in shadowing.
- 0.2.21: hello-ext renamed to **assistant** and promoted to a built-in
  (`packages/assistant`); agent-doctor moved to `examples/` as the
  installable demo.
- 0.2.24: built-ins are fully uniform again (Open / Settings / Enable /
  Remove on every row — the 0.2.21 "disable-only" detour is reverted).
  Built-in Remove hides via the store `removed` flag; a full removal
  cascades to the ext's attributes, `ext-data/<name>/` dir, and
  credential secrets; deleting a shadowing user copy reverts to the
  built-in and keeps config. Added the per-extension `ext-data/<name>/`
  data dir convention.
- 0.2.26: **bundle shadowing** — an ext package may carry the local
  agent bundle its host executes (`<userData>/extensions/assistant/
  cam-assist.js` wins over the bundled copy). The ext tar.gz now ships
  view + agent logic together; assistant iterations (prompt, tools,
  agent behavior) no longer need an app release. The execution
  privilege (spawn, secrets, disk) stays in the main process — only the
  file's origin changes. The bundle is minified to fit the 4MB
  per-file package cap.
- 0.2.30: **shadowing is version-gated** (`registry.resolvePackageDir`,
  same rule for the assistant bundle in assistant-host):
  when both copies of a name exist, the higher manifest version serves
  and a TIE goes to the built-in — reinstalling/upgrading the app now
  repairs stale shadows instead of silently running them. User-side
  updates keep working by bumping the package version in the tar.gz.
- 0.2.31: **skills/todos de-nativized** — both are now self-contained
  extension packages (iframe view + vendored modules) instead of
  app-shell mode pages, powered by the new `hub:api` capability and the
  `ext.hubCall` bridge passthrough (any `/api/*` endpoint, audit-logged).
  Their UI/logic iterations now ship as tar.gz like any other extension.
  The `native:` mechanism stays, but no built-in uses it. Also new:
  `ext.storageGet/Set` + `GET/PUT /api/extensions/<name>/storage` —
  the per-ext durable store for sandboxed views (opaque origin ⇒ no
  localStorage); both migrated views install a `localStorage` shim over
  it, and in-sandbox `window.confirm` is replaced by armed buttons.
- v2 (this spec): type C channel + `agents.start` — **pending
  implementation** (next mainline task).
