"""Shared helpers for API protocol proxies (Py3.6, stdlib-only)."""

import json
import os
import time
import urllib.error
import urllib.request

DROP_ANTHROPIC_KEYS = frozenset({
    "output_config",
    "context_management",
    "metadata",
})


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(data):
    return json.loads(data.decode("utf-8"))


def resolve_upstream_model(model, default):
    """Map camc API profile names to Inference Hub model ids."""
    value = str(model or default or "").strip()
    default = str(default or "").strip()
    if value.endswith("[1m]"):
        value = value[:-4]
    if not value and default:
        value = default
    if not value:
        return default

    # Already a full IHUB model id (vendor/org/...).
    if "/" in value:
        return value

    mapped = _ihub_model_for_api_name(value)
    if mapped:
        return mapped

    # Proxy started with --upstream-model; use when client model is the alias.
    if default and "/" in default:
        return default

    return value


def _ihub_model_for_api_name(name):
    """Resolve short API name via ~/.cam/api-models.json."""
    if not name:
        return None
    try:
        from camc_pkg.api_store import ensure_ready, resolve_api_name
        data = ensure_ready()
        key = resolve_api_name(data, name)
        entry = (data.get("apis") or {}).get(key) or {}
        model = entry.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    except (ValueError, IOError, OSError):
        pass
    return None


def text_from_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for part in content:
        if isinstance(part, dict):
            typ = part.get("type")
            if typ in ("text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(x for x in parts if x)


def call_chat_completions(payload, api_key, timeout, url=None):
    body = json_dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        body,
        method="POST",
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json_loads(resp.read())


def upstream_error_detail(exc):
    return exc.read().decode("utf-8", "replace")


class ProxyLogger(object):
    def __init__(self, route, enabled, path):
        self.route = route
        self.enabled = bool(enabled)
        self.path = path

    def log(self, event, **fields):
        if not self.enabled:
            return
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
        row.update(fields)
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass


class RequestTimer(object):
    def __init__(self):
        self._start = time.time()

    @property
    def elapsed_ms(self):
        return int((time.time() - self._start) * 1000)


def last_user_preview_messages(req, limit=120):
    for msg in reversed(req.get("messages") or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = text_from_content(msg.get("content"))
            if text:
                return text[:limit]
    return ""


def summarize_chat_response(raw):
    choices = raw.get("choices") or []
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    return {
        "finish_reason": (choices[0] or {}).get("finish_reason") if choices else None,
        "has_tool_calls": bool(msg.get("tool_calls")),
        "content_len": len(str(msg.get("content") or "")),
    }


def last_user_preview_responses(req, limit=120):
    """Best-effort user text preview from a Responses API request."""
    instructions = str(req.get("instructions") or "").strip()
    if instructions:
        return instructions[:limit]
    inp = req.get("input")
    if isinstance(inp, str) and inp.strip():
        return inp.strip()[:limit]
    if isinstance(inp, list):
        for item in reversed(inp):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and item.get("role") == "user":
                text = text_from_content(item.get("content"))
                if text:
                    return text[:limit]
    return ""
