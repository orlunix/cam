# Terminal-first run verification

All `camc run` modes now create tmux first. Its default shell starts a
foreground preparation worker, which reuses the existing environment,
API, tool-home and launcher policies. The worker starts the resolved tool
in the same pane; it does not create a second tmux session.

The CLI returns the agent ID after session creation. This means accepted,
not tool-ready. Preparation errors are recorded as failed and remain
visible in the pane. `stop`/`exit` cancel preparation; `kill`/`rm` remove
pending request files. Requests use mode 0600 and are deleted when read.
No monitor starts during preparation; monitor heal skips preparing agents.

## Verification

- 321 focused run, launcher, environment, API, reboot, home, tmux/heal and
  baseline preparation tests passed. This includes four real-tmux tests
  using temporary Claude/Codex-compatible tools, for local and custom launch.
- 285 CLI, monitor, API and bridge tests passed.
- Three candidate-artifact lifecycle tests passed, including preparation,
  monitor confirmation, message submission and record removal.
- An existing `test_heal_restart_runs_forced_restart_then_monitor_heal`
  failure was excluded from that focused pass. Its `_do_heal` mock does not
  accept `restart_cron=True`; the same production call exists at HEAD.
- Python 3.6 grammar and diff whitespace checks passed.

The candidate `/tmp/camc-pane-startup-candidate` was also used to start
installed Codex and Claude with `--tool-launcher auto --no-default-api`.
Both showed their actual tool interfaces through qsub fallback:

| Tool | CLI return | Tool banner visible | New qsub job |
| --- | ---: | ---: | --- |
| Codex 0.159.2 | 452 ms | 92.1 s | 1909440450 |
| Claude 2.1.296 | 478 ms | 86.1 s | 1909447046 |

These are startup tests, not model-response tests. LSF discovery/attachment
and scheduling time remain in the pane. Both test records and sockets were
removed; only their newly submitted jobs were reclaimed. Existing user
allocations were preserved. Evidence is in
`/home/hren/.cam/pane-startup-smoke-1791971690/result.json` and adjacent
capture files. The installed camc binaries were not replaced.

## Non-fast-path measurement

On this host, eight-run medians for `run --help` were 352.1 ms for the
shell-wrapped artifact, 132.3 ms for the source package, and 32.7 ms for an
empty Python process. A separate ten-run comparison of the same candidate
through Python measured 270.5 ms from source and 118.4 ms from precompiled
bytecode. This supports investigating compilation caching first; it is
not a shipped cache implementation. Any future cache must invalidate by
artifact contents and Python version, use a private owner-controlled path,
and fall back safely if unavailable or corrupt.
