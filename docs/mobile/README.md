# CAM Mobile Docs

Planning documents for **CamUI Mobile V2** (PWA + native WebView shells).

## V1 vs V2

| | **V1 (legacy)** | **V2 (this effort)** |
|---|-----------------|----------------------|
| Entry | `web/index.html` | `web/mobile.html` |
| Hub backend | Often `cam serve` (Python API) | **`camui start` → embedded Hub** (`embedded-hub.cjs`) |
| Connection | Relay (+ optional Direct URL/token) | **Relay only** (Relay URL + Relay token) |
| Feature surface | List / detail / start / basic settings | Desktop parity: Agent Settings, Skills, Nodes, … |

V2 assumes the relay **source** is started with:

```bash
node apps/cam-desktop/cli/camui-cli.cjs start \
  --profile <name> \
  --relay-url ws://<relay-host>:<port> \
  --relay-token <token>
```

The source owns the CAM API token (`~/.cam/camui/relay/<name>/profile.json`). The phone only stores Relay URL + Relay token (CAM-DESK-REMOTE-012).

## Documents

| File | Purpose |
|------|---------|
| [`relay-first-plan.md`](relay-first-plan.md) | **Active V2 plan** — Relay-only client, CamUI-start Hub contract, phases, gaps |

## Related

- Desktop requirements (Hub API contract): [`../desktop/requirements.md`](../desktop/requirements.md)
- Desktop UI / workspace gateway spec: [`../desktop-ui-spec.md`](../desktop-ui-spec.md)
- Shared API client: [`../../web/js/api.js`](../../web/js/api.js)
- Legacy mobile PWA entry: [`../../web/index.html`](../../web/index.html)
- Legacy Android shell: [`../../android/`](../../android/)

## Boundary

Mobile is a **pure client** of the existing Hub/API. It must not change Start Server behavior, embedded Hub code, server APIs, or camc backend behavior.

## Versioning (CamUI Mobile V2 APK)

Source of truth: `android/VERSION` (semver `major.minor.patch`). `android/build.sh` stamps `mobile.html`, `app.js`, and AndroidManifest.

| Phase | Bump | Example |
|-------|------|---------|
| **Daily / iterative builds** | **Patch** (小版本, third number) | `2.1.0` → `2.1.1` → `2.1.2` |
| **Official release** | **Minor** (中版本, second number); reset patch to `0` | `2.1.3` → **`2.2.0`** |

Rules:

- Use **patch** for every in-progress APK while features are still being validated.
- Use **minor** only when the milestone is stable and ready to ship as a release (e.g. tag, announce, wider rollout).
- Reserve **major** for breaking product changes (V1 → V2 was `1.x` → `2.0.0`).

Build: `cd android && ./build.sh` → `build/cam.apk` and `build/camui-v2-<version>.apk`.

## Android WebView viewport (V2)

V2 uses a **single height model** in `web/css/mobile.css` — `html/body/#app` = `100%` + flex. **No `dvh`/`vh`** in the V2 shell (unstable after immersive resume + screenshot).

Native side: non-immersive window, zoom disabled. Resume → **`__camReloadOnResume()`** (full page reload, same as first launch).

Recovery: **default = reload page on resume** (identical to first launch, keeps `#/route`). Settings → Display to disable or use full app restart. Menu ☰ → **Reload page**.
