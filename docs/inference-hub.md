# ACME Inference Hub with CAMC

CAMC's API mode starts one loopback Camc Proxy per agent and forwards that
agent's native protocol to ACME Inference Hub. The proxy adds bounded retry,
request isolation, compatibility mappings, and usage observability without a
shared service or cross-protocol fallback.

## Credentials

The managed provider uses:

```text
base_url: https://inference-api.example.com
auth_key: inference_hub
token_file: ~/.my_tokens.yaml
```

An environment value named by `env_names` wins. `token_file` is read-only;
`auth_key` is its default key and `token_key` is optional for files using a
different key. Tokens are never written to `api-models.json` or command-line
arguments.

## Native endpoints

The selected adapter controls the endpoint shape:

| Tool | Native endpoint | Base URL passed to tool |
|------|-----------------|-------------------------|
| Claude | Messages | Per-agent loopback proxy |
| Codex | Responses | Per-agent loopback proxy with `/v1` suffix |

Each API profile records `available_endpoints`. Claude profiles need
`messages`; Codex profiles need `responses`. CAMC rejects a profile before
starting an agent when the native endpoint is not advertised.

## Commands

```bash
camc api check
camc api list --all
camc api default set deepseek-v4-flash --tool claude
camc api default set deepseek-v4-flash --tool codex
camc run -t claude --api deepseek-v4-flash "prompt"
camc run -t codex --api deepseek-v4-flash "prompt"
```

Codex login and API runs use the same thin `~/.codex/<agent-id>` home. Shared
auth, skills, MCP, sessions, history, hooks, and permission profiles link to
the untouched `~/.codex` donor; `config.toml` is snapshotted per agent and
`tmp/arg0` plus configured SQLite storage are host-local. Only API runs add
provider/catalog settings to their snapshot. Claude API runs use the managed
`~/.cam/claude-api` config directory.

`camc api check` also refreshes context-window and output-token metadata from
the provider's public catalog when available. Missing fields use curated
model fallbacks; metadata does not change endpoint selection.
