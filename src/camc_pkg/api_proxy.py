import argparse
from collections import OrderedDict
from email.utils import parsedate_to_datetime
import http.client
import json
import os
import random
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

API_MODELS_FILE = os.path.join(os.path.expanduser("~/.cam"), "api-models.json")
_DEBUG_CACHE = {"path": None, "signature": None, "enabled": False}
_MAX_RESPONSE_CAPTURE = 4 * 1024 * 1024
_MAX_ERROR_INSPECT = 64 * 1024


def _is_retryable_internal_400(status, body):
    """Identify IH's transient generic internal 400, not schema errors."""
    if int(status or 0) != 400:
        return False
    text = bytes(body or b"").decode("utf-8", "replace").lower()
    return "invalid_argument" in text and "internal error occurred" in text


RETRY_STATUS_POLICIES = {
    429: {"max_elapsed": 300.0, "initial_delay": 1.0,
          "max_delay": 30.0},
    400: {"max_elapsed": 8.0, "initial_delay": 1.0,
          "max_delay": 2.0, "retry_if": _is_retryable_internal_400},
}

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python 3.6 ships HTTPServer but not ThreadingHTTPServer. CAMC supports
    # that runtime on older PDX hosts, so provide the identical stdlib mixin.
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True


def upstream_request_path(upstream_url, request_path):
    """Join a client request to the upstream without duplicating ``/v1``."""
    upstream = urlsplit(str(upstream_url or ""))
    request = urlsplit(str(request_path or "/"))
    base = upstream.path.rstrip("/")
    path = request.path or "/"
    if base and (path == base or path.startswith(base + "/")):
        joined = path
    else:
        joined = base + (path if path.startswith("/") else "/" + path)
    if request.query:
        joined += "?" + request.query
    return joined or "/"


def upstream_request_headers(headers, body_length):
    """Build upstream headers while keeping the response cache readable."""
    result = {key: value for key, value in headers.items()
              if key.lower() not in ("host", "content-length", "connection",
                                     "accept-encoding")}
    result["Accept-Encoding"] = "identity"
    result["Content-Length"] = str(body_length)
    return result


def _retry_after_seconds(value, now=None):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(value))
            return max(0.0, dt.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return None


def request_upstream_with_retry(connect, method, path, body, headers,
                                policies=None, sleep=time.sleep,
                                monotonic=time.monotonic,
                                jitter=None, on_retry=None):
    """Open an upstream response, hiding only configured transient statuses."""
    policies = RETRY_STATUS_POLICIES if policies is None else policies
    jitter = jitter or (lambda delay: random.uniform(delay * .8, delay * 1.2))
    started, attempts = monotonic(), 0
    while True:
        conn = connect()
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        policy = policies.get(response.status)
        if not policy:
            return conn, response
        retry_if = policy.get("retry_if")
        if retry_if:
            try:
                inspected = response.read(_MAX_ERROR_INSPECT)
            except (IOError, OSError):
                inspected = b""
            response._camc_prefetched_body = inspected
            if not retry_if(response.status, inspected):
                return conn, response
        elapsed = monotonic() - started
        remaining = float(policy.get("max_elapsed", 0)) - elapsed
        base = min(float(policy.get("max_delay", 30)),
                   float(policy.get("initial_delay", 1)) * (2 ** attempts))
        retry_after = _retry_after_seconds(response.getheader("Retry-After"))
        delay = max(.25, retry_after if retry_after is not None else jitter(base))
        if remaining <= 0 or delay > remaining:
            return conn, response
        response.read()
        conn.close()
        attempts += 1
        if on_retry:
            on_retry(response.status, attempts, delay)
        sleep(delay)


def read_upstream_response(response, size=65536):
    """Read a response while preserving the body inspected for retry."""
    prefetched = getattr(response, "_camc_prefetched_body", b"")
    if prefetched:
        chunk = prefetched[:size]
        remainder = prefetched[len(chunk):]
        if remainder:
            response._camc_prefetched_body = remainder
        else:
            try:
                del response._camc_prefetched_body
            except AttributeError:
                pass
        if len(chunk) < size:
            chunk += response.read(size - len(chunk))
        return chunk
    return response.read(size)


def stop_agent_api_proxy(agent):
    """Terminate only the proxy whose command line names this exact agent."""
    api = agent.get("api") if isinstance(agent, dict) else None
    try:
        pid = int((api or {}).get("proxy_pid") or 0)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            cmdline = handle.read().decode("utf-8", "replace")
        marker = "--owner\x00%s\x00" % agent.get("id", "")
        if "_api_proxy\x00" not in cmdline or marker not in cmdline:
            return
        os.kill(pid, signal.SIGTERM)
    except (IOError, OSError):
        pass


class ResponseCallCache(object):
    """Small process-local map of prior Responses output items."""

    def __init__(self, max_entries=128):
        self.max_entries = max(1, int(max_entries))
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, response_id):
        with self._lock:
            calls = self._items.get(str(response_id or ""))
            return list(calls) if calls else None

    def put(self, response_id, calls):
        response_id = str(response_id or "")
        calls = [dict(call) for call in (calls or [])
                 if isinstance(call, dict)]
        if not response_id or not calls:
            return
        with self._lock:
            self._items.pop(response_id, None)
            self._items[response_id] = calls
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


def expand_tool_continuation(payload, cache):
    """Expand IH's unsupported stateful tool result into stateless input."""
    if not isinstance(payload, dict):
        return False
    previous_id = payload.get("previous_response_id")
    inputs = payload.get("input")
    if not previous_id or not isinstance(inputs, list):
        return False
    if not any(isinstance(item, dict)
               and str(item.get("type") or "").endswith("_output")
               for item in inputs):
        return False
    calls = cache.get(previous_id) if hasattr(cache, "get") else None
    if not calls:
        return False
    payload["input"] = list(calls) + inputs
    payload.pop("previous_response_id", None)
    return True


def normalize_tool_history(payload):
    """Remove OpenAI-only reasoning state from IH tool continuations."""
    inputs = payload.get("input") if isinstance(payload, dict) else None
    if not isinstance(inputs, list) or not any(
            isinstance(item, dict) and
            str(item.get("type") or "").endswith("_output")
            for item in inputs):
        return False
    normalized = []
    pending_calls = set()
    for item in inputs:
        item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
        call_id = str(item.get("call_id") or "") if isinstance(item, dict) else ""
        if item_type.endswith("_call") and call_id:
            pending_calls.add(call_id)
        if item_type == "reasoning" or (item_type == "message" and pending_calls):
            continue
        normalized.append(item)
        if item_type.endswith("_output") and call_id:
            pending_calls.discard(call_id)
    if len(normalized) == len(inputs):
        return False
    payload["input"] = normalized
    return True


def normalize_response_text_format(payload):
    """Make the Responses API's default plain-text format explicit."""
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, dict) or "format" in text:
        return False
    text["format"] = {"type": "text"}
    return True


def normalize_reasoning_effort(payload, mapping):
    """Map Codex effort names to the upstream model's accepted values."""
    reasoning = payload.get("reasoning") if isinstance(payload, dict) else None
    if not isinstance(reasoning, dict) or not isinstance(mapping, dict):
        return False
    effort = str(reasoning.get("effort") or "").strip().lower()
    mapped = str(mapping.get(effort) or "").strip().lower()
    if not mapped or mapped == effort:
        return False
    reasoning["effort"] = mapped
    return True


def extract_response_output(body):
    """Extract response id/output items from JSON or Responses SSE bytes."""
    if not body:
        return "", []
    objects = []
    try:
        objects.append(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        for raw in body.splitlines():
            if not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                objects.append(json.loads(data.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                continue
    response_id = ""
    calls = []
    seen = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        response = obj.get("response")
        if not isinstance(response, dict):
            response = obj if obj.get("object") == "response" else None
        if response:
            response_id = str(response.get("id") or response_id)
            items = response.get("output") or []
        elif obj.get("type") == "response.output_item.done":
            response_id = str(obj.get("response_id") or response_id)
            items = [obj.get("item")]
        else:
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("call_id") or item.get("id") or
                      json.dumps(item, sort_keys=True))
            if key not in seen:
                seen.add(key)
                calls.append(dict(item))
    return response_id, calls


extract_response_calls = extract_response_output


def debug_log_file_for_owner(owner):
    """Return the fixed owner log path when JSON debug is enabled now."""
    try:
        stat = os.stat(API_MODELS_FILE)
        signature = (API_MODELS_FILE, stat.st_mtime, stat.st_size)
    except OSError:
        signature = (API_MODELS_FILE, None, None)
    if signature != (_DEBUG_CACHE["path"], _DEBUG_CACHE["signature"],
                     _DEBUG_CACHE.get("size")):
        enabled = False
        try:
            with open(API_MODELS_FILE, "r") as handle:
                enabled = json.load(handle).get("debug_api_proxy") is True
        except (IOError, ValueError, AttributeError):
            pass
        _DEBUG_CACHE["path"] = API_MODELS_FILE
        _DEBUG_CACHE["signature"] = signature[1]
        _DEBUG_CACHE["size"] = signature[2]
        _DEBUG_CACHE["enabled"] = enabled
    if not _DEBUG_CACHE["enabled"]:
        return ""
    safe_owner = re.sub(r"[^A-Za-z0-9_.-]", "_", str(owner or ""))
    return os.path.join(os.path.dirname(API_MODELS_FILE), "logs",
                        "api-proxy-%s.log" % safe_owner)


def write_proxy_log(log_file, transaction_id, event, method, request_path,
                    model, status, duration_ms, error_class, shape=""):
    """Append a deliberately body/token-free proxy transaction record."""
    if not log_file:
        return
    path = urlsplit(str(request_path or "/")).path or "/"
    safe_shape = re.sub(r"[^A-Za-z0-9_=,.-]", "_", str(shape or ""))
    line = ("%s tx=%s event=%s method=%s path=%s model=%s status=%s "
            "duration_ms=%d error=%s%s\n"
            % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               transaction_id, event, method, path, model, status,
               int(duration_ms), error_class or "-",
               (" shape=" + safe_shape) if safe_shape else ""))
    try:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(log_file, "a") as handle:
            handle.write(line)
    except IOError:
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        started = time.time()
        length = int(self.headers.get("Content-Length", "0") or 0)
        log_file = debug_log_file_for_owner(self.server.owner)
        with self.server.transaction_lock:
            self.server.transaction_count += 1
            transaction_id = str(self.server.transaction_count)
        write_proxy_log(log_file, transaction_id, "request",
                        self.command, self.path, self.server.upstream_model,
                        "-", 0, "")
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            input_types = [str(item.get("type") or "-")
                           for item in (payload.get("input") or [])
                           if isinstance(item, dict)]
            had_previous = bool(payload.get("previous_response_id"))
            expanded = expand_tool_continuation(
                payload, self.server.response_call_cache)
            normalized = normalize_tool_history(payload)
            text_normalized = normalize_response_text_format(payload)
            reasoning_normalized = normalize_reasoning_effort(
                payload, self.server.reasoning_mapping)
            write_proxy_log(
                log_file, transaction_id, "request_shape", self.command,
                self.path, self.server.upstream_model, "-", 0, "",
                "previous=%d,expanded=%d,normalized=%d,input=%s" %
                (had_previous, expanded, normalized,
                 ",".join(input_types) or "-"))
            model = str(payload.get("model") or "")
            if model == self.server.alias or model.startswith(self.server.alias + "["):
                payload["model"] = self.server.upstream_model
            if (text_normalized or reasoning_normalized or
                    model == self.server.alias or model.startswith(
                    self.server.alias + "[")):
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        target = urlsplit(self.server.upstream_url)
        conn_type = (http.client.HTTPSConnection if target.scheme == "https"
                     else http.client.HTTPConnection)
        connect = lambda: conn_type(target.hostname, target.port, timeout=90)
        headers = upstream_request_headers(self.headers, len(body))
        conn = None
        try:
            request_path = upstream_request_path(
                self.server.upstream_url, self.path)
            def on_retry(status, attempt, delay):
                write_proxy_log(
                    log_file, transaction_id, "retry", self.command,
                    self.path, self.server.upstream_model, status,
                    (time.time() - started) * 1000, "",
                    "attempt=%d,delay_ms=%d" % (attempt, delay * 1000))
            conn, response = request_upstream_with_retry(
                connect, self.command, request_path, body, headers,
                on_retry=on_retry)
            write_proxy_log(log_file, transaction_id, "response",
                            self.command, self.path,
                            self.server.upstream_model, response.status,
                            (time.time() - started) * 1000, "")
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in ("content-length", "transfer-encoding",
                                   "connection", "server", "date"):
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            captured = bytearray()
            while True:
                chunk = read_upstream_response(response, 65536)
                if not chunk:
                    break
                captured.extend(chunk)
                if len(captured) > _MAX_RESPONSE_CAPTURE:
                    del captured[:-_MAX_RESPONSE_CAPTURE]
                self.wfile.write(chunk)
                self.wfile.flush()
            if response.status == 200:
                response_id, calls = extract_response_output(bytes(captured))
                self.server.response_call_cache.put(response_id, calls)
                write_proxy_log(
                    log_file, transaction_id, "response_shape", self.command,
                    self.path, self.server.upstream_model, response.status,
                    (time.time() - started) * 1000, "",
                    "id=%d,output=%s" %
                    (bool(response_id), ",".join(str(item.get("type") or "-")
                                                for item in calls) or "-"))
        except Exception as exc:
            write_proxy_log(log_file, transaction_id, "error",
                            self.command, self.path,
                            self.server.upstream_model, "-",
                            (time.time() - started) * 1000,
                            exc.__class__.__name__)
            raise
        finally:
            if conn:
                conn.close()


def bind_server(port, upstream_url="", upstream_model="", alias="", owner="",
                reasoning_mapping=None):
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), _Handler)
    actual = int(server.server_address[1])
    if actual <= 0:
        server.server_close()
        raise RuntimeError("API proxy did not receive a port")
    server.upstream_url = str(upstream_url).rstrip("/")
    server.upstream_model = str(upstream_model)
    server.alias = str(alias)
    server.owner = str(owner or "")
    server.reasoning_mapping = dict(reasoning_mapping or {})
    server.transaction_count = 0
    server.transaction_lock = threading.Lock()
    server.response_call_cache = ResponseCallCache()
    return server


def run_api_proxy(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--upstream-model", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--owner", default="")
    parser.add_argument("--reasoning-map", default="{}")
    args = parser.parse_args(argv)
    try:
        reasoning_mapping = json.loads(args.reasoning_map)
    except ValueError:
        reasoning_mapping = {}
    server = bind_server(args.port, args.upstream_url, args.upstream_model,
                         args.alias, args.owner, reasoning_mapping)
    print(server.server_address[1], flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
