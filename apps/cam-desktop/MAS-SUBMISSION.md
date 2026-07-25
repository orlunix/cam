# MAS submission playbook — CAM Desktop

How CAM Desktop gets into the Mac App Store: the working pipeline, every
pitfall hit on the way (2026-07-24/25, 14 workflow runs), and how to
avoid them next time. Companion docs: `MAS-SPIKE.md` (sandbox smoke),
`MAS-REVIEW-RISKS.md` (review risks), `APP-STORE.md` (store metadata).

## The working pipeline

```
git tag -f cam-desktop-mas-vX.Y.Z && git push -f github <tag>
  └─ .github/workflows/cam-desktop-mas.yml (macos runner)
       1. write embedded.provisionprofile  (secret → file)
       2. npm ci + lint:electron
       3. import signing identities as PEM into a temp keychain
       4. electron-builder --mac mas --universal
            → signed CAM Desktop.app + CAM-Desktop-<v>-universal.pkg
       5. upload-artifact (backup)
       6. xcrun altool --upload-app (App Store Connect API key)
  └─ App Store Connect: build processes → VALID → attach to version → submit
```

Releasing is one tag push; everything else is automated.

## Prerequisites (one-time)

| Thing | Where it lives | Notes |
|---|---|---|
| Apple Distribution cert+key | secret `MAS_APP_PEM` | base64 of **PEM** (cert + private key), NOT p12 |
| Mac Installer Distribution cert+key | secret `MAS_INSTALLER_PEM` | same |
| p12 password | — | not needed anymore (PEM has no password) |
| MAS provisioning profile for `com.hren.cam.mas` | secret `MAS_PROVISIONING_PROFILE` | base64 of `.provisionprofile`; Mac App Store type |
| Team ID | secret `TEAM_ID` | `ULL2CR6L6J` |
| App Store Connect API key | secrets `APP_STORE_CONNECT_API_KEY` (base64 .p8), `APP_STORE_CONNECT_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID` | Users and Access → Integrations |
| App record | App Store Connect web UI | **must be created manually** (see pitfall 9) |
| mas config | `apps/cam-desktop/package.json` → `build.mas` | `appId: com.hren.cam.mas`, `minimumSystemVersion: 12.0`, entitlements plists |

## Pitfalls, in the order we hit them

### 1. `workflow_dispatch` 404 on a non-default branch

`POST /dispatches` only works for workflows present on the repo's
**default branch** (ours: `master`; desktop work lives on
`camui-desktop-v2`).
**Fix**: trigger the mas workflow by tag push (`on.push.tags:
['cam-desktop-mas-v*']`), which runs the workflow from the tag's ref.

### 2. Legacy p12 → `MAC verification failed during PKCS12 import`

A p12 exported with `openssl pkcs12 -export -legacy` (RC2-40, common in
older tutorials) is rejected by modern macOS `security import`.

### 3. openssl-3 default p12 → same error

openssl 3 writes a **SHA-256 MAC** that macOS `security` cannot verify.
`-macalg SHA1` does NOT save it either (next error). Two p12s also
can't be merged into one with openssl (`-export` takes a single key).

### 4. Any openssl p12 → `Unknown format in import`

Even a parseable (openssl-verified, sha256-matched) p12 with
AES-256-CBC + SHA1 MAC was refused by `security import`.
**Fix (the durable one): stop using p12 on CI.** Import identities as
plain **PEM** (cert + private key concatenated), which `security
import` accepts natively:

```bash
security import mas-app.pem -k "$KC" -T /usr/bin/codesign -T /usr/bin/productbuild
```

### 5. electron-builder mas config: `bundleId` is not a valid key

`ValidationError: Invalid configuration object`. The mas block accepts
`appId`, not `bundleId`:
**Fix**: `"mas": { "appId": "com.hren.cam.mas", ... }` — the DMG track
keeps the top-level `appId: com.hren.cam`, so the two tracks ship
different bundle ids cleanly.

### 6. Build step hangs 25+ minutes at `productbuild --sign …`

The .app signs fine; **pkg** signing then blocks forever: `productbuild`
waits on a GUI key-access prompt that can never appear on CI.
`codesign` being authorized is not enough.
**Fix**: authorize `productbuild` explicitly at import time AND in the
partition list:

```bash
security import x.pem -k "$KC" -T /usr/bin/codesign -T /usr/bin/security -T /usr/bin/productbuild
security set-key-partition-list -S apple-tool:,apple:,codesign:,productbuild:,security: -s -k "$KC_PASS" "$KC"
```

Also: put `timeout-minutes: 25` on the build step so a hang dies
visibly instead of burning an hour, and `DEBUG:
electron-osx-sign,electron-builder` to see exactly where it stops.

### 7. Artifact upload: `No files were found … dist/*.pkg`

electron-builder writes the pkg to `dist/mas-arm64/` (a subdirectory);
the glob was non-recursive.
**Fix**: `path: apps/cam-desktop/dist/**/*.pkg`.

### 8. Upload step: `find: apps/cam-desktop/dist: No such file or directory`

Shell steps run with `working-directory: apps/cam-desktop`, so the path
prefix was doubled. `actions/upload-artifact` paths are
workspace-relative, shell `run:` paths are cwd-relative — easy to mix
up.
**Fix**: `find dist -name '*.pkg'` in shell steps.

### 9. altool: `Cannot determine the Apple ID from Bundle ID …`

The **app record must exist in App Store Connect before the first
upload**. A Connect API key with the default (non-Admin) role gets
`403 FORBIDDEN` on `POST /v1/apps` — GET/UPDATE only.
**Fix**: create the record manually once (Apps → + → macOS, name,
bundle id `com.hren.cam.mas`, SKU). Everything after that is API-driven.

### 10. Apple bundle validation `90869` (the sneaky one)

> Invalid bundle. The "CAM Desktop.app" bundle supports arm64 but not
> Intel-based Mac computers. … arm64-only requires deployment target
> 12.0 or higher.

Setting `minimumSystemVersion` and even
`extendInfo.LSMinimumSystemVersion` in package.json did **not** help:
Apple reads the deployment target from the **mach-o load commands**
(`LC_BUILD_VERSION`), not from Info.plist. Electron 31 links macOS
11.0; re-linking is not an option.
**Fix**: build **universal** (x64+arm64) — no 12.0 requirement:

```json
"build:mas": "electron-builder --mac mas --universal ..."
```

## What remains manual (by design)

- App record creation (one-time, pitfall 9)
- 1280×800 Mac screenshots (take them on a real Mac against the demo
  server — see `APP-STORE.md`)
- Privacy labels, age rating, and the final **Submit for Review**
  click (review notes are pre-drafted in `APP-STORE.md`)

## Demo server (for review + screenshots)

Pre-staged node `115.159.85.212`, user `demo` (no sudo, process/login
limits): `claude-assistant` is a real Claude Code CLI backed by a Kimi
API key; `nightly-benchmark` / `code-review` / `weekly-summary` are
scripted camc agents; codex/cursor tool choices launch simulated CLIs.
Credentials go in App Review Information only — never in the repo.
