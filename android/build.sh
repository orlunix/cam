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
WEB_DIR="$PROJ_DIR/../web"
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
sed -i "s/?v=[0-9][^\"']*/?v=${VERSION}/g" "$WEB_DIR/js/mobile/app.js"
sed -i "s/?v=[0-9][^\"']*/?v=${VERSION}/g" "$WEB_DIR/js/mobile/settings.js"
sed -i "s/?v=[0-9][^\"']*/?v=${VERSION}/g" "$WEB_DIR/css/mobile.css" 2>/dev/null || true

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"/{compiled,gen,classes,dex}

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
# mwiede JSch 0.2.x (com.github.mwiede:jsch) — required for OpenSSH 9.x KEX on corporate hosts.
JSCH_JAR="$PROJ_DIR/libs/jsch.jar"
CLASSPATH="$PLATFORM"
if [ -f "$JSCH_JAR" ]; then
    CLASSPATH="$CLASSPATH:$JSCH_JAR"
else
    echo "WARNING: $JSCH_JAR missing — Sync Host SSH will not build"
fi
javac \
    -source 17 -target 17 \
    -classpath "$CLASSPATH" \
    -sourcepath "$SRC_DIR/java:$BUILD_DIR/gen" \
    -d "$BUILD_DIR/classes" \
    "$SRC_DIR/java/com/cam/app/MainActivity.java" \
    "$SRC_DIR/java/com/cam/app/CamAssetLoader.java" \
    "$SRC_DIR/java/com/cam/app/CamJsBridge.java" \
    "$SRC_DIR/java/com/cam/app/MobileEmbeddedHub.java" \
    "$SRC_DIR/java/com/cam/app/MobileCredentialStore.java" \
    "$SRC_DIR/java/com/cam/app/MobileHubLog.java" \
    "$SRC_DIR/java/com/cam/app/MobileSshAuth.java" \
    "$SRC_DIR/java/com/cam/app/MobileSshPool.java" \
    "$SRC_DIR/java/com/cam/app/MobileSshExec.java" \
    "$SRC_DIR/java/com/cam/app/MobileTerminalManager.java" \
    "$BUILD_DIR/gen/com/cam/app/R.java"

echo "[4/6] Dexing..."
D8_INPUT=$(find "$BUILD_DIR/classes" -name "*.class")
JSCH_DEX="$JSCH_JAR"
if [ -f "$JSCH_JAR" ]; then
    # mwiede 0.2.16+ ships multi-release classes that d8 rejects — strip for dex.
    STRIP_DIR="$BUILD_DIR/jsch-strip"
    STRIPPED="$BUILD_DIR/jsch-android.jar"
    rm -rf "$STRIP_DIR" "$STRIPPED"
    mkdir -p "$STRIP_DIR"
    (cd "$STRIP_DIR" && jar xf "$JSCH_JAR" && rm -rf META-INF/versions && jar cf "$STRIPPED" .)
    JSCH_DEX="$STRIPPED"
fi
if [ -f "$JSCH_DEX" ]; then
    "$BUILD_TOOLS/d8" \
        --lib "$PLATFORM" \
        --output "$BUILD_DIR/dex" \
        $D8_INPUT \
        "$JSCH_DEX"
else
    "$BUILD_TOOLS/d8" \
        --lib "$PLATFORM" \
        --output "$BUILD_DIR/dex" \
        $D8_INPUT
fi

echo "[5/6] Packaging APK..."
# Add dex to the APK
cp "$BUILD_DIR/app.unsigned.apk" "$BUILD_DIR/app.tmp.apk"
cd "$BUILD_DIR/dex"
zip -u "$BUILD_DIR/app.tmp.apk" classes.dex
cd "$PROJ_DIR"

# Bundle web app into assets/web/
echo "  Bundling web assets..."
mkdir -p "$BUILD_DIR/assets_staging/assets/web"
rsync -a --exclude='*.apk' "$WEB_DIR/" "$BUILD_DIR/assets_staging/assets/web/"
cd "$BUILD_DIR/assets_staging"
zip -r -u "$BUILD_DIR/app.tmp.apk" assets/
cd "$PROJ_DIR"

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
