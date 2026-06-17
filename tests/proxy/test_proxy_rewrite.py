"""Integration tests for proxy translators (production: camc_pkg.proxy.*)."""

from camc_pkg.proxy.textual_tools import parse_all_textual_tool_calls, rewrite_anthropic_response
from camc_pkg.proxy.messages import _assistant_blocks_from_chat_message


def test_reasoning_fallback_as_text_not_thinking():
    blocks, has_tools = _assistant_blocks_from_chat_message({
        "content": None,
        "reasoning_content": "Thinking about the answer",
        "tool_calls": [],
    })
    assert has_tools is False
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "Thinking" in blocks[0]["text"]
    assert not any(b.get("type") == "thinking" for b in blocks)


def test_tool_calls_preserved_with_empty_content():
    blocks, has_tools = _assistant_blocks_from_chat_message({
        "content": "",
        "reasoning_content": "internal trace",
        "tool_calls": [{
            "id": "call_1",
            "function": {"name": "Bash", "arguments": '{"command":"echo OK"}'},
        }],
    })
    assert has_tools is True
    assert any(b.get("type") == "tool_use" for b in blocks)


def test_dsml_fragment_to_task_tool():
    text = (
        "Fix bucket.py and window.py\n"
        "Subagent type: general-purpose\n"
        "</\uFF5CDSML\uFF5Cinvoke>\n"
        "</\uFF5CDSML\uFF5Ctool_calls>"
    )
    blocks, remaining = parse_all_textual_tool_calls(text, {"Task", "Bash"})
    assert len(blocks) == 1
    assert blocks[0]["name"] == "Task"
    assert "bucket.py" in blocks[0]["input"]["prompt"]


def test_rewrite_glm_tool_call_markup():
    req = {"tools": [{"name": "Bash"}]}
    resp = {
        "content": [{"type": "text", "text": '<tool_call>Bash<arg_key>command</arg_key><arg_value>ls</arg_value></tool_call>'}],
        "stop_reason": "end_turn",
    }
    out = rewrite_anthropic_response(req, resp)
    assert out["stop_reason"] == "tool_use"
    assert out["content"][-1]["name"] == "Bash"


def test_mid_turn_system_folded_to_leading_system():
    from camc_pkg.proxy.messages import anthropic_messages_to_chat
    payload = anthropic_messages_to_chat({
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "reminder"},
            {"role": "user", "content": "next"},
        ],
        "max_tokens": 100,
    }, "nvidia/qwen/eccn-qwen3-5-397b-a17b")
    roles = [m["role"] for m in payload["messages"]]
    assert roles[0] == "system"
    assert "system" not in roles[1:]
    assert "reminder" in payload["messages"][0]["content"]


def test_json_tool_call_rewrite():
    text = 'Need to run\n{"tool": "Bash", "arguments": {"command": "ls"}}'
    blocks, remaining = parse_all_textual_tool_calls(text, {"Bash"})
    assert len(blocks) == 1
    assert blocks[0]["name"] == "Bash"
    assert blocks[0]["input"]["command"] == "ls"
