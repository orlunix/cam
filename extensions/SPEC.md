# CAM Desktop Extensions — SPEC v1

Extensions are the unified "tool" surface of CAM Desktop. Skills and
Todos become ordinary extensions; third parties can add their own.

Design drivers:

- **python3.6 stdlib-only remote part** (same compatibility bar as
  camc: the last decade of Linux hosts)
- **Sandboxed UI** (iframe + narrow postMessage bridge, nothing else)
- **No downloaded code executing locally** (MAS review commitment —
  installs are user-gesture, local-folder only)
- **The hub never runs Python** (it is Node; the MAS sandbox forbids
  local shells anyway)

## 0. Repository layout

```
extensions/                  # the topic's home
  SPEC.md                    # this file
  README.md                  # overview + pointers
  GUIDE.md                   # how to design an extension
  host/
    registry.cjs             # hub side: manifest parse/validate/install/registry
    tool-proxy.cjs           # hub side: main.py deploy + remote call
  packages/                  # built-in extensions (app bundle content)
    skills/  todos/          # (homes for the iframe-ized versions)
  examples/
    hello-ext/               # minimal sample; used by tests
```

Renderer-side pieces MUST live under `web/` (the hub serves the pages
from the web root):

```
web/js/shared/ext-bridge.js       # postMessage protocol + capability gate
web/js/shared/ext-view-host.js    # iframe container
web/js/desktop/extensions-mode.js # the Extensions page
```

## 1. Package format

An extension is a **folder** — installed as-is (no archive parsing in
the hub; sharing a single file happens outside the app):

```
my-ext/
  manifest.yaml    # required
  index.html       # UI entry (optional — resolution rules below)
  main.js / style.css / assets/…   # anything index.html references
  main.py          # remote entry (optional — python3.6 stdlib only)
```

### Entry resolution rules

| part | rule |
|---|---|
| view | `index.html` wins → else exactly one `*.html` → else `view_ambiguous` error if several → else tool-only |
| tool | `main.py` wins → else exactly one `*.py` → else `tool_ambiguous` error if several → else UI-only |

## 2. manifest.yaml

```yaml
name: my-ext            # [a-z0-9-]{1,32}, unique per install
version: 0.1.0          # semver-ish
title: My Tool          # display name (optional, defaults to name)
capabilities:           # empty/absent = pure content extension
  - exec                # may run main.py on remote hosts
  - files:read          # may read remote files via the bridge
  - files:write         # may write remote files via the bridge
```

The hub parses only a flat subset (top-level `key: value`, `key:` +
`  - item` lists, `#` comments). No nested maps, no anchors.

## 3. Execution model

1. **The hub never runs Python.** `main.py` runs only on remote SSH
   hosts, deployed via the existing hardened path (chunked SFTP upload
   → `$HOME/.cam/extensions/<name>/` → redeploy on content-hash change).
   Invocation over the exec pool:
   `python3 $HOME/.cam/extensions/<name>/main.py <method> '<json-args>'`
   → one JSON object on stdout; errors as `{"error": "...", "detail": "..."}`.
   Host requirement: python3.6+. Nothing else.
2. **The view never touches app internals.** `index.html` loads in a
   sandboxed iframe (`sandbox="allow-scripts"`, no same-origin) served
   by the loopback hub, and talks to the app over a narrow postMessage
   bridge only.

## 4. Bridge API (v1)

View → app (postMessage, request/response with ids):

- `agents.list()` → agent records (read-only)
- `agents.capture(id, lines)` → captured text
- `ext.call(context, method, args)` → run `main.py <method>` on the
  given context's host (requires `exec`)
- `files.read(ctx, path)` / `files.write(ctx, path, text)` (require the
  matching capability)

App → view: bridge responses + `theme` push on init and theme change.
No app-state mutation, no credential access, no direct network.

## 5. Install / manage (Extensions page)

```
Extensions
─────────────────────────────────────────────
 [Install from folder…]
 ─────────────────────
 ● skills    built-in   [open]
 ● todos     built-in   [open]
 ○ my-ext    v0.1 user  [open] [disable] [remove]
```

Install flow (hub side, all local ops):

1. Renderer picks the extension folder via the Electron directory dialog.
2. Hub validates: manifest present, name/version legal, size caps,
   entry resolution must yield a clear answer (§1).
3. Copy to `userData/extensions/<name>/`; record in the store.
4. Listed as a `user` extension; enabled immediately. Same name again =
   update (replace). Remove deletes folder + record; an open view kicks
   back to Agents. Disable keeps files, hides the extension; a deployed
   main.py on remote hosts stays dormant (harmless).

Built-in extensions ship inside the app bundle (`extensions/packages/`)
— they are app content, not downloads. v1 registers **skills** and
**todos** as built-ins whose `open` navigates to their existing native
pages; their iframe-ization is a later, separate migration.

## 6. View serving & isolation

Extension view files are served by the loopback hub at
`/ext/<name>/<file>?token=<hub-token>`:

- Serving over the hub (not file://) gives a consistent
  `http://127.0.0.1` origin, which is cross-origin to the `file://` app
  page — combined with `sandbox="allow-scripts"` the view is fully
  isolated while postMessage keeps working.
- The token is the per-launch loopback bearer (renderer already holds
  it; loopback-only, never logged with the path).
- Path safety: resolved file must stay inside the extension dir.

## 7. MAS stance

- No in-app store, no auto-download. Installs are explicit user-folder
  gestures (same pattern as selecting an SSH key file).
- Review-notes line stays true: "extensions are user-installed local
  packages; no code is downloaded and executed automatically."

## 8. Feasibility checks (validated by the v1 spike)

- sandboxed iframe served from the loopback hub renders and postMessages
  the parent (`examples/hello-ext` view)
- bridge round-trip: view → renderer → hub API → response
- tool path: `main.py` deployed and invoked on a remote host via the
  exec pool, JSON in/out

## 9. Migration plan

1. This spec + `examples/hello-ext` end-to-end (the spike).
2. Registry + Extensions page + bridge + tool proxy.
3. Register **todos** and **skills** as built-ins (navigation entries).
4. Later: true iframe-ization of todos (simplest), then skills.
