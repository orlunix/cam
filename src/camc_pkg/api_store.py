"""Load/save ~/.cam/api-models.json — API profiles and providers."""

import json
import os
import socket
import time
import urllib.error
import urllib.request

from camc_pkg import CAM_DIR, LOGS_DIR
from camc_pkg.api_metadata import sync_metadata_in_data

API_MODELS_FILE = os.path.join(CAM_DIR, "api-models.json")
PROXY_RUNS_FILE = os.path.join(CAM_DIR, "proxy-runs.json")
TOKEN_ENV_FILE = os.path.join(CAM_DIR, "token.env")

DEFAULT_PROVIDER = "inference-hub"
IHUB_BASE = "https://inference-api.nvidia.com/v1"

# Provider templates (copy into providers/apis — not active until referenced).
PROVIDER_TEMPLATES = {
    "openai-chat-gateway": {
        "display_name": "OpenAI Chat Completions gateway",
        "auth_key": "openai_gateway",
        "env_names": ["OPENAI_API_KEY", "OPENAI_GATEWAY_API_KEY"],
        "base_url": "https://llm.example.com/v1",
        "upstream_protocol": "openai_chat_completions",
        "translator": "embedded",
        "catalog_path": "/models",
        "endpoints": {
            "openai_chat_completions": "/chat/completions",
        },
    },
    "anthropic-direct": {
        "display_name": "Native Anthropic Messages API",
        "auth_key": "anthropic",
        "env_names": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
        "base_url": "https://api.anthropic.com",
        "upstream_protocol": "anthropic_messages",
        "translator": "direct",
        "catalog_path": "/v1/models",
        "endpoints": {
            "anthropic_messages": "/v1/messages",
        },
    },
    "cc-switch": {
        "display_name": "CC Switch local routing",
        "auth_key": "cc_switch",
        "env_names": ["CC_SWITCH_API_KEY", "OPENAI_API_KEY"],
        "base_url": "http://127.0.0.1:15721",
        "client_base_url": "http://127.0.0.1:15721",
        "upstream_protocol": "anthropic_messages",
        "translator": "external",
        "external_translator": True,
        "catalog_path": "",
        "endpoints": {
            "anthropic_messages": "",
        },
    },
}

CURATED_APIS = [
    ("glm-5.1", "nvidia/zai-org/eccn-glm-5.1", ["glm", "glm51"]),
    ("deepseek-v4-pro", "nvidia/deepseek-ai/eccn-deepseek-v4-pro", ["deepseek", "ds-v4"]),
    ("kimi-k2.6", "nvidia/moonshotai/eccn-kimi-k2.6", ["kimi", "k2.6"]),
    ("minimax-m2.7", "nvidia/minimaxai/eccn-minimax-m2.7", ["minimax", "m2.7"]),
    ("qwen3-5-397b", "nvidia/qwen/eccn-qwen3-5-397b-a17b", ["qwen397", "qwen3.5"]),
    ("nemotron-3-ultra", "nvidia/nvidia/eccn-nemotron-3-ultra", ["nemotron", "nemo-ultra"]),
]

# Tools that may have a per-tool default API (empty = normal OAuth/login).
DEFAULT_API_TOOLS = ("claude", "codex")


def _default_seed():
    apis = {}
    for key, model, aliases in CURATED_APIS:
        apis[key] = {
            "provider": DEFAULT_PROVIDER,
            "model": model,
            "enabled": False,
            "aliases": list(aliases),
        }
    return {
        "version": 1,
        "default": "glm-5.1",
        "default_provider": DEFAULT_PROVIDER,
        "providers": {
            DEFAULT_PROVIDER: {
                "display_name": "NVIDIA Inference Hub",
                "auth_key": "inference_hub",
                "env_names": [
                    "INFERENCE_HUB_TOKEN",
                    "INFERENCE_HUB_API_KEY",
                    "INFERENCE_API_KEY",
                ],
                "base_url": IHUB_BASE,
                "upstream_protocol": "openai_chat_completions",
                "translator": "embedded",
                "catalog_path": "/models",
                "endpoints": {
                    "openai_chat_completions": "/chat/completions",
                    "anthropic_messages": "/messages",
                },
            },
        },
        "apis": apis,
        "_templates": dict(PROVIDER_TEMPLATES),
        "_aliases": {},
        "_catalog": {},
    }


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


def ensure_ready():
    """Create seed file if missing; return data dict."""
    if os.path.isfile(API_MODELS_FILE):
        data = load_api_models()
        rebuild_aliases(data)
        return data
    data = _default_seed()
    save_api_models(data)
    return data


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
    return _normalize_default_name(defaults.get(tool))


def list_tool_default_apis(data):
    """Return per-tool default status rows for display/CLI."""
    rows = []
    for tool in DEFAULT_API_TOOLS:
        name = resolve_tool_default_api(data, tool)
        if not name:
            rows.append({
                "tool": tool,
                "api": None,
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
            "mode": "api",
            "enabled": enabled,
            "reason": entry.get("enabled_reason"),
        })
    return rows


def set_tool_default_api(data, tool, api_name):
    """Set defaults.<tool> to api_name; sync legacy top-level default for Claude."""
    if tool not in DEFAULT_API_TOOLS:
        raise ValueError("unsupported tool %r for default API (use: claude, codex)" % tool)
    key = resolve_api_name(data, api_name)
    defaults = data.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        data["defaults"] = defaults
    defaults[tool] = key
    if tool == "claude":
        data["default"] = key
    save_api_models(data)
    return key


def clear_tool_default_api(data, tool):
    """Clear defaults.<tool>; empty tool default means normal login."""
    if tool not in DEFAULT_API_TOOLS:
        raise ValueError("unsupported tool %r for default API (use: claude, codex)" % tool)
    defaults = data.get("defaults")
    if isinstance(defaults, dict) and tool in defaults:
        defaults.pop(tool)
    if tool == "claude":
        data.pop("default", None)
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


def upstream_chat_url(data, api_entry, provider):
    """Legacy name: upstream URL for openai_chat_completions."""
    from camc_pkg.api_routing import PROTO_OPENAI_CHAT, provider_endpoint_url
    if api_entry.get("url"):
        return str(api_entry["url"])
    return provider_endpoint_url(provider, PROTO_OPENAI_CHAT)


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
            "provider": entry.get("provider", data.get("default_provider")),
            "enabled": enabled,
            "aliases": entry.get("aliases") or [],
        })
    return rows


def _fetch_ihub_model_ids(token, timeout=5.0):
    """Backward-compat wrapper for IHUB catalog."""
    provider = {
        "base_url": IHUB_BASE,
        "catalog_path": "/models",
    }
    return fetch_model_catalog(provider, token, timeout=timeout)


def check_provider(data, token_resolver):
    """Ping provider catalog, refresh enabled flags and _catalog."""
    from camc_pkg.api_token import resolve_token

    provider_id = data.get("default_provider") or DEFAULT_PROVIDER
    provider = get_provider(data, provider_id)
    auth_key = provider.get("auth_key") or "inference_hub"
    token, source = resolve_token(auth_key, provider.get("env_names") or [], cli_token=None)

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
