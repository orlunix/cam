# NVIDIA Inference Hub with CAMC

CAMC's API mode talks directly to the NVIDIA Inference Hub. It does not start
an HTTP server, proxy, or protocol-conversion process.

## Credentials

The managed provider uses:

```text
base_url: https://inference-api.nvidia.com
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
| Claude | Messages | `https://inference-api.nvidia.com` |
| Codex | Responses | `https://inference-api.nvidia.com/v1` |

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

Codex API runs use the isolated `~/.codex-api` home and link normal Codex
skills from `~/.codex/skills` when present. Claude API runs use the managed
`~/.cam/claude-api` config directory.

`camc api check` also refreshes context-window and output-token metadata from
the provider's public catalog when available. Missing fields use curated
model fallbacks; metadata does not change endpoint selection.
