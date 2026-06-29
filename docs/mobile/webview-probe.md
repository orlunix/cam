# WebView viewport drift — probe & reference

## Reference apps (downloaded / vendored)

| Source | Location | What we copied |
|--------|----------|----------------|
| [WebAppCapsule](https://github.com/usokawa/WebAppCapsule) | `android/reference/WebAppCapsule-MainActivity.kt` | Virtual HTTPS via `WebViewAssetLoader`, minimal `WebSettings`, `allowFileAccess=false` |
| Google docs | [Load local content](https://developer.android.com/develop/ui/views/layout/webapps/load-local-content) | Same pattern — **no `file://android_asset/`** |

CamUI implements the same idea in **`CamAssetLoader.java`** (stdlib-only, no Gradle/androidx).

## What changed in CamUI V2 (2.2.1+)

- Load URL: `https://appassets.androidplatform.net/web/mobile.html?native=1` (was `file:///android_asset/...`)
- Removed: `setInitialScale`, `setUseWideViewPort`, `allowUniversalAccessFromFileURLs`, aggressive zoom flags
- JS: no viewport-meta toggling on native shell; agent-detail no longer calls `__camResetLayout` on resume

## Probe APK (A/B/C)

Build:

```bash
cd android/probe && chmod +x build.sh && ./build.sh
```

Install **alongside** CamUI V2 (`com.cam.probe` vs `com.cam.app`):

```bash
adb install android/probe/build/camui-probe.apk
adb install android/build/cam.apk
```

Open **CamUI Probe** → pick a level:

| Level | Page | Isolates |
|-------|------|----------|
| **A** | `probe-a.html` | WebView shell only (static HTML) |
| **B** | `probe-b.html` | + CamUI CSS (`mobile.css`), no JS |
| **C** | `mobile.html` | Full CamUI |

## Test protocol (same on both apps)

1. Open level A → note grid corners + bottom bar
2. Screenshot **or** Home → switch back
3. Open system menu / dialog if applicable
4. Record: drift? native bar? web only?

Repeat for B, then C.

## Interpret results

| A | B | C | Likely cause |
|---|---|---|--------------|
| drift | drift | drift | Android WebView / device display settings |
| OK | drift | drift | CamUI CSS |
| OK | OK | drift | CamUI JS (`app.js`, `agent-detail`, API polling) |
| OK | OK | OK (Probe) but CamUI drifts | Native hybrid chrome in main app |

## Clone reference locally

```bash
git clone --depth 1 https://github.com/usokawa/WebAppCapsule.git /tmp/WebAppCapsule
```

Compare with `android/app/src/main/java/com/cam/app/MainActivity.java`.
