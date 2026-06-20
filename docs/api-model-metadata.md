# API model metadata (Inference Hub → agents)

Date: 2026-06-18  
Status: **implemented** (P0)  
Related: `docs/camc-api-proxy-plan.md`, `docs/inference-hub.md`, `docs/debug-notes/api-bench-ihub-proxy.md`

## Problem

Coding agents need **context window** and capability hints for compaction, `/context` display, and Codex startup. Our proxy path only translated wire formats and dropped Claude-only fields (`context_management`, `output_config`).

| Consumer | What it needs | Before P0 |
|----------|---------------|-----------|
| **Codex** | Full `model_catalog.json` (`context_window`, reasoning flags, …) | `Model metadata for glm-5.1 not found` |
| **Claude Code** | Built-in registry + optional `/v1/models` | Custom alias `glm-5.1` → fallback window |
| **Proxy** | Enriched `GET /v1/models` | Only `{id, object}` |

## What Inference Hub exposes

IHUB runs **LiteLLM** underneath. Model metadata is **not** on standard `GET /v1/models` (only `id`, `object`, `created`, `owned_by`).

| Endpoint | Auth | Useful fields |
|----------|------|---------------|
| `GET /v1/models` | Bearer | Model id list only |
| `GET /public/litellm_model_cost_map` | **None** | `max_input_tokens`, `max_output_tokens`, `supports_*` (sparse for `eccn-*`) |

Note: cost map is at **host root** (`https://inference-api.nvidia.com/public/...`), not under `base_url` `/v1`.
| `GET /model/info` | Admin key | Full LiteLLM model_info — **403** on typical IHUB virtual keys |

Curated IHUB ids (cost map, 2026-06-18):

| API key | IHUB model id | `max_input_tokens` |
|---------|---------------|-------------------|
| `glm-5.1` | `nvidia/zai-org/eccn-glm-5.1` | 202752 |
| `deepseek-v4-pro` | `nvidia/deepseek-ai/eccn-deepseek-v4-pro` | 1048576 |
| `qwen3-5-397b` | `nvidia/qwen/eccn-qwen3-5-397b-a17b` | 262144 |
| `kimi-k2.6` | `nvidia/moonshotai/eccn-kimi-k2.6` | 262144 |
| `minimax-m2.7` | `nvidia/minimaxai/eccn-minimax-m2.7` | 196608 |
| `nemotron-3-ultra` | `nvidia/nvidia/eccn-nemotron-3-ultra` | (null — use seed fallback) |

## How CC Switch and LiteLLM handle this

### CC Switch

- **Does not** rely on IHUB `/v1/models` for context windows.
- **Codex**: Provider **Model Mapping** table (model id + display name + **context window**) → projects to `~/.codex/cc-switch-model-catalog.json` → `model_catalog_json` in `config.toml`.
- Local routing (`:15721`) serves `GET /v1/models` from that catalog for Codex startup probes.
- **Claude**: Uses Claude Code's **built-in registry**; CC Switch manages role mapping (`sonnet`/`opus`/`haiku`) and `supports1m`.
- Strips `context_management` / incompatible params during **protocol conversion** (same as us).

### LiteLLM

- **Three layers**: global `model_prices_and_context_window.json`, per-deployment `model_list[].model_info`, and `/model/info`.
- **`drop_params` + `additional_drop_params`**: drops `output_config`, `context_management` (see `dev/ihub_proxy/litellm-glm-proxy.yaml`).
- **Recent**: enriches OpenAI `/v1/models` with `max_input_tokens` / `max_output_tokens` when the router knows them (PR #30272).

### CAM approach (P0)

Hybrid of both:

1. **Auto-sync** from IHUB `GET /public/litellm_model_cost_map` on `camc api check` → `apis.*.metadata`
2. **Curated seed fallbacks** when cost map fields are null (shipped in `api_metadata.py`)
3. **Proxy** `GET /v1/models` returns LiteLLM-style token limits from `metadata`
4. **Codex** `~/.cam/codex-api/camc-model-catalog.json` + `model_catalog_json` in `config.toml` (cloned from `gpt-5.5` row in `~/.codex/models_cache.json`)
5. **Claude** `camc run -t claude --api` sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` from `apis.*.metadata.context_window` so auto-compact triggers at the correct fraction of the real window (not the 200K default)

## Schema: `apis.<name>.metadata`

```json
{
  "context_window": 202752,
  "max_output_tokens": 131072,
  "supports_tools": true,
  "supports_reasoning": true,
  "reasoning_levels": [],
  "supports_reasoning_summaries": false,
  "source": "litellm_cost_map",
  "synced_at": "2026-06-18T12:00:00Z"
}
```

| Field | Purpose |
|-------|---------|
| `context_window` | Input context limit (from `max_input_tokens`) |
| `max_output_tokens` | Single-turn output cap |
| `supports_tools` | Tool calling (default `true` for curated chat models) |
| `reasoning_levels` | Codex catalog: `[]` disables unsupported thinking API params |
| `supports_reasoning_summaries` | Codex catalog: usually `false` for IHUB GLM path |

Tokens stay in `~/.cam/token.env` — never in `api-models.json`.

## Commands

```bash
# Refresh id list + enabled flags + metadata
camc api check

# Inspect metadata (after check)
python3 -c "import json; d=json.load(open('~/.cam/api-models.json'.replace('~',__import__('os').path.expanduser('~'))); print(d['apis']['glm-5.1'].get('metadata'))"

# Proxy models endpoint (after camc run --api or camc api proxy start)
curl -s http://127.0.0.1:18325/v1/models | python3 -m json.tool   # Codex route
curl -s http://127.0.0.1:18324/v1/models | python3 -m json.tool   # Claude route
```

## Still not covered (P1+)

| Gap | Notes |
|-----|-------|
| `context_management` passthrough | IHUB chat/completions does not support Anthropic beta API |
| `output_config.effort` mapping | Needs provider-specific reasoning rules (CC Switch auto-detect) |
| Proxy-side tool-result trimming | Threshold uses `metadata.context_window` once P1 lands |
| Claude `/context` status bar | May still show 200K for custom gateway model ids; `CLAUDE_CODE_AUTO_COMPACT_WINDOW` fixes compact timing only |

## Key files

| File | Role |
|------|------|
| `src/camc_pkg/api_metadata.py` | Sync, fallbacks, Codex catalog, OpenAI model list |
| `src/camc_pkg/api_store.py` | `check_provider()` calls metadata sync |
| `src/camc_pkg/api_resolver.py` | `ensure_codex_api_config_dir()` writes catalog |
| `src/camc_pkg/proxy/messages.py` | Enriched `/v1/models`, `dropped_keys` log |
| `src/camc_pkg/proxy/responses.py` | Enriched `/v1/models` |
