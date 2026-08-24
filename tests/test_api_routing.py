"""Tests for JSON-owned native endpoint routing."""

import pytest

from camc_pkg.api_routing import (
    PROTO_ANTHROPIC_MESSAGES,
    PROTO_OPENAI_RESPONSES,
    json_tool_api_shape,
    apply_base_url_suffix,
    build_routing_plan,
    require_native_endpoint,
)


def _provider():
    return {
        "display_name": "IHUB",
        "base_url": "https://inference-api.nvidia.com",
    }


def test_claude_uses_native_messages_and_one_provider_base_url():
    plan = build_routing_plan(
        "claude", _provider(),
        {"available_endpoints": ["messages", "responses"]})
    assert plan["mode"] == "proxy"
    assert plan["tool_protocol"] == PROTO_ANTHROPIC_MESSAGES
    assert plan["local_base_url"] == "https://inference-api.nvidia.com"
    assert "route" not in plan
    assert "proxy_port" not in plan


def test_codex_uses_adapter_v1_suffix_and_native_responses():
    plan = build_routing_plan(
        "codex", _provider(),
        {"available_endpoints": ["responses"]})
    assert plan["mode"] == "proxy"
    assert plan["tool_protocol"] == PROTO_OPENAI_RESPONSES
    assert plan["local_base_url"] == "https://inference-api.nvidia.com/v1"


def test_missing_native_endpoint_is_a_clear_error():
    with pytest.raises(ValueError, match="native messages endpoint"):
        build_routing_plan(
            "claude", _provider(),
            {"available_endpoints": ["completions"]})


def test_provider_does_not_select_protocol_or_url_override():
    with pytest.raises(ValueError, match="native responses endpoint"):
        require_native_endpoint(
            {"available_endpoints": ["completions"]},
            PROTO_OPENAI_RESPONSES,
        )


def test_json_tool_shapes_and_suffix_normalization():
    assert json_tool_api_shape("claude", ["messages"])[0] == PROTO_ANTHROPIC_MESSAGES
    assert json_tool_api_shape("codex", ["responses"], "/v1")[0] == PROTO_OPENAI_RESPONSES
    assert apply_base_url_suffix("https://example.test/", "/v1") == (
        "https://example.test/v1")
    assert apply_base_url_suffix("https://example.test/v1", "/v1") == (
        "https://example.test/v1")
