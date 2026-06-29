#!/bin/bash
# Upload CamUI APK(s) to Jianguoyun WebDAV (default: WebDev/).
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_FILE="$PROJ_DIR/.upload-target"
VERSION="$(head -1 "$PROJ_DIR/VERSION" | tr -d '[:space:]')"
APK="$PROJ_DIR/build/cam.apk"
VERSIONED="$PROJ_DIR/build/camui-v2-${VERSION}.apk"

if [[ -f "$TARGET_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$TARGET_FILE"
fi

WEBDAV_BASE="${WEBDAV_BASE:-https://dav.jianguoyun.com/dav}"
WEBDAV_USER="${WEBDAV_USER:-}"
WEBDAV_PASS="${WEBDAV_PASS:-${JIANGUOYUN_PASS:-}}"
WEBDAV_DIR="${WEBDAV_DIR:-WebDev}"

if [[ -z "$WEBDAV_USER" || -z "$WEBDAV_PASS" ]]; then
  echo "SKIP upload: set WEBDAV_USER/WEBDAV_PASS in android/.upload-target or env"
  exit 0
fi

if [[ ! -f "$APK" ]]; then
  echo "ERROR: missing $APK — run build.sh first"
  exit 1
fi

AUTH="${WEBDAV_USER}:${WEBDAV_PASS}"
BASE="${WEBDAV_BASE%/}/${WEBDAV_DIR}"

echo "=== Upload CamUI v${VERSION} → ${WEBDAV_DIR}/ ==="
/usr/bin/curl -sS -u "$AUTH" -X MKCOL "$BASE/" -o /dev/null -w "MKCOL HTTP %{http_code}\n" || true

for dest in "cam.apk" "camui-v2-${VERSION}.apk"; do
  src="$APK"
  [[ "$dest" != "cam.apk" && -f "$VERSIONED" ]] && src="$VERSIONED"
  echo "Uploading $dest ..."
  /usr/bin/curl -sS -u "$AUTH" -T "$src" "$BASE/$dest" \
    -w "  HTTP %{http_code} size %{size_upload}\n" -o /dev/null
done

echo "Done: ${WEBDAV_DIR}/cam.apk + ${WEBDAV_DIR}/camui-v2-${VERSION}.apk"
