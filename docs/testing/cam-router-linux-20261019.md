# CAM Router Linux verification — 2026-10-19

Implementation and usage: [components/cam-router](../../components/cam-router/README.md).

## Environment and artifacts

- Linux x86_64, kernel 4.18, glibc 2.28 (Rocky/RHEL 8 family).
- Development build/test: system Rust/Cargo 1.71.1.
- Release: official Rust 1.71.1, `x86_64-unknown-linux-musl`, baseline
  `target-cpu=x86-64`, bundled SQLite, musl C headers/compiler for SQLite and
  the Rust target's self-contained runtime. Toolchains/cache only in `/tmp`.
- Final `cam-server` and `cam-forwarder`: **static-pie linked**, no ELF
  interpreter and no `DT_NEEDED` dynamic dependencies. The checked-in build
  script verifies this and smoke-tests native `version` before accepting output.
- Artifacts: `/tmp/cam-router-target/linux/x86_64-unknown-linux-musl/`, with
  SHA256SUMS. No install, release upload, commit or push performed.
- A minimal musl executable exposed an incompatible custom C-linker startup
  combination during validation. Final artifacts use the official Rust target
  runtime with the native GNU linker and passed actual process tests, rather
  than relying on static-link inspection alone.

## Results

| Verification | Result |
| --- | --- |
| Existing full Python regression (`PYTHONPATH=src:/tmp/cam-merge-test-deps python3 -m pytest -q`) | 1708 passed, 1 skipped; 12 existing datetime deprecation warnings |
| Native lifecycle deadline regression | 1 passed; a real reboot dedup check now has a 30-second message deadline instead of the mock fixture's 2 seconds |
| Rust all-target tests | 3 passed: protocol/digest, bounded unchanged framing, partial-write status |
| Debug Server/Client end-to-end | 14 passed |
| Final production musl artifacts end-to-end | 14 passed in 19.334 seconds |
| Python SDK/client protocol syntax | Python 3.6 grammar passed |
| Rust formatting, shell syntax, whitespace diff | Passed |
| `cam-server version` process latency | 20 samples: median 0.74 ms, p95 1.03 ms on this host; not a routing latency measurement |

End-to-end tests use separately spawned Servers and two independent Client
processes, with a third Node for concurrent destinations. The unshipped
`router_test_node` fixture simulates separate hosts while production Server
tests enforce a machine/user singleton even for different Node IDs.

Coverage includes raw Unicode/ANSI frame preservation, concurrent messages,
all ten native agent operations through real tmux, actual bidirectional
agent mailbox turns, waiting for a reply while capturing another result,
reply through a surviving Client, offline queue recovery, source/target
restart, target deduplication, same-ID/content conflicts, same message ID from
different source Nodes, no replay after interrupted execution, backend process
cleanup, authentication/source scope, route TTL/generation, queue-full rejection
and message expiry. Explicit IDs, Client cached names, ambiguity and dead agent
targets are also exercised.

Tests create private Homes, sockets, credentials and disposable `/bin/cat`
tools. They do not reuse existing user agents, allocations, SSH configuration
or installed binaries. Test-owned tools, tmux sessions and Servers are removed.
Existing dirty workspace files were compared against the pre-task SHA-256
manifest; only this task's intended files changed, and the Git index is unchanged.

## Remaining platform acceptance

- No aarch64 runtime/binary validation performed; build target is documented.
- No physical cross-host SSH or multiple-distribution runtime matrix performed.
  A stdio transport uses the real `cam-server connect` shim in process tests.
- Existing Desktop/Mobile backends have not embedded the forwarding component;
  no packaged Electron transport acceptance is claimed.
- The SDK supports existing native runtime operations but does not migrate agent
  registry/monitor ownership. A tool launched onto another LSF execution host
  still needs a route back to its owner Node for callbacks; a shared Unix socket
  pathname cannot provide this.
