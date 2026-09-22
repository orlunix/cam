# Camc Proxy Test Report

Date: 2026-09-05

This report records the TDD and live acceptance evidence for the design in
`docs/camc-proxy-design.md`. It distinguishes Camc Proxy results from unrelated
repository or host-environment failures.

## Automated results

| Scope | Result | Command / note |
|---|---:|---|
| Post-pull Proxy/API regression | **176 passed** | API metadata, routing, proxy runtime/schema, direct-only behavior, CamC API lifecycle/reboot, monitor, and long-static fallback suites. |
| Merge collection regression | **fixed** | Restored the `tmux_submit_input` compatibility entry point expected by the pulled CLI and tests after upstream introduced `insert_prompt`. |
| Environment isolation | **fixed** | Disabled-profile tests no longer consume the developer's real Inference Hub token and catalog. |
| Single-file build | **passed** | `dist/camc` v1.2.136, 20,421 lines; Python 3.6/current-Python syntax and command smoke passed. |
| Built native-stream smoke | **passed** | The built proxy forwarded local Messages and Responses SSE bytes exactly, pinned the bound model, and retained an unknown JSON field. |
| Full repository suite | **1069 passed, 2 skipped, 93 failed, 4 errors** | Failures are outside the focused Proxy/API set and are grouped below. |

The 408/409/429/5xx retry matrix uses a real local HTTP upstream and proxy
handler, including bounded `Retry-After`, response closure, and successful
continuation. It was not re-injected into the external Inference Hub service.

The post-merge full-suite failures are concentrated in existing environment or
unrelated upstream areas:

- The active Python environment lacks `pytest-asyncio`; async agent/client/SSH
  transport and relay tests therefore fail before exercising their bodies.
- `tests/test_probe_detection.py` still fails when isolated because its private
  tmux panes return empty output on this host.
- Cursor boot-trust assertions disagree with the pulled boot configuration.
- One runtime resolver assertion expects a temporary PATH binary, while the
  current resolver intentionally selects the installed golden Claude binary.

`build_camc.py --verify` compares the complete `version` output and therefore
reports a mismatch solely because source mode prints build `dev` while the
single-file artifact prints its real dirty-tree build stamp. Its `--help`,
`run --help`, and `list --help` comparisons all passed; the artifact was then
validated independently as recorded above.

## Historical live Agent results (pre-pull implementation)

These results remain useful provider evidence, but they predate the current
upstream merge and are not presented as post-merge acceptance. A fresh live
run is required before release.

### Ten-turn run

| Agent | Native path | Model | Turns | HTTP results | Proxy requests | Input tokens | Output tokens | Cache-read tokens |
|---|---|---|---:|---|---:|---:|---:|---:|
| Claude Code 2.1.261 | Messages | Kimi K3 | 10/10 | all 200 | 12 | 232,046 | 677 | 139,328 |
| Codex 0.153.4 | Responses | Qwen 3.8 27B | 10/10 | all 200 | 11 | 120,315 | 466 | 67,200 |

The number of HTTP requests is not required to equal the number of user
turns; the agents may issue initialization or replacement requests. Logs are
request-scoped. Claude's run recorded one client-disconnected stream followed
by successful requests. Codex reported cached tokens on seven requests. Using
the provider's native counters, the aggregate observed cache ratios were about
37.5% for Claude/Kimi (`cache_read / (input + cache_read)`) and 55.9% for
Codex/Qwen (`cached / input`). No cache or usage fields were synthesized by
the proxy.

### Concurrency and isolation

- Claude/Kimi and Codex/Qwen completed their ten-turn runs concurrently with
  distinct PIDs, ephemeral ports, logs, generated homes, and native endpoints.
- Three simultaneous agents (one Claude/Kimi and two Codex/Qwen) each returned
  its unique marker. Each log contained only its configured upstream model and
  native endpoint.
- A different-model three-agent run exposed and then fixed a shared
  `CODEX_HOME` bug. Codex configuration is now generated under
  `~/.codex-api/<owner-id>/`, so a later GLM agent cannot overwrite a Qwen
  agent's model or proxy URL.
- GLM 5.3 remained correctly isolated but its native Responses endpoint
  rejected the current Codex tool schema with HTTP 400 (`format` missing).
  This is preserved as provider capability evidence; Camc Proxy did not drop
  the field, translate through Chat, or conceal the upstream error.
- Removing one agent left the other agents' proxies healthy. Removing the last
  agents removed their PIDs, listening ports, run records, and generated
  per-agent Codex homes. Request logs were intentionally retained.

## Defects found by live testing

1. Proxy startup occurred before tool readiness. It now occurs only after
   preflight succeeds.
2. Claude may replace the display model with an organization-approved alias.
   The proxy now routes through the API profile bound at startup and maps only
   the model field to that profile's provider model.
3. Multiple Codex agents shared one mutable `CODEX_HOME`. It is now isolated
   by owner id and reclaimed after the tool exits.
4. A client disconnect while response headers were being written produced a
   server traceback. It is now handled as a request-local disconnect.

## Current acceptance status

The merged source passes the 176-test focused Proxy/API regression. The live
native-endpoint, ten-turn, concurrency, cache-observability, interrupted-stream,
and lifecycle figures above are historical evidence and must be repeated after
the single-file build before this merge is released.
