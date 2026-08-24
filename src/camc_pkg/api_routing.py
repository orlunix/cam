"""API routing from JSON-owned tool endpoint declarations."""

from camc_pkg.api_metadata import normalize_available_endpoints


# Wire protocol ids used internally by adapter declarations.
PROTO_ANTHROPIC_MESSAGES = "anthropic_messages"
PROTO_OPENAI_CHAT = "openai_chat_completions"
PROTO_OPENAI_RESPONSES = "openai_responses"

TOOL_PROTOCOL = {
    "claude": PROTO_ANTHROPIC_MESSAGES,
    "codex": PROTO_OPENAI_RESPONSES,
    "cursor": PROTO_ANTHROPIC_MESSAGES,
}

API_ENDPOINT_ALIASES = {
    "messages": PROTO_ANTHROPIC_MESSAGES,
    "anthropic_messages": PROTO_ANTHROPIC_MESSAGES,
    "completions": PROTO_OPENAI_CHAT,
    "openai_chat_completions": PROTO_OPENAI_CHAT,
    "responses": PROTO_OPENAI_RESPONSES,
    "openai_responses": PROTO_OPENAI_RESPONSES,
}


def json_tool_api_shape(tool, endpoints, suffix=""):
    """Choose the first JSON-declared endpoint supported by the tool."""
    for endpoint in normalize_available_endpoints(endpoints):
        protocol = API_ENDPOINT_ALIASES.get(endpoint)
        if protocol:
            suffix = str(suffix or "").strip()
            if suffix and not suffix.startswith("/"):
                suffix = "/" + suffix
            return protocol, suffix
    raise ValueError("API tool %r has no supported endpoint" % tool)


def apply_base_url_suffix(base_url, suffix):
    """Append an adapter suffix once, without creating duplicate slashes."""
    base = str(base_url or "").rstrip("/")
    suffix = str(suffix or "").strip()
    if not suffix:
        return base
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    if base.endswith(suffix):
        return base
    return base + suffix


def _endpoint_name(protocol):
    return {
        PROTO_ANTHROPIC_MESSAGES: "messages",
        PROTO_OPENAI_CHAT: "completions",
        PROTO_OPENAI_RESPONSES: "responses",
    }.get(protocol, str(protocol))


def require_native_endpoint(api_entry, tool_proto):
    """Require the selected tool's native endpoint capability."""
    protocol = tool_proto or PROTO_OPENAI_CHAT
    available = normalize_available_endpoints(
        api_entry.get("available_endpoints"))
    required = _endpoint_name(protocol)
    if required not in available:
        raise ValueError(
            "API profile does not advertise native %s endpoint" % required)
    return protocol


def resolve_client_base_url(provider):
    """Return the provider's one canonical base URL."""
    base = str((provider or {}).get("base_url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("provider missing base_url")
    return base


def build_routing_plan(tool, provider, api_entry, tool_endpoints=None,
                       base_url_suffix=""):
    """Compute an upstream-native plan from JSON tool declarations."""
    if tool_endpoints is None:
        tool_endpoints = {
            "claude": ["messages"], "codex": ["responses"],
        }.get(tool, [])
        base_url_suffix = {"claude": "", "codex": "/v1"}.get(tool, "")
    tool_proto, base_url_suffix = json_tool_api_shape(
        tool, tool_endpoints, base_url_suffix)
    require_native_endpoint(api_entry, tool_proto)
    base_url = resolve_client_base_url(provider)
    local_base_url = apply_base_url_suffix(base_url, base_url_suffix)
    return {
        "tool_protocol": tool_proto,
        "api_endpoint": tool_proto,
        "base_url_suffix": base_url_suffix,
        "mode": "proxy",
        "local_base_url": local_base_url,
    }
