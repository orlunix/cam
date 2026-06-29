# CamUI Mobile — Native Android Plan

## Problem

Android **System WebView** + full-page PWA can develop viewport scale/scroll drift after screenshot or app switch. CSS/JS mitigations and even `location.reload()` may not reset internal WebView state on all devices.

## Strategy (phased)

| Phase | What | Drift risk | Effort |
|-------|------|------------|--------|
| **0 (2.1.8+)** | Native top bar + **destroy/recreate WebView** on resume | Low for chrome; content reset on resume | Done in `MainActivity` |
| **1** | Native **Agents list** + **Settings** (Kotlin/Java + Relay HTTP/WS client) | None on list | ~1–2 weeks |
| **2** | Native **Agent detail chrome** (title, stop, menu); WebView **only for terminal output** | Isolated to output pane | ~2 weeks |
| **3** | Optional: native input bar + key shortcuts | Minimal WebView | ~1 week |
| **4** | Full native (no WebView) | None | Large; terminal rendering is hard |

**API contract unchanged:** Relay + embedded Hub (`camui start`). Native client calls same endpoints as `web/js/api.js`.

## Phase 0 (current APK)

- `activity_main.xml`: fixed native header (menu, title, relay dot).
- Web hides `#header` when `CamBridge.usesNativeChrome()` is true.
- On `onPause` → save route via `CamBridge.saveRoute(hash)`.
- On `onResume` → **destroy WebView, create new instance**, reload `mobile.html` + saved route (same as cold start).
- Android 14+: screen capture callback triggers WebView recreate (screenshot while in app).

## Phase 1 sketch

```
NativeAgentListActivity
  └ RelayClient (OkHttp + WebSocket, port from api.js relay protocol)
  └ RecyclerView ← GET agents via relay
  └ tap → NativeAgentActivity or WebView terminal-only fragment
```

Reuse: `web/js/api.js` as spec; do not change server/hub.

## When to choose full native

- Mobile is a **primary** product surface.
- WebView drift or terminal UX cannot meet bar after Phase 0–2.
- Team accepts maintaining Kotlin UI + shared API client.

## Build

```bash
cd android && ./build.sh
```

Versioning: patch builds during Phase 0–2; minor `2.2.0` when Phase 1 list ships.
