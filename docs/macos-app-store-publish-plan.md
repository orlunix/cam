# CAM Desktop Mac App Store Publish Plan

Status: draft
Owner: CAM Desktop
Scope: macOS App Store distribution of CAM Desktop, not outside-store DMG distribution.

## Goal

Prepare CAM Desktop for Mac App Store submission as a stable developer tool for
managing local and remote CAM agents. The App Store build must be reviewable,
sandbox-compatible, privacy-clear, and operational without hidden setup steps.

This plan intentionally does not rely on dynamic local HTML/CSS/plugin loading
to change app behavior after review. Fast-changing behavior should be expressed
as reviewed app code plus declarative user data such as contexts, workflows,
bots, themes, and server responses.

## Current State

The repository currently supports a macOS DMG proof-of-concept workflow:

- `.github/workflows/cam-desktop-macos.yml` builds unsigned / not-notarized DMG
  artifacts for testing.
- `apps/cam-desktop/package.json` has a `mac` target for `dmg`.
- The active Desktop product is an Electron app with Direct and Relay modes.
- Direct mode embeds a local Hub and uses SSH / xterm / bundled `camc`.
- Relay mode talks to an externally hosted relay/source pair.

This is not enough for Mac App Store submission. Mac App Store requires a
different distribution track from a notarized DMG.

## Distribution Tracks

### Track A: Outside App Store DMG

Use for early testers and internal release.

Required:

- Developer ID Application certificate.
- Hardened runtime.
- Apple notarization.
- Stapled `.dmg`.
- Optional auto-update mechanism.

This track is easier and should remain the near-term public beta path.

### Track B: Mac App Store

Use for public App Store availability.

Required:

- Apple Developer Program membership.
- App Store Connect app record.
- Mac App Store signing identity / provisioning.
- `mas` build target.
- App Sandbox entitlements.
- App Review metadata, demo access, privacy labels, and stable backend access.

This track is stricter and needs product hardening before submission.

## App Store Readiness Gaps

### 1. App Sandbox Compatibility

Mac App Store builds must run under App Sandbox. CAM Desktop needs a dedicated
MAS entitlement set instead of reusing the DMG build.

Likely entitlements:

- Network client access for SSH, Relay, API, and update checks.
- User-selected file read access for key files and attachments.
- User-selected file write access only if export/download is supported.
- Keychain access group for stored secrets.

Avoid or justify:

- Broad filesystem access.
- Background services that are not user-visible.
- Arbitrary local executable launch.
- Unsandboxed helper binaries.

### 2. SSH / Terminal Behavior

Direct mode opens SSH connections, xterm terminal sessions, uploads files, and
controls remote agents. This must be clearly user-initiated and explained to App
Review.

Requirements:

- No hidden SSH activity before the user configures a host.
- Clear host identity, username, and port in the UI.
- Clear credential storage description.
- User-controlled delete/reset for hosts and credentials.
- No bundled key material.
- No remote execution without explicit user action.

### 3. Bundled Executables

The app bundles `camc` and `skillm`. For App Store:

- Every bundled executable must be signed as part of the app bundle.
- The app must not download/replace executable code to bypass review.
- Bundled CLI behavior must be final enough for review.
- Any self-update/remote-upgrade behavior for bundled tools must be disabled or
  carefully scoped for the MAS build.

Open question:

- Whether the MAS sandbox allows the current bundled `camc` execution model
  unchanged. This must be tested on a real macOS sandbox build.

### 4. Credential Storage

Current Desktop behavior must be audited for macOS:

- SSH passwords/passphrases/tokens should be stored in Keychain.
- Local config files should not contain plaintext secrets.
- Logs must not print secrets or raw tokens.
- App Store privacy labels must reflect any collected/stored data.

### 5. Reviewable Demo Path

Apple Review needs to use the app. We need one of:

- A hosted demo Relay/source with temporary credentials and safe fake agents.
- A local demo mode that simulates agents without external credentials.
- A reviewer package with a temporary SSH host and explicit instructions.

Recommended: implement a built-in demo mode for App Review and screenshots,
plus optional demo Relay credentials in Review Notes.

### 6. Product Completeness

Before App Store submission, remove or hide unfinished surfaces:

- No "POC", "alpha", "debug", or placeholder copy in user-visible UI.
- Disabled tabs should either explain requirements clearly or be removed from
  the MAS build.
- Error states must be actionable.
- Direct/Relay onboarding must be understandable without developer context.
- Support and privacy links must be live.

### 7. Privacy and Legal

Needed before submission:

- Public privacy policy URL.
- Support URL.
- App Store privacy nutrition labels.
- Terms or acceptable-use note if users can connect to external systems.
- Export-control review if required by Apple account workflow.
- Third-party license audit for Electron, ssh2, xterm, bundled Python/CLI code,
  and any vendored assets.

## Build and Signing Work

### Add MAS Build Target

Add a separate `mas` target in `apps/cam-desktop/package.json`, keeping `dmg`
separate.

Expected shape:

```json
{
  "mac": {
    "target": ["dmg", "mas"],
    "hardenedRuntime": true,
    "entitlements": "build/entitlements.mac.plist",
    "entitlementsInherit": "build/entitlements.mac.inherit.plist"
  },
  "mas": {
    "entitlements": "build/entitlements.mas.plist",
    "entitlementsInherit": "build/entitlements.mas.inherit.plist",
    "provisioningProfile": "embedded.provisionprofile"
  }
}
```

Exact Electron Builder settings should be validated on a macOS runner.

### Required CI Secrets

For App Store upload:

- Apple Developer team ID.
- App Store Connect API key ID.
- App Store Connect issuer ID.
- App Store Connect private key.
- Mac App Store signing certificate / provisioning profile.
- Certificate password if using `.p12`.

For DMG notarization:

- Developer ID Application certificate.
- Apple ID app-specific password or App Store Connect API key.

Keep DMG and MAS secrets separate.

### CI Jobs

Recommended workflows:

1. `macos-dmg.yml`
   - Build signed/notarized outside-store DMG.
   - Upload artifact.

2. `macos-mas.yml`
   - Build signed MAS artifact.
   - Validate sandbox launch.
   - Upload to App Store Connect / TestFlight for Mac.

3. `macos-mas-smoke.yml`
   - Install MAS-signed build on macOS runner if possible.
   - Launch app.
   - Run basic UI smoke.

## Product Changes for MAS Build

### MAS-Specific Feature Flags

Add a build-time mode flag:

- `CAM_DISTRIBUTION=mas`

Use it to:

- Disable unsupported self-update behavior.
- Disable arbitrary executable replacement.
- Prefer Keychain for secrets.
- Show App Store-safe onboarding.
- Hide debug-only diagnostics unless useful for support.

### Demo Mode

Add `Demo` connection profile:

- Shows fake or local-only agents.
- Demonstrates Agent list, Terminal-like output, Browse, Workflows, Todos, and
  Settings without needing external infrastructure.
- Safe for screenshots and App Review.

### User Data Model

Allowed dynamic data:

- Host/context configs.
- Bot packages as declarative data.
- Workflow YAML.
- Theme JSON mapped to known tokens.
- Markdown and text preview content.

Avoid in MAS:

- User-supplied JavaScript.
- Arbitrary HTML/CSS controlling app chrome.
- Downloaded executable plugins.
- Remote config that unlocks unreviewed product behavior.

## App Review Notes Draft

App Review should receive:

- Short product explanation:
  "CAM Desktop is a developer tool for connecting to user-provided SSH hosts and
  managing the user's own coding-agent sessions."
- Demo instructions:
  "Use Demo mode to inspect the app without external SSH credentials."
- If a live demo is provided:
  Relay URL, temporary token, and test host description.
- Explanation of SSH:
  "The app connects only to hosts configured by the user. It does not include
  credentials or connect to any host automatically."
- Explanation of bundled tools:
  "`camc` is bundled so the app can manage the user's agent sessions on their
  configured hosts."

## Acceptance Checklist

### Engineering

- [ ] MAS target builds on macOS.
- [ ] App launches under App Sandbox.
- [ ] Direct mode can add/test a host under sandbox.
- [ ] Relay mode connects under sandbox.
- [ ] Keychain storage works.
- [ ] Terminal mode works or is disabled with clear copy if sandbox blocks it.
- [ ] File attach uses user-selected files only.
- [ ] Bundled `camc` is signed and executable inside the app bundle.
- [ ] No secrets in logs.
- [ ] No debug-only UI in release build.

### Review Readiness

- [ ] App Store Connect app record created.
- [ ] Bundle ID configured.
- [ ] Privacy policy URL live.
- [ ] Support URL live.
- [ ] Screenshots prepared.
- [ ] App description prepared.
- [ ] App Review notes prepared.
- [ ] Demo mode or demo credentials ready.
- [ ] Privacy labels completed.
- [ ] Third-party licenses reviewed.

### Release

- [ ] Upload MAS build to App Store Connect.
- [ ] Test via TestFlight for Mac.
- [ ] Fix sandbox/review issues.
- [ ] Submit for App Review.

## Recommended Timeline

### Phase 0: Keep DMG Path Working

Build signed/notarized DMG first. This provides a usable public beta while MAS
work is ongoing.

### Phase 1: MAS Feasibility Spike

- Add MAS target and entitlements.
- Build on macOS.
- Launch under sandbox.
- Test Direct/Relay/Terminal/Browse.
- Record what breaks.

### Phase 2: Product Hardening

- Keychain audit.
- Demo mode.
- App Store-safe onboarding.
- Remove debug/placeholder surfaces.
- License/privacy docs.

### Phase 3: App Store Connect Submission

- Prepare metadata.
- Upload TestFlight build.
- Run external tester pass.
- Submit to App Review.

## Open Questions

- Does Terminal Direct mode pass sandbox review unchanged?
- Does executing bundled `camc` from Electron require additional entitlements or
  helper packaging?
- Should MAS build support Direct mode, or should first MAS submission be
  Relay-only plus Demo mode?
- Should file Browse remain read-only in MAS v1?
- Which server/demo environment will App Review use?

## Recommendation

Do not submit the current DMG-oriented app directly to Mac App Store.

Recommended path:

1. Ship notarized DMG for early users.
2. Run a MAS feasibility spike focused on sandbox + bundled executable behavior.
3. Add Demo mode and App Store metadata.
4. Submit a conservative MAS build after Direct/Relay behavior is proven under
   sandbox.
