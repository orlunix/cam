"""RunResolver: tool + api -> direct, embedded proxy, or external translator plan."""

import json
import os

from camc_pkg import CAM_DIR
from camc_pkg.api_routing import (
    TRANSLATOR_DIRECT,
    TRANSLATOR_EMBEDDED,
    TRANSLATOR_EXTERNAL,
    TOOL_PROTOCOL,
    build_routing_plan,
)
from camc_pkg.api_store import (
    CURATED_APIS,
    ensure_ready,
    get_api_entry,
    get_provider,
    resolve_api_name,
)

# Supported today: Claude + Codex with curated Inference Hub models.
API_SUPPORTED_TOOLS = frozenset(["claude", "codex"])
CURATED_API_KEYS = frozenset(key for key, _model, _aliases in CURATED_APIS)

# One shared Claude Code config for all --api runs (onboarding once).
CLAUDE_API_CONFIG_DIR = os.path.join(CAM_DIR, "claude-api")
# Isolated Codex home — does not touch ~/.codex/ OAuth login.
CODEX_API_CONFIG_DIR = os.path.join(CAM_DIR, "codex-api")
CODEX_API_PROVIDER = "camc-ihub"
CODEX_API_ENV_KEY = "CAMC_CODEX_API_KEY"


def supported_api_models_text():
    """Comma-separated curated API keys for error messages."""
    return ", ".join(sorted(CURATED_API_KEYS))


def is_api_runnable(api_key, api_entry):
    """Whether camc run --api may use this API profile today."""
    if api_entry.get("allow_run") is True:
        return True
    return api_key in CURATED_API_KEYS


def validate_api_run(tool, api_key, api_entry=None):
    """Raise ValueError if tool/model is not supported for --api today."""
    if tool not in API_SUPPORTED_TOOLS:
        raise ValueError(
            "--api is not supported for tool %r. "
            "Supported today: camc run -t claude|codex --api NAME. "
            "Cursor: use normal login without --api (not implemented). "
            "Supported models: %s."
            % (tool, supported_api_models_text())
        )
    entry = api_entry or {}
    if not is_api_runnable(api_key, entry):
        raise ValueError(
            "API %r is not supported for --api. "
            "Only curated Inference Hub models are supported: %s. "
            "Custom providers: set allow_run=true in api-models.json (see docs/api-routing.md). "
            "Run: camc api check"
            % (api_key, supported_api_models_text())
        )


def ensure_claude_api_config_dir():
    """Seed ~/.cam/claude-api/ so first-run dialogs only happen once."""
    os.makedirs(CLAUDE_API_CONFIG_DIR, exist_ok=True)
    settings_path = os.path.join(CLAUDE_API_CONFIG_DIR, "settings.json")
    claude_path = os.path.join(CLAUDE_API_CONFIG_DIR, ".claude.json")
    if os.path.isfile(settings_path) and os.path.isfile(claude_path):
        return CLAUDE_API_CONFIG_DIR

    settings = {"theme": "dark"}
    claude_json = {
        "numStartups": 1,
        "customApiKeyResponses": {
            "approved": ["sk-camc-local"],
            "rejected": [],
        },
        "tipsHistory": {"new-user-warmup": 1},
    }
    # Reuse any existing onboarded claude-* profile as donor.
    try:
        for name in sorted(os.listdir(CAM_DIR)):
            if not name.startswith("claude-"):
                continue
            donor = os.path.join(CAM_DIR, name)
            try:
                with open(os.path.join(donor, "settings.json"), "r") as f:
                    settings = json.load(f)
            except (IOError, ValueError):
                pass
            try:
                with open(os.path.join(donor, ".claude.json"), "r") as f:
                    raw = json.load(f)
                claude_json["numStartups"] = max(int(raw.get("numStartups") or 1), 1)
                if isinstance(raw.get("customApiKeyResponses"), dict):
                    approved = list(raw["customApiKeyResponses"].get("approved") or [])
                    if "sk-camc-local" not in approved:
                        approved.append("sk-camc-local")
                    claude_json["customApiKeyResponses"] = {
                        "approved": approved,
                        "rejected": list(raw["customApiKeyResponses"].get("rejected") or []),
                    }
                if isinstance(raw.get("tipsHistory"), dict):
                    claude_json["tipsHistory"] = dict(raw["tipsHistory"])
            except (IOError, ValueError):
                pass
            break
    except OSError:
        pass

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    with open(claude_path, "w") as f:
        json.dump(claude_json, f, indent=2)
        f.write("\n")
    return CLAUDE_API_CONFIG_DIR


def ensure_codex_api_config_dir(base_url, api_name):
    """Seed ~/.cam/codex-api/ for Codex --api (isolated from ~/.codex/)."""
    from camc_pkg.api_metadata import resolve_api_metadata, write_codex_model_catalog

    os.makedirs(CODEX_API_CONFIG_DIR, exist_ok=True)
    base_url = str(base_url or "").rstrip("/")
    if base_url and not base_url.endswith("/v1"):
        base_url = base_url + "/v1"
    api_name = str(api_name or "api")
    catalog_path = os.path.join(CODEX_API_CONFIG_DIR, "camc-model-catalog.json")
    metadata = resolve_api_metadata(api_name)
    write_codex_model_catalog(catalog_path, api_name, metadata)
    config_path = os.path.join(CODEX_API_CONFIG_DIR, "config.toml")
    content = (
        'model = "%s"\n'
        'model_provider = "%s"\n'
        'model_catalog_json = "%s"\n'
        '\n'
        '[model_providers.%s]\n'
        'name = "CAM Inference Hub Proxy"\n'
        'base_url = "%s"\n'
        'env_key = "%s"\n'
        'wire_api = "responses"\n'
    ) % (
        api_name,
        CODEX_API_PROVIDER,
        catalog_path,
        CODEX_API_PROVIDER,
        base_url,
        CODEX_API_ENV_KEY,
    )
    try:
        prev = ""
        if os.path.isfile(config_path):
            with open(config_path, "r") as f:
                prev = f.read()
        if prev != content:
            with open(config_path, "w") as f:
                f.write(content)
    except IOError:
        pass
    return CODEX_API_CONFIG_DIR


def resolve_run_plan(tool, api_name, no_api_proxy=False, proxy_debug=False):
    """Return plan dict for camc run --api."""
    data = ensure_ready()
    key = resolve_api_name(data, api_name)
    api_entry = dict((data.get("apis") or {}).get(key) or {})
    validate_api_run(tool, key, api_entry)
    if api_entry.get("enabled") is False:
        reason = api_entry.get("enabled_reason") or "disabled"
        raise ValueError("API %r is disabled (%s)" % (key, reason))

    provider_id = api_entry.get("provider") or data.get("default_provider")
    provider = get_provider(data, provider_id)
    routing = build_routing_plan(tool, provider, api_entry, key)
    client_model = key

    if routing["translator"] == TRANSLATOR_EMBEDDED and no_api_proxy:
        raise ValueError(
            "API %r requires embedded proxy route %s but --no-api-proxy was set"
            % (key, routing.get("route"))
        )

    plan = {
        "name": key,
        "tool": tool,
        "provider": provider_id,
        "model": api_entry.get("model"),
        "mode": routing["mode"],
        "translator": routing["translator"],
        "route": routing.get("route"),
        "upstream_url": routing["upstream_url"],
        "local_base_url": routing["local_base_url"],
        "upstream_protocol": routing["upstream_protocol"],
        "tool_protocol": routing["tool_protocol"],
        "upstream_proto": routing["upstream_protocol"],
        "auth_key": provider.get("auth_key") or "inference_hub",
        "env_names": provider.get("env_names") or [],
        "proxy_debug": proxy_debug,
        "proxy_port": routing.get("proxy_port"),
        "env": {},
    }

    plan["env"] = _build_env_overrides(tool, plan, provider, api_entry, client_model)
    return plan


def _build_env_overrides(tool, plan, provider, api_entry, client_model):
    env = {}
    base = plan["local_base_url"].rstrip("/")
    translator = plan.get("translator") or plan.get("mode")

    if tool == "claude":
        env["ANTHROPIC_BASE_URL"] = base
        env["ANTHROPIC_MODEL"] = client_model
        env["CLAUDE_CONFIG_DIR"] = ensure_claude_api_config_dir()
        if translator == TRANSLATOR_EMBEDDED:
            env["ANTHROPIC_API_KEY"] = str(
                api_entry.get("client_api_key")
                or provider.get("client_api_key")
                or "sk-camc-local"
            )
            env["ANTHROPIC_AUTH_TOKEN"] = ""
        elif translator == TRANSLATOR_EXTERNAL:
            env["ANTHROPIC_API_KEY"] = str(
                api_entry.get("client_api_key")
                or provider.get("client_api_key")
                or "sk-camc-local"
            )
            env["ANTHROPIC_AUTH_TOKEN"] = ""
        elif translator == TRANSLATOR_DIRECT:
            env["ANTHROPIC_AUTH_TOKEN"] = ""
            env["_API_USE_RESOLVED_TOKEN"] = "1"
        from camc_pkg.api_metadata import claude_context_env_overrides
        env.update(claude_context_env_overrides(client_model, api_entry.get("metadata")))
        return env

    if tool == "codex":
        if translator == TRANSLATOR_EMBEDDED:
            env[CODEX_API_ENV_KEY] = str(
                api_entry.get("client_api_key")
                or provider.get("client_api_key")
                or "sk-camc-local"
            )
        elif translator == TRANSLATOR_DIRECT:
            env["_API_USE_RESOLVED_TOKEN"] = "1"
            env[CODEX_API_ENV_KEY] = ""
        # CODEX_HOME + config.toml finalized after proxy port is known (cmd_run).
        return env

    return env
