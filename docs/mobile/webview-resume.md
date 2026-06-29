# CamUI Mobile — WebView path (master)

Active development on **`master`** uses the **WebView shell** + **`web/mobile.html`** relay-only PWA.

## Version

- **WebView line starts at `2.2.0`** on `master` (2026-06).
- **`2.1.x` is reserved** for branch `camui-mobile-native` (last: `2.1.21`).

## Baseline commit

- `2d6964a` — first WebView V2 shell (`MainActivity` → `mobile.html`)
- Version bumped to **`2.2.0`** when resuming WebView after native archive

## Build

```bash
cd android && ./build.sh
adb install -r build/cam.apk
```

## Native archive

All native Kotlin/Java UI work (v2.1.4–2.1.21) is on branch **`camui-mobile-native`**.

```bash
git checkout camui-mobile-native   # resume native (stay on 2.1.x)
git checkout master                # WebView (2.2.0+)
```

See `camui-mobile-native:docs/mobile/native-archive.md` for details.

## Drift debugging

- `docs/mobile/webview-probe.md`
- `android/probe/` — side-by-side WebView probe APK
