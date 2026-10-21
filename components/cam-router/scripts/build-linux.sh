#!/bin/sh
# Run on the matching CPU, with the official musl Rust target installed.
set -eu
task_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_root"
task_arch=$(uname -m)
case "${1:-$task_arch}" in
    x86_64|x86_64-unknown-linux-musl) task_target=x86_64-unknown-linux-musl; task_cpu=x86-64 ;;
    aarch64|aarch64-unknown-linux-musl) task_target=aarch64-unknown-linux-musl; task_cpu=generic ;;
    *) printf '%s\n' 'Supported targets: x86_64-unknown-linux-musl, aarch64-unknown-linux-musl' >&2; exit 2 ;;
esac
for task_program in cargo readelf sha256sum python3; do
    command -v "$task_program" >/dev/null || { printf 'Required: %s\n' "$task_program" >&2; exit 2; }
done
# Set CC_<target> to a musl C compiler for bundled SQLite. Rust's target
# supplies its own libc; do not inject a host glibc sysroot/library path.
RUSTFLAGS="${RUSTFLAGS:-} -C target-cpu=$task_cpu"
export RUSTFLAGS
cargo build --release --locked --target "$task_target" --bins
task_target_dir=${CARGO_TARGET_DIR:-target}
task_artifacts_dir=${CAM_ROUTER_ARTIFACT_DIR:-$task_target_dir/linux/$task_target}
mkdir -p "$task_artifacts_dir"
for task_bin in cam-server cam-forwarder cam-clientd cam-tui; do
    task_source=$task_target_dir/$task_target/release/$task_bin
    if readelf -l "$task_source" | grep -q INTERP || readelf -d "$task_source" | grep -q '(NEEDED)'; then
        printf 'Refusing dynamically linked artifact: %s\n' "$task_source" >&2; exit 2
    fi
    cp "$task_source" "$task_artifacts_dir/$task_bin"
done
python3 scripts/package-server.py "$task_artifacts_dir/cam-server" "$task_artifacts_dir/cam-server.bundle" "$task_target"
if [ "$task_target" = "$task_arch-unknown-linux-musl" ]; then
    "$task_artifacts_dir/cam-server" version
fi
(cd "$task_artifacts_dir" && sha256sum cam-server cam-forwarder cam-clientd cam-tui cam-server.bundle cam-server.bundle.json > SHA256SUMS)
printf 'Static Linux artifacts: %s\n' "$task_artifacts_dir"
