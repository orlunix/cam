# API model metadata

CAMC keeps model capability metadata in `~/.cam/api-models.json` so Claude
and Codex can use the provider's native API without a local translation
server. Metadata is refreshed by `camc api check`; curated fallbacks cover
models that do not publish complete limits.

## Sources and projections

- `GET /v1/models` refreshes the provider model-id catalog.
- `/public/litellm_model_cost_map` supplies context/output limits when
  available; it is fetched directly from the provider host.
- Codex receives an isolated `~/.codex-api/camc-model-catalog.json` and can
  switch among enabled profiles advertising `responses` with `/model`.
- Claude receives `CLAUDE_CODE_AUTO_COMPACT_WINDOW` from `context_window`.

The Codex API home links `~/.codex/skills` when that directory exists, and
does not modify the normal subscription home.

## API entry metadata

```json
{
  "available_endpoints": ["completions", "messages", "responses"],
  "metadata": {
    "context_window": 1048576,
    "max_output_tokens": 131072,
    "supports_tools": true,
    "supports_reasoning": true,
    "reasoning_levels": [],
    "supports_reasoning_summaries": false,
    "source": "litellm_cost_map",
    "synced_at": "2026-08-11T12:00:00Z"
  }
}
```

`available_endpoints` is a capability declaration, not a fallback order.
Claude requires native `messages`; Codex requires native `responses`. A
profile missing the selected adapter's endpoint is rejected before launch.
Endpoint paths and `/v1` suffixes come from adapter configuration. Provider
JSON has one `base_url` and no endpoint map or URL override.

## Commands

```bash
camc api check
camc api list --all
camc api default set deepseek-v4-flash --tool claude
```

Tokens remain outside this file (environment, provider `token_file`, or the
normal CAMC token fallback).
