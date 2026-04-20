#!/usr/bin/env bash
# camc release: build → test → scp to every machine in machines.json → verify.
#
# Mirrors the TeaSpirit single-file-artifact model: one self-contained camc
# binary is produced from src/camc_pkg/, then shipped to each remote host
# (list comes from ~/.cam/machines.json) and its version is sanity-checked
# via ssh.
#
# Usage:
#   scripts/release.sh                     # full release
#   scripts/release.sh --skip-tests        # skip pytest
#   scripts/release.sh --skip-build        # reuse dist/camc as-is
#   scripts/release.sh --only NAME[,NAME]  # deploy to a subset of machines
#   scripts/release.sh --dry-run           # print planned actions, don't scp
#   scripts/release.sh --help
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MACHINES_FILE="${CAMC_MACHINES_FILE:-$HOME/.cam/machines.json}"
DIST_BIN="$REPO_ROOT/dist/camc"
REMOTE_PATH="~/.cam/camc"
SSH_TIMEOUT=10

SKIP_TESTS=0
SKIP_BUILD=0
DRY_RUN=0
ONLY=""

usage() {
  sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log()   { printf '\033[1;34m[release]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[release]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[release]\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[1;31m[release]\033[0m %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --only)       ONLY="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage 0 ;;
    *)            err "unknown flag: $1"; usage 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# 1. Build
# ---------------------------------------------------------------------------
if [[ $SKIP_BUILD -eq 1 ]]; then
  [[ -x "$DIST_BIN" ]] || { err "--skip-build but $DIST_BIN is missing"; exit 1; }
  log "skip build (using existing $DIST_BIN)"
else
  log "building dist/camc ..."
  cd "$REPO_ROOT"
  python3 build_camc.py
  [[ -x "$DIST_BIN" ]] || { err "build produced no $DIST_BIN"; exit 1; }
fi

LOCAL_VERSION="$("$DIST_BIN" version 2>/dev/null | head -1)"
[[ -n "$LOCAL_VERSION" ]] || { err "dist/camc didn't print a version"; exit 1; }
ok "local: $LOCAL_VERSION"

# ---------------------------------------------------------------------------
# 2. Test
# ---------------------------------------------------------------------------
if [[ $SKIP_TESTS -eq 1 ]]; then
  warn "skip tests (--skip-tests)"
else
  log "running tests (tests/test_camc_session_id.py) ..."
  cd "$REPO_ROOT"
  python3 -m pytest tests/test_camc_session_id.py -q
fi

# ---------------------------------------------------------------------------
# 3. Read machines.json
# ---------------------------------------------------------------------------
[[ -f "$MACHINES_FILE" ]] || { err "machines file not found: $MACHINES_FILE"; exit 1; }

# Emit "name\thost\tuser\tport" lines for each ssh machine; skip local-only /
# unreachable entries (port blank AND host = localhost).
MACHINES="$(
python3 - "$MACHINES_FILE" "$ONLY" <<'PY'
import json, sys
path, only = sys.argv[1], sys.argv[2]
want = set(x.strip() for x in only.split(",") if x.strip()) if only else None
with open(path) as f:
    data = json.load(f)
for m in data:
    if m.get("type") != "ssh":
        continue
    name = m.get("name", "")
    if want is not None and name not in want:
        continue
    host = m.get("host", "")
    user = m.get("user", "")
    port = m.get("port") or ""
    if not host:
        continue
    print("%s\t%s\t%s\t%s" % (name, host, user, port))
PY
)"

if [[ -z "$MACHINES" ]]; then
  err "no machines to deploy to"
  exit 1
fi

log "deploy targets:"
printf '%s\n' "$MACHINES" | awk -F'\t' '{printf "   %-28s %s@%s:%s\n", $1, $3, $2, $4}'

# ---------------------------------------------------------------------------
# 4. Deploy + verify, per machine
# ---------------------------------------------------------------------------
# Reuse SSH ControlMaster sockets (same scheme cam uses) so each ssh/scp
# round-trip piggy-backs on an already-authenticated connection when one
# exists. The path mirrors SSHTransport: sha256(user@host:port)[:12].
cm_path() {
  local user="$1" host="$2" port="$3"
  [[ -z "$port" ]] && port=22
  python3 -c "
import hashlib, sys
h = hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12]
print('/tmp/cam-ssh-%s' % h)
" "${user}@${host}:${port}"
}

ssh_args() {
  local user="$1" host="$2" port="$3"
  local cm; cm="$(cm_path "$user" "$host" "$port")"
  # -n prevents ssh from reading from stdin — critical when ssh runs inside
  # the `while read ... <<< "$MACHINES"` loop below, where an ssh that
  # consumes stdin will eat the remaining machine rows and truncate the loop.
  local args=(
    -n
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout="$SSH_TIMEOUT"
    -o ControlPath="$cm"
    -o ControlMaster=auto
    -o ControlPersist=600
  )
  [[ -n "$port" ]] && args+=(-p "$port")
  printf '%s\n' "${args[@]}"
}

# Stable port flag for scp (-P instead of -p) and no -n (unsupported by scp).
scp_args() {
  ssh_args "$@" | sed -e '/^-n$/d' -e 's/^-p$/-P/'
}

deployed=0
verified=0
failed=0
failures=()

while IFS=$'\t' read -r name host user port; do
  [[ -z "$name" ]] && continue
  target="${user:+${user}@}${host}"
  label="$name ($target${port:+:$port})"
  log "→ $label"

  mapfile -t SSH_OPTS < <(ssh_args "$user" "$host" "$port")
  mapfile -t SCP_OPTS < <(scp_args "$user" "$host" "$port")

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '   scp %s %s %s:%s\n' "${SCP_OPTS[*]}" "$DIST_BIN" "$target" "$REMOTE_PATH"
    printf '   ssh %s %s %s\n' "${SSH_OPTS[*]}" "$target" "$REMOTE_PATH version"
    continue
  fi

  # 4a: ensure ~/.cam exists on the remote (scp won't mkdir parents)
  if ! ssh "${SSH_OPTS[@]}" "$target" "mkdir -p ~/.cam" >/dev/null 2>&1; then
    err "   mkdir ~/.cam failed on $label"
    failed=$((failed + 1)); failures+=("$name:mkdir"); continue
  fi

  # 4b: scp the binary (explicit </dev/null to guarantee scp doesn't touch
  # the while-read heredoc even if some future scp build reads stdin).
  if ! scp "${SCP_OPTS[@]}" "$DIST_BIN" "$target:$REMOTE_PATH" </dev/null >/dev/null 2>&1; then
    err "   scp failed on $label"
    failed=$((failed + 1)); failures+=("$name:scp"); continue
  fi
  ssh "${SSH_OPTS[@]}" "$target" "chmod +x $REMOTE_PATH" >/dev/null 2>&1 || true
  deployed=$((deployed + 1))

  # 4c: verify version. Keep the remote command trivial (`camc version`)
  # so it runs on any login shell — several containers here use tcsh and
  # don't like `2>&1 | head` syntax. Do the filtering on the client side.
  remote_ver="$(ssh "${SSH_OPTS[@]}" "$target" "$REMOTE_PATH version" 2>/dev/null | head -1 | tr -d '\r' || true)"
  if [[ -z "$remote_ver" ]]; then
    err "   verify failed (no output) on $label"
    failed=$((failed + 1)); failures+=("$name:verify"); continue
  fi

  if [[ "$remote_ver" == "$LOCAL_VERSION" ]]; then
    ok "   $remote_ver"
    verified=$((verified + 1))
  else
    warn "   version mismatch: expected '$LOCAL_VERSION', got '$remote_ver'"
    failures+=("$name:mismatch")
    failed=$((failed + 1))
  fi
done <<< "$MACHINES"

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo
printf '\033[1m%s\033[0m\n' "summary"
printf '   local version: %s\n' "$LOCAL_VERSION"
printf '   deployed     : %d\n' "$deployed"
printf '   verified     : %d\n' "$verified"
printf '   failed       : %d\n' "$failed"
if (( ${#failures[@]} > 0 )); then
  printf '   failed hosts : %s\n' "${failures[*]}"
  exit 1
fi
exit 0
