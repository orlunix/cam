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

First-party built-ins have a third option: declare `native: <mode>` and
the view is a **built-in app page** (like Skills) instead of an iframe —
the package then carries no `index.html`, only the remote tool. Use this
when the UI needs full app integration; the trade-off is that view
changes need an app release, while the tool (`main.py`) stays updatable
via a same-name user install.

## 2. manifest.yaml

```yaml
name: my-ext          # [a-z0-9-]{1,32} — becomes the install dir name
version: 0.1.0
title: My Tool        # shown in the Extensions page
native: my-mode       # built-ins only: view is a native app page (no .html)
mounts:
  - agent             # also list in the agent page Ext▾ menu (per-agent)
capabilities:
  - exec              # only if you actually call main.py
  - files:read        # only if you read remote files
```

Rules of thumb:

- Declare the **minimum** capabilities. They are shown to the user and
  enforced by the bridge; over-declaring erodes trust.
- Keep `name` stable forever — installs replace by name, and the remote
  deploy dir is `$HOME/.cam/extensions/<name>/`.
- Add `mounts: [agent]` when the extension works on one agent (read its
  binding via `camExt.call('app.context')`; native pages receive the
  agent through their open handoff).
- The app Edit page (Extensions → Settings) renders exactly one uniform
  platform field per extension: `show_in_agent_menu` (boolean, default
  false — the agent page Ext menu starts empty; users pin entries
  explicitly). Custom settings are your own affair: read them with
  `await camExt.call('ext.config')` and surface them on your own page,
  like agent-doctor does with its `prompt_warn_kb` reference next to
  its review controls.

## 3. The view (index.html)

- **No own title bar.** Every extension opens under the app-managed
  chrome: `title@version`, your manifest `description`, a Back button,
  then a divider. Start your view with content — do not repeat the
  title/description in the page (the built-in assistant models this).
- Set a manifest `description` — it is the chrome's subtitle (and the
  list row's summary).
- Self-contained: relative paths only, **no CDN** (offline + MAS).
- Include the bridge client: `<script src="../client.js"></script>`.
- Everything you can ask the app (v1):

  ```js
  const agents   = await camExt.call('agents.list');
  const ctxs     = await camExt.call('contexts.list');
  const text     = await camExt.call('agents.capture', { id, lines: 200 });
  const ctx      = await camExt.call('app.context');                    // per-agent binding
  const prompt   = await camExt.call('agents.workspaceRead', { path: 'AGENTS.md' });
  const cron     = await camExt.call('agents.cronJobs');
  const result   = await camExt.call('ext.call', { context, method, args });   // needs exec
  const listing  = await camExt.call('files.read', { context, path });         // needs files:read
  const ctxs2    = await camExt.hubCall('GET', '/api/contexts');               // needs hub:api — full /api/* passthrough
  ```

- Errors reject with `Error(<code>)` — e.g. `capability_denied:exec`
  means your manifest is missing the capability.
- Do not try to reach the parent page, cookies, localStorage, or the
  network — the sandbox blocks all of it by design.
- Durable local state: `camExt.storageGet()` / `camExt.storageSet(obj)` —
  your own `ext-data/<name>/storage.json` (≤512KB), surviving reinstalls
  and updates, deleted only on full Remove. Never write files anywhere
  else. Per-OS locations of `ext-data/`: SPEC §6 "Data locations".

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

Template: copy `packages/assistant/main.py` — it has the dispatcher,
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
folder (`.tar.gz` / `.tgz` / `.tar` packages install directly). There
is no store and no auto-download (MAS constraint, SPEC §7).

## 7. Common pitfalls

- **Multiple .html / .py files** with neither `index.html` nor
  `main.py` → `view_ambiguous` / `tool_ambiguous`. Keep one entry of
  each kind.
- **`native:` + index.html** → `invalid_native`. A native extension's
  UI is app code; the package ships no view.
- **Absolute paths or URLs** in index.html → break under the sandbox.
  Use relative paths.
- **Thinking the tool runs locally.** It never does. Test on a real
  host early.
- **Big packages.** 8MB total / 4MB per file caps; keep assets lean.
