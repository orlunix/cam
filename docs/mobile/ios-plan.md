# iOS Port Plan — CamUI Mobile

Status: **planning** (2026-08-04). No code yet. Decisions so far come
from the working sessions on Android 2.4.52–2.4.75.

## Goal

Ship CamUI on the iPhone App Store with maximum reuse of the existing
web layer and the MAS (Mac App Store) pipeline experience.

## Strategy A (chosen v1): WKWebView shell + direct-to-desktop-hub

The phone runs a thin Swift/WKWebView shell loading the **same** web
app (`web/mobile.html` + `web/js/mobile/*`) and connects directly to a
desktop hub (`camui start` / Electron embedded hub) over LAN/Tailscale
via the existing `CamApi` direct HTTP mode. **No on-device SSH, no
on-device hub** in v1.

- Terminal in v1 rides the capture-poll path through the desktop hub
  (the hub does the SSH). Good enough for management + light terminal.
- Relay is deliberately NOT ported (it is being retired on Android).

### What is shared vs rewritten

| Layer | Shared? | Notes |
|---|---|---|
| Web UI (all views, gestures, keybar, xterm) | **100% shared** | same files in WKWebView |
| `CamApi` connection logic | shared | direct HTTP mode |
| Shell (WKWebView, safe-area, keyboard) | Swift rewrite | ~300–500 lines |
| JS bridge | Swift rewrite | contract: `term_*`, `directHub_*`, `files_*` — same names/payloads as `CamJsBridge` |
| Credential storage | Swift rewrite | iOS Keychain (bridge: `MobileCredentialStore` semantics) |
| SSH layer (JSch) | **not needed in v1** | B plan: Citadel / SwiftNIO-SSH |
| Embedded hub | **not needed in v1** | B plan: Swift port of `MobileEmbeddedHub` |

### Bridge contract (must match Android exactly)

`term_open / term_input / term_resize / term_close / term_copymode`,
`directHub_start/stop/restart/check/logs/getProfile/request`,
`files_pickFile / files_saveText`, `pickPrivateKey`, plus the
`__camTermCb / __camTermEvent / __camDirectHubCb / __camFilesCb /
__camOnKeyPicked(+Error)` callback globals and the
`window.__camBridgeShared` pending-map discipline (see 2.4.52 fix in
`stability-roadmap.md` §1).

## Strategy B (later, if v1 proves iOS needs standalone connections)

Swift port of the hub + SSH via Citadel (SwiftNIO-SSH) — mirrors
Android Direct mode. Defer until A ships and users actually need
"phone → machines without desktop online". B also removes the desktop
hub from the terminal path (full-speed PTY).

## SSH keys onto the phone (v1 scope: picker + paste)

1. **Document picker** (`UIDocumentPickerViewController`) — pick from
   Files (iCloud Drive etc.), copy into the app sandbox. Same role as
   Android's SAF picker (`pickPrivateKey` bridge).
2. **Paste key text** — OpenSSH keys are text; paste box writes them
   into the sandbox. (Worth adding to Android too.)
3. AirDrop → Files → picker works with no extra code.

Keys live in the app sandbox dir (encrypted at rest by iOS); node
records reference the sandbox path as `key_file`. Keychain is NOT
needed for file material (SSH libs want bytes/files).

## Build & release pipeline (no local Mac)

GitHub Actions macOS runner, cloning the proven MAS pattern
(`apps/cam-desktop/MAS-SUBMISSION.md`):

- Reuse existing secrets: `MAS_APP_PEM` (Apple Distribution cert,
  signs iOS too), `APP_STORE_CONNECT_API_KEY`/`KEY_ID`/`ISSUER_ID`,
  `TEAM_ID` (ULL2CR6L6J).
- Import identities as **PEM** into a temp keychain (never p12 — MAS
  pitfalls 2–4), authorize codesign in the partition list, set a
  build-step timeout.
- iOS-only new items: iOS App Store provisioning profile + App record
  (must be created manually once — MAS pitfall 9), bundle id, Privacy
  Manifest file (iOS 17+), per-build export compliance
  (`usesNonExemptEncryption: false` via API), screenshots, privacy
  labels, final submit click.
- macOS runner minutes: 10x billing on private repos (~200 free
  macOS-min/month) — batch pushes, don't iterate via CI.

## Decisions pending (user)

- bundle id (suggestion: same family as MAS `com.hren.cam.mas`, e.g.
  `com.hren.cam.ios`) and app display name.
- Mac-less debugging aid: in-page console capture panel (WKWebView has
  no Safari Web Inspector without a Mac).

## Timeline estimate (from skeleton-complete)

| Step | Duration |
|---|---|
| Swift skeleton + bridge + CI green | 1–2 days |
| Direct-to-hub flow verified on device | 1 day |
| Store metadata + privacy + screenshots | 1–2 days |
| TestFlight (internal) | same day after upload |
| App Store review (first submission) | 1–3 days typical |

Realistic: ~1 week to TestFlight, ~1.5 weeks to App Store, once the
prerequisites (secrets profile + app record) are clicked through.

## Verification plan

- Bridge contract tests on both sides (JS: mock-bridge smoke, same as
  Android's; Swift: XCTest with a mock SSH/hub server).
- JVM lockrepro equivalents become XCTest for any Swift hub/SSH logic.
- On-device checklist mirrors `stability-roadmap.md` §4 (launch,
  dashboard, agent detail, terminal attach, copy-mode gestures,
  background/resume) + iOS specifics (keyboard, safe area, TestFlight
  install over previous build).
