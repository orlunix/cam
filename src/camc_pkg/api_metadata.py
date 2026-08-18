"""Model metadata sync (LiteLLM cost map) and agent-facing projections."""

import json
import os
import socket
import time
import urllib.error
import urllib.request

DEFAULT_MAX_OUTPUT_TOKENS = 131072
DEFAULT_EFFECTIVE_CONTEXT_PERCENT = 95
CODEX_CATALOG_TEMPLATE_SLUG = "gpt-5.5"
CODEX_MODELS_CACHE_FILE = os.path.expanduser("~/.codex/models_cache.json")

# Claude Code's accepted session effort values. Keep this independent from
# the upstream model metadata: the latter is often absent from IHUB's public
# cost map. A model profile may later narrow/verify the set, but normalization
# must never silently drop Claude's highest supported value.
CLAUDE_EFFORT_OPTIONS = ("low", "medium", "high", "xhigh", "max")
AGENT_EFFORT_OPTIONS = {"claude": CLAUDE_EFFORT_OPTIONS}

# Protocol names persisted in each API profile.  These are deliberately
# short, user-facing names; api_routing maps them to wire protocol ids.
AVAILABLE_ENDPOINTS = ("completions", "messages", "responses")


def normalize_available_endpoints(value):
    """Keep only known endpoint names while preserving configured order."""
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for endpoint in value:
        endpoint = str(endpoint or "").strip().lower()
        if endpoint in AVAILABLE_ENDPOINTS and endpoint not in result:
            result.append(endpoint)
    return result

# Curated fallbacks when IHUB cost map omits fields (see docs/api-model-metadata.md).
CURATED_METADATA_FALLBACKS = {
    "glm-5.2": {
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "deepseek-v4-pro": {
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "deepseek-v4-flash": {
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "qwen3-5-397b": {
        "context_window": 262144,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "kimi-k2.6": {
        "context_window": 262144,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "kimi-k3": {
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "minimax-m3": {
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "supports_tools": True,
        "supports_reasoning": True,
    },
    "nemotron-3-ultra": {
        "context_window": 128000,
        "max_output_tokens": 65536,
        "supports_tools": True,
        "supports_reasoning": False,
    },
}

DISPLAY_NAMES = {
    "glm-5.2": "GLM 5.2",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "qwen3-5-397b": "Qwen3.5 397B",
    "kimi-k2.6": "Kimi K2.6",
    "kimi-k3": "Kimi K3",
    "minimax-m3": "MiniMax M3",
    "nemotron-3-ultra": "Nemotron 3 Ultra",
}


def _default_reasoning_profile():
    """Return an independent, unverified Claude effort candidate profile."""
    return {
        "supported": list(CLAUDE_EFFORT_OPTIONS),
        "mapping": {
            "claude": {level: level for level in CLAUDE_EFFORT_OPTIONS},
        },
        "verified": False,
        "source": "claude_cli_capability_unverified",
    }


def normalize_reasoning_profile(value):
    """Normalize the durable Claude reasoning profile stored in JSON.

    ``None`` means a new profile and gets the full Claude candidate range.
    Explicit profiles are not widened: a missing mapping remains missing so
    callers can report an unsafe request instead of silently downgrading it.
    """
    if value is None:
        return _default_reasoning_profile()
    raw = value if isinstance(value, dict) else {}
    supported = []
    for level in raw.get("supported") or []:
        level = str(level).strip().lower()
        if level in CLAUDE_EFFORT_OPTIONS and level not in supported:
            supported.append(level)
    mapping = {}
    raw_mapping = raw.get("mapping")
    if isinstance(raw_mapping, dict):
        raw_claude = raw_mapping.get("claude")
        if isinstance(raw_claude, dict):
            for requested, mapped in raw_claude.items():
                requested = str(requested).strip().lower()
                mapped = str(mapped).strip().lower()
                if (requested in CLAUDE_EFFORT_OPTIONS
                        and mapped in CLAUDE_EFFORT_OPTIONS):
                    mapping[requested] = mapped
    codex_mapping = {}
    raw_codex = raw_mapping.get("codex") if isinstance(raw_mapping, dict) else None
    if isinstance(raw_codex, dict):
        for requested, mapped in raw_codex.items():
            requested = str(requested).strip().lower()
            mapped = str(mapped).strip().lower()
            if requested and mapped:
                codex_mapping[requested] = mapped
    result = {
        "supported": supported,
        "mapping": {"claude": mapping},
        "verified": bool(raw.get("verified", False)),
    }
    if codex_mapping:
        result["mapping"]["codex"] = codex_mapping
    raw_default = raw.get("default")
    if isinstance(raw_default, dict) and raw_default.get("codex"):
        result["default"] = {"codex": str(raw_default["codex"]).strip().lower()}
    source = raw.get("source")
    if source:
        result["source"] = str(source)
    return result


def resolve_claude_effort(profile, requested):
    """Resolve a requested Claude effort without lossy implicit fallback."""
    requested = str(requested or "").strip().lower()
    if requested not in CLAUDE_EFFORT_OPTIONS:
        raise ValueError(
            "unknown Claude effort %r (choose: %s)"
            % (requested, ", ".join(CLAUDE_EFFORT_OPTIONS))
        )
    normalized = normalize_reasoning_profile(profile)
    mapped = normalized.get("mapping", {}).get("claude", {}).get(requested)
    if not mapped:
        raise ValueError("no Claude effort mapping for %r" % requested)
    supported = normalized.get("supported") or []
    if requested not in supported:
        raise ValueError("Claude effort %r is not supported by this model" % requested)
    return mapped


def litellm_cost_map_url(provider):
    """LiteLLM cost map lives at host root, not under provider base_url /v1."""
    base = str(provider.get("base_url") if provider else "" or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/public/litellm_model_cost_map"


def fetch_litellm_cost_map(provider, timeout=30.0):
    """Fetch LiteLLM public cost map (no auth). Returns dict or {}."""
    url = litellm_cost_map_url(provider)
    if not url:
        return {}
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body if isinstance(body, dict) else {}


def metadata_from_cost_entry(entry):
    """Map one LiteLLM cost-map row to camc metadata fields."""
    if not isinstance(entry, dict):
        return {}
    out = {}
    for src, dst in (
        ("max_input_tokens", "context_window"),
        ("max_output_tokens", "max_output_tokens"),
    ):
        val = entry.get(src)
        if val is not None:
            try:
                out[dst] = int(val)
            except (TypeError, ValueError):
                pass
    if out.get("max_output_tokens") is None:
        val = entry.get("max_tokens")
        if val is not None:
            try:
                out["max_output_tokens"] = int(val)
            except (TypeError, ValueError):
                pass
    if entry.get("supports_function_calling") is not None:
        out["supports_tools"] = bool(entry.get("supports_function_calling"))
    if entry.get("supports_reasoning") is not None:
        out["supports_reasoning"] = bool(entry.get("supports_reasoning"))
    if entry.get("mode"):
        out["mode"] = str(entry.get("mode"))
    if out:
        out["source"] = "litellm_cost_map"
    return out


def apply_metadata_fallbacks(api_key, metadata):
    """Fill gaps from curated seed defaults."""
    merged = dict(metadata or {})
    fb = CURATED_METADATA_FALLBACKS.get(api_key) or {}
    for key, val in fb.items():
        if merged.get(key) is None:
            merged[key] = val
    if merged.get("max_output_tokens") is None and merged.get("context_window"):
        merged["max_output_tokens"] = min(
            int(merged["context_window"]),
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
    if merged.get("supports_tools") is None:
        merged["supports_tools"] = True
    if merged.get("supports_reasoning") is None:
        merged["supports_reasoning"] = bool(fb.get("supports_reasoning", True))
    # Codex: IHUB GLM/DeepSeek paths use reasoning_content, not OpenAI thinking params.
    if "reasoning_levels" not in merged:
        merged["reasoning_levels"] = []
    if "supports_reasoning_summaries" not in merged:
        merged["supports_reasoning_summaries"] = False
    return merged


def merge_api_metadata(api_key, model_id, cost_map, synced_at=None):
    """Build metadata dict for one API profile."""
    entry = (cost_map or {}).get(model_id) if model_id else None
    meta = metadata_from_cost_entry(entry)
    if not meta.get("source"):
        meta["source"] = "curated_fallback"
    meta = apply_metadata_fallbacks(api_key, meta)
    meta["synced_at"] = synced_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return meta


def sync_metadata_in_data(data, provider, cost_map=None, synced_at=None):
    """Merge metadata into apis.*; return count updated."""
    if cost_map is None:
        try:
            cost_map = fetch_litellm_cost_map(provider)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, socket.timeout):
            cost_map = {}
    synced_at = synced_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    updated = 0
    apis = data.get("apis") or {}
    for key, entry in apis.items():
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        entry["metadata"] = merge_api_metadata(key, model.strip(), cost_map, synced_at=synced_at)
        updated += 1
    if cost_map:
        data["_metadata_catalog"] = {
            "synced_at": synced_at,
            "source_url": litellm_cost_map_url(provider),
            "entry_count": len(cost_map),
        }
    return updated


def resolve_api_metadata(api_name, data=None):
    """Return metadata dict for an API key (loads api-models.json if needed)."""
    from camc_pkg.api_store import get_api_entry, load_api_models

    if data is None:
        data = load_api_models()
    _key, entry = get_api_entry(data, api_name)
    meta = entry.get("metadata")
    if isinstance(meta, dict) and meta.get("context_window"):
        return apply_metadata_fallbacks(_key, meta)
    model = entry.get("model")
    return merge_api_metadata(_key, str(model or ""), {})


def claude_context_env_overrides(api_name, metadata=None):
    """Env override so Claude Code auto-compact uses camc metadata context window."""
    if metadata is None:
        meta = resolve_api_metadata(api_name)
    else:
        meta = apply_metadata_fallbacks(api_name, metadata)
    ctx = meta.get("context_window")
    if ctx is None:
        return {}
    return {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(int(ctx)),
    }


def load_codex_catalog_template(cache_path=None):
    """Clone a known-good Codex model row from ~/.codex/models_cache.json."""
    path = cache_path or CODEX_MODELS_CACHE_FILE
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (IOError, ValueError):
        return None
    for model in data.get("models") or []:
        if isinstance(model, dict) and model.get("slug") == CODEX_CATALOG_TEMPLATE_SLUG:
            return json.loads(json.dumps(model))
    return None


def codex_catalog_model(api_name, metadata=None, template=None, model_id=None,
                        tool_capabilities=None, reasoning=None):
    """One Codex model_catalog.json entry (clone official template + overlay)."""
    meta = apply_metadata_fallbacks(api_name, metadata or {})
    slug = str(model_id or api_name)
    ctx = int(meta.get("context_window") or DEFAULT_MAX_OUTPUT_TOKENS)
    entry = template if template is not None else load_codex_catalog_template()
    if not isinstance(entry, dict):
        raise ValueError(
            "Codex model catalog template %r not found in %s; run `codex` once "
            "to populate models_cache.json"
            % (CODEX_CATALOG_TEMPLATE_SLUG, CODEX_MODELS_CACHE_FILE)
        )
    entry = json.loads(json.dumps(entry))
    entry["slug"] = slug
    entry["display_name"] = DISPLAY_NAMES.get(str(api_name), slug)
    entry["description"] = "CAM Inference Hub API (%s)" % api_name
    entry["context_window"] = ctx
    entry["max_context_window"] = ctx
    entry["effective_context_window_percent"] = DEFAULT_EFFECTIVE_CONTEXT_PERCENT
    entry["supported_reasoning_levels"] = list(meta.get("reasoning_levels") or [])
    entry["supports_reasoning_summaries"] = bool(meta.get("supports_reasoning_summaries"))
    entry["supports_parallel_tool_calls"] = bool(meta.get("supports_tools", True))
    capabilities = tool_capabilities if isinstance(tool_capabilities, dict) else {}
    if "supports_search_tool" in capabilities:
        entry["supports_search_tool"] = bool(capabilities["supports_search_tool"])
    entry["supported_in_api"] = True
    entry["visibility"] = "list"
    entry["priority"] = 1000
    codex_mapping = ((reasoning or {}).get("mapping") or {}).get("codex") or {}
    if codex_mapping:
        descriptions = {
            str(row.get("effort")): str(row.get("description") or "")
            for row in (entry.get("supported_reasoning_levels") or [])
            if isinstance(row, dict) and row.get("effort")
        }
        entry["supported_reasoning_levels"] = [
            {"effort": effort,
             "description": descriptions.get(effort) or
                            ("Mapped by CAMC to %s" % mapped)}
            for effort, mapped in codex_mapping.items()
        ]
        entry["default_reasoning_level"] = str(
            ((reasoning or {}).get("default") or {}).get("codex") or
            next(iter(codex_mapping)))
        entry["default_reasoning_summary"] = "none"
    elif not entry.get("supported_reasoning_levels"):
        entry["default_reasoning_level"] = "low"
        entry["default_reasoning_summary"] = "none"
    return entry


def codex_catalog_models(default_api_name, data=None, template=None,
                         require_endpoint=None):
    """Build a Codex catalog for every enabled, compatible API profile.

    The selected profile is always included first, even when its enabled flag
    is stale or absent. Direct Responses runs include only profiles that
    advertise the native ``responses`` endpoint.
    """
    if data is None:
        from camc_pkg.api_store import load_api_models
        data = load_api_models()
    apis = data.get("apis") if isinstance(data, dict) else {}
    apis = apis if isinstance(apis, dict) else {}
    default_name = str(default_api_name or "").strip()
    names = []
    for name in sorted(apis):
        entry = apis.get(name)
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        if (require_endpoint
                and require_endpoint not in normalize_available_endpoints(
                    entry.get("available_endpoints"))):
            continue
        names.append(str(name))
    if default_name in names:
        names.remove(default_name)
    if default_name:
        names.insert(0, default_name)

    rows = []
    seen_slugs = set()
    for name in names:
        from camc_pkg.api_store import tool_supports_api
        if not tool_supports_api(data, "codex", name):
            continue
        metadata = resolve_api_metadata(name, data=data)
        entry = apis.get(name) or {}
        slug = str(entry.get("model") or name)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        rows.append(codex_catalog_model(
            name, metadata, template=template, model_id=entry.get("model"),
            tool_capabilities=(entry.get("tool_capabilities") or {}).get("codex"),
            reasoning=entry.get("reasoning")))
    return rows


def write_codex_model_catalog(catalog_path, api_name, metadata=None, data=None,
                              require_endpoint=None, model_id=None):
    """Write ~/.codex-api/camc-model-catalog.json for Codex --api."""
    if data is None:
        from camc_pkg.api_store import load_api_models
        data = load_api_models()
    rows = codex_catalog_models(
        api_name, data=data, require_endpoint=require_endpoint)
    if metadata and rows:
        # Keep the caller's freshly resolved metadata for the selected row;
        # the remaining rows come from the durable api-models.json matrix.
        selected = ((data or {}).get("apis") or {}).get(api_name) or {}
        capabilities = (selected.get("tool_capabilities") or {}).get("codex")
        if capabilities is None and "supports_search_tool" in rows[0]:
            capabilities = {"supports_search_tool": rows[0]["supports_search_tool"]}
        rows[0] = codex_catalog_model(
            api_name, metadata, model_id=model_id,
            tool_capabilities=capabilities,
            reasoning=selected.get("reasoning"))
    payload = {"models": rows}
    os.makedirs(os.path.dirname(catalog_path), exist_ok=True)
    tmp = catalog_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, catalog_path)
    return catalog_path


def curated_api_keys():
    from camc_pkg.api_store import CURATED_APIS
    return [key for key, _model, _aliases in CURATED_APIS]
