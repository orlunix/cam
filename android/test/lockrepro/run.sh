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

echo "=== TestAgentOps ==="
rm -rf "$WORK/agentops-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$JSON_JAR" TestAgentOps "$WORK/agentops-data"

echo "=== TestProxyJump ==="
rm -rf "$WORK/jump-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$JSON_JAR" TestProxyJump "$WORK/jump-data"

echo "=== TestSshConfig ==="
rm -rf "$WORK/sshconfig-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$JSON_JAR" TestSshConfig "$WORK/sshconfig-data"

# Real two-hop ProxyJump test: two user-level sshd instances (jump 22221,
# target 22222), RSA key auth on both hops. Skipped when sshd is missing.
if [ -x /usr/sbin/sshd ]; then
  echo "=== TestJumpConnect (real two-hop sshd) ==="
  JT=/tmp/jumptest
  rm -rf "$JT"
  mkdir -p "$JT"/{jump,target}
  ssh-keygen -t rsa -b 3072 -N '' -f "$JT/id_rsa_test" -q
  cat "$JT/id_rsa_test.pub" > "$JT/authorized_keys"
  chmod 600 "$JT/authorized_keys" "$JT/id_rsa_test"
  ssh-keygen -t rsa -N '' -f "$JT/jump/hostkey" -q
  ssh-keygen -t rsa -N '' -f "$JT/target/hostkey" -q
  for inst in jump:22221 target:22222; do
    name="${inst%%:*}"; port="${inst##*:}"
    cat > "$JT/$name/sshd_config" <<EOF
Port $port
ListenAddress 127.0.0.1
HostKey $JT/$name/hostkey
AuthorizedKeysFile $JT/authorized_keys
PasswordAuthentication no
PubkeyAuthentication yes
UsePAM no
StrictModes no
PidFile $JT/$name/pid
AllowTcpForwarding yes
EOF
    /usr/sbin/sshd -f "$JT/$name/sshd_config"
  done
  sleep 1
  "$JAVA" -cp "$WORK/classes:$JSCH_JAR:$JSON_JAR" TestJumpConnect
  RC=$?
  pkill -f "sshd -f $JT/" 2>/dev/null || true
  [ "$RC" -eq 0 ] || exit "$RC"
else
  echo "=== TestJumpConnect SKIPPED (no /usr/sbin/sshd) ==="
fi
