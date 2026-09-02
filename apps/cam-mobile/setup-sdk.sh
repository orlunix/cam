#!/bin/bash
#
# Install JDK 17 + Android SDK for building CAM APK
# Usage: sudo ./setup-sdk.sh
#

set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
CMDLINE_ZIP="commandlinetools-linux-11076708_latest.zip"
CMDLINE_URL="https://dl.google.com/android/repository/$CMDLINE_ZIP"

echo "=== CAM Android SDK Setup ==="

# 1. JDK 17
echo "[1/3] Installing OpenJDK 17..."
if java -version 2>&1 | grep -q "17\."; then
    echo "  Already installed"
else
    apt install -y openjdk-17-jdk-headless
fi
java -version 2>&1 | head -1

# 2. Android command-line tools
echo "[2/3] Installing Android command-line tools..."
if [ -f "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
    echo "  Already installed"
else
    mkdir -p "$ANDROID_HOME/cmdline-tools"
    cd /tmp
    if [ ! -f "$CMDLINE_ZIP" ]; then
        wget -q --show-progress "$CMDLINE_URL"
    fi
    unzip -qo "$CMDLINE_ZIP" -d "$ANDROID_HOME/cmdline-tools/"
    # Google packages it as cmdline-tools/cmdline-tools, rename to latest
    rm -rf "$ANDROID_HOME/cmdline-tools/latest"
    mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm -f "$CMDLINE_ZIP"
fi

# 3. SDK components
echo "[3/3] Installing SDK platform + build-tools..."
export ANDROID_HOME
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
    "platforms;android-34" "build-tools;34.0.0" 2>&1 | tail -5

echo ""
echo "=== SETUP COMPLETE ==="
echo "ANDROID_HOME=$ANDROID_HOME"
echo ""
echo "Add to your shell profile:"
echo "  export ANDROID_HOME=$ANDROID_HOME"
echo ""
echo "Now run:  ./build.sh"
