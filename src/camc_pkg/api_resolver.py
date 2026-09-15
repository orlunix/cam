"""RunResolver: tool + API profile -> direct native endpoint plan."""

import json
import os
import shutil
import time

from camc_pkg import CAM_DIR
from camc_pkg.api_routing import build_routing_plan
from camc_pkg.api_store import (
    CURATED_APIS,
    check_provider,
    ensure_ready,
    get_api_entry,
    get_provider,
    resolve_api_name,
    tool_api_config,
    tool_supports_api,
)

# API profiles are supported for Claude and Codex. Cursor remains login-only.
API_SUPPORTED_TOOLS = frozenset(["claude", "codex"])
CURATED_API_KEYS = frozenset(key for key, _model, _aliases in CURATED_APIS)

# One shared Claude Code config for all --api runs (onboarding once).
CLAUDE_API_CONFIG_DIR = os.path.join(CAM_DIR, "claude-api")
# Isolated Codex home — does not touch ~/.codex/ OAuth login or ~/.cam state.
CODEX_API_CONFIG_DIR = os.path.expanduser("~/.codex-api")
CODEX_LOGIN_SKILLS_DIR = os.path.expanduser("~/.codex/skills")
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
            "--api is currently only supported for Claude and Codex; tool %r is not "
            "supported for API profiles. "
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


def _codex_default_home():
    """Return the normal Codex home used only as a read-only donor."""
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.expanduser("~/.codex")


def _link_codex_defaults(config_dir, default_home=None):
    """Link skills and named permission profiles into one generated home."""
    default_home = os.path.abspath(os.path.expanduser(
        default_home or _codex_default_home()))
    config_dir = os.path.abspath(os.path.expanduser(config_dir))
    if os.path.realpath(default_home) == os.path.realpath(config_dir):
        return {}
    sources = [("skills", os.path.join(default_home, "skills"))]
    try:
        names = sorted(os.listdir(default_home))
    except OSError:
        names = []
    for name in names:
        if name.endswith(".config.toml"):
            sources.append((name, os.path.join(default_home, name)))

    results = {}
    for name, source in sources:
        target = os.path.join(config_dir, name)
        if not (os.path.isdir(source) or os.path.isfile(source)):
            results[name] = "missing_source"
            continue
        if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
            results[name] = "linked"
            continue
        if os.path.lexists(target):
            results[name] = "preserved"
            continue
        try:
            os.symlink(source, target, target_is_directory=os.path.isdir(source))
            results[name] = "linked"
        except OSError:
            results[name] = "error"
    return results


def _ensure_codex_skills_link(config_dir=None):
    """Reuse normal Codex skills from the isolated API home, idempotently."""
    source = CODEX_LOGIN_SKILLS_DIR
    config_dir = config_dir or CODEX_API_CONFIG_DIR
    target = os.path.join(config_dir, "skills")
    if not os.path.isdir(source):
        return "missing_source"
    if os.path.islink(target):
        if os.path.realpath(target) == os.path.realpath(source):
            return "linked"
        try:
            os.unlink(target)
        except OSError:
            return "error"
    elif os.path.lexists(target):
        # Never delete a non-empty API-home skills directory owned by a user.
        if not os.path.isdir(target) or os.listdir(target):
            return "preserved"
        try:
            os.rmdir(target)
        except OSError:
            return "preserved"
    try:
        os.makedirs(config_dir, exist_ok=True)
        os.symlink(source, target, target_is_directory=True)
        return "linked"
    except OSError:
        return "error"


def _codex_owner_dir(owner_id=""):
    if not owner_id:
        return CODEX_API_CONFIG_DIR
    safe = "".join(c for c in str(owner_id) if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("invalid Codex API config owner id")
    return os.path.join(CODEX_API_CONFIG_DIR, safe)


def ensure_codex_api_config_dir(base_url, api_name, require_endpoint="responses",
                                model_id=None, owner_id=""):
    """Seed one agent-owned Codex API home without touching normal login."""
    from camc_pkg.api_metadata import resolve_api_metadata, write_codex_model_catalog

    config_dir = _codex_owner_dir(owner_id)
    os.makedirs(config_dir, exist_ok=True)
    _link_codex_defaults(config_dir)
    base_url = str(base_url or "").rstrip("/")
    api_name = str(api_name or "api")
    model_id = str(model_id or api_name)
    catalog_path = os.path.join(config_dir, "camc-model-catalog.json")
    metadata = resolve_api_metadata(api_name)
    write_codex_model_catalog(
        catalog_path, api_name, metadata, require_endpoint=require_endpoint,
        model_id=model_id)
    config_path = os.path.join(config_dir, "config.toml")
    content = (
        'model = "%s"\n'
        'model_provider = "%s"\n'
        'model_catalog_json = "%s"\n'
        '\n'
        '[model_providers.%s]\n'
        'name = "CAM Inference Hub"\n'
        'base_url = "%s"\n'
        'env_key = "%s"\n'
        'wire_api = "responses"\n'
    ) % (
        model_id,
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
    return config_dir


def remove_codex_api_config_dir(owner_id):
    """Remove only one generated per-agent Codex home."""
    if not owner_id:
        return False
    path = _codex_owner_dir(owner_id)
    root = os.path.realpath(CODEX_API_CONFIG_DIR)
    target = os.path.realpath(path)
    if not target.startswith(root + os.sep):
        return False
    if not os.path.isdir(target):
        return False
    for attempt in range(10):
        try:
            shutil.rmtree(target)
            return True
        except OSError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))
    return False


def resolve_run_plan(tool, api_name):
    """Return a direct native plan for ``camc run --api``."""
    data = ensure_ready()
    key = resolve_api_name(data, api_name)
    api_entry = dict((data.get("apis") or {}).get(key) or {})
    for alias_tool, alias in (api_entry.get("client_models") or {}).items():
        if api_name == alias and alias_tool != tool:
            raise ValueError("API %r is not supported for tool %r" %
                             (api_name, tool))
    validate_api_run(tool, key, api_entry)
    if api_entry.get("enabled") is False:
        try:
            check_provider(data)
        except Exception:
            pass
        api_entry = dict((data.get("apis") or {}).get(key) or {})
        if api_entry.get("enabled") is False:
            reason = api_entry.get("enabled_reason") or "disabled"
            raise ValueError("API %r is disabled (%s)" % (key, reason))

    provider_id = api_entry.get("provider")
    if not provider_id:
        raise ValueError("API %r has no provider" % key)
    provider = get_provider(data, provider_id)
    if not tool_supports_api(data, tool, key):
        raise ValueError("API %r is not supported for tool %r" % (key, tool))
    tool_endpoints, base_url_suffix = tool_api_config(data, tool)
    routing = build_routing_plan(
        tool, provider, api_entry, tool_endpoints=tool_endpoints,
        base_url_suffix=base_url_suffix)
    upstream_model = str(api_entry.get("model") or key)
    client_model = str((api_entry.get("client_models") or {}).get(tool)
                       or upstream_model)

    plan = {
        "name": key,
        "tool": tool,
        "provider": provider_id,
        "model": api_entry.get("model"),
        "client_model": client_model,
        "proxy_required": True,
        "mode": routing["mode"],
        "local_base_url": routing["local_base_url"],
        "tool_protocol": routing["tool_protocol"],
        "api_endpoint": routing.get("api_endpoint"),
        "available_endpoints": list(api_entry.get("available_endpoints") or []),
        "base_url_suffix": routing.get("base_url_suffix") or "",
        "upstream_base_url": str(provider.get("base_url") or "").rstrip("/"),
        "auth_key": provider.get("auth_key") or "inference_hub",
        "env_names": provider.get("env_names") or [],
        "token_file": provider.get("token_file"),
        "token_key": provider.get("token_key"),
        "reasoning_mapping": ((api_entry.get("reasoning") or {}).get(
            "mapping") or {}).get("codex") or {},
        "env": {},
    }

    plan["env"] = _build_env_overrides(tool, plan, provider, api_entry, client_model)
    return plan


def _build_env_overrides(tool, plan, provider, api_entry, client_model):
    env = {}
    base = plan["local_base_url"].rstrip("/")

    if tool == "claude":
        env["ANTHROPIC_BASE_URL"] = base
        env["ANTHROPIC_MODEL"] = client_model
        env["CLAUDE_CONFIG_DIR"] = ensure_claude_api_config_dir()
        env["ANTHROPIC_AUTH_TOKEN"] = ""
        env["_API_USE_RESOLVED_TOKEN"] = "1"
        from camc_pkg.api_metadata import claude_context_env_overrides
        env.update(claude_context_env_overrides(plan.get("name") or client_model,
                                                api_entry.get("metadata")))
        return env

    if tool == "codex":
        env["_API_USE_RESOLVED_TOKEN"] = "1"
        env[CODEX_API_ENV_KEY] = ""
        return env

    return env


def apply_resolved_token(tool, env, token):
    """Apply a resolved provider token using the adapter's auth variable."""
    overrides = dict(env or {})
    overrides.pop("_API_USE_RESOLVED_TOKEN", None)
    if tool == "claude":
        # ACME Inference Hub expects the bearer token header used by
        # ANTHROPIC_AUTH_TOKEN; ANTHROPIC_API_KEY selects x-api-key instead.
        overrides.pop("ANTHROPIC_API_KEY", None)
        overrides["ANTHROPIC_AUTH_TOKEN"] = token
    elif tool == "codex":
        overrides[CODEX_API_ENV_KEY] = token
    return overrides
