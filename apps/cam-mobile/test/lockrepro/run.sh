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
BCPROV_JAR="$HERE/../../libs/bcprov.jar"
WORK="$HERE/build"
JSON_JAR="$WORK/json.jar"
JAVAC="${JAVAC:-javac}"
JAVA="${JAVA:-java}"

for REQUIRED_JAR in "$JSCH_JAR" "$BCPROV_JAR"; do
    if [ ! -f "$REQUIRED_JAR" ]; then
        echo "ERROR: missing $REQUIRED_JAR"
        exit 1
    fi
done
mkdir -p "$WORK/real/com/cam/app" "$WORK/classes"

# org.json jar (not in the Android build — android provides it in-framework)
if [ ! -f "$JSON_JAR" ]; then
    curl -fsSL -o "$JSON_JAR" \
        "https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar"
fi

# Real classes under test — copied fresh so the test always reflects the tree.
for f in MobileEmbeddedHub MobileSshAuth MobileSshPool MobileSshExec MobileHubLog MobileAgentOutputSessions MobileTerminalManager MobileWebSocket MobileHostKeyRepository; do
    cp "$APP_SRC/$f.java" "$WORK/real/com/cam/app/"
done

SOURCES=$(find "$HERE/stubs" "$HERE/tests" "$WORK/real" -name "*.java")
"$JAVAC" -cp "$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" -d "$WORK/classes" $SOURCES

echo "=== TestMobileWebSocket ==="
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" com.cam.app.TestMobileWebSocket

echo "=== TestMobileTerminalReadyGate ==="
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" com.cam.app.TestMobileTerminalReadyGate

echo "=== TestHubLifecycle ==="
rm -rf "$WORK/lifecycle-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestHubLifecycle "$WORK/lifecycle-data"

echo "=== TestHubLock ==="
rm -rf "$WORK/data"   # fresh store per run — the test seeds its own context
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestHubLock "$WORK/data"

echo "=== TestAgentOps ==="
rm -rf "$WORK/agentops-data"
mkdir -p "$WORK/agentops-data/assets/extensions"
cp -r "$HERE/../../../../extensions/examples/agent-doctor" "$WORK/agentops-data/assets/extensions/"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestAgentOps "$WORK/agentops-data"

echo "=== TestHubContract ==="
rm -rf "$WORK/contract-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestHubContract "$WORK/contract-data" "$HERE/../hub-contract/contract.json"

echo "=== TestProxyJump ==="
rm -rf "$WORK/jump-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestProxyJump "$WORK/jump-data"

echo "=== TestSshConfig ==="
rm -rf "$WORK/sshconfig-data"
"$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestSshConfig "$WORK/sshconfig-data"

# Real sshd tests: RSA ProxyJump plus ed25519 direct and two-hop auth.
if [ -x /usr/sbin/sshd ]; then
  echo "=== TestJumpConnect (real sshd) ==="
  JT=/tmp/jumptest
  rm -rf "$JT"
  mkdir -p "$JT"/{jump,target}
  trap 'pkill -f "sshd -f /tmp/jumptest/" 2>/dev/null || true' EXIT
  ssh-keygen -t rsa -b 3072 -N '' -f "$JT/id_rsa_test" -q
  ssh-keygen -t ed25519 -N '' -f "$JT/id_ed25519_test" -q
  cat "$JT/id_rsa_test.pub" "$JT/id_ed25519_test.pub" > "$JT/authorized_keys"
  chmod 600 "$JT/authorized_keys" "$JT/id_rsa_test" "$JT/id_ed25519_test"
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
  "$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestJumpConnect
  "$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestJumpConnect "$JT/id_ed25519_test" direct
  "$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestJumpConnect "$JT/id_ed25519_test"
  pkill -f "sshd -f $JT/target/sshd_config" 2>/dev/null || true
  rm -f "$JT/target/hostkey" "$JT/target/hostkey.pub"
  ssh-keygen -t rsa -N '' -f "$JT/target/hostkey" -q
  /usr/sbin/sshd -f "$JT/target/sshd_config"
  sleep 1
  "$JAVA" -cp "$WORK/classes:$JSCH_JAR:$BCPROV_JAR:$JSON_JAR" TestJumpConnect "$JT/id_ed25519_test" changed
  pkill -f "sshd -f $JT/" 2>/dev/null || true
  trap - EXIT
else
  echo "=== TestJumpConnect SKIPPED (no /usr/sbin/sshd) ==="
fi
