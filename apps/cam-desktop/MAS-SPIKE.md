# MAS spike — sandbox checklist

Goal: verify CAM Desktop runs correctly under the Mac App Store sandbox
before submitting. Build: `cam-desktop-mas.yml` → signed `CAM-Desktop-<v>-arm64.pkg`
(mas target, entitlements in `apps/cam-desktop/build/entitlements.mas*.plist`).

## Why the sandbox surface is small

After removing local sessions, the app is a pure network control surface:

| Runtime behavior | Sandbox entitlement | Expected to work |
|---|---|---|
| ssh2 → user hosts / relay | `network.client` | yes |
| Embedded hub binds `127.0.0.1` | `network.server` | yes |
| safeStorage (Keychain) | `keychain-access-groups` | yes |
| File pickers (keys/attachments) | `files.user-selected.read-only` | yes |
| Clipboard read | none needed | verify |
| camc/skillm upload (SFTP) | `network.client` | yes |
| No local process spawn | — | nothing to entitle (by design) |

## Install + verify on a real Mac

```bash
# verify signature + entitlements of the built app before installing
codesign --display --entitlements - --xml "CAM Desktop.app" | xmllint --format -
sudo installer -pkg CAM-Desktop-0.2.0-arm64.pkg -target /
```

## Smoke checklist (each must pass before submission)

- [ ] App launches under sandbox (no crash on start)
- [ ] Direct mode: embedded hub binds loopback, renderer connects, token handshake ok
- [ ] `codesign --verify --deep --strict` passes on the .app
- [ ] Add SSH node (key auth) → auto-sync pulls agents
- [ ] Start an agent on a node → camc auto-deploys over SFTP
- [ ] Attach terminal → interactive I/O, resize follows window
- [ ] Switch tabs rapidly → no detach, no size drift
- [ ] Relay profile: connects through the relay
- [ ] File picker: pick an SSH private key file (user-selected read works)
- [ ] Password save/reload: credential persists across restart (Keychain)
- [ ] Clipboard paste into terminal works
- [ ] No hidden outbound connections before user configures a host
- [ ] Logs show no sandbox denial (`log stream --predicate 'process == "CAM Desktop"'`)

## Known review-sensitive points to watch

- `net:probe` (renderer-triggered arbitrary GET) — must be explained in
  review notes or constrained before submission.
- Clipboard attachment read (`files:readClipboardAttachments`) — reads
  paths from the clipboard; user-gesture driven, must be documented.
- Upload of `camc`/`skillm` to the user's own SSH hosts — runs remotely,
  review notes must state "no code executes locally from the network".

## Next after spike

1. Demo mode (**DONE 2026-07-18**): Settings → Demo installs an offline
   simulated node (`demo-node`) — agents, terminal playback, and input
   all work without SSH (`electron/demo-transport.cjs`, self-contained
   and deletable). Reviewers can exercise the full flow offline; also
   used for screenshots.
2. Privacy policy + support URLs, screenshots, privacy labels.
3. App Store Connect record for com.hren.cam + MAS provisioning profile
   (→ `MAS_PROVISIONING_PROFILE` secret) and installer certificate.
4. TestFlight for Mac pass, then App Review.
