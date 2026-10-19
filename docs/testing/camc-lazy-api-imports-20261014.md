# Lazy API network imports

The CLI and monitor retain their existing proxy entry functions, but load
the API proxy implementation on use. API catalog/metadata HTTP imports also
move into their fetch/check functions. Ordinary help now avoids
`http.server`, `http.client`, `ssl`, and `urllib.request` in both source and
standalone entry modes.

The standalone builder initializes the proxy module through a locked,
one-time loader, preserving its global bindings and internal entry aliases.
This handles HTTP server classes whose bases require the network imports at
definition time. Existing bytecode caching and shell fast paths remain in
place. Command arguments, launcher policy and API routing are unchanged.

## Validation

- 168 API, monitor and lazy-import tests passed, including a real loopback
  proxy startup and successful `/health` response followed by shutdown.
- 231 cache, fast-version, launcher, environment, real-tmux worker and
  candidate-artifact lifecycle tests passed; temporary agents were removed.
- Python 3.6 grammar and whitespace checks passed.
- `test_from_official_template` was excluded from the API pass after its
  host-dependent Codex catalog fixture lacked `base_instructions`. The
  catalog-building logic was not modified by this change.

Fifteen paired warm-cache `run --help` measurements:

| Candidate | Median | p95 |
| --- | ---: | ---: |
| Bytecode cache, before lazy imports | 125.8 ms | 131.4 ms |
| Bytecode cache plus lazy API imports | 111.5 ms | 124.6 ms |

These timings vary with host load and exclude tool initialization and LSF
waiting. Evidence: `/tmp/camc-lazy-import-latency.json`. Candidate:
`/tmp/camc-lazy-import-candidate`. Installed binaries were not replaced;
no commit or push was made.
