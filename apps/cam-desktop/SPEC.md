# CAM Desktop Spec

Status: **superseded** — historical Tauri / `camc`-CLI scaffold notes.

This document originally described a Tauri+React desktop client that
shelled out to `camc` via a typed CLI bridge. The active CAM Desktop is
**Electron** (under `apps/cam-desktop/electron/`) and is a thin client
over the CAM hub HTTP/WS API; it does not run controller commands
locally.

For current requirements and design, read:

- [`../../docs/desktop/README.md`](../../docs/desktop/README.md) —
  architecture overview (Desktop UI → CAM Hub/API → Remote
  Controller/Node) and file index.
- [`../../docs/desktop/requirements.md`](../../docs/desktop/requirements.md)
  — canonical requirement registry with stable IDs (`ARCH/HUB/NODE/
  REMOTE/SSH/RUN/EDIT/INP/OUT/SET/PKG/TERM/LOC/SEC/VFY`).
- [`../../docs/desktop-ui-spec.md`](../../docs/desktop-ui-spec.md) —
  current milestone / product spec.
- [`../../docs/desktop/local-integrated-mode-spec.md`](../../docs/desktop/local-integrated-mode-spec.md)
  — the Advanced Local Node Mode (single-machine fallback). Not the
  default product flow.

The old content (Tauri scaffold goals, P0 / P1 `camc` command mapping,
backend profile shape `{kind: 'local'|'wsl'|'ssh'}`) does not apply to
the Electron path and would actively mislead an implementer; it has
been removed. The Tauri files remain in this folder (`src/`,
`src-tauri/`) only as historical reference. See `apps/cam-desktop/README.md`.
