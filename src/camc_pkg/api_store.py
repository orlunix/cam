"""Load/save ~/.cam/api-models.json — API profiles and providers."""

import json
import os
import socket
import time
import urllib.error
import urllib.request

from camc_pkg import CAM_DIR
from camc_pkg.api_metadata import (
    normalize_available_endpoints,
    normalize_reasoning_profile,
    sync_metadata_in_data,
)

API_MODELS_FILE = os.path.join(CAM_DIR, "api-models.json")
TOKEN_ENV_FILE = os.path.join(CAM_DIR, "token.env")

DEFAULT_PROVIDER = "inference-hub"
IHUB_BASE = "https://inference-api.nvidia.com"
API_SCHEMA_VERSION = "1.0.5"

# Kimi K3 is now one model/profile.  The phase-specific names remain only as
# migration aliases so an existing default or explicit command keeps working.
KIMI_K3_KEY = "kimi-k3"
LEGACY_KIMI_K3_PROFILES = ("kimi-k3-low", "kimi-k3-high", "kimi-k3-max")

CURATED_APIS = [
    ("glm-5.2", "nvidia/zai-org/eccn-glm-5.2", ["glm", "glm52"]),
    ("deepseek-v4-flash", "nvidia/deepseek-ai/eccn-deepseek-v4-flash", ["deepseek-flash", "ds-v4-flash"]),
    ("deepseek-v4-pro", "nvidia/deepseek-ai/eccn-deepseek-v4-pro", ["deepseek", "ds-v4"]),
    ("kimi-k2.6", "nvidia/moonshotai/eccn-kimi-k2.6", ["kimi", "k2.6"]),
    (KIMI_K3_KEY, "nvidia/moonshotai/eccn-kimi-k3", ["k3"]),
    ("minimax-m3", "nvidia/minimaxai/eccn-minimax-m3", ["minimax", "m3"]),
    ("qwen3-5-397b", "nvidia/qwen/eccn-qwen3-5-397b-a17b", ["qwen397", "qwen3.5"]),
    ("nemotron-3-ultra", "nvidia/nvidia/eccn-nemotron-3-ultra", ["nemotron", "nemo-ultra"]),
]

# Endpoint probes against the NVIDIA Inference Hub.  Kimi K3 Max exposes the
# Messages route but returned empty native content in the verification probe,
# so it intentionally keeps the completion fallback for Claude.
CURATED_AVAILABLE_ENDPOINTS = {
    "glm-5.2": ["completions", "messages", "responses"],
    "deepseek-v4-flash": ["completions", "messages", "responses"],
    "deepseek-v4-pro": ["completions", "messages", "responses"],
    "kimi-k2.6": ["completions", "messages", "responses"],
    KIMI_K3_KEY: ["completions", "responses"],
    "minimax-m3": ["completions", "messages", "responses"],
    "qwen3-5-397b": ["completions", "messages", "responses"],
    "nemotron-3-ultra": ["completions", "messages", "responses"],
}

CLAUDE_CLIENT_MODELS = {
    "deepseek-v4-flash": "claude-haiku-5-dsv4f",
    "deepseek-v4-pro": "claude-opus-5-dsv4p",
    "glm-5.2": "claude-sonnet-5-glm52",
    "kimi-k2.6": "claude-sonnet-5-kimi26",
}

CURATED_CODEX_REASONING = {
    "glm-5.2": {
        "mapping": {
            "none": "none", "low": "high", "medium": "high",
            "high": "high", "xhigh": "max", "max": "max",
        },
        "default": "high",
    },
    KIMI_K3_KEY: {
        "mapping": {
            "none": "low", "low": "low", "medium": "high",
            "high": "high", "xhigh": "max", "max": "max",
        },
        "default": "high",
    },
}


TOOL_SUPPORTED_ENDPOINTS = {
    "claude": ["messages"],
    "codex": ["responses"],
}

TOOL_BASE_URL_SUF = {
    "claude": "",
    "codex": "/v1",
}


def _curated_aliases(key, aliases):
    result = list(aliases)
    client_model = CLAUDE_CLIENT_MODELS.get(key)
    if client_model:
        result.append(client_model)
    return result


def _curated_tool_supported_apis():
    return {
        "claude": sorted(CLAUDE_CLIENT_MODELS.values()),
        "codex": [key for key, _model, _aliases in CURATED_APIS],
    }


def _migrate_legacy_kimi_k3(data):
    """Collapse retired K3 phase profiles without losing user selections."""
    apis = data.get("apis")
    if not isinstance(apis, dict):
        return
    legacy = {}
    for name in LEGACY_KIMI_K3_PROFILES:
        entry = apis.pop(name, None)
        if isinstance(entry, dict):
            legacy[name] = entry
    if not legacy:
        return

    current = apis.get(KIMI_K3_KEY)
    if not isinstance(current, dict):
        # Prefer the former max profile as the closest match to the unified
        # model, then fall back to high/low when that is the only record.
        for name in ("kimi-k3-max", "kimi-k3-high", "kimi-k3-low"):
            if name in legacy:
                current = dict(legacy[name])
                break
        else:
            current = {}
    else:
        current = dict(current)

    # Preserve useful fields from a phase entry when a hand-edited canonical
    # record did not have them yet.  Enabled is merged as an OR so an enabled
    # phase cannot silently turn the unified profile off.
    for name in ("kimi-k3-max", "kimi-k3-high", "kimi-k3-low"):
        entry = legacy.get(name) or {}
        if entry.get("enabled") is True:
            current["enabled"] = True
        for field in ("provider", "model", "enabled_reason", "metadata"):
            if current.get(field) is None and entry.get(field) is not None:
                current[field] = entry[field]

    hidden_aliases = set(current.get("legacy_aliases") or [])
    hidden_aliases.update(LEGACY_KIMI_K3_PROFILES)
    hidden_aliases.update(("k3-low", "k3-high", "k3-max",
                           "kimi-low", "kimi-high", "kimi-max"))
    for entry in legacy.values():
        hidden_aliases.update(
            alias for alias in (entry.get("aliases") or [])
            if isinstance(alias, str) and alias.strip()
        )
        clients = entry.get("client_models")
        if isinstance(clients, dict):
            hidden_aliases.update(
                value for value in clients.values()
                if isinstance(value, str) and value.strip()
            )
    current["legacy_aliases"] = sorted(hidden_aliases)
    apis[KIMI_K3_KEY] = current


def _canonicalize_k3_references(data):
    """Replace retired profile names in defaults and tool policy lists."""
    retired = set(LEGACY_KIMI_K3_PROFILES)
    for field in ("defaults", "tool_supported_apis"):
        values = data.get(field)
        if not isinstance(values, dict):
            continue
        for tool, names in list(values.items()):
            if not isinstance(names, list):
                continue
            rewritten = []
            for name in names:
                name = KIMI_K3_KEY if name in retired else name
                if name not in rewritten:
                    rewritten.append(name)
            values[tool] = rewritten


def _curated_client_models(key, model):
    models = {"codex": model}
    if key in CLAUDE_CLIENT_MODELS:
        models["claude"] = CLAUDE_CLIENT_MODELS[key]
    return models


def _curated_tool_capabilities():
    # NVIDIA Responses profiles do not accept Codex's tool_search type.
    # This is durable profile data, not catalog-generator behavior.
    return {"codex": {"supports_search_tool": False}}


def _curated_reasoning(key, value=None):
    profile = normalize_reasoning_profile(value)
    curated = CURATED_CODEX_REASONING.get(key)
    if curated:
        profile.setdefault("mapping", {}).setdefault(
            "codex", dict(curated["mapping"]))
        profile.setdefault("default", {}).setdefault(
            "codex", curated["default"])
    return profile

# Tools that may have a per-tool default API (empty = normal OAuth/login).
DEFAULT_API_TOOLS = ("claude", "codex")


def _default_seed():
    apis = {}
    for key, model, aliases in CURATED_APIS:
        apis[key] = {
            "provider": DEFAULT_PROVIDER,
            "model": model,
            "enabled": False,
            "aliases": _curated_aliases(key, aliases),
            "available_endpoints": list(CURATED_AVAILABLE_ENDPOINTS.get(key, [])),
            "client_models": _curated_client_models(key, model),
            "tool_capabilities": _curated_tool_capabilities(),
            "reasoning": _curated_reasoning(key),
        }
    data = {
        "version": API_SCHEMA_VERSION,
        "providers": {
            DEFAULT_PROVIDER: {
                "display_name": "NVIDIA Inference Hub",
                "auth_key": "inference_hub",
                "env_names": [
                    "INFERENCE_HUB_TOKEN",
                    "INFERENCE_HUB_API_KEY",
                    "INFERENCE_API_KEY",
                ],
                "token_file": "~/.my_tokens.yaml",
                "base_url": IHUB_BASE,
                "catalog_path": "/v1/models",
            },
        },
        "apis": apis,
        "tool_supported_endpoints": dict(TOOL_SUPPORTED_ENDPOINTS),
        "tool_base_url_suf": dict(TOOL_BASE_URL_SUF),
        "tool_supported_apis": _curated_tool_supported_apis(),
        "debug_api_proxy": False,
        "_aliases": {},
        "_catalog": {},
    }
    rebuild_aliases(data)
    return data


def load_api_models():
    """Load api-models.json; return seed if missing."""
    if not os.path.isfile(API_MODELS_FILE):
        return _default_seed()
    with open(API_MODELS_FILE, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("api-models.json must be a JSON object")
    return data


def save_api_models(data):
    os.makedirs(CAM_DIR, exist_ok=True)
    rebuild_aliases(data)
    tmp = API_MODELS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, API_MODELS_FILE)


def merge_curated_apis(data):
    """Refresh the managed direct profiles and their endpoint capabilities."""
    for field in ("default", "default_provider", "_templates"):
        data.pop(field, None)
    _migrate_legacy_kimi_k3(data)
    _canonicalize_k3_references(data)
    providers = data.setdefault("providers", {})
    provider = providers.get(DEFAULT_PROVIDER)
    if not isinstance(provider, dict):
        providers[DEFAULT_PROVIDER] = dict(_default_seed()["providers"][DEFAULT_PROVIDER])
    else:
        provider.setdefault("display_name", "NVIDIA Inference Hub")
        provider.setdefault("auth_key", "inference_hub")
        provider.setdefault("env_names", ["INFERENCE_HUB_TOKEN"])
        provider.setdefault("token_file", "~/.my_tokens.yaml")
        provider.setdefault("base_url", IHUB_BASE)
        provider.setdefault("catalog_path", "/v1/models")
        # The direct schema has no provider-level protocol or endpoint map.
        for field in ("client_base_url", "upstream_protocol", "translator",
                      "external_translator", "endpoints"):
            provider.pop(field, None)
    for configured in providers.values():
        if not isinstance(configured, dict):
            continue
        for field in ("client_base_url", "upstream_protocol", "translator",
                      "external_translator", "endpoints", "proxy",
                      "proxy_port"):
            configured.pop(field, None)
    apis = data.setdefault("apis", {})
    for key, model, aliases in CURATED_APIS:
        entry = apis.get(key)
        if not isinstance(entry, dict):
            entry = {}
            apis[key] = entry
        entry.setdefault("provider", DEFAULT_PROVIDER)
        entry["model"] = model
        entry["aliases"] = _curated_aliases(key, aliases)
        entry["client_models"] = _curated_client_models(key, model)
        entry["tool_capabilities"] = _curated_tool_capabilities()
        entry.setdefault("enabled", False)
        if "available_endpoints" not in entry:
            entry["available_endpoints"] = list(
                CURATED_AVAILABLE_ENDPOINTS.get(key, []))
        else:
            entry["available_endpoints"] = normalize_available_endpoints(
                entry.get("available_endpoints"))
        # Keep this independent from synced metadata. User-authored mappings
        # survive api check; missing/malformed mappings get a safe candidate
        # profile without changing the selected API/default.
        entry["reasoning"] = _curated_reasoning(key, entry.get("reasoning"))
        for field in ("url", "client_url", "upstream_protocol", "translator",
                      "proxy", "proxy_port"):
            entry.pop(field, None)
    for field, default in (
            ("tool_supported_endpoints", TOOL_SUPPORTED_ENDPOINTS),
            ("tool_base_url_suf", TOOL_BASE_URL_SUF),
            ("tool_supported_apis", _curated_tool_supported_apis())):
        value = data.get(field)
        if not isinstance(value, dict):
            data[field] = dict(default)
            continue
        for tool, setting in default.items():
            value.setdefault(tool, setting)
    _canonicalize_k3_references(data)
    if not isinstance(data.get("debug_api_proxy"), bool):
        data["debug_api_proxy"] = False
    rebuild_aliases(data)


def ensure_ready():
    """Create seed file if missing; return data dict."""
    if os.path.isfile(API_MODELS_FILE):
        data = load_api_models()
        if _api_schema_needs_refresh(data.get("version")):
            data["version"] = API_SCHEMA_VERSION
        merge_curated_apis(data)
        try:
            save_api_models(data)
        except OSError:
            # Read-only hosts may still use normal login runs.  Keep the
            # in-memory direct profile usable without making startup fail.
            pass
        return data
    data = _default_seed()
    try:
        save_api_models(data)
    except OSError:
        # A read-only ~/.cam is valid for login-only operation; API check or
        # an explicit profile will report persistence/network errors later.
        pass
    return data


def _api_schema_needs_refresh(value):
    """Return whether an API JSON version is older than the current schema."""
    if not isinstance(value, str):
        return True
    try:
        version = tuple(int(part) for part in value.split("."))
    except (TypeError, ValueError):
        return True
    return version < tuple(int(part) for part in API_SCHEMA_VERSION.split("."))


def rebuild_aliases(data):
    """Rebuild flat _aliases index from apis.*.aliases."""
    aliases = {}
    apis = data.get("apis") or {}
    for key, entry in apis.items():
        if not isinstance(entry, dict):
            continue
        aliases[key] = key
        aliases[key.lower()] = key
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                aliases[alias] = key
                aliases[alias.lower()] = key
        for alias in entry.get("legacy_aliases") or []:
            if isinstance(alias, str) and alias.strip():
                aliases[alias] = key
                aliases[alias.lower()] = key
        model = entry.get("model")
        if isinstance(model, str) and model:
            aliases[model] = key
    data["_aliases"] = aliases


def resolve_api_name(data, name):
    """Resolve API key from name or alias."""
    if not name:
        raise ValueError("--api name is required")
    apis = data.get("apis") or {}
    if name in apis:
        return name
    aliases = data.get("_aliases") or {}
    key = aliases.get(name) or aliases.get(name.lower())
    if key and key in apis:
        return key
    raise ValueError("unknown API %r (not in api-models.json)" % name)


def get_api_entry(data, name):
    key = resolve_api_name(data, name)
    return key, dict((data.get("apis") or {}).get(key) or {})


def _normalize_default_name(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_tool_default_api(data, tool):
    """Return opt-in run default from defaults.<tool>, or None for login path."""
    if tool not in DEFAULT_API_TOOLS:
        return None
    defaults = data.get("defaults")
    if not isinstance(defaults, dict) or tool not in defaults:
        return None
    names = defaults.get(tool)
    if not isinstance(names, list) or not names:
        return None
    return _normalize_default_name(names[0])


def list_tool_default_apis(data):
    """Return per-tool default status rows for display/CLI."""
    rows = []
    for tool in DEFAULT_API_TOOLS:
        defaults = data.get("defaults") or {}
        names = defaults.get(tool) if isinstance(defaults, dict) else []
        if not isinstance(names, list):
            names = []
        name = _normalize_default_name(names[0]) if names else None
        if not name:
            rows.append({
                "tool": tool,
                "api": None,
                "apis": names,
                "mode": "login",
                "enabled": None,
                "reason": None,
            })
            continue
        key, entry = get_api_entry(data, name)
        enabled = entry.get("enabled") is not False
        rows.append({
            "tool": tool,
            "api": key,
            "apis": names,
            "mode": "api",
            "enabled": enabled,
            "reason": entry.get("enabled_reason"),
        })
    return rows


def set_tool_default_api(data, tool, api_name):
    """Set the first entry in the ordered defaults.<tool> list."""
    if tool not in DEFAULT_API_TOOLS:
        raise ValueError("unsupported tool %r for default API (use: claude, codex)" % tool)
    key = resolve_api_name(data, api_name)
    defaults = data.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        data["defaults"] = defaults
    defaults[tool] = [key]
    save_api_models(data)
    return key


def clear_tool_default_api(data, tool):
    """Clear defaults.<tool>; empty tool default means normal login."""
    if tool not in DEFAULT_API_TOOLS:
        raise ValueError("unsupported tool %r for default API (use: claude, codex)" % tool)
    defaults = data.get("defaults")
    if isinstance(defaults, dict) and tool in defaults:
        defaults.pop(tool)
    save_api_models(data)


def resolve_run_api_name(tool, cli_api=None, no_default_api=False, data=None):
    """Pick API profile for camc run: explicit --api, tool default, or login."""
    if cli_api:
        return _normalize_default_name(cli_api), "cli"
    if no_default_api:
        return None, "login"
    data = data or ensure_ready()
    name = resolve_tool_default_api(data, tool)
    if not name:
        return None, "login"
    key, entry = get_api_entry(data, name)
    if entry.get("enabled") is False:
        try:
            check_provider(data)
        except Exception:
            pass
        key, entry = get_api_entry(data, name)
        if entry.get("enabled") is False:
            reason = entry.get("enabled_reason") or "disabled"
            raise ValueError(
                "Default API %r for tool %r is disabled (%s). Run: camc api check"
                % (key, tool, reason)
            )
    return key, "default"


def get_provider(data, provider_id):
    providers = data.get("providers") or {}
    if provider_id not in providers:
        raise ValueError("unknown provider %r" % provider_id)
    return dict(providers[provider_id])


def catalog_url(provider):
    """GET URL for provider model catalog (empty = skip catalog sync)."""
    path = str(provider.get("catalog_path") if "catalog_path" in provider else "/models")
    if not path:
        return ""
    base = str(provider.get("base_url") or "").rstrip("/")
    if not base:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def fetch_model_catalog(provider, token, timeout=5.0):
    """Fetch model ids from provider catalog endpoint. Returns set of ids."""
    url = catalog_url(provider)
    if not url:
        return set()
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer %s" % token},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ids = set()
    for item in body.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
    return ids


def list_apis(data, show_all=False):
    rows = []
    for key, entry in sorted((data.get("apis") or {}).items()):
        if not isinstance(entry, dict):
            continue
        enabled = entry.get("enabled", True)
        if not show_all and enabled is False:
            continue
        rows.append({
            "name": key,
            "model": entry.get("model", ""),
            "provider": entry.get("provider", ""),
            "enabled": enabled,
            "aliases": entry.get("aliases") or [],
            "available_endpoints": normalize_available_endpoints(
                entry.get("available_endpoints")),
            "tools": tools_supporting_api(data, key),
        })
    return rows


def tool_api_config(data, tool):
    """Return JSON-owned endpoint and URL rules for one API-capable tool."""
    endpoints = (data.get("tool_supported_endpoints") or {}).get(tool)
    suffix = (data.get("tool_base_url_suf") or {}).get(tool)
    if not isinstance(endpoints, list) or suffix is None:
        raise ValueError("API tool %r is not configured" % tool)
    return normalize_available_endpoints(endpoints), str(suffix or "")


def tool_supports_api(data, tool, api_key):
    """Whether a profile is allowed for a tool by JSON policy and endpoint."""
    allowed = (data.get("tool_supported_apis") or {}).get(tool)
    if not isinstance(allowed, list):
        return False
    try:
        permitted = {resolve_api_name(data, name) for name in allowed}
    except ValueError:
        return False
    if api_key not in permitted:
        return False
    entry = (data.get("apis") or {}).get(api_key) or {}
    endpoints, _suffix = tool_api_config(data, tool)
    return bool(set(normalize_available_endpoints(
        entry.get("available_endpoints"))).intersection(endpoints))


def tools_supporting_api(data, api_key):
    return [tool for tool in sorted((data.get("tool_supported_apis") or {}))
            if tool_supports_api(data, tool, api_key)]


def check_provider(data):
    """Ping provider catalog, refresh enabled flags and _catalog."""
    from camc_pkg.api_token import resolve_token

    providers = data.get("providers") or {}
    provider_id = DEFAULT_PROVIDER if DEFAULT_PROVIDER in providers else next(
        iter(sorted(providers)), None)
    if not provider_id:
        return {
            "provider": None,
            "reachable": False,
            "token_source": "none",
            "model_count": 0,
            "apis": [],
            "error": "no API providers configured",
            "catalog_skipped": True,
        }
    provider = get_provider(data, provider_id)
    auth_key = provider.get("auth_key") or "inference_hub"
    token, source = resolve_token(
        auth_key,
        provider.get("env_names") or [],
        cli_token=None,
        token_file=provider.get("token_file"),
        token_key=provider.get("token_key"),
    )

    result = {
        "provider": provider_id,
        "reachable": False,
        "token_source": source,
        "model_count": 0,
        "apis": [],
        "error": None,
        "catalog_skipped": False,
    }

    if not token:
        result["error"] = (
            "no token found; set one of %s in ~/.cam/token.env"
            % ", ".join(provider.get("env_names") or ["INFERENCE_HUB_TOKEN"])
        )
        return result

    catalog = catalog_url(provider)
    if not catalog:
        result["reachable"] = True
        result["catalog_skipped"] = True
        ids = set()
    else:
        try:
            ids = fetch_model_catalog(provider, token)
            result["reachable"] = True
            result["model_count"] = len(ids)
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, ValueError) as exc:
            result["error"] = str(exc)
            return result

    merge_curated_apis(data)
    apis = data.get("apis") or {}
    for key, entry in apis.items():
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        if not model:
            continue
        if not catalog:
            result["apis"].append({
                "name": key,
                "model": model,
                "enabled": entry.get("enabled"),
                "reason": entry.get("enabled_reason") or "catalog_skipped",
            })
            continue
        if model in ids:
            entry["enabled"] = True
            entry["enabled_reason"] = "catalog"
        else:
            entry["enabled"] = False
            entry["enabled_reason"] = "id_not_on_key" if ids else "catalog_skipped"
        result["apis"].append({
            "name": key,
            "model": model,
            "enabled": entry.get("enabled"),
            "reason": entry.get("enabled_reason"),
        })

    if ids:
        data["_catalog"] = {
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": provider_id,
            "ids": sorted(ids),
        }

    metadata_updated = 0
    metadata_error = None
    try:
        metadata_updated = sync_metadata_in_data(data, provider)
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, ValueError) as exc:
        metadata_error = str(exc)
        # Still apply fallbacks without cost map.
        sync_metadata_in_data(data, provider, cost_map={})

    result["metadata_updated"] = metadata_updated
    if metadata_error:
        result["metadata_error"] = metadata_error

    save_api_models(data)
    return result
