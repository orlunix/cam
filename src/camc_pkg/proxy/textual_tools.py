"""Parse vendor-specific textual tool markup into Anthropic tool_use blocks."""

import json
import re
import time

TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?P<name>[^<\s]+)(?P<body>.*?)</\s*tool_call\s*>",
    re.DOTALL,
)
ARG_RE = re.compile(
    r"<arg_key>\s*(?P<key>.*?)\s*</\s*arg_key\s*>\s*"
    r"<arg_value>\s*(?P<value>.*?)\s*</\s*arg_value\s*>",
    re.DOTALL,
)

# DeepSeek DSML markers use fullwidth vertical bar (U+FF5C).
_DSML = "\uFF5C"
DSML_INVOKE_RE = re.compile(
    r"<" + _DSML + r"DSML" + _DSML
    + r"invoke\s+name=\"(?P<name>[^\"]+)\"\s*>(?P<body>.*?)</"
    + _DSML + r"DSML" + _DSML + r"invoke>",
    re.DOTALL,
)
DSML_PARAM_RE = re.compile(
    r"<" + _DSML + r"DSML" + _DSML
    + r"parameter\s+name=\"(?P<key>[^\"]+)\"[^>]*>(?P<value>.*?)</"
    + _DSML + r"DSML" + _DSML + r"parameter>",
    re.DOTALL,
)
DSML_STRAY_RE = re.compile(
    r"</?" + _DSML + r"DSML" + _DSML + r"(?:tool_calls|function_calls|invoke|parameter)[^>]*>",
    re.DOTALL,
)


def tool_names(req):
    names = set()
    for tool in req.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            names.add(tool["name"])
    return names


def normalize_tool_name(raw_name, valid_names):
    name = raw_name.strip()
    if name in valid_names:
        return name
    underscored = name.replace("-", "_")
    if underscored in valid_names:
        return underscored
    return name


def _tool_block(name, args, idx):
    return {
        "type": "tool_use",
        "id": "toolu_ihub_%d_%d" % (int(time.time() * 1000), idx),
        "name": name,
        "input": args,
    }


def parse_textual_tool_calls(text, valid_names):
    blocks = []
    for idx, match in enumerate(TOOL_CALL_RE.finditer(text), start=1):
        args = {}
        for arg in ARG_RE.finditer(match.group("body")):
            key = arg.group("key").strip()
            if key:
                args[key] = arg.group("value").strip()
        blocks.append(_tool_block(
            normalize_tool_name(match.group("name"), valid_names), args, idx))
    return blocks, TOOL_CALL_RE.sub("", text).strip()


def parse_dsml_tool_calls(text, valid_names):
    blocks = []
    for idx, match in enumerate(DSML_INVOKE_RE.finditer(text), start=1):
        args = {}
        for arg in DSML_PARAM_RE.finditer(match.group("body")):
            key = arg.group("key").strip()
            if key:
                args[key] = arg.group("value").strip()
        blocks.append(_tool_block(
            normalize_tool_name(match.group("name"), valid_names), args, idx))
    if not blocks:
        return [], text
    cleaned = DSML_INVOKE_RE.sub("", text)
    cleaned = DSML_STRAY_RE.sub("", cleaned).strip()
    return blocks, cleaned


def parse_dsml_fragment(text, valid_names):
    """Handle truncated DSML (closing tags only) from DeepSeek."""
    marker = "</" + _DSML + "DSML" + _DSML + "invoke>"
    if marker not in text and (_DSML + "DSML" + _DSML) not in text:
        return [], text
    if DSML_INVOKE_RE.search(text):
        return [], text
    body = text.split(marker)[0]
    body = DSML_STRAY_RE.sub("", body).strip()
    if not body:
        return [], text
    name = "Task" if "Task" in valid_names else None
    if not name:
        for candidate in ("Bash", "Write", "Edit", "Read"):
            if candidate in valid_names:
                name = candidate
                break
    if not name:
        return [], text
    inp = {"prompt": body} if name == "Task" else {"command": "true"}
    if name != "Task":
        inp = {"description": body[:2000]}
    return [_tool_block(name, inp, 1)], ""


def _extract_json_objects(text):
    """Yield top-level {...} substrings with balanced braces."""
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        for j in range(i, len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    i = j + 1
                    break
        else:
            break


def _infer_tool_from_args(args):
    if not isinstance(args, dict) or not args:
        return None
    keys = set(args.keys())
    if "file_path" in keys or "path" in keys and "pattern" not in keys and "command" not in keys:
        if "old_string" in keys or "new_string" in keys or "replace_all" in keys:
            return "Edit"
        return "Read"
    if "pattern" in keys and ("path" in keys or "glob_pattern" in keys):
        return "Glob"
    if "command" in keys:
        return "Bash"
    if "prompt" in keys or "description" in keys:
        return "Task"
    return None


def parse_json_tool_calls(text, valid_names):
    """GPT-OSS sometimes emits tool JSON as plain text."""
    blocks = []
    remaining = text
    for chunk in list(_extract_json_objects(text)):
        try:
            obj = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        name = obj.get("tool") or obj.get("name")
        args = obj.get("arguments") or obj.get("input")
        if not isinstance(name, str) or not name.strip():
            name = _infer_tool_from_args(obj if isinstance(args, dict) else obj)
            args = obj if isinstance(args, dict) else {}
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(args, dict):
            args = obj if isinstance(obj, dict) else {"value": args}
        blocks.append(_tool_block(normalize_tool_name(name, valid_names), args, len(blocks) + 1))
        remaining = remaining.replace(chunk, "", 1)
    return blocks, remaining.strip()


def parse_all_textual_tool_calls(text, valid_names):
    cleaned = text.split("<|call|>")[0].split("</|call|>")[0]
    blocks, remaining = parse_textual_tool_calls(cleaned, valid_names)
    if blocks:
        return blocks, remaining
    blocks, remaining = parse_dsml_tool_calls(remaining, valid_names)
    if blocks:
        return blocks, remaining
    blocks, remaining = parse_json_tool_calls(remaining, valid_names)
    if blocks:
        return blocks, remaining
    return parse_dsml_fragment(remaining, valid_names)


def rewrite_anthropic_response(req, resp):
    text_parts = []
    passthrough = []
    for part in resp.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        elif isinstance(part, dict):
            passthrough.append(part)
    text = "\n".join(text_parts)
    tool_blocks, remaining = parse_all_textual_tool_calls(text, tool_names(req))
    if not tool_blocks:
        return resp
    rewritten = dict(resp)
    content = []
    if remaining:
        content.append({"type": "text", "text": remaining})
    content.extend(passthrough)
    content.extend(tool_blocks)
    rewritten["content"] = content
    rewritten["stop_reason"] = "tool_use"
    rewritten.setdefault("stop_sequence", None)
    return rewritten
