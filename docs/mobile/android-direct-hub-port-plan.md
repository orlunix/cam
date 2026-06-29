# Android Direct Hub Port Plan

**Goal:** Mobile Direct = same as Desktop Direct — local **embedded CAM Hub** on the phone. Relay mode stays unchanged. Renderer uses the same `CamApi` + `shared/direct-settings.js` + `shared/nodes-mode.js`.

**Reference (Desktop):**

| Component | Path |
|-----------|------|
| Hub server | `apps/cam-desktop/electron/embedded-hub.cjs` (~3800 lines, stdlib + ssh2) |
| SSH pool | `apps/cam-desktop/electron/ssh-transport.cjs` |
| Credential encrypt | `apps/cam-desktop/electron/credential-store.cjs` |
| Main IPC | `apps/cam-desktop/electron/main.cjs` |
| Renderer bridge | `apps/cam-desktop/electron/preload.cjs` → `CamBridge.*` |
| Web Direct UI | `web/js/shared/direct-settings.js` |
| Web startup | `web/js/desktop/app.js` → `autoStartConnection()` |

---

## Architecture (target)

```text
┌──────────────── Android APK ────────────────────────────────┐
│  WebView (mobile.html + web/js/mobile/*)                     │
│    CamApi ──HTTP Bearer──▶ 127.0.0.1:<port>/api/...          │
│    CamBridge.directHub.*  ──JNI──▶ HubService (Node thread)  │
├──────────────────────────────────────────────────────────────┤
│  HubService (foreground when active)                         │
│    embedded-hub.cjs + ssh-transport.cjs + credential-store   │
│    data: /data/data/com.cam.app/files/cam-hub/               │
│    ssh2 → remote nodes → camc                                │
└──────────────────────────────────────────────────────────────┘

Relay tab (unchanged): WebView ──relay──▶ remote camui start Hub
```

Direct is a **superset**: local Hub is always available when enabled; Relay tab still connects to remote Hubs.

---

## Phase 0 — Cleanup (web, no Node yet)

**Remove wrong phone-SSH Direct path** (do not touch Relay frozen files):

| Remove / replace | Reason |
|------------------|--------|
| `web/js/mobile/direct-identity.js` | Wrong model |
| `web/js/mobile/direct-local-api.js` | Parallel registry; Hub owns nodes |
| `web/js/mobile/settings-direct.js` (phone key UI) | Replace with `mountDirectSettings()` |
| `web/js/mobile/direct-init.js` phone-SSH logic | Replace with Desktop-shaped bootstrap |

**Rewire (additive):**

| File | Change |
|------|--------|
| `settings.js` | Direct tab calls `mountDirectSettings()` from `shared/direct-settings.js` (same IDs as Desktop) |
| `nodes-page.js` | Direct + Hub up → `shared/nodes-mode.js`; Relay → frozen `nodes.js` |
| `app.js` | When `CamBridge.directHub` exists: mirror Desktop `autoStartConnection()` (Relay profile wins; else auto-start Hub) |

**Acceptance:** Relay APK behavior identical to v2.2.0 baseline. Direct tab shows Enable/Start/Stop (buttons no-op until Phase 1).

---

## Phase 1 — `CamBridge.directHub` (Hub lifecycle only)

Implement the **exact** preload contract in Android Java/Kotlin + `@JavascriptInterface`:

```javascript
CamBridge.directHub = {
  check()    → Promise<HubCheckResult>
  start()    → Promise<{ ok, apiUrl, apiToken, ... }>
  stop()     → Promise<{ ok }>
  restart()  → Promise<{ ok, apiUrl, apiToken, ... }>
  logs()     → Promise<{ lines: [...] }>
  getProfile() → Promise<{ port, tokenFingerprint, running, ... }>
}
```

Match return shapes from `embedded-hub.cjs` `check` / `start` / `getProfile` (read Desktop smoke tests or log Desktop responses).

**Runtime options (pick one for Phase 1):**

| Option | Pros | Cons |
|--------|------|------|
| **A. Bundled Node binary** (arm64-v8a) + `ProcessBuilder` | Reuse `embedded-hub.cjs` verbatim | APK size +50MB; Play policy on executables |
| **B. nodejs-mobile** (JNI embed Node in app) | Same JS codebase, in-process | Integration complexity, NDK build |
| **C. Foreground `camui` sidecar** (Termux/user installs Node) | Fastest dev prototype | Not shippable as product |
| **D. Kotlin rewrite of Hub subset** | No Node in APK | Large effort, drift from Desktop |

**Recommendation:** **B (nodejs-mobile)** or **A (bundled Node)** for production; **C** only for dev validation.

**Hub entry script** (new, thin):

```text
android/hub/run-hub.cjs
  require('../../apps/cam-desktop/electron/embedded-hub.cjs')
  require('../../apps/cam-desktop/electron/credential-store.cjs')
  require('../../apps/cam-desktop/electron/ssh-transport.cjs')
  configure + start({ dataDir: process.env.CAM_HUB_DATA_DIR })
  listen on 127.0.0.1:8420..8469 (same as Desktop)
```

**Android storage layout:**

```text
/data/data/com.cam.app/files/cam-hub/
  embedded-hub.json          # contexts + agents
  embedded-hub-credentials.json
  logs/
```

**Credential store on Android:** implement `safeStorage` stub in `credential-store.cjs` using **Android Keystore** (via JNI) — same contract as Electron `encryptString` / `decryptString`. Refuse Remember if Keystore unavailable (same as Desktop headless Linux).

**Acceptance:**

- Settings → Direct → Start → `api.connect()` succeeds on loopback
- `GET /api/system/health` returns 200 from WebView
- `localStorage` gets `cam_server_url` + `cam_token` (via existing `startEmbeddedHubAndPersist`)

---

## Phase 2 — SSH + Nodes (Hub parity)

| Item | Desktop reference | Android work |
|------|-------------------|--------------|
| ssh2 pool | `ssh-transport.cjs` | Bundle `node_modules/ssh2` for arm64; same as Desktop asarUnpack |
| Nodes CRUD | Hub `POST/PUT/DELETE /api/contexts` | Web uses `shared/nodes-mode.js` — no mobile fork |
| Browse key | `CamBridge.files.pickPrivateKey()` | SAF → copy to app-private path → return `{ path, label }` |
| SSH config import | `GET /api/system/ssh-config` | Hub reads app-accessible ssh config or skip on Android |
| Remember password | `credential-store.cjs` | Keystore-backed blob store |
| Sync Host | Hub checks `~/.cam/camc` + `camc --json list` (no upload) | Same: probe remote camc; error `camc_missing` if absent |
| Deploy camc | `_ensureRemoteCamc` on agent send/key/stop only | Not on Sync Host; operator runs `cam sync` once on a workstation |

**Acceptance:** Add Host on phone (Direct) creates context in Hub store; Sync Host SSH-checks `~/.cam/camc` then runs `camc --json list` (no bundled deploy).

---

## Phase 3 — Agents (full Direct superset)

Hub already implements (verify against current `embedded-hub.cjs`):

- `GET /api/agents`, output, input, key, upload
- `POST /api/agents` (start agent) — confirm not 501 on Android build
- Agent Settings / cron / skillm routes

**Web:** Mobile agent views already use `CamApi` — should work once Hub is up (same as Desktop DIRECT-019).

**Optional later:** `CamBridge.term.*` for Terminal mode (CAM-DESK-TERM-001..005) — needs ssh2 PTY in HubService.

---

## Phase 4 — Optional local Relay export

Desktop workstation runs **Hub + relay connector** together (`camui start --relay-url`).

On phone Direct, optional future feature:

```text
Phone embedded Hub + lightweight relay connector
  → other devices can use Relay tab to reach phone's Hub
```

Not required for MVP. Phone as Relay **client** (existing tab) is enough.

---

## `CamBridge` full surface (priority order)

| API | Phase | Notes |
|-----|-------|-------|
| `directHub.*` | 1 | Required for Direct |
| `files.pickPrivateKey` | 2 | Nodes Browse |
| `files.pickAttachment` | 3 | Agent upload |
| `getAppVersion` | done | Already in MainActivity |
| `restartApp` | done | Already in MainActivity |
| `net.probe` | 2 | Settings diagnostics |
| `term.*` | 4+ | Terminal attach |
| `installApk` | done | OTA update |

---

## Files to add (Android tree)

```text
android/
  hub/
    run-hub.cjs              # Node entrypoint
    package.json             # ssh2 dependency
  app/src/main/java/com/cam/app/
    HubService.java          # Foreground service, owns Node process
    DirectHubBridge.java     # @JavascriptInterface directHub + files
    KeystoreCredentialStore.java  # JNI or Java crypto for credential-store
  app/src/main/jni/          # (if nodejs-mobile)
```

---

## Files must NOT change for Direct (Relay frozen)

- `web/js/mobile/settings-relay.js`
- `web/js/mobile/nodes.js`
- Relay branch in `web/js/mobile/app.js` (`_connectRelay`, `readRelayConfig`)

Direct work only adds / replaces files under `settings-direct` (Desktop-shaped), `nodes-direct.js`, `HubService`, and `CamBridge`.

---

## Verification checklist

### Relay regression (every release)

1. Save Relay URL + token → Save & Connect → Agents list loads
2. Nodes page = v2.2.0 machines list + Add Context
3. Agent detail capture/send works

### Direct (after Phase 1+)

1. Settings → Direct → Enable/Start → conn dot = direct
2. No Hub URL/token fields in normal UI (Diagnostics only, redacted)
3. Nodes = Desktop host cards (`shared/nodes-mode.js`)
4. Start Agent → creates agent via Hub
5. Switch to Relay tab → still connects to workstation Hub
6. Switch back to Direct → local Hub still running

---

## Estimated effort

| Phase | Scope | Estimate |
|-------|-------|----------|
| 0 | Web cleanup, wire shared direct-settings | 1–2 days |
| 1 | Node runtime + directHub lifecycle | 1–2 weeks |
| 2 | ssh2 + Nodes + credentials | 1 week |
| 3 | Agents/Skills parity smoke | 3–5 days |
| 4 | Terminal + local relay export | deferred |

---

## Related docs

- [`direct-mode.md`](./direct-mode.md) — product model (Direct superset, not phone-SSH)
- [`../desktop/requirements.md`](../desktop/requirements.md) — CAM-DESK-DIRECT-010..019
- [`relay-first-plan.md`](./relay-first-plan.md) — Relay client (unchanged)
