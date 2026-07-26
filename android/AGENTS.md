# Android Shell — Agent Notes

WebView wrapper APK for CamUI Mobile V2. The mobile mission, connection
modes, boundaries, and known issues are documented in
`web/AGENTS.md` — read that first. Shell-specific notes:

- **No Gradle**: `build.sh` drives aapt2 → javac → d8 → zipalign →
  apksigner directly; needs `~/android-sdk` with `platforms;android-34`
  and `build-tools;34.0.0` (`setup-sdk.sh` installs them). Output:
  `build/cam.apk` + `build/camui-v2-<version>.apk`.
- Version source of truth: `android/VERSION` (semver; versionCode =
  M*10000 + m*100 + p). Bump the patch per iteration. `build.sh` stamps
  `?v=<version>` cache-bust strings into the bundled web assets — do not
  hand-edit them.
- Manifest: `minSdk 23`, `targetSdk 34` (do not regress — Android 14+
  refuses targetSdk < 23), `usesCleartextTraffic="true"`,
  `configChanges` covers rotation (no Activity recreate).
- Web app is loaded via `CamAssetLoader.java` virtual HTTPS
  (`https://appassets.androidplatform.net/...`), not `file://`.
- JS bridge lives in `CamJsBridge.java` (`restartApp`, `installApk`,
  `getAppVersion*`). Known gaps: `installApk` uses `Uri.fromFile`
  without a FileProvider and is currently dead code (no JS caller);
  `MainActivity.onDestroy` does not stop the embedded hub
  (ServerSocket/thread-pool leak until process death).
- Direct mode native side: `MobileEmbeddedHub.java`, `MobileSshExec.java`
  (phone-hosted hub).
- `probe/` — side-by-side WebView probe APK (`com.cam.probe`, levels
  A/B/C) for viewport-drift debugging; see `docs/mobile/webview-probe.md`.
- Active branch: `camui-desktop-v2`. The archived native Kotlin UI line
  (`camui-mobile-native`, 2.1.x) is frozen — do not resurrect it.
