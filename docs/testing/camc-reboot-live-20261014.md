# Real-tool reboot and latency verification — 2026-10-14

The current source was bundled into temporary candidates without changing the
installed camc, version, or dist artifacts. These tests used actual installed
tools and verified model replies, not substitute executables.

## Successful lifecycle tests

Both tools used `--tool-launcher auto --no-default-api`, selecting battach to the
existing user-owned autorun allocation `1796124140` on `site-a3-sim-g46-042`.
The remote tools resolved to Claude Code **2.1.296** and Codex **0.159.2**.
These differ from this login host's latest paths; the versions below are those
shown by the running remote tools.

| Check | Claude | Codex |
| --- | --- | --- |
| Test agent ID, retained across reboot | `c8147288` | `5c4e855b` |
| Old → new tmux server PID | 283441 → 285701 | 276400 → 281116 |
| `run` CLI return | 440 ms | 440 ms |
| `reboot` CLI return | 358 ms | 327 ms |
| Initial tool interface observed | 22.9 s | 65.6 s* |
| Tool interface observed after reboot | 18.7 s | 67.5 s* |
| Reply observed after pre-reboot send | 2.3 s | 10.4 s |
| Reply observed after post-reboot send | 2.2 s | 4.3 s |

Reply checks require a response-only line matching a unique marker; merely
seeing the marker echoed in the submitted prompt does not pass. Captures were
polled every two seconds, so interface/reply times are observations, not precise
model-inference durations. The CLI return measurements use a monotonic clock.

Claude's session UUID changed from its original deterministic ID to
`8f436808-2012-47d6-8165-8a28b6bec5a5`. Both launches changed their launch generation
IDs and recreated tmux; the post-reboot screen contained a fresh conversation.
Codex's remote session UUID was not bound locally, so no UUID comparison is
claimed for Codex.

*Codex required manual Escape at “Hooks need review” before both conversations.
The interface explicitly offers Escape to skip; hooks were not trusted or run.
Those two interface times include time awaiting that manual interaction, plus
LSF query/attachment retries. This is a successful real interactive lifecycle
test, **not** a fully unattended Codex startup pass.

Commands used (with the candidate path below):

```sh
camc run -t claude -n <unique-test-name> -p <temporary-workdir> --tool-launcher auto --no-default-api
camc run -t codex -n <unique-test-name> -p <temporary-workdir> --tool-launcher auto --no-default-api
camc send <test-id> --text 'Reply exactly <unique-marker>. Do not use tools or inspect files.'
camc reboot <test-id>
camc send <test-id> --text 'Reply exactly <different-marker>. Do not use tools or inspect files.'
camc rm <test-id>
```

An earlier Claude reboot hit LSF's concurrent-query limit and its ten-second
discovery timeout, correctly proceeding to qsub. New job `1913531726` was
submitted but stayed PEND for the four-minute observation window. It was
cancelled, and a separate retry completed the Claude lifecycle above via battach.
This confirms fallback submission, not a successful qsub tool-start test.

## Non-fast-path timing

One warm-up plus twelve measured executions per command/version on this host,
using captured output pipes and `perf_counter` (avoiding timed-wait polling
quantization). `CAMC_PRELUDE_DISABLE=all` was the same for both binaries.

| Command | Installed camc median | Current candidate median |
| --- | ---: | ---: |
| `version` | 2.01 ms | 1.68 ms |
| `run --help` | 248.23 ms | 87.05 ms |

The candidate reduces measured `run --help` latency by approximately **65%**.
The candidate includes the prior bytecode-cache/lazy-import work as well as the
reboot change; this is not a claim that reboot alone caused the improvement.
Real `run`/`reboot` returned in approximately 0.3–0.4 seconds; launcher discovery,
LSF scheduling, tool startup, and model replies happen afterward in tmux.

## Cleanup and evidence

Removed all three test records (`2812b098`, `5c4e855b`, `c8147288`), sockets,
workers/monitors, isolated Codex overlay, and temporary shared workdirs. A final
host-side scan restricted to those exact workdirs found no remaining processes.
The newly submitted qsub job `1913531726` is EXIT. Reused allocation `1796124140`
was preserved. No production agent was rebooted.

Retained evidence:

- `/tmp/camc-reboot-live-20261014-205439/result.json`: first Claude attempt and Codex pass.
- `/tmp/camc-reboot-live-20261014-210054-retry/result.json`: successful Claude retry.
- `/tmp/camc-reboot-live-20261014-205439/latency.json`: raw twelve-sample measurements.
- `/tmp/camc-reboot-live-20261014-205439/cleanup-audit.json`: final resource audit.
- Adjacent `*-reply.txt` and startup captures contain tool interfaces and responses.
- Temporary candidates remain alongside evidence; installed binaries were not replaced.
