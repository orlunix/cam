#!/usr/bin/env bash
# Patch a locally-installed CAM Desktop with the current assistant-host.cjs
# and assistant extension view, without rebuilding the MSI.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP_DIR="${REPO_ROOT}/apps/cam-desktop"
ASAR_BIN="${DESKTOP_DIR}/node_modules/.bin/asar"

# Common Windows install locations (WSL paths).
# The WSL user may differ from the Windows user, so check both.
CANDIDATES=(
  "/mnt/c/Program Files/CAM Desktop"
  "/mnt/c/Program Files (x86)/CAM Desktop"
  "/mnt/c/Users/${USER:-hren}/AppData/Local/Programs/cam-desktop"
  "/mnt/c/Users/Thinkpad/AppData/Local/Programs/cam-desktop"
)

INSTALL_DIR="${1:-}"
if [ -z "$INSTALL_DIR" ]; then
  for c in "${CANDIDATES[@]}"; do
    if [ -f "$c/resources/app.asar" ]; then
      INSTALL_DIR="$c"
      break
    fi
  done
fi

if [ -z "$INSTALL_DIR" ] || [ ! -f "$INSTALL_DIR/resources/app.asar" ]; then
  echo "Usage: $0 <install-dir>"
  echo "Example (WSL): $0 /mnt/c/Program\ Files/CAM\ Desktop"
  echo "Could not auto-detect a CAM Desktop install with resources/app.asar."
  exit 1
fi

echo "Target install: $INSTALL_DIR"
APP_ASAR="$INSTALL_DIR/resources/app.asar"
BACKUP_ASAR="$INSTALL_DIR/resources/app.asar.bak.$(date +%Y%m%d-%H%M%S)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# 1. Backup and unpack app.asar
cp "$APP_ASAR" "$BACKUP_ASAR"
echo "Backed up app.asar -> $BACKUP_ASAR"
"$ASAR_BIN" extract "$APP_ASAR" "$TMP_DIR/app"

# 2. Replace assistant-host.cjs
SRC_HOST="$DESKTOP_DIR/electron/assistant-host.cjs"
DST_HOST="$TMP_DIR/app/electron/assistant-host.cjs"
if [ -f "$DST_HOST" ]; then
  cp "$SRC_HOST" "$DST_HOST"
  echo "Patched electron/assistant-host.cjs"
else
  echo "Warning: $DST_HOST not found in app.asar, skipping host patch"
fi

# 3. Repack app.asar
"$ASAR_BIN" pack "$TMP_DIR/app" "$APP_ASAR"
echo "Repacked app.asar"

# 4. Replace built-in assistant extension view
SRC_VIEW="$REPO_ROOT/extensions/packages/assistant/index.html"
DST_VIEW="$INSTALL_DIR/resources/extensions/packages/assistant/index.html"
if [ -f "$DST_VIEW" ]; then
  cp "$SRC_VIEW" "$DST_VIEW"
  echo "Patched resources/extensions/packages/assistant/index.html"
else
  echo "Warning: $DST_VIEW not found, skipping built-in extension view patch"
fi

# 5. Replace user-data shadow copy if it exists (the embedded hub serves
#    user extensions before built-ins, so this is the file actually used).
USER_DATA_DIR=""
if [[ "$INSTALL_DIR" =~ ^/mnt/c/Users/([^/]+)/AppData/Local/Programs/cam-desktop$ ]]; then
  USER_DATA_DIR="/mnt/c/Users/${BASH_REMATCH[1]}/AppData/Roaming/cam-desktop"
fi
# Fallbacks in case the install path is non-standard.
CANDIDATE_DATA_DIRS=(
  "$USER_DATA_DIR"
  "/mnt/c/Users/Thinkpad/AppData/Roaming/cam-desktop"
  "/mnt/c/Users/${USER:-hren}/AppData/Roaming/cam-desktop"
)
declare -A PATCHED_DATA_DIRS
for dd in "${CANDIDATE_DATA_DIRS[@]}"; do
  [ -n "$dd" ] || continue
  [ -z "${PATCHED_DATA_DIRS[$dd]:-}" ] || continue
  PATCHED_DATA_DIRS[$dd]=1
  USER_VIEW="$dd/extensions/assistant/index.html"
  if [ -f "$USER_VIEW" ]; then
    cp "$SRC_VIEW" "$USER_VIEW"
    echo "Patched user-data shadow copy: $USER_VIEW"
  fi
done

# 6. Patch the web app cache-buster so the next mount loads fresh extension
#    views instead of a stale iframe cache entry.
SRC_EXT_HOST="$REPO_ROOT/web/js/shared/ext-view-host.js"
WEB_EXT_HOST="$INSTALL_DIR/resources/web/js/shared/ext-view-host.js"
if [ -f "$WEB_EXT_HOST" ]; then
  chmod +w "$WEB_EXT_HOST" 2>/dev/null || true
  cp "$SRC_EXT_HOST" "$WEB_EXT_HOST"
  echo "Patched resources/web/js/shared/ext-view-host.js"
fi

# 7. Bump the ext-view-host import version in the two parent modules so the
#    browser reloads the shared module instead of using a cached versionless
#    import (AGENTS.md: identical ?v= URL required for shared modules).
_WEB_PATCH_FILE() {
  local src="$1"
  local dst="$2"
  if [ -f "$dst" ]; then
    chmod +w "$dst" 2>/dev/null || true
    cp "$src" "$dst"
    echo "Patched $dst"
  fi
}
_WEB_PATCH_FILE "$REPO_ROOT/web/js/desktop/agent-console.js" "$INSTALL_DIR/resources/web/js/desktop/agent-console.js"
_WEB_PATCH_FILE "$REPO_ROOT/web/js/desktop/extensions-mode.js" "$INSTALL_DIR/resources/web/js/desktop/extensions-mode.js"
_WEB_PATCH_FILE "$REPO_ROOT/web/js/desktop/app.js" "$INSTALL_DIR/resources/web/js/desktop/app.js"
_WEB_PATCH_FILE "$REPO_ROOT/web/desktop.html" "$INSTALL_DIR/resources/web/desktop.html"

# 8. Clear Chromium's disk cache so the main app and extension views are
#    not served stale entries on the next launch.
for dd in "${CANDIDATE_DATA_DIRS[@]}"; do
  [ -n "$dd" ] || continue
  for cache in "$dd/Cache" "$dd/Code Cache"; do
    if [ -d "$cache" ]; then
      rm -rf "$cache"
      echo "Cleared cache: $cache"
    fi
  done
done

echo "Done. Restart CAM Desktop to use the patched files."
