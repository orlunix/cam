"""API routing: tool protocol, upstream protocol, and translator mode selection.

Three deployment patterns (docs/api-routing.md):
embedded, direct, external.
"""

# Wire protocol ids (keys in providers.*.endpoints)
PROTO_ANTHROPIC_MESSAGES = "anthropic_messages"
PROTO_OPENAI_CHAT = "openai_chat_completions"
PROTO_OPENAI_RESPONSES = "openai_responses"

# How camc reaches the model
TRANSLATOR_EMBEDDED = "embedded"
TRANSLATOR_DIRECT = "direct"
TRANSLATOR_EXTERNAL = "external"

TOOL_PROTOCOL = {
    "claude": PROTO_ANTHROPIC_MESSAGES,
    "codex": PROTO_OPENAI_RESPONSES,
    "cursor": PROTO_ANTHROPIC_MESSAGES,
}

# Embedded stdlib proxy routes (tool_proto, upstream_proto) -> route metadata
PROXY_ROUTES = {
    (PROTO_ANTHROPIC_MESSAGES, PROTO_OPENAI_CHAT): {
        "route": "completions_to_messages",
        "port": 18324,
    },
    # Future codex --api:
    # (PROTO_OPENAI_RESPONSES, PROTO_OPENAI_CHAT): {
    #     "route": "completions_to_responses",
    #     "port": 18325,
    # },
}


def provider_endpoint_url(provider, protocol):
    """Join provider.base_url + providers.endpoints[protocol]."""
    base = str(provider.get("base_url") or "").rstrip("/")
    if not base:
        raise ValueError("provider missing base_url")
    endpoints = provider.get("endpoints") or {}
    path = endpoints.get(protocol)
    if path is None:
        raise ValueError(
            "provider %r has no endpoint for protocol %r"
            % (provider.get("display_name") or "?", protocol)
        )
    path = str(path)
    if path == "":
        return base
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def resolve_upstream_protocol(provider, api_entry):
    """Which upstream wire format this API uses."""
    proto = api_entry.get("upstream_protocol") or provider.get("upstream_protocol")
    if proto:
        return str(proto)
    # Backward compat: inference-hub and most gateways default to chat completions.
    return PROTO_OPENAI_CHAT


def resolve_translator(provider, api_entry, tool_proto, upstream_proto):
    """Pick embedded proxy, direct passthrough, or external translator."""
    explicit = api_entry.get("translator") or provider.get("translator")
    if explicit:
        return str(explicit)

    if tool_proto == upstream_proto:
        if provider.get("external_translator"):
            return TRANSLATOR_EXTERNAL
        return TRANSLATOR_DIRECT

    if PROXY_ROUTES.get((tool_proto, upstream_proto)):
        return TRANSLATOR_EMBEDDED

    raise ValueError(
        "no translator for tool protocol %s -> upstream %s "
        "(set apis.*.translator or providers.*.translator)"
        % (tool_proto, upstream_proto)
    )


def resolve_upstream_url(provider, api_entry, upstream_proto):
    """Full upstream URL for embedded proxy or direct Anthropic."""
    if api_entry.get("url"):
        return str(api_entry["url"])
    return provider_endpoint_url(provider, upstream_proto)


def resolve_client_base_url(provider, api_entry):
    """URL Claude/Codex should call (may differ from upstream for external translators)."""
    if api_entry.get("client_url"):
        return str(api_entry["client_url"]).rstrip("/")
    client_base = provider.get("client_base_url") or provider.get("base_url")
    if not client_base:
        raise ValueError("provider missing client_base_url / base_url")
    return str(client_base).rstrip("/")


def build_routing_plan(tool, provider, api_entry, api_key):
    """Compute translator mode, URLs, and proxy route (if any)."""
    tool_proto = TOOL_PROTOCOL.get(tool, PROTO_ANTHROPIC_MESSAGES)
    upstream_proto = resolve_upstream_protocol(provider, api_entry)
    translator = resolve_translator(provider, api_entry, tool_proto, upstream_proto)
    upstream_url = resolve_upstream_url(provider, api_entry, upstream_proto)

    plan = {
        "translator": translator,
        "tool_protocol": tool_proto,
        "upstream_protocol": upstream_proto,
        "upstream_url": upstream_url,
        "mode": translator,
        "route": None,
        "proxy_port": None,
        "local_base_url": upstream_url,
    }

    if translator == TRANSLATOR_EMBEDDED:
        route_info = PROXY_ROUTES.get((tool_proto, upstream_proto))
        if not route_info:
            raise ValueError(
                "embedded translator not configured for %s -> %s"
                % (tool_proto, upstream_proto)
            )
        plan["mode"] = "proxy"  # cli.ProxyManager legacy field
        plan["route"] = route_info["route"]
        plan["proxy_port"] = route_info["port"]
        plan["local_base_url"] = "http://127.0.0.1:%d" % route_info["port"]
    elif translator == TRANSLATOR_EXTERNAL:
        plan["local_base_url"] = resolve_client_base_url(provider, api_entry)
    elif translator == TRANSLATOR_DIRECT:
        plan["local_base_url"] = upstream_url
    else:
        raise ValueError("unknown translator mode %r" % translator)

    return plan
