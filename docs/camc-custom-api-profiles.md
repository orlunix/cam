# camc Custom API Profiles Development Note

Date: 2026-06-15

Status: **partial** — Claude + Codex + 6 curated IHUB models (`camc run -t claude|codex --api NAME`). Cursor `--api` not supported. Opt-in per-tool defaults: `camc api default set`.

Routing architecture (embedded / direct / external): **`docs/api-routing.md`**
(includes three end-to-end JSON + token + run examples).

## Supported today

```bash
camc run -t claude --api glm-5.1 -n claude-proxy-work "..."
camc run -t codex  --api glm-5.1 -n codex-proxy-work "..."
```

| Tool | `--api` | Notes |
|------|---------|-------|
| **claude** | ✅ | 6 curated models; isolated `~/.cam/claude-api/`; proxy route `completions_to_messages` (:18324) |
| **codex** | ✅ | Same curated models; isolated `~/.cam/codex-api/`; proxy route `completions_to_responses` (:18325) |
| **cursor** | ❌ | Not supported — use normal login |

Optional **per-tool default** (opt-in): `camc api default set glm-5.1 --tool claude`. Empty/missing → login. `camc run --no-default-api` skips default.

Unsupported combinations print a clear error and exit (no silent fallback).

## Goal (extended profiles)

The same logical API provider must be usable by different tools even when
those tools need different protocol endpoints. For example, Claude Code may
need an Anthropic Messages shaped endpoint, while Codex may need an
OpenAI Responses or Chat Completions shaped endpoint.

## Non-Goals

- Do not bind an API provider to one tool.
- Do not mutate global auth files such as `~/.claude.json` as the main
  switching mechanism.
- Do not rely on agents manually sourcing shell snippets after launch.
- Do not start arbitrary protocol translators. Proxy routes must be explicit
  and table-driven.
- Do not store secret values in `agents.json` or API profile files.

## Current camc Launch Path

camc already has the right integration point:

- `src/camc_pkg/cli.py::cmd_run` loads the tool adapter config.
- `cmd_run` builds one effective runtime env through
  `camc_pkg.runtime_env.build_runtime_env`.
- The same env is used for preflight and for `create_tmux_session`.
- Adapter TOML files in `src/cam/adapters/configs/*.toml` own tool launch
  command and readiness policy.

Custom API support should extend this path by resolving a per-agent API plan
before preflight, merging the resolved environment into `runtime.env`, then
launching tmux with the same resolved environment.

## Core Abstractions

### API Provider

An API provider is a named, tool-neutral object. It owns shared auth metadata
and one or more protocol endpoints.

Example:

```json
{
  "name": "nvidia-ai",
  "auth": {
    "type": "bearer",
    "source": "env",
    "name": "NVIDIA_TOKEN"
  },
  "endpoints": {
    "anthropic_messages": {
      "url": "http://bpmpfw.nvidia.com/anthropic",
      "models": {
        "default": "claude-sonnet-4-20250514",
        "fast": "claude-3-7-sonnet-20250219"
      }
    },
    "openai_responses": {
      "url": "http://bpmpfw.nvidia.com/openai/responses",
      "models": {
        "default": "gpt-4.1"
      }
    },
    "openai_chat_completions": {
      "url": "http://bpmpfw.nvidia.com/v1",
      "models": {
        "default": "gpt-4.1"
      }
    }
  }
}
```

The provider says what protocol each endpoint speaks. It does not say which
tool should use it.

### Tool API Capability

Each adapter declares which protocols it can consume directly and which
protocols can be reached through a supported local proxy.

Example for Claude:

```toml
[api]
preferred_protocols = ["anthropic_messages"]
direct_protocols = ["anthropic_messages"]

[api.env.anthropic_messages]
base_url = "ANTHROPIC_BASE_URL"
token = "ANTHROPIC_AUTH_TOKEN"
token_alt = "ANTHROPIC_API_KEY"
model = "ANTHROPIC_MODEL"
fast_model = "ANTHROPIC_FAST_MODEL"
```

Example for Codex:

```toml
[api]
preferred_protocols = ["openai_responses", "openai_chat_completions"]
direct_protocols = ["openai_responses", "openai_chat_completions"]

[api.env.openai_responses]
base_url = "OPENAI_BASE_URL"
token = "OPENAI_API_KEY"
model = "OPENAI_MODEL"

[[api.proxy_routes]]
from = "openai_completions"
to = "openai_responses"
proxy = "completions2responses"

[[api.proxy_routes]]
from = "openai_chat_completions"
to = "openai_responses"
proxy = "chat2responses"
```

The exact Codex env/config keys must be verified against the installed Codex
CLI version before implementation.

### Runtime API Plan

The resolver returns a concrete launch plan:

```json
{
  "provider": "nvidia-ai",
  "tool": "claude",
  "mode": "direct",
  "source_protocol": "anthropic_messages",
  "target_protocol": "anthropic_messages",
  "endpoint_url": "http://bpmpfw.nvidia.com/anthropic",
  "env": {
    "ANTHROPIC_BASE_URL": "http://bpmpfw.nvidia.com/anthropic",
    "ANTHROPIC_MODEL": "claude-sonnet-4-20250514",
    "ANTHROPIC_FAST_MODEL": "claude-3-7-sonnet-20250219"
  },
  "secret_env": {
    "ANTHROPIC_AUTH_TOKEN": "NVIDIA_TOKEN"
  }
}
```

For a proxied route:

```json
{
  "provider": "nvidia-ai",
  "tool": "codex",
  "mode": "proxy",
  "source_protocol": "openai_chat_completions",
  "target_protocol": "openai_responses",
  "proxy": "chat2responses",
  "local_url": "http://127.0.0.1:18321/v1",
  "env": {
    "OPENAI_BASE_URL": "http://127.0.0.1:18321/v1"
  },
  "secret_env": {
    "OPENAI_API_KEY": "NVIDIA_TOKEN"
  }
}
```

## Resolution Algorithm

Given `tool` and `api_provider`:

1. Load the API provider.
2. Load the tool adapter.
3. For each adapter preferred protocol:
   - If the provider has the same protocol endpoint, use it directly.
   - Otherwise, search provider endpoints for a supported proxy route into
     that preferred protocol.
4. If a direct endpoint is selected, map provider endpoint fields into the
   adapter's env/config keys.
5. If a proxy route is selected:
   - start or reuse the local proxy for `(provider, source_protocol,
     target_protocol, proxy)`;
   - health-check the proxy;
   - point the tool at the local proxy endpoint.
6. Validate required secret sources exist in the effective runtime env.
7. Merge resolved env into `runtime.env`.
8. Run the normal camc preflight using the merged env.
9. Launch tmux using the same merged env.

If no direct endpoint or supported proxy route exists, fail before creating
the tmux session or agent record.

## Default Proxy Behavior

Default behavior should be automatic only for supported routes:

```text
tool wants protocol P
provider has endpoint P
  -> direct

provider has endpoint Q and adapter declares route Q -> P
  -> start/reuse local proxy

otherwise
  -> hard error before launch
```

Do not start a generic proxy by guessing. The supported route table is the
contract.

Useful flags:

```bash
camc run --api nvidia-ai --no-api-proxy ...
camc run --api nvidia-ai --api-direct-only ...
```

`--api-direct-only` should error if a proxy would be required.

## Storage

Suggested files:

```text
~/.cam/api-providers.json
~/.cam/api-proxy-runs.json
```

`api-providers.json` stores provider definitions and secret references only.
It must not store raw tokens.

`api-proxy-runs.json` records running local proxy processes, ports, health,
and route identity.

Example agent record addition:

```json
{
  "task": {
    "name": "codex-proxy-work",
    "tool": "codex"
  },
  "api": {
    "provider": "nvidia-ai",
    "mode": "proxy",
    "source_protocol": "openai_chat_completions",
    "target_protocol": "openai_responses",
    "proxy": "chat2responses",
    "local_url": "http://127.0.0.1:18321/v1"
  }
}
```

Never persist the resolved token value.

## CLI Surface

P0:

```bash
camc api list
camc api show <provider>
camc api add <provider> --auth-env NVIDIA_TOKEN
camc api endpoint add <provider> <protocol> --url URL --model MODEL
camc api rm <provider>
camc api check <provider>
camc run -t claude --api <provider> ...
camc run -t codex  --api <provider> ...
```

P1:

```bash
camc api routes
camc api proxy status
camc api proxy restart <provider>
camc api endpoint rm <provider> <protocol>
```

## Local Proxy Requirements

A proxy route implementation must define:

- source protocol
- target protocol
- local URL shape exposed to the tool
- upstream URL shape
- auth header mapping
- model mapping
- stream conversion behavior
- health-check endpoint
- process lifecycle and lock file behavior

Initial useful routes:

```text
openai_chat_completions -> openai_responses
openai_completions      -> openai_responses
anthropic_messages      -> openai_responses
openai_chat_completions -> anthropic_messages
openai_responses        -> anthropic_messages
```

Implement only routes that have tests. Unsupported routes must fail early.

## Tool-Specific Notes

### Claude Code

Local proxy docs show Claude Code can be pointed at a custom Anthropic-shaped
endpoint with environment variables such as:

```bash
ANTHROPIC_BASE_URL=http://bpmpfw.nvidia.com/anthropic
ANTHROPIC_AUTH_TOKEN=<token>
ANTHROPIC_MODEL=claude-sonnet-4-20250514
ANTHROPIC_FAST_MODEL=claude-3-7-sonnet-20250219
```

Some local scripts use `ANTHROPIC_API_KEY` and `CLAUDE_MODEL`; implementation
must verify the current Claude Code version and choose one canonical mapping
in the adapter.

### Codex

Codex likely wants an OpenAI-compatible endpoint, but the exact override
surface must be verified for the installed Codex CLI. Candidate knobs:

```bash
OPENAI_BASE_URL
OPENAI_API_KEY
OPENAI_MODEL
```

or config TOML provider fields. This belongs in the Codex adapter, not in the
API provider.

### Cursor

Cursor endpoint override support is less clear. It should remain unsupported
until its CLI/config contract is verified. The resolver should fail with a
clear error if no adapter capability is declared.

## Error Behavior

Fail before launch when:

- API provider does not exist.
- Provider has no compatible endpoint and no supported proxy route.
- Required secret source env var is missing.
- Required local proxy cannot start or fails health check.
- Adapter does not declare API capability for the selected tool.

Error messages should show the provider name, tool, wanted protocol, available
provider protocols, and any missing env var name. Do not print secret values.

## Tests

Focused unit tests:

- provider JSON load/store rejects duplicate names and invalid protocols
- resolver direct match: Claude + `anthropic_messages`
- resolver direct match: Codex + `openai_responses`
- resolver proxy match: Codex + `openai_completions -> openai_responses`
- resolver fails when route unsupported
- resolver fails when secret env missing
- `cmd_run --api` merges env before preflight
- agent record stores route metadata without secrets

Smoke tests:

- fake Claude adapter + fake provider validates env injection
- fake Codex adapter + fake proxy validates local proxy URL injection
- `camc env --tool claude --api provider` reports resolved API plan without
  leaking secret values

## Reference Implementations

Working examples for Inference Hub GLM (outside camc `--api`, which is not
implemented yet):

| Implementation | Routes | Notes |
|----------------|--------|-------|
| `dev/ihub_proxy/` | `completions_to_messages`, `completions_to_responses` | CAM default; stdlib |
| [CC Switch](https://github.com/farion1231/cc-switch) | Built-in local routing | GUI profiles + hot-switch; [routing docs](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.2-routing.md) |
| `dev/ihub_proxy/litellm-glm-proxy.yaml` | Anthropic messages → chat/completions | LiteLLM; see [proxy quick start](https://docs.litellm.ai/docs/proxy/quick_start) |

CC Switch's Codex **Responses ↔ Chat Completions** loop matches the design of
`completions_to_responses` (see
[guide](https://github.com/farion1231/cc-switch/blob/main/docs/guides/codex-deepseek-routing-guide-en.md)).
Full usage: `docs/inference-hub.md`.

**Implementation plan (Py3.6 embed + auto-proxy):** `docs/camc-api-proxy-plan.md`

## Implementation Plan

1. Add `src/camc_pkg/api_profiles.py` for provider store and validation.
2. Extend adapter TOML parser with `[api]` capability metadata.
3. Add `src/camc_pkg/api_resolver.py` returning a runtime API plan.
4. Add P0 `camc api` commands.
5. Add `camc run --api`, `--no-api-proxy`, and `--api-direct-only`.
6. Merge API plan env into `runtime.env` before `_preflight`.
7. Persist sanitized API route metadata in the agent record.
8. Add tests.
9. Only then add real proxy route implementations, one route at a time.

The first implementation can support direct Claude `anthropic_messages`
without any proxy. Proxy routes should be added incrementally after the direct
path is verified.
