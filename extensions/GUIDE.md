# Designing a CAM Desktop Extension

Read `SPEC.md` first — it is the contract. This guide is the practical
side: how to shape an extension so it installs cleanly, behaves well,
and survives review.

## 1. The mental model

An extension is a **folder**:

```
my-ext/
  manifest.yaml     # name / version / capabilities
  index.html        # optional UI (sandboxed iframe)
  main.py           # optional remote tool (python3.6 stdlib, runs on SSH hosts)
```

Two halves, both optional, each doing what it is best at:

- **view** (index.html) — presentation. Runs sandboxed, talks to the
  app only through the bridge (`camExt.call(...)`).
- **tool** (main.py) — remote work. Runs on the user's SSH hosts, not
  on the machine running the app.

If you need neither heavy UI nor remote work, you probably want a
native app feature, not an extension.

## 2. manifest.yaml

```yaml
name: my-ext          # [a-z0-9-]{1,32} — becomes the install dir name
version: 0.1.0
title: My Tool        # shown in the Extensions page
capabilities:
  - exec              # only if you actually call main.py
  - files:read        # only if you read remote files
```

Rules of thumb:

- Declare the **minimum** capabilities. They are shown to the user and
  enforced by the bridge; over-declaring erodes trust.
- Keep `name` stable forever — installs replace by name, and the remote
  deploy dir is `$HOME/.cam/extensions/<name>/`.

## 3. The view (index.html)

- Self-contained: relative paths only, **no CDN** (offline + MAS).
- Include the bridge client: `<script src="../client.js"></script>`.
- Everything you can ask the app (v1):

  ```js
  const agents   = await camExt.call('agents.list');
  const ctxs     = await camExt.call('contexts.list');
  const text     = await camExt.call('agents.capture', { id, lines: 200 });
  const result   = await camExt.call('ext.call', { context, method, args });   // needs exec
  const listing  = await camExt.call('files.read', { context, path });         // needs files:read
  ```

- Errors reject with `Error(<code>)` — e.g. `capability_denied:exec`
  means your manifest is missing the capability.
- Do not try to reach the parent page, cookies, localStorage, or the
  network — the sandbox blocks all of it by design.

## 4. The tool (main.py)

Contract (SPEC §3):

```
python3 main.py <method> '<json-args>'   →  one JSON object on stdout
```

- python3.6+ stdlib only. No pip, no third-party imports — the host may
  be a bare 2016-era Linux.
- Dispatch on `method`, parse argv[2] as JSON, print ONE JSON object.
  Errors: `{"error": "code", "detail": "..."}`.
- Keep a method under ~25s (the exec pool default budget is 30s); page
  or stream long work.
- Stay read-only unless the extension's whole point is mutation — and
  say so in the title/notes.

Template: copy `examples/hello-ext/main.py` — it has the dispatcher,
JSON contract, and error shapes wired.

## 5. Local development loop

1. Build your folder anywhere (e.g. `~/dev/my-ext/`).
2. App → Extensions → Install from folder… → pick it.
3. Open → iterate: edit files, close the view, reopen (no reinstall
   needed unless the manifest changed — reinstall on manifest changes).
4. For tool work, iterate on the host directly:
   `python3 ~/.cam/extensions/my-ext/main.py mymethod '{}'`.

## 6. Distribution

Zip the folder, share it; the recipient unpacks and installs the
folder. There is no store and no auto-download (MAS constraint,
SPEC §7).

## 7. Common pitfalls

- **Multiple .html / .py files** with neither `index.html` nor
  `main.py` → `view_ambiguous` / `tool_ambiguous`. Keep one entry of
  each kind.
- **Absolute paths or URLs** in index.html → break under the sandbox.
  Use relative paths.
- **Thinking the tool runs locally.** It never does. Test on a real
  host early.
- **Big packages.** 8MB total / 4MB per file caps; keep assets lean.
