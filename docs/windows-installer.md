# Cam Desktop — Windows Installer

Cam Desktop ships a **single Windows installer**: one `.msi` file. Users
download one artifact, run it, and the app is installed. There is no NSIS
`.exe` companion and no separate WSL or `camc` bootstrap installer.

This mirrors the ChatShell distribution model: one MSI built via
`tauri build --target x86_64-pc-windows-msvc --bundles msi` on a
`windows-latest` GitHub Actions runner.

## What the MSI installs

- The Cam Desktop UI (Tauri 2 + React frontend, Rust shell).
- The `camc_exec` Tauri bridge that runs `camc` over argv for the local
  and WSL backend profiles, or over SSH for the SSH profile.
- The Cam Desktop app icon (derived from the existing CAM PWA icon at
  `web/assets/icon-512.png`).

## What the MSI does NOT install

- It does **not** bundle `camc`, Python, WSL, or any tmux server.
- It does **not** install the cam Python package, `cam serve`, or the
  relay.
- It does **not** ship per-user contexts, profiles, or credentials.

Cam Desktop is a control surface. At runtime it reaches `camc` through one
of three backend profiles selected in the Connection Panel:

| Profile | Where `camc` lives                        | Prerequisite                                       |
|---------|-------------------------------------------|----------------------------------------------------|
| local   | A Windows-native `camc.exe` on the user's `PATH` | (Currently unsupported — `camc` is Linux/macOS first) |
| wsl     | `camc` inside a WSL distro (`wsl.exe --exec camc ...`) | WSL distro with Python 3.10+ and `camc` installed |
| ssh     | `~/.cam/camc` on a remote machine        | OpenSSH client + reachable host with `camc` installed |

The expected first-time flow on Windows is therefore:

1. Run the MSI to install Cam Desktop.
2. Open Cam Desktop, switch the backend profile to `wsl` (or `ssh`).
3. The UI talks to whichever `camc` instance is configured.

A guided WSL/`camc` bootstrap wizard is **out of scope for this milestone**
and tracked separately. The packaging convention here exists so that a
future bootstrap can be added without changing the installer story.

## Bundle policy

`apps/cam-desktop/src-tauri/tauri.conf.json` keeps the generic
`bundle.targets` set to `"all"`, the Tauri default. This means a developer
running `npm run build` on macOS or Linux still gets the platform-native
bundle for that host (`.dmg`, `.deb`, `.AppImage`) without a surprising
hard failure from a Windows-only target.

The "single Windows installer" rule is enforced at the **workflow** level,
not in the generic config:

- `apps/cam-desktop/package.json` script `build:windows-msi` passes
  `--bundles msi` so a local Windows developer can also produce exactly
  one MSI on demand.
- `.github/workflows/cam-desktop-windows.yml` runs that script on
  `windows-latest` and uploads only the resulting `.msi`. No NSIS, no
  unsigned exe, no companion installer.

Cam Desktop currently has no formal release pipeline for macOS or Linux;
those bundles produced by a generic `npm run build` are dev-only artifacts
until a corresponding workflow is added.

## Building locally

On Windows:

```powershell
cd apps/cam-desktop
npm ci
npm run build:windows-msi
```

Produces a single MSI at:

```
apps/cam-desktop/src-tauri/target/x86_64-pc-windows-msvc/release/bundle/msi/*.msi
```

Requires the Rust toolchain with the `x86_64-pc-windows-msvc` target,
the WiX toolset (Tauri downloads it on first run), and Visual Studio Build
Tools (Tauri prerequisites).

On macOS or Linux a generic `npm run build` will still succeed and produce
the host's native bundle; it just will not produce an MSI because the
Windows toolchain is unavailable.

## CI release (GitHub Actions)

Workflow file: `.github/workflows/cam-desktop-windows.yml`.

- Triggers: pushing a tag matching `cam-desktop-v*`, or manual
  `workflow_dispatch`.
- Runner: `windows-latest`.
- Steps: checkout → Node 20 + npm ci → Rust stable with
  `x86_64-pc-windows-msvc` target → `Swatinem/rust-cache` → `npm run
  build:windows-msi` → upload the `cam-desktop-windows-msi` artifact
  containing the single `.msi`.

The workflow uploads exactly one artifact named `cam-desktop-windows-msi`
and uses `if-no-files-found: error` so a green build cannot silently ship
without the installer. A future `release` job can attach this artifact to
a GitHub Release; that job is intentionally not part of this first
packaging milestone so that the build can be exercised via
`workflow_dispatch` without creating draft releases.

## Icons

`apps/cam-desktop/src-tauri/icons/` contains the icon set referenced from
`bundle.icon` (32×32 PNG, 128×128 PNG, 128×128@2x PNG, 256×256 PNG, and a
multi-size `icon.ico` covering 16/24/32/48/64/128/256). All sizes are
generated from the existing CAM PWA icon at `web/assets/icon-512.png`,
keeping branding consistent with the other CAM frontends and avoiding any
ChatShell-derived assets.

Total icon footprint is under 10 KB.

## Known follow-ups

- **Code signing**: not configured yet. The MSI will install with a
  SmartScreen warning until an Authenticode certificate and signing step
  are added. Tauri 2 supports this via `WINDOWS_CERTIFICATE` /
  `WINDOWS_CERTIFICATE_PASSWORD` secrets — to be wired when a cert is
  available.
- **Higher-resolution icon**: the current icons are derived from a 512×512
  source. If the brand team produces a vector or higher-res master, regenerate
  the set so the MSI installer chrome looks crisper at 256×256.
- **macOS / Linux release workflows**: out of scope here; only the Windows
  MSI is in scope for this packaging milestone.
- **WSL/`camc` setup wizard**: out of scope here; tracked as a desktop UI
  enhancement that runs after the MSI installs.
