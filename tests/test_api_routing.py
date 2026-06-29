"""Tests for API routing (embedded / direct / external translator modes)."""

import pytest

from camc_pkg.api_routing import (
    PROTO_ANTHROPIC_MESSAGES,
    PROTO_OPENAI_CHAT,
    TRANSLATOR_DIRECT,
    TRANSLATOR_EMBEDDED,
    TRANSLATOR_EXTERNAL,
    build_routing_plan,
    resolve_translator,
)


IHUB_PROVIDER = {
    "display_name": "IHUB",
    "base_url": "https://inference-api.nvidia.com/v1",
    "upstream_protocol": "openai_chat_completions",
    "translator": "embedded",
    "endpoints": {
        "openai_chat_completions": "/chat/completions",
        "anthropic_messages": "/messages",
    },
}

ANTHROPIC_PROVIDER = {
    "display_name": "Anthropic",
    "base_url": "https://api.anthropic.com",
    "upstream_protocol": "anthropic_messages",
    "translator": "direct",
    "endpoints": {
        "anthropic_messages": "/v1/messages",
    },
}

CC_SWITCH_PROVIDER = {
    "display_name": "CC Switch",
    "base_url": "http://127.0.0.1:15721",
    "client_base_url": "http://127.0.0.1:15721",
    "upstream_protocol": "anthropic_messages",
    "translator": "external",
    "external_translator": True,
    "endpoints": {"anthropic_messages": ""},
}


class TestApiRouting:
    def test_ihub_embedded_proxy(self):
        entry = {"provider": "inference-hub", "model": "nvidia/zai-org/eccn-glm-5.1"}
        plan = build_routing_plan("claude", IHUB_PROVIDER, entry, "glm-5.1")
        assert plan["translator"] == TRANSLATOR_EMBEDDED
        assert plan["mode"] == "proxy"
        assert plan["route"] == "completions_to_messages"
        assert plan["upstream_url"].endswith("/chat/completions")
        assert plan["local_base_url"].startswith("http://127.0.0.1:")

    def test_ihub_embedded_proxy_codex(self):
        entry = {"provider": "inference-hub", "model": "nvidia/zai-org/eccn-glm-5.1"}
        plan = build_routing_plan("codex", IHUB_PROVIDER, entry, "glm-5.1")
        assert plan["translator"] == TRANSLATOR_EMBEDDED
        assert plan["mode"] == "proxy"
        assert plan["route"] == "completions_to_responses"
        assert plan["proxy_port"] == 18325
        assert plan["local_base_url"].startswith("http://127.0.0.1:")

    def test_anthropic_direct(self):
        entry = {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}
        plan = build_routing_plan("claude", ANTHROPIC_PROVIDER, entry, "sonnet")
        assert plan["translator"] == TRANSLATOR_DIRECT
        assert plan["route"] is None
        assert plan["local_base_url"].endswith("/v1/messages")
        assert plan["upstream_protocol"] == PROTO_ANTHROPIC_MESSAGES

    def test_cc_switch_external(self):
        entry = {
            "provider": "cc-switch",
            "model": "glm-5.1",
            "client_url": "http://127.0.0.1:15721",
        }
        plan = build_routing_plan("claude", CC_SWITCH_PROVIDER, entry, "glm")
        assert plan["translator"] == TRANSLATOR_EXTERNAL
        assert plan["local_base_url"] == "http://127.0.0.1:15721"
        assert plan["route"] is None

    def test_api_url_override(self):
        entry = {
            "url": "https://gateway.example/v1/chat/completions",
            "model": "my-model",
        }
        plan = build_routing_plan("claude", IHUB_PROVIDER, entry, "my-model")
        assert plan["upstream_url"] == "https://gateway.example/v1/chat/completions"

    def test_resolve_translator_explicit(self):
        provider = {"translator": "external", "external_translator": True}
        entry = {"translator": "direct"}
        assert resolve_translator(
            provider, entry, PROTO_ANTHROPIC_MESSAGES, PROTO_ANTHROPIC_MESSAGES
        ) == TRANSLATOR_DIRECT

    def test_missing_route_raises(self):
        provider = {
            "base_url": "https://x/v1",
            "upstream_protocol": "openai_responses",
            "endpoints": {"openai_responses": "/responses"},
        }
        entry = {"model": "x"}
        with pytest.raises(ValueError, match="no translator"):
            build_routing_plan("claude", provider, entry, "x")
