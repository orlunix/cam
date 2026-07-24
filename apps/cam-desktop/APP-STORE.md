# CAM Desktop — App Store submission pack

Everything needed to submit the mas build to App Store Connect.
Secrets (demo password, certificates) are **never** committed here — they
live only in App Store Connect fields / GitHub secrets.

## Store metadata

- **Name**: CAM Desktop
- **Subtitle**: AI agent control surface
- **Category**: Developer Tools
- **Keywords**: ssh, terminal, agent, ai, coding, tmux, remote, claude, codex, kimi
- **Description** (draft):
  > CAM Desktop connects to your own machines over SSH and gives you one
  > window to start, watch, and attach to AI coding agents. Switch between
  > agents instantly, scroll full terminal history, and manage long-running
  > sessions across many hosts. Credentials stay in the system Keychain;
  > nothing leaves your device except connections to hosts you configure.
- **Support URL**: https://orlunix.github.io/cam/
- **Privacy policy URL**: https://orlunix.github.io/cam/privacy.html
- **Screenshots**: 1280×800, captured against the demo server
  (Nodes → Sync → agent list → attached terminal with live output).

## Privacy labels (App Privacy section)

- Data collected: **none**
- Note for reviewers: SSH credentials are stored only in the device's
  Keychain / secure storage; no data leaves the device except connections
  to user-configured hosts.

## Export compliance

- Standard SSH/TLS encryption only → exempt (ITSAR/ENC self-classification,
  answer "Yes" to exemption, no CCATS needed).

## App Review Information

- **Sign-in required**: no account in the app itself; a demo SSH server is
  provided instead (fill credentials in App Store Connect only):

  ```
  Host: 115.159.85.212  Port: 22
  User: demo            Password: <demo password — App Store Connect only>
  ```

- **Review notes** (paste + fill password):

  > CAM Desktop manages AI coding agents on the user's own SSH hosts.
  > To review without owning a host, we provide a demo server:
  >
  > 1. Nodes → Add Node → enter Host 115.159.85.212, user "demo",
  >    password <password>, port 22 → Save → Sync.
  > 2. Three pre-staged agents appear (nightly-benchmark, code-review,
  >    weekly-summary). Open any agent and press Attach for a live
  >    terminal; nightly-benchmark prints continuous progress, code-review
  >    accepts interactive input.
  > 3. The app stores credentials only in the system Keychain. The
  >    `net:probe` IPC is a status-only reachability check (HTTP status,
  >    byte count, latency — no content) against the user's own relay URL.
  >    Clipboard file reads happen only on an explicit paste gesture.
  >    camc/skillm helpers are uploaded to and execute on the user's own
  >    SSH hosts only; no downloaded code executes locally.

## Pre-submission checklist

- [x] Demo server hardened (dedicated `demo` user, no sudo, process/login limits)
- [x] Demo agents pre-staged + verified end-to-end through the app's own SSH transport
- [x] Local filesystem-write surface removed (`local_unsupported` everywhere)
- [x] Support + privacy pages live (GitHub Pages)
- [ ] Apple-side (account holder only):
  - [ ] App Store Connect app record for `com.hren.cam.mas`
  - [ ] MAS provisioning profile → GitHub secret `MAS_PROVISIONING_PROFILE`
  - [ ] Two p12s (one key each): Apple Distribution cert+key →
        `MAS_APP_CSC_LINK` (base64), Mac Installer Distribution cert+key →
        `MAS_INSTALLER_CSC_LINK` (base64), shared p12 password →
        `MAS_CSC_KEY_PASSWORD` — separate from the Developer-ID `CSC_LINK`
        used by the DMG flow so the two tracks never interfere
  - [ ] Run `CAM Desktop — Mac App Store (mas pkg)` workflow → signed pkg
  - [ ] Sandbox smoke per `MAS-SPIKE.md` on a real Mac
  - [ ] 1280×800 screenshots (Mac)
  - [ ] TestFlight for Mac → submit for review
