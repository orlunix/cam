# CAM Desktop — Release Process

How the macOS DMG and Windows MSI get built and published, and how the
signing secrets are managed. Current distribution: **Developer ID
direct download** (Mac App Store line is parked — see MAS-SPIKE.md).

## Overview

```
camui-desktop-v2 (branch)
   │  commit + push → GitLab (origin) + GitHub (github)
   │  git tag -f cam-desktop-v0.2.0 <sha>
   │  git push -f github cam-desktop-v0.2.0
   ▼
GitHub Actions (trigger: tags matching 'cam-desktop-v*')
   ├─ cam-desktop-macos.yml   → signed + notarized DMG (arm64)
   └─ cam-desktop-windows.yml → MSI (unsigned)
   ▼
GitHub Release "cam-desktop-v0.2.0"  (assets attached with --clobber)
```

There is deliberately **one moving tag** (`cam-desktop-v0.2.0`, matching
`version` in `apps/cam-desktop/package.json`): the Release page always
carries exactly the latest verified build. When we want immutable
versions, bump `package.json` version and use a new tag instead of
moving this one.

## Pre-release gate (mandatory)

Do not move the tag until all of these are green:

1. Unit/syntax suites (from `apps/cam-desktop`):
   `npm run lint:electron && npm run test:hub && npm run test:term && npm run test:start`
2. Boot smoke (launches the real app, asserts preload loads + hub
   starts — catches sandbox-require and boot-chain regressions that
   static checks cannot see):
   `npm run test:smoke`
3. Real-machine E2E against at least one real SSH node (attach, tab
   strip, tab switching). The CDP harnesses used so far live in
   `.tools/cdp-*.cjs` (dev-run Electron with `--remote-debugging-port`).
4. The diff is minimal and scoped to the fix being released.

## Release steps

```bash
git add -A && git commit -m "desktop: ..."
git push origin camui-desktop-v2      # GitLab (primary repo)
git push github camui-desktop-v2      # GitHub (build farm)
git tag -f cam-desktop-v0.2.0 <commit>
git push -f github cam-desktop-v0.2.0 # triggers both workflows
```

Watch the builds:

```bash
curl -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/orlunix/cam/actions/runs?per_page=4
```

Verify the published assets (sha256 must match what you ship to users):

```bash
curl -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/orlunix/cam/releases/tags/cam-desktop-v0.2.0
# assets[].digest holds "sha256:..."; compare with `shasum -a 256 file.dmg`
```

Optional content check of the DMG (Windows, 7-Zip + asar):

```bash
"/c/Program Files/7-Zip/7z.exe" e CAM-Desktop-0.2.0-arm64.dmg \
  "CAM Desktop.app/Contents/Resources/app.asar"
npx asar list app.asar | head
```

## Secrets

All six live in GitHub → orlunix/cam → Settings → Secrets and variables
→ Actions. Nothing secret is committed anywhere.

| Secret | Value | Used by |
|---|---|---|
| `CSC_LINK` | base64 of the Developer ID Application `.p12` | electron-builder code signing (macOS) |
| `CSC_KEY_PASSWORD` | the `.p12` export password | electron-builder code signing (macOS) |
| `APPLE_API_KEY` | full text of the App Store Connect `.p8` | notarization |
| `APPLE_API_KEY_ID` | `9JAL6YATH8` | notarization |
| `APPLE_API_ISSUER` | `bdfd51d9-34cf-4757-9327-8c2a9f41292c` | notarization |
| `TEAM_ID` | `ULL2CR6L6J` | signing + notarization |

Flow inside `cam-desktop-macos.yml`:

- `CSC_LINK` + `CSC_KEY_PASSWORD` are electron-builder's standard
  signing env vars; when `CSC_LINK` is non-empty the job sets
  `CSC_IDENTITY_AUTO_DISCOVERY=true` and `--config.mac.notarize=true`.
- The workflow writes `$APPLE_API_KEY` (secret text) to
  `$RUNNER_TEMP/AuthKey.p8` and exports `APPLE_API_KEY=<that path>`
  plus `APPLE_API_KEY_ID` / `APPLE_API_ISSUER` / `APPLE_TEAM_ID` — the
  quartet electron-builder's notarizer consumes (App Store Connect API,
  no Apple ID password anywhere).
- Windows MSI needs **no** secrets: `build:win-msi` sets
  `signAndEditExecutable=false` and `verifyUpdateCodeSignature=false`.
- Release upload uses the auto-provided `GITHUB_TOKEN`
  (`gh release create … || true` then `gh release upload … --clobber`).

### The `.p12` gotcha (cost us one failed build)

Apple's `security import` rejects PKCS#12 files written by OpenSSL 3
with its default PBES2/AES encryption. The certificate had to be
re-exported in legacy format before base64-ing into `CSC_LINK`:

```bash
openssl pkcs12 -in original.p12 -nodes -out /tmp/cert.pem   # decrypt
openssl pkcs12 -export -legacy -out fixed.p12 -in /tmp/cert.pem
base64 -w0 fixed.p12 > developerID_application.p12.base64.txt
```

If a build fails at signing with `errSecAuthFailed` / MAC verification
errors, this is why.

### Where the plaintext lives (local only, never committed)

- `.p12` base64 + `p12.password`: `~/nuts/notes/notes/appkey/developerID/`
- `.p8`: `~/Downloads/AuthKey_9JAL6YATH8.p8`
- GitHub personal token (for API/secret writes): `~/.my_tokens.yaml`
  (`GITHUB:` line) — outside any repo
- Secrets were written to GitHub via the REST API (sealed-box
  encryption with the repo public key) using the throwaway venv in
  `.tools/ghsecrets/` (pynacl + requests). To rotate a secret: update
  the local file, re-run the same API write.

## Parked: Mac App Store line

`cam-desktop-mas.yml`, `apps/cam-desktop/build/entitlements.mas*.plist`,
and `npm run build:mas` are in place but dormant: the MAS job needs a
`MAS_PROVISIONING_PROFILE` secret plus an Apple Distribution /
Mac Installer certificate that are not configured. Current releases do
not use it.
