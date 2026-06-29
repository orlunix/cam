"""Tests for completions_to_responses proxy (Codex wire)."""

import json

from camc_pkg.proxy.responses import (
    build_chat_payload,
    responses_payload,
    translate_tools,
)


def test_translate_tools_flattens_namespace():
    tools = [{
        "type": "namespace",
        "name": "cam",
        "tools": [{
            "type": "function",
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
        }],
    }]
    chat_tools, reverse = translate_tools(tools)
    assert len(chat_tools) == 1
    flat = chat_tools[0]["function"]["name"]
    assert flat.startswith("mcp__cam__")
    assert reverse[flat] == ("cam", "read_file")


def test_build_chat_payload_includes_system_hint():
    req = {
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "instructions": "be helpful",
        "tools": [],
    }
    payload, _reverse = build_chat_payload(req, "nvidia/zai-org/eccn-glm-5.1")
    assert payload["messages"][0]["role"] == "system"
    assert "MCP tools are exposed" in payload["messages"][0]["content"]
    assert payload["messages"][-1]["content"] == "hello"


def test_responses_payload_maps_assistant_text():
    req = {"model": "glm-5.1"}
    raw = {
        "id": "chatcmpl-test",
        "choices": [{
            "message": {"role": "assistant", "content": "hi there"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    resp = responses_payload(req, raw, {})
    assert resp["status"] == "completed"
    assert resp["output"][0]["type"] == "message"
    assert resp["output"][0]["content"][0]["text"] == "hi there"
