# Cam Desktop

Cam Desktop is the Windows-first desktop package for CAM.

The canonical product spec is now:

- [Desktop UI Spec v2](../../docs/desktop-ui-spec.md)

The original independent React/Tauri desktop direction is archived:

- `../../docs/archive/desktop-ui-v1-spec.md`
- `../../docs/archive/cam-desktop-v1-scaffold-spec.md`
- `../../docs/archive/cam-desktop-v1-chatshell-analysis.md`
- `../../docs/archive/cam-desktop-v1-chatshell-baseline.md`
- `../../docs/archive/chatshell-reference-evaluation-v1.md`

The current code in this directory is the V1 desktop implementation. It proved
Windows packaging and a minimal list/output/input loop, but future Desktop v2
work should derive the UI from the existing `web/` app instead of expanding this
separate product surface.

Target direction:

- Electron shell with a small native boundary.
- Bundled WebUI-derived desktop entry.
- Shared `web/js/api.js` and `web/js/state.js` behavior with mobile/PWA.
- Desktop-specific layout: persistent left nav, large output workspace, bottom
  composer.
- Windows MSI first, macOS package later.
- Phase 1 connects to an already-running CAM direct/relay endpoint. It does not
  start or bundle the relay server.

## Legacy V1 Backend Profiles

The profile shape below belongs to the legacy standalone React/Tauri V1 app.
Desktop v2 should instead reuse the existing WebUI direct/relay connection
model and treat the CAM/relay server as an already-running external endpoint.

Supported profile shapes:

```ts
type BackendProfile =
  | { kind: "local"; camcPath?: string }
  | { kind: "wsl"; distro?: string; camcPath?: string }
  | { kind: "ssh"; host: string; user?: string; port?: number; camcPath?: string };
```

Default Windows path should be WSL:

```text
wsl.exe --exec /usr/bin/env camc ...
```

Default Linux/macOS path should be local:

```text
camc ...
```

SSH is available for remote Linux hosts, but a future stdio API will be
more robust for long-running streams.

## Development

Prerequisites for the legacy V1 Tauri app:

- Node.js 20+
- Rust stable
- Tauri prerequisites for the current OS

Install and run:

```bash
npm install
npm run dev
```

Build:

```bash
npm run build
```

## Product Direction

See [Desktop UI Spec v2](../../docs/desktop-ui-spec.md).

Do not add new product features to the standalone V1 React/Tauri UI unless
explicitly requested. The next desktop work should start by adding a
WebUI-derived desktop entry and an Electron shell that loads it.

## Non-Goals For MVP

- Bundling `camc` on Windows.
- Replacing tmux.
- Running agents natively on Windows.
- Starting or supervising the relay server from the desktop app in Phase 1.
- Forking mobile/WebUI product behavior into a second desktop-only UI.
