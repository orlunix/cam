# CAM Desktop Extensions — Spec v0 (draft)

Extensions are the unified "tool" surface of CAM Desktop. Skills and
Todos become ordinary extensions; third parties can add their own.
Design drivers: python3.6 stdlib-only remote part (same compatibility
bar as camc), sandboxed UI, and **no downloaded code executing locally**
(MAS review commitment — user-gesture installs only).

## 1. Package format

An extension is a **folder** — installed as-is:

```
my-ext/
  manifest.yaml    # required
  index.html       # UI entry (optional — see resolution rules)
  main.js / style.css / assets/…   # anything index.html references
  main.py          # remote entry (optional — python3.6 stdlib only)
```

Folder-only on purpose: no archive parsing in the hub (no tar/zip
reader, no traversal or decompression-bomb surface), and the dev loop
is identical to the install loop. Sharing a single file happens
outside the app — the recipient unpacks it with the OS tools and
installs the resulting folder.

## 2. Entry resolution rules

| part | rule |
|---|---|
| view | `index.html` wins → else exactly one `*.html` → else error (`view_ambiguous`) if several → else tool-only (no UI) |
| tool | `main.py` wins → else exactly one `*.py` → else error (`tool_ambiguous`) if several → else UI-only |

## 3. manifest.yaml

```yaml
name: my-ext            # [a-z0-9-]{1,32}, unique per install
version: 0.1.0          # semver-ish
title: My Tool          # display name (optional, defaults to name)
capabilities:           # empty list = pure content extension
  - exec                # may run its main.py on remote hosts
  - files:read          # may read remote files via the bridge
  - files:write         # may write remote files via the bridge
```

`capabilities` is the whole permission model: declared up front, shown
at install time, enforced by the bridge. An extension gets nothing it
did not declare.

## 4. Execution model

Two iron rules:

1. **The hub never runs Python.** `main.py` runs ONLY on remote SSH
   hosts, deployed via the existing hardened path (chunked SFTP upload
   → `$HOME/.cam/extensions/<name>/` → version probe). Invocation:
   `python3 main.py <method> '<json-args>'` over the exec pool; JSON
   on stdout. Host needs python3.6+ and nothing else.
2. **The view never touches app internals.** `index.html` loads in a
   sandboxed iframe (no same-origin, locked CSP) and talks to the app
   over a narrow `postMessage` bridge only.

## 5. Bridge API (v1)

View → app (postMessage, request/response with ids):

- `agents.list()` → agent records (read-only)
- `agents.capture(id, lines)` → captured text
- `ext.call(hostContext, method, args)` → run `main.py <method>` on the
  given context's host (requires `exec`)
- `files.read(ctx, path)` / `files.write(ctx, path, text)` (require the
  matching `files:*` capability)

App → view: only the response channel plus `theme` (current palette).
No app-state mutation, no credential access, no direct network.

## 6. Install / manage (Extensions page)

```
Extensions
─────────────────────────────────────────────
 [Install from folder…]
 ─────────────────────
 ● skills    v1.0  built-in   [open]
 ● todos     v1.0  built-in   [open]
 ○ my-ext    v0.1  user       [open] [disable] [remove]
```

Install flow (hub side, all local ops):

1. Renderer picks the extension folder via the Electron directory
   dialog.
2. Hub validates: manifest present, name/version legal, size caps,
   entry resolution rules (§2) must yield a clear answer.
3. Copy to `userData/extensions/<name>/`; record in the store.
4. Listed as `user` extension; enabled immediately. Same name again =
   update (replace). Remove deletes the folder + record; if its view
   is open the UI kicks back to Agents. Disable keeps the files but
   hides the extension (its deployed main.py stays on remote hosts,
   dormant and harmless).

Built-in extensions (skills, todos) ship inside the app bundle —
they are app content, not downloads.

## 7. MAS stance

- No in-app store, no auto-download. Installs are explicit user-file
  gestures (same pattern as selecting an SSH key file).
- Review notes line stays true: "extensions are user-installed local
  packages; no code is downloaded and executed automatically."

## 8. Migration plan

1. Spec sign-off (this document).
2. Loader + bridge + Extensions page (desktop-only first).
3. Migrate **todos** (simplest: remote file read + one panel).
4. Migrate **skills** (has a remote tool part — proves the full shape).
5. Stabilize, then document for third parties.
