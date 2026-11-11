#!/bin/sh
# Scrub internal hostnames and code names from a tree before publishing it to
# the public GitHub mirror. Idempotent. Usage: scrub.sh <dir>
#
# Every deny-listed token is written obfuscated (PD[X], nvidia[.]com, ...) so
# this file survives its own scrub. Keep that convention when editing.
set -eu

DIR=${1:?usage: scrub.sh DIR}

# Deny-list (discovery + final verification gate). Boundary rules use
# ([^A-Za-z0-9]|$) so underscore-embedded identifiers are caught while commit
# hashes such as dc78e41 are not.
TEXT_RE='hlren[.]duckdns[.]org'
TEXT_RE="$TEXT_RE"'|[A-Za-z0-9][A-Za-z0-9.-]*[.]nvidia[.]com'
TEXT_RE="$TEXT_RE"'|svc[r]vgit'
TEXT_RE="$TEXT_RE"'|PD[X]([0-9]*)(-[0-9]+)?([^A-Za-z0-9]|$)|pd[x]([0-9]*)(-[0-9]+)?([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|KI[X]([0-9]*)([^A-Za-z0-9]|$)|ki[x]([0-9]*)([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|DC[67]([^A-Za-z0-9]|$)|dc[67]([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|prg[n][_]share|PRG[N]([^A-Za-z0-9]|$)|prg[n]([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|RN10[2][NS]?([^A-Za-z0-9]|$)|rn10[2][ns]?([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|FN10[0]([^A-Za-z0-9]|$)|fn10[0]([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|FN21[1]([^A-Za-z0-9]|$)|fn21[1]([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|XRI[S]CV([^A-Za-z0-9]|$)|xri[s]cv([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|NVB[u]g([^A-Za-z0-9]|$)|nvbug[s](-cli)?([^A-Za-z0-9]|$)'
TEXT_RE="$TEXT_RE"'|gsp[-]riscv[-]pilot[-]|NR[1]0([^A-Za-z0-9]|$)|\bS[C]\b'

scrub_text() {
  root=$1
  files=$(grep -rlIE "$TEXT_RE" "$root" || true)
  [ -z "$files" ] && return 0
  printf '%s\n' "$files" | while IFS= read -r f; do
    sed -i -E \
      -e 's/hlren[.]duckdns[.]org/relay.example.com/g' \
      -e 's/[A-Za-z0-9][A-Za-z0-9.-]*[.]nvidia[.]com/node.example.com/g' \
      -e 's/svc[r]vgit/svcuser/g' \
      -e 's/PD[X]([0-9]*)(-[0-9]+)?([^A-Za-z0-9]|$)/SITE-A\1\2\3/g' \
      -e 's/pd[x]([0-9]*)(-[0-9]+)?([^A-Za-z0-9]|$)/site-a\1\2\3/g' \
      -e 's/KI[X]([0-9]*)([^A-Za-z0-9]|$)/SITE-B\1\2/g' \
      -e 's/ki[x]([0-9]*)([^A-Za-z0-9]|$)/site-b\1\2/g' \
      -e 's/DC[7]([^A-Za-z0-9]|$)/SITE-C\1/g' \
      -e 's/dc[7]([^A-Za-z0-9]|$)/site-c\1/g' \
      -e 's/DC[6]([^A-Za-z0-9]|$)/SITE-D\1/g' \
      -e 's/dc[6]([^A-Za-z0-9]|$)/site-d\1/g' \
      -e 's/prg[n][_]share/site_share/g' \
      -e 's/PRG[N]([^A-Za-z0-9]|$)/SITE-E\1/g' \
      -e 's/prg[n]([^A-Za-z0-9]|$)/site-e\1/g' \
      -e 's/RN10[2][NS]?([^A-Za-z0-9]|$)/PROJ-A\1/g' \
      -e 's/rn10[2][ns]?([^A-Za-z0-9]|$)/proj-a\1/g' \
      -e 's/FN10[0]([^A-Za-z0-9]|$)/MOD-A\1/g' \
      -e 's/fn10[0]([^A-Za-z0-9]|$)/mod-a\1/g' \
      -e 's/FN21[1]([^A-Za-z0-9]|$)/MOD-B\1/g' \
      -e 's/fn21[1]([^A-Za-z0-9]|$)/mod-b\1/g' \
      -e 's/XRI[S]CV([^A-Za-z0-9]|$)/PROJ-B\1/g' \
      -e 's/xri[s]cv([^A-Za-z0-9]|$)/proj-b\1/g' \
      -e 's/NVB[u]g([^A-Za-z0-9]|$)/bugtracker\1/g' \
      -e 's/nvbug[s](-cli)?([^A-Za-z0-9]|$)/bugtracker\1\2/g' \
      -e 's/gsp[-]riscv[-]pilot[-]/workspace-/g' \
      -e 's/NR[1]0([^A-Za-z0-9]|$)/TAG10\1/g' \
      -e 's/\bS[C]\b/SITE-F/g' \
      "$f"
  done
}

# Rename files/directories whose names contain code names (contents alone are
# not enough: paths are published too). Deepest first so parents rename last.
rename_paths() {
  root=$1
  find "$root" -depth -mindepth 1 \( -type f -o -type d \) | while IFS= read -r p; do
    b=$(basename "$p")
    n=$(printf '%s' "$b" | sed -E \
      -e 's/pd[x]/site-a/g'   -e 's/PD[X]/SITE-A/g' \
      -e 's/ki[x]/site-b/g'   -e 's/KI[X]/SITE-B/g' \
      -e 's/dc[7]/site-c/g'   -e 's/DC[7]/SITE-C/g' \
      -e 's/dc[6]/site-d/g'   -e 's/DC[6]/SITE-D/g' \
      -e 's/prg[n]/site-e/g'  -e 's/PRG[N]/SITE-E/g' \
      -e 's/rn10[2]/proj-a/g' -e 's/RN10[2]/PROJ-A/g' \
      -e 's/fn10[0]/mod-a/g'  -e 's/FN10[0]/MOD-A/g' \
      -e 's/fn21[1]/mod-b/g'  -e 's/FN21[1]/MOD-B/g' \
      -e 's/xri[s]cv/proj-b/g' -e 's/XRI[S]CV/PROJ-B/g' \
      -e 's/nvb[u]g/bugtracker/g' -e 's/NVB[u]g/bugtracker/g' \
      -e 's/gsp[-]riscv[-]pilot/workspace/g' \
      -e 's/nr[1]0/tag10/g'   -e 's/NR[1]0/TAG10/g')
    [ "$n" = "$b" ] && continue
    mv "$p" "$(dirname "$p")/$n"
  done
}

# Tar archives: scrub their text members, repack preserving member metadata,
# and refresh a sibling .json manifest's size/sha256 when present.
scrub_archive() {
  archive=$1
  [ -f "$archive" ] || return 0
  tmp=$(mktemp -d)
  case "$archive" in
    *.tar.gz) tar -xzf "$archive" -C "$tmp" ;;
    *)        tar -xf  "$archive" -C "$tmp" ;;
  esac
  scrub_text "$tmp"
  if grep -rlIE "$TEXT_RE" "$tmp" >/dev/null 2>&1; then
    echo "scrub: sensitive patterns remain in $archive members" >&2
    rm -rf "$tmp"
    exit 1
  fi
  python3 - "$archive" "$tmp" <<'PY'
import hashlib, io, json, os, sys, tarfile

archive, root = sys.argv[1], sys.argv[2]
mode = "w:gz" if archive.endswith(".tar.gz") else "w"
out = archive + ".scrubbed"
with tarfile.open(archive) as src:
    members = [m for m in src.getmembers() if m.isfile()]
with tarfile.open(out, mode, format=tarfile.USTAR_FORMAT) as dst:
    for m in members:
        with open(os.path.join(root, m.name), "rb") as fh:
            data = fh.read()
        m.size = len(data)
        dst.addfile(m, io.BytesIO(data))
os.replace(out, archive)
manifest = archive + ".json"
if os.path.exists(manifest):
    with open(manifest) as fh:
        meta = json.load(fh)
    data = open(archive, "rb").read()
    meta["size"] = len(data)
    meta["sha256"] = hashlib.sha256(data).hexdigest()
    with open(manifest, "w") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")
PY
  rm -rf "$tmp"
}

rename_paths "$DIR"
scrub_text "$DIR"
scrub_archive "$DIR/apps/cam-desktop/resources/cam-server/cam-server.bundle"
scrub_archive "$DIR/extensions/vendor/cam-assist/dist/assistant-0.7.0.tar.gz"

left=$(grep -rlIE "$TEXT_RE" "$DIR" || true)
if [ -n "$left" ]; then
  echo "scrub: sensitive patterns remain:" >&2
  printf '%s\n' "$left" >&2
  exit 1
fi

bad=$(find "$DIR" -mindepth 1 \( -type f -o -type d \) \
  | sed "s|^$DIR/||" \
  | grep -iE 'pd[x]|ki[x]|dc[67]|prg[n]|rn10[2]|fn10[0]|fn21[1]|xri[s]cv|nvb[u]g|gsp[-]riscv[-]pilot|nr[1]0' || true)
if [ -n "$bad" ]; then
  echo "scrub: code-name paths remain:" >&2
  printf '%s\n' "$bad" >&2
  exit 1
fi
