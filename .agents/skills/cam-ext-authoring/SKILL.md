---
name: cam-ext-authoring
description: Design and build a CAM Desktop extension (folder with manifest.yaml + optional index.html view + optional main.py remote tool). Use when the user wants to create, package, or debug an extension for CAM Desktop, or asks how the extensions system works.
---

# CAM Desktop extension authoring

Contract: `extensions/SPEC.md`. Authoring guide: `extensions/GUIDE.md`.
Canonical sample: `extensions/packages/assistant/` (built-in, used by tests); installable demo: `extensions/examples/agent-doctor/`.

## Package shape

```
my-ext/
  manifest.yaml     # name [a-z0-9-]{1,32}, version, title, description, capabilities
  index.html        # optional UI — sandboxed iframe, bridge-only
  main.py           # optional remote tool — python3.6 stdlib only
```

Manifest extras: `native: <mode>` (first-party built-ins only — view is
a built-in app page, package must NOT carry `.html` → `invalid_native`)
and `mounts: [agent]` (marks the ext agent-bindable; iframe views read
the binding via `camExt.call('app.context')`, plus per-agent reads
`agents.cronJobs` / `agents.workspaceList` / `agents.workspaceRead`).

App chrome & visibility: every ext opens under the app-managed header
(`title@version` + manifest `description` + Back + divider) — set a
`description`, and never draw your own title bar in the view (start
with content). The agent page Ext▾ menu is purely opt-in: an ext shows
there only after the user enables `show_in_agent_menu` in
Extensions → Settings (default off); mounts does not gate the menu.

Entry resolution: `index.html` wins → else exactly one `*.html` → else
`view_ambiguous` error. Same for `main.py` / `*.py`. No entries at all
= `empty_extension` error.

## Bridge (view side)

`<script src="../client.js"></script>`, then:

- `camExt.call('agents.list')` / `contexts.list` — read-only, always allowed
- `camExt.call('agents.capture', {id, lines})`
- `camExt.call('ext.call', {context, method, args})` — needs `exec` capability
- `camExt.call('files.read', {context, path})` — needs `files:read`
- `camExt.hubCall(method, path, body)` — needs `hub:api`: generic `/api/*`
  passthrough (all verbs, audit-logged). The same power as the app itself —
  declare it deliberately; this is how the built-in skills/todos views work.
- `camExt.storageGet()` / `camExt.storageSet(obj)` — the ext's own durable
  store. Sandboxed views have NO localStorage (opaque origin — it throws);
  install a global shim before your modules if you port code expecting it
  (see packages/todos/index.html for the pattern). `window.confirm` is
  likewise unavailable — use an armed two-step button.
- `camExt.call('ext.config')` — the ext's own JSON config (custom settings
  are self-managed: read here, surface them on your own page; the app
  Settings page only renders the platform's `show_in_agent_menu` toggle)

Errors reject with `Error(<code>)`, e.g. `capability_denied:exec`.

## Tool contract (main.py)

`python3 main.py <method> '<json-args>'` → one JSON object on stdout;
errors as `{"error": "...", "detail": "..."}`. python3.6 stdlib only;
deploys to `$HOME/.cam/extensions/<name>/` on remote hosts (never runs
on the machine running the app).

## Install / test loop

Desktop app → Extensions → Install from folder… → pick the folder.
Iterate by editing files and reopening the view; reinstall when
manifest changes. Test the tool directly on a host:
`python3 ~/.cam/extensions/<name>/main.py <method> '{}'`.
A same-name install shadows a built-in ("built-in · updated by user
copy") **when its manifest version is strictly newer** — that is the
offline update path for built-ins without an app release; bump the
version on every tar.gz you ship. An equal or older copy loses to the
built-in (an app reinstall/upgrade repairs stale shadows). Remove on
the winning user copy falls back to the bundled one.

## Hard rules

- No CDN / external URLs in the view (offline + MAS).
- No local Python execution anywhere in the pipeline (MAS sandbox).
- User-gesture folder installs only — never auto-download code.
- Package size caps: 4 MB per file, 8 MB total (registry.cjs); tar.gz
  packages must be `tar --format=ustar` with a single top-level folder.
