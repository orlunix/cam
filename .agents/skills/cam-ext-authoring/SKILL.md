---
name: cam-ext-authoring
description: Design and build a CAM Desktop extension (folder with manifest.yaml + optional index.html view + optional main.py remote tool). Use when the user wants to create, package, or debug an extension for CAM Desktop, or asks how the extensions system works.
---

# CAM Desktop extension authoring

Contract: `extensions/SPEC.md`. Authoring guide: `extensions/GUIDE.md`.
Canonical sample: `extensions/examples/hello-ext/` (used by tests).

## Package shape

```
my-ext/
  manifest.yaml     # name [a-z0-9-]{1,32}, version, title, capabilities
  index.html        # optional UI — sandboxed iframe, bridge-only
  main.py           # optional remote tool — python3.6 stdlib only
```

Entry resolution: `index.html` wins → else exactly one `*.html` → else
`view_ambiguous` error. Same for `main.py` / `*.py`. No entries at all
= `empty_extension` error.

## Bridge (view side)

`<script src="../client.js"></script>`, then:

- `camExt.call('agents.list')` / `contexts.list` — read-only, always allowed
- `camExt.call('agents.capture', {id, lines})`
- `camExt.call('ext.call', {context, method, args})` — needs `exec` capability
- `camExt.call('files.read', {context, path})` — needs `files:read`

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

## Hard rules

- No CDN / external URLs in the view (offline + MAS).
- No local Python execution anywhere in the pipeline (MAS sandbox).
- User-gesture folder installs only — never auto-download code.
