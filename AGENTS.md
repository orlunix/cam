# Agent Workflow Notes

## Cam Project Collaboration

When working in this repository, default to a reviewer/integrator split
when the sibling `cam-dev` agent is available:

- `cam-dev` implements changes.
- Codex reviews the diff, runs focused and full verification, asks for
  revisions when needed, and summarizes the result to the user.

Current known local sibling:

- name: `cam-dev`
- agent id: `f1a1a661`
- expected path: `/home/hren/.openclaw/workspace/cam`

Use `camc list`, `camc status f1a1a661`, and `camc capture f1a1a661
--lines N` to verify the agent is available before delegating. Send
implementation requests with `camc msg send f1a1a661 -t "..."`, using
clear scope, expected files, tests, and a parseable completion format.
For planner-style requests, prefix the prompt with an explicit preflight:
use `ls`, `grep`, and `find` first to inspect relevant files, paths, and
attributes before proposing or changing implementation details.

Direct local edits are acceptable for tiny mechanical changes, urgent
debugging/unblocks, or when `cam-dev` is unavailable. If bypassing
`cam-dev`, state the reason briefly.

## Ownership Split (user instruction, 2026-07-23)

- **Kimi (this assistant) owns DESKTOP work only** — `apps/cam-desktop/`
  and `web/` — because this machine has the desktop build/test/release
  environment (electron-builder, MSI/DMG, Release flow). Also in scope:
  `apps/cam-container/` (the CAM WebUI container — it *reuses*
  `apps/cam-desktop/electron/*.cjs` backends **read-only**; browser/
  container adaptations belong in `apps/cam-container/` as serve-time
  transforms or shims, not in desktop files).
- **camc is owned by another agent** — do NOT edit `src/camc`,
  `src/camc_pkg/`, `dist/camc`, or `build_camc.py` on this machine.
  Route camc changes to the owning agent (see the cam-dev sibling
  above) instead of implementing them here.
- Exception: only when the user explicitly asks for a change outside
  the desktop scope may Kimi edit those files, and only for that
  specific ask.

Do not push unless the user explicitly authorizes it. Commit only when
the user asks for a commit or clearly approves the completed change.

## Extension-first Rule (user instruction, 2026-08-11)

After the 0.2.4 ext stabilization round, **avoid rebuilding the MSI for
extension work**. New tools/features ship as extensions (iframe view +
`main.py` remote tool + per-ext attributes via Extensions → Settings);
built-in tool logic updates ship as same-name user packages (shadowing).
Shadowing is version-gated (0.2.30): the user copy wins only when its
manifest version is strictly newer than the built-in — bump the version
in every shipped tar.gz; a tie or an older copy loses, so an app
reinstall/upgrade repairs stale shadows.
App-shell changes (new MSI) are justified only for: new bridge
capabilities, new native pages, registry/hub/transport fixes.

Stability rules learned the hard way (test/ext-nav.test.cjs guards them —
keep it green):

- Every import of a shared module must use the identical `?v=` URL —
  a versionless duplicate creates a second module instance and cross-
  module handoffs fail silently.
- Page-specific CSS must be namespaced (`#mode-...`); never reuse
  generic classes for hide/show rules (a generic selector once hid the
  page's own Back button).
- Never declare a local function named like a mount parameter — a local
  `function setMode` in agent-console.js hoisted and shadowed the
  app-level `setMode` param and killed Ext▾ navigation for weeks
  (renamed `setOutputMode`).
- No source edits while an MSI build is running (WiX EBUSY).
- Same-version MSI does not replace files (Windows Installer rule);
  local test installs need uninstall-first, or bump the version.
- Extension packages are capped at 4 MB per file / 8 MB total
  (registry.cjs `MAX_FILE_BYTES`/`MAX_TOTAL_BYTES`). cam-assist's SDK
  bundle exceeded the per-file cap after the 0.5.0 pi migration, so
  `build.mjs` emits a self-inflating gzip+base64 wrapper — always ship
  the wrapped `dist/cam-assist.js` in the tar.gz (guide §15.13), and
  pack with `tar --format=ustar` (the registry's minimal untar skips
  GNU longname entries).

## Verification Rules (hard-won)

- **Connection-layer changes (ssh-transport, auth, algorithms) must be
  verified against the packaged Electron runtime, not only WSL node.**
  Electron ships BoringSSL while WSL/dev node links OpenSSL — feature
  support differs (2026-07-28: chacha20-poly1305 passed in WSL, broke
  every attach in the packaged MSI). WSL tests are reference only;
  the MSI smoke test is the bar.
- Never enumerate feature-detected crypto algorithms by name; extend
  the library's runtime-verified defaults (append form) with pure-JS
  legacy algorithms only.
