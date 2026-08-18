# CAMC proxy API routing

`camc run -t claude --api NAME` and `camc run -t codex --api NAME` start a
loopback proxy for that agent. It forwards the native endpoint and stream to
the provider and rewrites only the client model alias when required.

## Profile shape

`~/.cam/api-models.json` contains providers, API profiles, and ordered
per-tool defaults:

```json
{
  "version": "1.0.2",
  "tool_supported_endpoints": {
    "claude": ["messages"],
    "codex": ["responses"]
  },
  "tool_base_url_suf": {"claude": "", "codex": "/v1"},
  "tool_supported_apis": {
    "claude": ["claude-sonnet-5-glm52"],
    "codex": ["deepseek-v4-flash"]
  },
  "providers": {
    "inference-hub": {
      "display_name": "NVIDIA Inference Hub",
      "base_url": "https://inference-api.nvidia.com",
      "auth_key": "inference_hub",
      "env_names": ["INFERENCE_HUB_TOKEN"],
      "token_file": "~/.my_tokens.yaml",
      "catalog_path": "/v1/models"
    }
  },
  "apis": {
    "deepseek-v4-flash": {
      "provider": "inference-hub",
      "model": "nvidia/deepseek-ai/eccn-deepseek-v4-flash",
      "client_models": {
        "claude": "claude-sonnet-5-glm52",
        "codex": "nvidia/deepseek-ai/eccn-deepseek-v4-flash"
      },
      "available_endpoints": ["completions", "messages", "responses"],
      "enabled": true
    }
  },
  "defaults": {
    "claude": ["deepseek-v4-flash"],
    "codex": ["deepseek-v4-flash", "deepseek-v4-pro"]
  }
}
```

`auth_key` is the default key in `token_file`; add `token_key` only when the
file uses a different key. A provider has one upstream `base_url`.

Tool endpoint and URL-suffix rules are JSON-owned. The selected profile must
be allowed by `tool_supported_apis[tool]` and share an endpoint with
`tool_supported_endpoints[tool]`, otherwise launch fails before proxy start.
`camc api list` shows the resulting `tools=` support set.

## Commands

```text
camc api list [--all]
camc api check
camc api default set NAME --tool claude|codex
camc api default clear --tool claude|codex
camc api default show [--json]
camc run -t claude --api NAME "prompt"
camc run -t codex --api NAME "prompt"
```

`--api` is limited to Claude and Codex. API runs use the provider token from
the environment, an optional provider token file, or the normal CAMC token
fallbacks. Codex API runs use the isolated `~/.codex-api` home and link the
normal Codex skills directory.
