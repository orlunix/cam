#!/bin/bash
#
# JVM regression test: MobileEmbeddedHub must not let one unreachable
# host's SSH operation stall unrelated API requests (the global-lock bug).
#
# Compiles the REAL hub + SSH classes fresh from android/app/src/main/
# with minimal Android stubs, then runs tests/TestHubLock.java:
#   - POST /api/contexts/bh/sync against a blackhole host (192.0.2.1)
#     blocks for the full SSH connect budget (~120s) in the background
#   - GET /api/system/health and /api/agents must stay instant
#
# Needs: JDK 17+ (javac/java on PATH, or set JAVAC/JAVA), curl.
# Usage: android/test/lockrepro/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_SRC="$HERE/../../app/src/main/java/com/cam/app"
JSCH_JAR="$HERE/../../libs/jsch.jar"
WORK="$HERE/build"
JSON_JAR="$WORK/json.jar"
JAVAC="${JAVAC:-javac}"
JAVA="${JAVA:-java}"

if [ ! -f "$JSCH_JAR" ]; then
    echo "ERROR: missing $JSCH_JAR"
    exit 1
fi
mkdir -p "$WORK/real/com/cam/app" "$WORK/classes"

# org.json jar (not in the Android build — android provides it in-framework)
if [ ! -f "$JSON_JAR" ]; then
    curl -fsSL -o "$JSON_JAR" \
        "https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar"
fi

# Real classes under test — copied fresh so the test always reflects the tree.
for f in MobileEmbeddedHub MobileSshAuth MobileSshPool MobileSshExec MobileHubLog MobileAgentOutputSessions; do
    cp "$APP_SRC/$f.java" "$WORK/real/com/cam/app/"
done

SOURCES=$(find "$HERE/stubs" "$HERE/tests" "$WORK/real" -name "*.java")
"$JAVAC" -cp "$JSCH_JAR:$JSON_JAR" -d "$WORK/classes" $SOURCES

echo "=== TestHubLock ==="
rm -rf "$WORK/data"   # fresh store per run — the test seeds its own context
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$JSON_JAR" TestHubLock "$WORK/data"
