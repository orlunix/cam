"""HTTP proxy: OpenAI chat/completions upstream -> OpenAI /v1/responses (Codex)."""

import argparse
import os
import re
import sys
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from camc_pkg import LOGS_DIR
from camc_pkg.proxy.common import (
    ProxyLogger,
    RequestTimer,
    call_chat_completions,
    json_dumps,
    json_loads,
    last_user_preview_responses,
    resolve_upstream_model,
    summarize_chat_response,
    text_from_content,
    upstream_error_detail,
)
from camc_pkg.proxy.textual_tools import ARG_RE, TOOL_CALL_RE
from camc_pkg.api_metadata import openai_models_list_response, resolve_api_metadata

RESPONSES_ROUTE = "completions_to_responses"
NAME_RE = re.compile(r"[^A-Za-z0-9_]+")

SKIP_CHAT_FUNCTIONS = frozenset({
    "shell_command",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
})

PROXY_TOOL_HINT = (
    "MCP tools are exposed as chat functions named mcp__<server>__<tool>. "
    "Call the concrete mcp__... function directly instead of shell_command "
    "or MCP resource-list/read helpers."
)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def clean_ident(value):
    return NAME_RE.sub("_", str(value or "")).strip("_")


def namespace_ident(namespace):
    raw = clean_ident(namespace)
    if not raw:
        return ""
    if raw.startswith("mcp__"):
        return raw
    return "mcp__%s" % raw


def flatten_name(namespace, name):
    right = clean_ident(name) or "tool"
    left = namespace_ident(namespace)
    if not left:
        return right[:64]
    return ("%s__%s" % (left, right))[:64]


def schema_for_chat(schema):
    if isinstance(schema, dict) and schema:
        return schema
    return {"type": "object", "properties": {}}


def translate_tools(tools):
    chat_tools = []
    reverse = {}
    used = set()

    def add_tool(namespace, tool):
        name = str(tool.get("name") or "").strip()
        if not name:
            return
        if namespace is None and name in SKIP_CHAT_FUNCTIONS:
            return
        flat = flatten_name(namespace, name)
        base = flat
        idx = 2
        while flat in used:
            suffix = "_%d" % idx
            flat = "%s%s" % (base[:64 - len(suffix)], suffix)
            idx += 1
        used.add(flat)
        reverse[flat] = (namespace, name)
        desc = str(tool.get("description") or "")
        if namespace:
            flat_hint = flatten_name(namespace, name)
            desc = (
                "Tool name: %s\nOriginal namespace: %s.%s\n%s"
                % (flat_hint, namespace, name, desc)
            ).strip()
        chat_tools.append({
            "type": "function",
            "function": {
                "name": flat,
                "description": desc,
                "parameters": schema_for_chat(tool.get("parameters")),
            },
        })

    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        typ = tool.get("type")
        if typ == "function":
            add_tool(None, tool)
        elif typ == "namespace":
            ns = str(tool.get("name") or "").strip() or None
            for child in tool.get("tools") or []:
                if isinstance(child, dict) and child.get("type") == "function":
                    add_tool(ns, child)
    return chat_tools, reverse


def content_item_text(content):
    if isinstance(content, list):
        return text_from_content(content)
    return "" if content is None else str(content)


def translate_input(input_items, instructions, reverse):
    messages = []
    system_parts = [x for x in (instructions, PROXY_TOOL_HINT) if x]
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
        return messages
    if not isinstance(input_items, list):
        return messages

    for item in input_items:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "message":
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in ("system", "user", "assistant", "tool"):
                role = "user"
            messages.append({
                "role": role,
                "content": content_item_text(item.get("content")),
            })
        elif typ == "function_call":
            name = str(item.get("name") or "")
            namespace = item.get("namespace")
            if namespace:
                flat = flatten_name(str(namespace), name)
            else:
                flat = flatten_name(None, name)
            call_id = str(item.get("call_id") or item.get("id") or "call_%d" % (len(messages) + 1))
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": flat,
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }],
            })
        elif typ == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": content_item_text(item.get("output")),
            })
    return messages


def build_chat_payload(req, upstream_model):
    chat_tools, reverse = translate_tools(req.get("tools") or [])
    payload = {
        "model": upstream_model,
        "messages": translate_input(
            req.get("input"),
            str(req.get("instructions") or ""),
            reverse,
        ),
    }
    if chat_tools:
        payload["tools"] = chat_tools
        payload["tool_choice"] = (
            "auto" if req.get("tool_choice") in (None, "auto") else req.get("tool_choice")
        )
        payload["parallel_tool_calls"] = bool(req.get("parallel_tool_calls", False))
    max_tokens = req.get("max_output_tokens") or req.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    return payload, reverse


def split_flat_mcp_name(flat):
    if flat.startswith("mcp__"):
        parts = flat.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return "mcp__%s" % parts[1], parts[2]
    return None, flat


def textual_tool_call_items(content, reverse):
    text = "" if content is None else str(content)
    output = []
    for idx, match in enumerate(TOOL_CALL_RE.finditer(text), start=1):
        raw_name = match.group("name").strip()
        flat = clean_ident(raw_name) or raw_name
        namespace, name = reverse.get(flat, split_flat_mcp_name(flat))
        args = {}
        for arg in ARG_RE.finditer(match.group("body")):
            key = arg.group("key").strip()
            if key:
                args[key] = arg.group("value").strip()
        item = {
            "id": "call_%d" % idx,
            "type": "function_call",
            "status": "completed",
            "call_id": "call_%d" % idx,
            "name": name,
            "arguments": json_dumps(args),
        }
        if namespace:
            item["namespace"] = namespace
        output.append(item)
    return output, TOOL_CALL_RE.sub("", text).strip()


def responses_output_from_chat(raw, reverse):
    choices = raw.get("choices") or []
    msg = ((choices[0] or {}).get("message") or {}) if choices else {}
    output = []
    for idx, call in enumerate(msg.get("tool_calls") or []):
        fn = call.get("function") or {}
        flat = str(fn.get("name") or "")
        namespace, name = reverse.get(flat, split_flat_mcp_name(flat))
        item = {
            "id": str(call.get("id") or "call_%d" % (idx + 1)),
            "type": "function_call",
            "status": "completed",
            "call_id": str(call.get("id") or "call_%d" % (idx + 1)),
            "name": name,
            "arguments": str(fn.get("arguments") or "{}"),
        }
        if namespace:
            item["namespace"] = namespace
        output.append(item)
    content = msg.get("content")
    if content:
        text_calls, remaining = textual_tool_call_items(content, reverse)
        output.extend(text_calls)
        if remaining:
            output.append({
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": remaining}],
            })
    return output


def responses_payload(req, chat_raw, reverse):
    usage = chat_raw.get("usage") or {}
    output = responses_output_from_chat(chat_raw, reverse)
    return {
        "id": str(chat_raw.get("id") or "resp_%d" % int(time.time() * 1000)),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": req.get("model"),
        "output": output,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


def responses_sse_events(resp):
    events = [
        {"type": "response.created", "response": dict(resp, status="in_progress", output=[])},
    ]
    for idx, item in enumerate(resp.get("output") or []):
        events.append({
            "type": "response.output_item.added",
            "output_index": idx,
            "item": item,
        })
        if item.get("type") == "message":
            text = text_from_content(item.get("content"))
            events.append({
                "type": "response.content_part.added",
                "output_index": idx,
                "item_id": item.get("id"),
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            })
            if text:
                events.append({
                    "type": "response.output_text.delta",
                    "output_index": idx,
                    "item_id": item.get("id"),
                    "content_index": 0,
                    "delta": text,
                })
            events.append({
                "type": "response.output_text.done",
                "output_index": idx,
                "item_id": item.get("id"),
                "content_index": 0,
                "text": text,
            })
            events.append({
                "type": "response.content_part.done",
                "output_index": idx,
                "item_id": item.get("id"),
                "content_index": 0,
                "part": {"type": "output_text", "text": text},
            })
        events.append({
            "type": "response.output_item.done",
            "output_index": idx,
            "item": item,
        })
    events.append({"type": "response.completed", "response": resp})
    return "".join(
        "event: %s\ndata: %s\n\n" % (e["type"], json_dumps(e)) for e in events
    ).encode("utf-8")


class ResponsesHandler(BaseHTTPRequestHandler):
    def _path(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self):
        path = self._path()
        if path in ("/", "/health", "/v1"):
            self._send_json({"ok": True, "route": RESPONSES_ROUTE})
            return
        if path == "/v1/models":
            meta = getattr(self.server, "model_metadata", None) or {}
            if openai_models_list_response:
                self._send_json(openai_models_list_response(self.server.model_alias, meta))
            else:
                self._send_json({
                    "object": "list",
                    "data": [{"id": self.server.model_alias, "object": "model"}],
                })
            return
        self.send_error(404)

    def do_POST(self):
        if self._path() != "/v1/responses":
            self.send_error(404)
            return
        timer = RequestTimer()
        client_model = ""
        upstream_model = ""
        stream = False
        try:
            length = int(self.headers.get("Content-Length") or "0")
            req = json_loads(self.rfile.read(length))
            client_model = str(req.get("model") or "")
            upstream_model = resolve_upstream_model(req.get("model"), self.server.upstream_model)
            stream = bool(req.get("stream"))
            self.server.debug.log(
                "request_start",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                max_tokens=req.get("max_output_tokens") or req.get("max_tokens"),
                input_items=len(req.get("input") or []) if isinstance(req.get("input"), list) else 1,
                tool_count=len(req.get("tools") or []),
                user_preview=last_user_preview_responses(req),
            )
            chat_payload, reverse = build_chat_payload(req, upstream_model)
            raw = call_chat_completions(
                chat_payload, self.server.api_key, self.server.timeout,
                url=self.server.upstream_url,
            )
            resp = responses_payload(req, raw, reverse)
            self.server.debug.log(
                "request_done",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                latency_ms=timer.elapsed_ms,
                response_id=resp.get("id"),
                output_items=len(resp.get("output") or []),
                usage=resp.get("usage") or {},
                **summarize_chat_response(raw)
            )
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(responses_sse_events(resp))
                self.wfile.flush()
            else:
                self._send_json(resp)
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
            self._send_json({"error": {"message": detail}}, status=exc.code)
        except Exception as exc:
            detail = str(exc)
            self.server.debug.log(
                "request_error",
                path=self.path,
                client_model=client_model,
                upstream_model=upstream_model,
                stream=stream,
                latency_ms=timer.elapsed_ms,
                status=500,
                error=detail,
            )
            self._send_json({"error": {"message": detail}}, status=500)

    def _send_json(self, obj, status=200):
        self._send_bytes(json_dumps(obj).encode("utf-8"), "application/json", status)

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s - %s\n" % (RESPONSES_ROUTE, self.address_string(), fmt % args))
        sys.stdout.flush()


class ResponsesServer(ThreadingHTTPServer):
    def __init__(self, addr, handler, api_key, model_alias, upstream_model,
                 upstream_url, timeout, debug, model_metadata=None):
        ThreadingHTTPServer.__init__(self, addr, handler)
        self.api_key = api_key
        self.model_alias = model_alias
        self.upstream_model = upstream_model
        self.upstream_url = upstream_url
        self.timeout = timeout
        self.debug = debug
        self.model_metadata = model_metadata or {}


def run_responses_proxy(argv=None):
    p = argparse.ArgumentParser(description="Route: chat/completions -> openai/responses")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18325)
    p.add_argument("--model-alias", default="glm-5.1")
    p.add_argument("--upstream-model", default="")
    p.add_argument("--upstream-url", required=True)
    p.add_argument("--ready-file", default="")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--debug-log", default="")
    p.add_argument("--api-name", default="", help="API profile for model metadata")
    args = p.parse_args(argv)

    api_key = os.environ.get("INFERENCE_HUB_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.get("INFERENCE_HUB_TOKEN", "").strip()
    if not api_key:
        raise SystemExit("INFERENCE_HUB_API_KEY is required for proxy")

    upstream = resolve_upstream_model(args.upstream_model or args.model_alias, args.model_alias)
    model_metadata = {}
    if args.api_name and resolve_api_metadata:
        try:
            model_metadata = resolve_api_metadata(args.api_name)
        except (ValueError, IOError, OSError):
            model_metadata = {}
    debug_log = args.debug_log or os.path.join(LOGS_DIR, "proxy-responses-llm.jsonl")
    debug = ProxyLogger(RESPONSES_ROUTE, args.debug, debug_log)
    httpd = ResponsesServer(
        (args.host, args.port),
        ResponsesHandler,
        api_key=api_key,
        model_alias=args.model_alias,
        upstream_model=upstream,
        upstream_url=args.upstream_url,
        timeout=args.timeout,
        debug=debug,
        model_metadata=model_metadata,
    )
    host, port = httpd.server_address[:2]
    if args.ready_file:
        with open(args.ready_file, "w") as handle:
            handle.write("%s:%s" % (host, port))
    sys.stdout.write(
        "[%s] http://%s:%s/v1/responses -> %s model=%s\n" % (
            RESPONSES_ROUTE, host, port, args.upstream_url, upstream)
    )
    sys.stdout.flush()
    httpd.serve_forever()
    return 0
