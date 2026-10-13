# Camc Proxy Design and TDD Plan

## Status

Camc Proxy is the single proxy implementation embedded in the standalone
`camc` build. Each API run owns one proxy process for the lifetime of its
agent. Proxy processes, loopback ports, logs, credentials, and generated
API config overlays are not shared across agents. The owning agent record stores the
proxy PID and port; there is no second proxy registry or readiness-file format.

This document is the executable contract for the implementation. A change is
complete only when its corresponding automated tests pass and the applicable
live acceptance scenarios have been run. The latest evidence and known
repository-level blockers are recorded in `docs/camc-proxy-test-report.md`.

## Design principles

1. **Native endpoint first.** Messages forwards to Messages, Responses to
   Responses, and Chat Completions to Chat Completions. A missing native
   endpoint is an explicit error; there is no hidden Chat fallback.
2. **Preserve by default.** Unknown body fields, tool descriptors, cache
   controls, end-to-end headers, stream events, usage, and error bodies are
   retained. The proxy replaces the model with the profile bound at startup,
   replaces authentication, and applies only documented compatibility maps.
3. **Mappings are explicit.** Agent/provider compatibility mappings must be
   configured, documented, tested, and semantically reversible. Unsupported
   fields are never silently dropped.
4. **Retry only before output.** Transient connection errors and retryable
   HTTP status codes may be retried before the first response byte. A stream
   is never replayed after output begins.
5. **Shared config, local runtime.** Every agent has a unique owner id. API
   runs add one loopback proxy/process and optional config overlay. Adapters
   redirect only lock/janitor-sensitive paths to a host-local filesystem.
6. **No secrets in observability.** Logs contain lifecycle and request
   metadata only, never credentials, full prompts, or response content.
7. **Single-file release.** The source is stdlib-only and is embedded in
   `dist/camc` by `build_camc.py`.

## Shared-vs-local runtime storage

Runtime-storage policy belongs to the adapter, not to the flow implementation.
An adapter may declare a `[runtime]` block with its donor home, host-local
links, config keys whose directory values need bootstrap, and the adapter's
Boolean `isolate_tool_home` default. A single run can override that default
with `--isolate-tool-home 1` or `--isolate-tool-home 0`; reboot preserves the
resolved value. `camc run` and the DAG scheduler persist the resolved,
non-secret layout in `agent.runtime.storage`.

Codex is the first adapter using this contract:

- login and API runs set `CODEX_HOME=~/.codex/<agent-id>`;
- each thin home directly links safe donor entries such as auth, MCP, skills,
  sessions, and history, while keeping a private `config.toml` snapshot;
- `<agent-home>/tmp/arg0` links to adapter-resolved host-local
  storage, whose target is created and chmod 0700 before Codex starts;
- the configured `sqlite_home` directory is likewise bootstrapped locally;
- inherited stale arg0 helper entries are removed from `PATH`;
- qsub/SSH launcher commands repeat target creation/chmod on the target host
  before `exec`, with POSIX argv quoting and no `eval`;
- API runs alone add provider/catalog settings to their private snapshot.

Managed-home ownership markers prevent recursive links and make cleanup
path-safe. Existing unmarked directories are rejected and never modified.

The donor is never removed or rewritten. Missing optional shared assets are
reported in the runtime manifest and do not prevent a login that can use the
tool's other authentication paths.

## Request flow

```text
camc run [--api MODEL]
  -> resolve an optional profile and token
  -> preflight coding tool
  -> allocate agent id and bootstrap adapter-declared runtime storage
  -> (API) start Camc Proxy and pass its loopback URL to the tool
  -> run the selected adapter
  -> (API) native endpoint forwarding with pre-output retry
  -> terminal path stops owned resources and removes its run record
```

## Permitted transformations

| Input | Output | Reason |
|---|---|---|
| CamC model alias | Provider model id | Routing |
| Agent provider credential | Forwarded as end-to-end authorization | Isolated runtime |
| Hop-by-hop headers | Removed | HTTP transport correctness |
| Codex reasoning effort | Profile-configured provider effort | Compatibility |
| Responses tool continuation | Stateless native Responses input | IH compatibility |
| Missing Responses text format | Explicit plain-text format | IH compatibility |

Everything not listed above is forwarded unchanged at the JSON semantic and
HTTP end-to-end-header level. JSON whitespace and object key order are not
part of the contract.

## Lifecycle ownership

The CLI launches `_api_proxy` with an owner id and an ephemeral (or explicitly
reused during reboot) loopback port. The child prints the bound port only after
the HTTP server is listening. The owning agent record persists its PID, port,
profile, provider, and model. Before cleanup, `/proc/<pid>/cmdline` is matched
against both `_api_proxy` and the exact owner id so a reused PID is not
signalled. Shared host-local targets are retained for other agents. Terminal
cleanup first stops the tool/tmux and only then removes the owned overlay;
reboot refreshes the same overlay after the old process exits.

## Automated test matrix

### Lifecycle and process isolation

1. Current PID is alive; a missing/invalid PID is not.
2. Each owner receives a distinct process and port.
3. Each owner receives a distinct optional debug log and any declared runtime
   home.
4. Concurrent starts do not collide or overwrite agent records.
5. `stop`, `exit`, `kill`, and `rm` reclaim only the owning proxy.
6. Auto-exit and unexpected session termination reclaim the proxy.
7. Tool preflight and tmux launch failures leave no proxy.
8. Failed proxy startup removes partial process/config state.
9. A stale or reused PID is not signalled.

### Native routing and configuration

1. Messages, Responses, and Chat use only their matching upstream endpoints.
2. A missing native endpoint returns an explicit no-conversion error.
3. Provider endpoint overrides are honored.
4. Model aliases and provider model ids resolve correctly.
5. Unknown models, providers, and malformed configuration fail clearly.

### Transparency

1. Unknown JSON fields and every tool type survive forwarding.
2. Cache, reasoning, metadata, and tool-choice fields survive forwarding.
3. End-to-end request and response headers survive forwarding.
4. Hop-by-hop headers are removed and authentication is replaced.
5. Upstream status, body, usage, cache usage, and error shape are preserved.

### Streaming and concurrency

1. All three protocols preserve SSE byte order and terminal usage events.
2. Normal completion and client disconnect close only that upstream response.
3. No stream is retried after its first byte.
4. Concurrent agents, models, and broken streams never cross-contaminate.
5. A slow client does not block another proxy or request thread.

### Retry and errors

1. 408, 409, 429, 500, 502, 503, and 504 retry before output.
2. Other 4xx responses do not retry.
3. Connect, request, and response-header failures retry within the configured
   maximum. Once upstream response headers are accepted, body-stream failures
   are not replayed because doing so can duplicate a billed request.
4. Retry-After is bounded and honored.
5. Every abandoned response is closed.

### Credentials, logging, and build

1. Environment, token file, YAML, and one-off CLI tokens reach only the
   owning proxy/provider.
2. Logs and agent records never contain secrets or complete prompts.
3. Usage/cache metadata can be observed without modifying the response.
4. The source and `dist/camc` behavior match, require only the stdlib, expose
   health/help, and pass the supported-Python syntax checks.

The contract is implemented across the API store, resolver, routing, proxy,
lifecycle, reboot, and monitor suites. Parametrized cases may cover several
matrix rows; the exact pytest count is recorded in the test report instead of
being frozen in this design.

## Live acceptance scenarios

1. Claude through native Messages for ten conversational turns.
2. Codex through native Responses for ten turns on a model supporting the
   tool descriptors emitted by that Codex version.
3. Three mixed Claude/Codex agents concurrently.
4. Concurrent different-model runs with isolated model, token, and logs.
5. Injected 429 followed by successful retry without agent exit.
6. One interrupted stream while other agents continue.
7. After all agents finish, no owned PID, listening port, or generated Codex
   home remains.

Live-provider capability failures are reported separately from proxy defects;
they are never hidden by dropping request fields.

## Definition of done

- All automated contract points pass from source.
- The single-file build passes the same smoke and lifecycle tests.
- The full CamC suite passes in the project test environment.
- All seven live scenarios pass, or an external provider/tool blocker is
  recorded with request-safe evidence and no proxy correctness claim.
