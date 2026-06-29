#!/bin/bash
# Build CamUI WebView Probe APK (side-by-side with CamUI V2 for drift A/B/C testing).
set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
BUILD_TOOLS="$ANDROID_HOME/build-tools/34.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-34/android.jar"

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
ANDROID_DIR="$(cd "$PROJ_DIR/.." && pwd)"
SRC_DIR="$PROJ_DIR/src/main"
BUILD_DIR="$PROJ_DIR/build"
WEB_DIR="$ANDROID_DIR/../web"
MAIN_RES="$ANDROID_DIR/app/src/main/res"
KEYSTORE="$ANDROID_DIR/cam-release.keystore"
KEY_ALIAS="cam"
KEY_PASS="camapp123"

echo "=== CamUI WebView Probe APK Build ==="

for tool in "$BUILD_TOOLS/aapt2" "$BUILD_TOOLS/d8" "$BUILD_TOOLS/apksigner" "$BUILD_TOOLS/zipalign"; do
    [ -f "$tool" ] || { echo "ERROR: Missing $tool"; exit 1; }
done
[ -f "$PLATFORM" ] || { echo "ERROR: Missing platform jar"; exit 1; }

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"/{compiled,gen,classes,dex,res}

# Reuse launcher icon from main CamUI app
rsync -a "$MAIN_RES/mipmap-hdpi" "$MAIN_RES/mipmap-mdpi" "$MAIN_RES/mipmap-xhdpi" \
      "$MAIN_RES/mipmap-xxhdpi" "$MAIN_RES/mipmap-xxxhdpi" "$BUILD_DIR/res/" 2>/dev/null || true
rsync -a "$MAIN_RES/mipmap-anydpi-v26" "$BUILD_DIR/res/" 2>/dev/null || true

echo "[1/6] Compiling resources..."
"$BUILD_TOOLS/aapt2" compile --dir "$BUILD_DIR/res" -o "$BUILD_DIR/compiled/" 2>/dev/null || true
"$BUILD_TOOLS/aapt2" compile --dir "$MAIN_RES/values" -o "$BUILD_DIR/compiled/" 2>/dev/null || true

echo "[2/6] Linking resources..."
"$BUILD_TOOLS/aapt2" link \
    -I "$PLATFORM" \
    --manifest "$SRC_DIR/AndroidManifest.xml" \
    --java "$BUILD_DIR/gen" \
    -o "$BUILD_DIR/app.unsigned.apk" \
    "$BUILD_DIR"/compiled/*.flat 2>/dev/null || \
"$BUILD_TOOLS/aapt2" link \
    -I "$PLATFORM" \
    --manifest "$SRC_DIR/AndroidManifest.xml" \
    --java "$BUILD_DIR/gen" \
    -o "$BUILD_DIR/app.unsigned.apk"

echo "[3/6] Compiling Java..."
javac \
    -source 17 -target 17 \
    -classpath "$PLATFORM" \
    -sourcepath "$SRC_DIR/java:$ANDROID_DIR/app/src/main/java:$BUILD_DIR/gen" \
    -d "$BUILD_DIR/classes" \
    "$ANDROID_DIR/app/src/main/java/com/cam/app/CamAssetLoader.java" \
    "$SRC_DIR/java/com/cam/probe/ProbeActivity.java" \
    "$BUILD_DIR/gen/com/cam/probe/R.java"

echo "[4/6] Dexing..."
"$BUILD_TOOLS/d8" --lib "$PLATFORM" --output "$BUILD_DIR/dex" \
    $(find "$BUILD_DIR/classes" -name "*.class")

echo "[5/6] Packaging APK..."
cp "$BUILD_DIR/app.unsigned.apk" "$BUILD_DIR/app.tmp.apk"
cd "$BUILD_DIR/dex" && zip -u "$BUILD_DIR/app.tmp.apk" classes.dex && cd "$PROJ_DIR"

mkdir -p "$BUILD_DIR/assets_staging/assets/web"
cp "$PROJ_DIR/assets/web/probe-"*.html "$BUILD_DIR/assets_staging/assets/web/"
rsync -a --exclude='*.apk' "$WEB_DIR/" "$BUILD_DIR/assets_staging/assets/web/"
cd "$BUILD_DIR/assets_staging" && zip -r -u "$BUILD_DIR/app.tmp.apk" assets/ && cd "$PROJ_DIR"

"$BUILD_TOOLS/zipalign" -f 4 "$BUILD_DIR/app.tmp.apk" "$BUILD_DIR/app.aligned.apk"

echo "[6/6] Signing..."
"$BUILD_TOOLS/apksigner" sign \
    --ks "$KEYSTORE" \
    --ks-key-alias "$KEY_ALIAS" \
    --ks-pass "pass:$KEY_PASS" \
    --key-pass "pass:$KEY_PASS" \
    --out "$BUILD_DIR/camui-probe.apk" \
    "$BUILD_DIR/app.aligned.apk"

"$BUILD_TOOLS/apksigner" verify "$BUILD_DIR/camui-probe.apk"
SIZE=$(du -h "$BUILD_DIR/camui-probe.apk" | cut -f1)
echo ""
echo "=== PROBE BUILD SUCCESS ==="
echo "APK: $BUILD_DIR/camui-probe.apk ($SIZE)"
echo "Install: adb install $BUILD_DIR/camui-probe.apk"
echo "(Can install alongside CamUI V2 — different package com.cam.probe)"
