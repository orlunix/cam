"""HTTP proxy: OpenAI chat/completions upstream -> Anthropic /v1/messages."""

import argparse
import os
import sys
import uuid
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from camc_pkg import LOGS_DIR
from camc_pkg.proxy.common import (
    DROP_ANTHROPIC_KEYS,
    ProxyLogger,
    RequestTimer,
    call_chat_completions,
    json_dumps,
    json_loads,
    last_user_preview_messages,
    resolve_upstream_model,
    summarize_chat_response,
    upstream_error_detail,
    text_from_content,
)
from camc_pkg.proxy.textual_tools import rewrite_anthropic_response

ROUTE = "completions_to_messages"


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def anthropic_tools_to_openai(tools):
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": str(name),
                "description": str(tool.get("description") or ""),
                "parameters": schema,
            },
        })
    return out


def _append_system(messages, text):
    text = str(text or "").strip()
    if not text:
        return
    if messages and messages[0].get("role") == "system":
        prev = str(messages[0].get("content") or "").strip()
        messages[0]["content"] = (prev + "\n\n" + text).strip() if prev else text
    else:
        messages.insert(0, {"role": "system", "content": text})


def anthropic_messages_to_chat(req, upstream_model):
    messages = []

    system = req.get("system")
    if isinstance(system, str) and system.strip():
        _append_system(messages, system)
    elif isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            _append_system(messages, "\n\n".join(parts))

    for msg in req.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        # IHUB/Qwen reject system messages after the first turn — fold into user text.
        if role == "system":
            if isinstance(content, str):
                _append_system(messages, content)
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                if parts:
                    _append_system(messages, "\n\n".join(parts))
            continue

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue

        if role == "assistant":
            text_parts = []
            tool_calls = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part.get("type") == "tool_use":
                    tool_calls.append({
                        "id": str(part.get("id") or "call_%s" % uuid.uuid4().hex[:8]),
                        "type": "function",
                        "function": {
                            "name": str(part.get("name") or "tool"),
                            "arguments": json_dumps(part.get("input") or {}),
                        },
                    })
            assistant = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else ""}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
            continue

        if role == "user":
            text_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part.get("type") == "tool_result":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(part.get("tool_use_id") or ""),
                        "content": text_from_content(part.get("content")) or "",
                    })
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})
            continue

        messages.append({"role": role, "content": text_from_content(content)})

    payload = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": int(req.get("max_tokens") or 4096),
    }
    if "temperature" in req:
        payload["temperature"] = req["temperature"]
    if "top_p" in req:
        payload["top_p"] = req["top_p"]
    tools = anthropic_tools_to_openai(req.get("tools") or [])
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _assistant_blocks_from_chat_message(msg):
    """Map OpenAI chat message -> Anthropic content blocks."""
    content = []
    text = msg.get("content")
    reasoning = msg.get("reasoning_content")
    tool_calls = msg.get("tool_calls") or []

    # Proxied reasoning models often leave content empty. Claude Code expects
    # text/tool_use blocks — not thinking blocks with null signatures.
    if not isinstance(text, str) or not text.strip():
        if isinstance(reasoning, str) and reasoning.strip():
            text = reasoning

    if isinstance(text, str) and text.strip():
        content.append({"type": "text", "text": text})

    for call in tool_calls:
        fn = call.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json_loads(str(args_raw).encode("utf-8"))
        except (ValueError, TypeError):
            args = {"raw": str(args_raw)}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or "toolu_%s" % uuid.uuid4().hex[:12]),
            "name": str(fn.get("name") or "tool"),
            "input": args if isinstance(args, dict) else {"value": args},
        })
    return content, bool(tool_calls)


def chat_to_anthropic_message(req, chat_raw):
    choices = chat_raw.get("choices") or []
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    content, has_tools = _assistant_blocks_from_chat_message(msg)

    usage = chat_raw.get("usage") or {}
    stop_reason = "tool_use" if has_tools else "end_turn"
    return {
        "id": str(chat_raw.get("id") or "msg_%s" % uuid.uuid4().hex[:12]),
        "type": "message",
        "role": "assistant",
        "model": req.get("model"),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
        "content": content or [{"type": "text", "text": ""}],
    }


def sse_events(message):
    start = dict(message)
    start["content"] = []
    start["stop_reason"] = None
    start["stop_sequence"] = None
    chunks = [
        "event: message_start\n"
        "data: %s\n\n" % json_dumps({"type": "message_start", "message": start})
    ]
    for idx, block in enumerate(message.get("content") or []):
        start_block = block
        if block.get("type") == "text":
            start_block = {"type": "text", "text": ""}
        elif block.get("type") == "tool_use":
            start_block = {
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": {},
            }
        chunks.append(
            "event: content_block_start\n"
            "data: %s\n\n" % json_dumps({
                "type": "content_block_start",
                "index": idx,
                "content_block": start_block,
            })
        )
        if block.get("type") == "text" and block.get("text"):
            chunks.append(
                "event: content_block_delta\n"
                "data: %s\n\n" % json_dumps({
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": block.get("text")},
                })
            )
        elif block.get("type") == "tool_use":
            chunks.append(
                "event: content_block_delta\n"
                "data: %s\n\n" % json_dumps({
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json_dumps(block.get("input") or {}),
                    },
                })
            )
        chunks.append(
            "event: content_block_stop\n"
            "data: %s\n\n" % json_dumps({"type": "content_block_stop", "index": idx})
        )
    chunks.append(
        "event: message_delta\n"
        "data: %s\n\n" % json_dumps({
            "type": "message_delta",
            "delta": {
                "stop_reason": message.get("stop_reason"),
                "stop_sequence": message.get("stop_sequence"),
            },
            "usage": message.get("usage") or {},
        })
    )
    chunks.append("event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
    return "".join(chunks).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _path(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self):
        path = self._path()
        if path in ("/", "/health", "/v1"):
            self._send_json({"ok": True, "route": ROUTE})
            return
        if path == "/v1/models":
            self._send_json({
                "object": "list",
                "data": [{"id": self.server.model_alias, "object": "model"}],
            })
            return
        self.send_error(404)

    def do_POST(self):
        if self._path() != "/v1/messages":
            self.send_error(404)
            return
        timer = RequestTimer()
        client_model = ""
        upstream_model = ""
        stream = False
        try:
            length = int(self.headers.get("Content-Length") or "0")
            req = json_loads(self.rfile.read(length))
            for key in list(req.keys()):
                if key in DROP_ANTHROPIC_KEYS:
                    req.pop(key, None)
            stream = bool(req.get("stream"))
            client_model = str(req.get("model") or "")
            upstream_model = resolve_upstream_model(req.get("model"), self.server.upstream_model)
            self.server.debug.log(
                "request_start",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                max_tokens=req.get("max_tokens"),
                message_count=len(req.get("messages") or []),
                tool_count=len(req.get("tools") or []),
                user_preview=last_user_preview_messages(req),
            )
            chat_payload = anthropic_messages_to_chat(req, upstream_model)
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.flush()
            raw = call_chat_completions(
                chat_payload, self.server.api_key, self.server.timeout,
                url=self.server.upstream_url,
            )
            msg = rewrite_anthropic_response(req, chat_to_anthropic_message(req, raw))
            self.server.debug.log(
                "request_done",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                latency_ms=timer.elapsed_ms,
                stop_reason=msg.get("stop_reason"),
                usage=msg.get("usage") or {},
                **summarize_chat_response(raw)
            )
            if stream:
                self.wfile.write(sse_events(msg))
                self.wfile.flush()
            else:
                self._send_json(msg)
        except urllib.error.HTTPError as exc:
            detail = upstream_error_detail(exc)[:500]
            self.server.debug.log(
                "request_error",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                latency_ms=timer.elapsed_ms,
                status=exc.code,
                error=detail,
            )
            self._send_json(
                {"type": "error", "error": {"type": "upstream_error", "message": detail}},
                status=exc.code,
            )
        except Exception as exc:
            detail = str(exc)
            status = 500
            self.server.debug.log(
                "request_error",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                latency_ms=timer.elapsed_ms,
                status=status,
                error=detail,
            )
            self._send_json(
                {"type": "error", "error": {"type": "proxy_error", "message": detail}},
                status=status,
            )

    def _send_json(self, obj, status=200):
        self._send_bytes(json_dumps(obj).encode("utf-8"), "application/json", status)

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s - %s\n" % (ROUTE, self.address_string(), fmt % args))
        sys.stdout.flush()


class Server(ThreadingHTTPServer):
    def __init__(self, addr, handler, api_key, model_alias, upstream_model,
                 upstream_url, timeout, debug):
        ThreadingHTTPServer.__init__(self, addr, handler)
        self.api_key = api_key
        self.model_alias = model_alias
        self.upstream_model = upstream_model
        self.upstream_url = upstream_url
        self.timeout = timeout
        self.debug = debug


def run_proxy(argv=None):
    p = argparse.ArgumentParser(description="Route: chat/completions -> anthropic/messages")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18324)
    p.add_argument("--model-alias", default="glm-5.1")
    p.add_argument("--upstream-model", default="")
    p.add_argument("--upstream-url", required=True)
    p.add_argument("--ready-file", default="")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--debug-log", default="")
    args = p.parse_args(argv)

    api_key = os.environ.get("INFERENCE_HUB_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.get("INFERENCE_HUB_TOKEN", "").strip()
    if not api_key:
        raise SystemExit("INFERENCE_HUB_API_KEY is required for proxy")

    upstream = resolve_upstream_model(args.upstream_model or args.model_alias, args.model_alias)
    debug_log = args.debug_log or os.path.join(LOGS_DIR, "proxy-messages-llm.jsonl")
    debug = ProxyLogger(ROUTE, args.debug, debug_log)
    httpd = Server(
        (args.host, args.port),
        Handler,
        api_key=api_key,
        model_alias=args.model_alias,
        upstream_model=upstream,
        upstream_url=args.upstream_url,
        timeout=args.timeout,
        debug=debug,
    )
    host, port = httpd.server_address[:2]
    if args.ready_file:
        with open(args.ready_file, "w") as handle:
            handle.write("%s:%s" % (host, port))
    sys.stdout.write(
        "[%s] http://%s:%s/v1/messages -> %s model=%s\n" % (
            ROUTE, host, port, args.upstream_url, upstream)
    )
    sys.stdout.flush()
    httpd.serve_forever()
    return 0
