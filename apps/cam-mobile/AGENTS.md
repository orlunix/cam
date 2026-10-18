# Android Shell — Agent Notes

WebView wrapper APK for CamUI Mobile V2. The mobile mission, connection
modes, boundaries, and known issues are documented in
`web/AGENTS.md` — read that first. Shell-specific notes:

- **No Gradle**: `build.sh` drives aapt2 → javac → d8 → zipalign →
  apksigner directly; needs `~/android-sdk` with `platforms;android-34` and
  `build-tools;34.0.0` (`setup-sdk.sh` installs them). JDK 17 may be exposed
  with `PATH="$HOME/tools/jdk17/bin:$PATH"`. Output:
  `build/cam.apk` + `build/camui-v2-<version>.apk`.
- Version source of truth: `apps/cam-mobile/VERSION` (semver; versionCode =
  M*10000 + m*100 + p). Bump the patch per iteration. `build.sh` stamps
  `?v=<version>` only into non-module web assets — never stamp local
  ES-module specifiers or the mobile module entry.
- **APK release upload (user instruction, 2026-10-18)**: upload every
  successful build by default. From the repository root, run:
  ```bash
  cd apps/cam-mobile
  read -r -p 'WebDAV user: ' WEBDAV_USER
  read -rs -p 'WebDAV app password: ' WEBDAV_PASS; echo
  export WEBDAV_USER WEBDAV_PASS
  WEBDAV_DIR='WebDev/cam' bash ./upload-apk.sh
  unset WEBDAV_USER WEBDAV_PASS
  ```
  This uploads `build/cam.apk` and `build/camui-v2-<version>.apk` to
  `WebDev/cam/`; check the HTTP response for both files. Always set
  `WEBDAV_DIR` explicitly — after the move, the script's workspace-derived
  default is the incorrect `WebDev/apps`. Use hidden input or the existing
  secure reference for credentials; never commit or print passwords.
- Packaging uses Python for ZIP and web-asset copying; host `zip`/`rsync`
  binaries are not required. Required Java dependencies are
  `libs/jsch.jar` (mwiede JSch) and `libs/bcprov.jar` (ed25519). Both are
  stripped of multi-release classes before d8.
- Manifest: `minSdk 23`, `targetSdk 34`, `adjustNothing`, and decor-inset
  whole-WebView IME translation. Do not reintroduce JavaScript keyboard
  offsets or resize the terminal to compensate for the keyboard.
- Web app loads through `CamAssetLoader.java` virtual HTTPS
  (`https://appassets.androidplatform.net/...`), not `file://`.
- `CamJsBridge.java` now exposes only Direct Hub control/API, file helpers,
  layout/lifecycle helpers, and app/version/key-picking methods. **Terminal
  bytes do not cross this string bridge.** Android Direct uses the embedded
  hub's authenticated loopback `/term` WebSocket: JSON control messages and
  binary PTY input/output. `MobileWebSocket.java` owns RFC6455 framing;
  `MobileTerminalManager.java` owns SSH PTYs and per-socket backpressure.
- `MobileTerminalService.java` is the foreground service for active terminal
  transports. There is no 10-minute background kill. `MainActivity.onDestroy`
  and `MobileEmbeddedHub.stop()` must close terminal sessions, client sockets,
  and the executor; restart recreates the executor.
- Release WebView debugging is off. Mixed content is denied by default; only
  an explicitly enabled Android Direct loopback profile selects compatibility
  mode for the local `ws://127.0.0.1` terminal. Relay should use HTTPS/WSS.
- SSH authentication supports RSA/ECDSA/ed25519 through JSch + BouncyCastle.
  `MobileSshAuth` uses `StrictHostKeyChecking=yes` with the persistent TOFU
  `MobileHostKeyRepository`. First contact records a host key; later changes
  fail as `host_key_changed`. `DELETE /api/system/ssh-host-key` implements the
  per-host reset exposed by Nodes → Reset Host Key.
- `test/lockrepro/` compiles the real hub/SSH/WebSocket classes with minimal
  Android stubs. It covers blackhole concurrency, lifecycle/restart, shared
  Hub API contract, WebSocket frames, real sshd RSA/ed25519 direct/ProxyJump,
  and host-key change/retrust. Run with:
  `PATH="$HOME/tools/jdk17/bin:$PATH" bash apps/cam-mobile/test/lockrepro/run.sh`.
- **Signing keystore**: `cam-release.keystore` (gitignored) is the release
  signature — losing it means every install becomes uninstall+reinstall.
  Cloud backup lives at `WebDev/cam/cam-release.keystore.gpg`
  (AES256-encrypted). **Rule (user instruction, 2026-08-09): never
  upload key/secret files in plaintext — ask the user for a password,
  `gpg -c` encrypt, upload the `.gpg`, then delete any plaintext copy
  from cloud storage.** Restore: `gpg -d cam-release.keystore.gpg >
  apps/cam-mobile/cam-release.keystore`.
- `probe/` — side-by-side WebView probe APK (`com.cam.probe`, levels
  A/B/C) for viewport-drift debugging; see `docs/mobile/webview-probe.md`.
  Probe debugging settings are intentionally not copied into the release app.
- Active branch: `camui-desktop-v2`. The archived native Kotlin UI line
  (`camui-mobile-native`, 2.1.x) is frozen — do not resurrect it.
