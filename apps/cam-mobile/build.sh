#!/bin/bash
#
# Build CAM APK using Android SDK command-line tools (no Gradle/Android Studio)
#
# Version is read from VERSION file (semver: major.minor.patch)
# and automatically applied to AndroidManifest.xml and web assets.
#
# Prerequisites:
#   sudo apt install -y openjdk-17-jdk-headless
#   mkdir -p ~/android-sdk/cmdline-tools && cd ~/android-sdk/cmdline-tools
#   wget https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
#   unzip commandlinetools-linux-11076708_latest.zip && mv cmdline-tools latest
#   yes | ~/android-sdk/cmdline-tools/latest/bin/sdkmanager "platforms;android-34" "build-tools;34.0.0"

set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
BUILD_TOOLS="$ANDROID_HOME/build-tools/34.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-34/android.jar"

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$PROJ_DIR/app/src/main"
BUILD_DIR="$PROJ_DIR/build"
WEB_DIR="$PROJ_DIR/../../web"
KEYSTORE="$PROJ_DIR/cam-release.keystore"
KEY_ALIAS="cam"
KEY_PASS="camapp123"

# --- Read version from VERSION file ---
VERSION_FILE="$PROJ_DIR/VERSION"
if [ ! -f "$VERSION_FILE" ]; then
    echo "ERROR: VERSION file not found"
    exit 1
fi
VERSION=$(head -1 "$VERSION_FILE" | tr -d '[:space:]')
IFS='.' read -r V_MAJOR V_MINOR V_PATCH <<< "$VERSION"
VERSION_CODE=$(( V_MAJOR * 10000 + V_MINOR * 100 + V_PATCH ))

echo "=== CamUI Mobile V2 APK Build v${VERSION} (code: ${VERSION_CODE}) ==="

# Verify tools
for tool in "$BUILD_TOOLS/aapt2" "$BUILD_TOOLS/d8" "$BUILD_TOOLS/apksigner" "$BUILD_TOOLS/zipalign"; do
    if [ ! -f "$tool" ]; then
        echo "ERROR: Missing $tool"
        echo "Install: yes | sdkmanager 'build-tools;34.0.0'"
        exit 1
    fi
done

if [ ! -f "$PLATFORM" ]; then
    echo "ERROR: Missing $PLATFORM"
    echo "Install: yes | sdkmanager 'platforms;android-34'"
    exit 1
fi

# --- Stamp version into source files ---
echo "[0/6] Stamping version v${VERSION}..."

# AndroidManifest.xml: update versionCode and versionName
sed -i "s/android:versionCode=\"[^\"]*\"/android:versionCode=\"${VERSION_CODE}\"/" "$SRC_DIR/AndroidManifest.xml"
sed -i "s/android:versionName=\"[^\"]*\"/android:versionName=\"${VERSION}\"/" "$SRC_DIR/AndroidManifest.xml"

# Web assets: stamp CamUI V2 entry
sed -i "s/content=\"v[^\"]*\"/content=\"v${VERSION}\"/" "$WEB_DIR/mobile.html"
sed -i "s/?v=[0-9][^\"']*/?v=${VERSION}/g" "$WEB_DIR/mobile.html"
sed -i "s/?v=[0-9][^\"']*/?v=${VERSION}/g" "$WEB_DIR/css/mobile.css" 2>/dev/null || true

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"/{compiled,gen,classes,dex}

# --- JS syntax gate (module parse) — a broken JS file kills the whole
# module graph at runtime (grey "Connecting" dot); catch it at build time.
NODE_BIN="${NODE:-$HOME/tools/node/bin/node}"
if [ -x "$NODE_BIN" ]; then
  echo "[0.5/6] Checking JS syntax..."
  JS_FAIL=0
  CHECK_DIR="$BUILD_DIR/jscheck"
  mkdir -p "$CHECK_DIR"
  while IFS= read -r f; do
    cp "$f" "$CHECK_DIR/check.mjs"
    if ! "$NODE_BIN" --check "$CHECK_DIR/check.mjs" 2>"$CHECK_DIR/err.txt"; then
      echo "  JS SYNTAX ERROR: $f"
      head -5 "$CHECK_DIR/err.txt"
      JS_FAIL=1
    fi
  done < <(find "$WEB_DIR/js" -name '*.js' -not -path '*/vendor/*')
  if [ "$JS_FAIL" -ne 0 ]; then
    echo "ERROR: JS syntax check failed — refusing to build"
    exit 1
  fi
else
  echo "[0.5/6] node not found at $NODE_BIN — skipping JS syntax check"
fi

echo "  Syncing launcher icon..."
python3 "$PROJ_DIR/sync-launcher-icon.py"

echo "[1/6] Compiling resources..."
"$BUILD_TOOLS/aapt2" compile \
    --dir "$SRC_DIR/res" \
    -o "$BUILD_DIR/compiled/"

echo "[2/6] Linking resources..."
"$BUILD_TOOLS/aapt2" link \
    -I "$PLATFORM" \
    --manifest "$SRC_DIR/AndroidManifest.xml" \
    --java "$BUILD_DIR/gen" \
    -o "$BUILD_DIR/app.unsigned.apk" \
    "$BUILD_DIR"/compiled/*.flat

echo "[3/6] Compiling Java..."
# mwiede JSch 0.2.x + BouncyCastle (ed25519) are required runtime dependencies.
JSCH_JAR="$PROJ_DIR/libs/jsch.jar"
BCPROV_JAR="$PROJ_DIR/libs/bcprov.jar"
CLASSPATH="$PLATFORM"
for REQUIRED_JAR in "$JSCH_JAR" "$BCPROV_JAR"; do
    if [ ! -f "$REQUIRED_JAR" ]; then
        echo "ERROR: missing required dependency $REQUIRED_JAR"
        exit 1
    fi
    CLASSPATH="$CLASSPATH:$REQUIRED_JAR"
done
javac \
    -source 17 -target 17 \
    -classpath "$CLASSPATH" \
    -sourcepath "$SRC_DIR/java:$BUILD_DIR/gen" \
    -d "$BUILD_DIR/classes" \
    "$SRC_DIR/java/com/cam/app/MainActivity.java" \
    "$SRC_DIR/java/com/cam/app/CamAssetLoader.java" \
    "$SRC_DIR/java/com/cam/app/CamJsBridge.java" \
    "$SRC_DIR/java/com/cam/app/MobileEmbeddedHub.java" \
    "$SRC_DIR/java/com/cam/app/MobileHostKeyRepository.java" \
    "$SRC_DIR/java/com/cam/app/MobileCredentialStore.java" \
    "$SRC_DIR/java/com/cam/app/MobileHubLog.java" \
    "$SRC_DIR/java/com/cam/app/MobileSshAuth.java" \
    "$SRC_DIR/java/com/cam/app/MobileSshPool.java" \
    "$SRC_DIR/java/com/cam/app/MobileSshExec.java" \
    "$SRC_DIR/java/com/cam/app/MobileAgentOutputSessions.java" \
    "$SRC_DIR/java/com/cam/app/MobileTerminalManager.java" \
    "$SRC_DIR/java/com/cam/app/MobileTerminalService.java" \
    "$SRC_DIR/java/com/cam/app/MobileWebSocket.java" \
    "$BUILD_DIR/gen/com/cam/app/R.java"

echo "[4/6] Dexing..."
D8_INPUT=$(find "$BUILD_DIR/classes" -name "*.class")
# Both dependencies ship multi-release classes that d8 rejects — strip for dex.
# Repacking 4,500+ BouncyCastle/JSch entries on the Windows-mounted tree is
# the dominant build cost, so cache the stripped jar on a native filesystem.
DEPS_CACHE="${CAM_DEPS_CACHE:-${TMPDIR:-/tmp}/cam-mobile-deps}"
mkdir -p "$DEPS_CACHE"
DEPS_KEY=$(sha256sum "$JSCH_JAR" "$BCPROV_JAR" | sha256sum | cut -d' ' -f1)
STRIPPED="$DEPS_CACHE/dependencies-android-$DEPS_KEY.jar"
if [ ! -f "$STRIPPED" ]; then
    echo "  Stripping dependency jars..."
    STRIP_DIR="$DEPS_CACHE/strip-$DEPS_KEY"
    rm -rf "$STRIP_DIR"
    mkdir -p "$STRIP_DIR/jsch" "$STRIP_DIR/bcprov"
    (cd "$STRIP_DIR/jsch" && jar xf "$JSCH_JAR" && rm -rf META-INF/versions)
    (cd "$STRIP_DIR/bcprov" && jar xf "$BCPROV_JAR" && rm -rf META-INF/versions)
    TMP_JAR="$STRIPPED.tmp.$$"
    jar cf0 "$TMP_JAR" -C "$STRIP_DIR/jsch" . -C "$STRIP_DIR/bcprov" .
    mv "$TMP_JAR" "$STRIPPED"
    rm -rf "$STRIP_DIR"
else
    echo "  Using cached stripped dependencies"
fi
"$BUILD_TOOLS/d8" \
    --lib "$PLATFORM" \
    --output "$BUILD_DIR/dex" \
    $D8_INPUT \
    "$STRIPPED"

echo "[5/6] Packaging APK..."
# Add dex to the APK. Python is already a build dependency, so packaging does
# not depend on a platform zip binary.
cp "$BUILD_DIR/app.unsigned.apk" "$BUILD_DIR/app.tmp.apk"
python3 - "$BUILD_DIR/app.tmp.apk" "$BUILD_DIR/dex/classes.dex" <<'PY'
import sys
import zipfile
apk, dex = sys.argv[1:]
with zipfile.ZipFile(apk, "a", compression=zipfile.ZIP_DEFLATED) as out:
    out.write(dex, "classes.dex")
PY

# Bundle web app into assets/web/ and camc into assets/camc/.
echo "  Bundling web assets..."
mkdir -p "$BUILD_DIR/assets_staging/assets/web"
python3 - "$WEB_DIR" "$BUILD_DIR/assets_staging/assets/web" <<'PY'
import shutil
import sys
src, dst = sys.argv[1:]
shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.apk"))
PY
CAMC_DIST="$PROJ_DIR/../../dist/camc"
if [ ! -f "$CAMC_DIST" ]; then
    echo "ERROR: Missing bundled camc at $CAMC_DIST"
    exit 1
fi
mkdir -p "$BUILD_DIR/assets_staging/assets/camc"
cp "$CAMC_DIST" "$BUILD_DIR/assets_staging/assets/camc/camc"
chmod 700 "$BUILD_DIR/assets_staging/assets/camc/camc"

# Extensions disabled for the 2.4.105 isolation build: no bundled agent-doctor.

python3 - "$BUILD_DIR/app.tmp.apk" "$BUILD_DIR/assets_staging" <<'PY'
import pathlib
import sys
import zipfile
apk, root = sys.argv[1:]
root = pathlib.Path(root)
with zipfile.ZipFile(apk, "a", compression=zipfile.ZIP_DEFLATED) as out:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out.write(path, path.relative_to(root).as_posix())
PY

# Zipalign
"$BUILD_TOOLS/zipalign" -f 4 \
    "$BUILD_DIR/app.tmp.apk" \
    "$BUILD_DIR/app.aligned.apk"

echo "[6/6] Signing..."
# Generate keystore if not exists
if [ ! -f "$KEYSTORE" ]; then
    echo "  Generating signing key..."
    keytool -genkeypair \
        -keystore "$KEYSTORE" \
        -alias "$KEY_ALIAS" \
        -keyalg RSA -keysize 2048 \
        -validity 10000 \
        -storepass "$KEY_PASS" \
        -keypass "$KEY_PASS" \
        -dname "CN=CAM,O=CAM,L=Unknown,ST=Unknown,C=US"
fi

"$BUILD_TOOLS/apksigner" sign \
    --ks "$KEYSTORE" \
    --ks-key-alias "$KEY_ALIAS" \
    --ks-pass "pass:$KEY_PASS" \
    --key-pass "pass:$KEY_PASS" \
    --out "$BUILD_DIR/cam.apk" \
    "$BUILD_DIR/app.aligned.apk"

# Also create versioned copy (CamUI V2)
cp "$BUILD_DIR/cam.apk" "$BUILD_DIR/camui-v2-${VERSION}.apk"

# Verify
"$BUILD_TOOLS/apksigner" verify "$BUILD_DIR/cam.apk"

SIZE=$(du -h "$BUILD_DIR/cam.apk" | cut -f1)
echo ""
echo "=== BUILD SUCCESS ==="
echo "Version: v${VERSION} (versionCode: ${VERSION_CODE})"
echo "APK: $BUILD_DIR/cam.apk ($SIZE)"
echo "APK: $BUILD_DIR/camui-v2-${VERSION}.apk"
echo "Install: adb install $BUILD_DIR/cam.apk"

# Auto-upload to Jianguoyun WebDev (see android/.upload-target)
if [[ -x "$PROJ_DIR/upload-apk.sh" ]]; then
  bash "$PROJ_DIR/upload-apk.sh" || echo "WARNING: APK upload failed (build still OK)"
fi
