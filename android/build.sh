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

echo "=== CAM APK Build v${VERSION} (code: ${VERSION_CODE}) ==="

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

# Web assets: update cam-version meta tag and cache-busting ?v= query strings
sed -i "s/content=\"v[^\"]*\"/content=\"v${VERSION}\"/" "$WEB_DIR/index.html"
sed -i "s/?v=[^\"]*/?v=${VERSION}/g" "$WEB_DIR/index.html"
sed -i "s/?v=[^']*'/?v=${VERSION}'/g" "$WEB_DIR/js/app.js"
sed -i "s/cam-v[^']*/cam-v${VERSION}/" "$WEB_DIR/sw.js"
sed -i "s/?v=[^']*'/?v=${VERSION}'/g" "$WEB_DIR/sw.js"

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
javac \
    -source 17 -target 17 \
    -classpath "$PLATFORM" \
    -sourcepath "$SRC_DIR/java:$BUILD_DIR/gen" \
    -d "$BUILD_DIR/classes" \
    "$SRC_DIR/java/com/cam/app/MainActivity.java" \
    "$BUILD_DIR/gen/com/cam/app/R.java"

echo "[4/6] Dexing..."
"$BUILD_TOOLS/d8" \
    --lib "$PLATFORM" \
    --output "$BUILD_DIR/dex" \
    $(find "$BUILD_DIR/classes" -name "*.class")

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

# Also create versioned copy
cp "$BUILD_DIR/cam.apk" "$BUILD_DIR/cam-v${VERSION}.apk"

# Verify
"$BUILD_TOOLS/apksigner" verify "$BUILD_DIR/cam.apk"

SIZE=$(du -h "$BUILD_DIR/cam.apk" | cut -f1)
echo ""
echo "=== BUILD SUCCESS ==="
echo "Version: v${VERSION} (versionCode: ${VERSION_CODE})"
echo "APK: $BUILD_DIR/cam.apk ($SIZE)"
echo "APK: $BUILD_DIR/cam-v${VERSION}.apk"
echo "Install: adb install $BUILD_DIR/cam.apk"
