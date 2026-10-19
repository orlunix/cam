# Standalone bytecode cache

The standalone shell fallback now executes a small embedded Python loader.
It caches compiled code in `/tmp/camc-bytecode-<uid>/`, with directory mode
0700 and file mode 0600. Each key includes the artifact's complete contents,
source path, Python version/magic and optimization level. Same-size changes
with unchanged timestamps still invalidate the cache.

Writes are atomic. Ownership, permissions, symlinks, bytecode header and
payload checksum are checked before loading. Unavailable, unsafe or corrupt
caches fall back to compiling the current source. Cached code executes with
fresh runtime globals and environment; prompts, credentials and registry
data are not cached.

Existing shell fast paths retain priority. Direct `python3 camc ...` and
`python3 -m camc_pkg ...` retain their previous behavior; this cache applies
to the standalone executable's normal shell fallback. For diagnostics,
`CAMC_BYTECODE_CACHE_DISABLE=1` disables application cache access.

## Validation

53 cache, fast-version, worker and candidate-artifact tests passed. Coverage
includes corrupt/non-code/stale caches, unsafe ownership and permissions,
symlinks/FIFOs, concurrent cold starts, read-only/write failures, argument
and stdin preservation, exit codes, changed environment, and real tmux
launch/removal using temporary Claude/Codex-compatible tools. Python 3.6
grammar and whitespace checks passed.

The final fifteen paired `run --help` measurements on this host used the
same Python interpreter:

| Entry | Median | p95 |
| --- | ---: | ---: |
| Original direct Python path | 314.7 ms | 368.1 ms |
| Standalone executable, warm cache | 140.8 ms | 206.3 ms |

Timing varies with host load. An earlier cold-cache sample was 311.1 ms;
the first launch still pays compilation and cache-write costs. This does
not reduce LSF discovery or queue waiting time.

Candidate: `/tmp/camc-bytecode-candidate`. Final timings:
`/tmp/camc-bytecode-latency-final.json`. Existing installed binaries and
`dist/camc` were not replaced for this change; no commit or push was made.
