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
_MAX_CONNECT_ATTEMPTS = 3
_HOP_HEADERS = frozenset((
    "connection", "content-length", "host", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "accept-encoding",
))
_NATIVE_PATHS = {
    "/messages": "messages",
    "/messages/count_tokens": "messages",
    "/responses": "responses",
    "/chat/completions": "completions",
}


def _is_retryable_internal_400(status, body):
    """Identify IH's transient generic internal 400, not schema errors."""
    if int(status or 0) != 400:
        return False
    text = bytes(body or b"").decode("utf-8", "replace").lower()
    return "invalid_argument" in text and "internal error occurred" in text


RETRY_STATUS_POLICIES = {
    408: {"max_elapsed": 8.0, "initial_delay": .5, "max_delay": 2.0},
    409: {"max_elapsed": 8.0, "initial_delay": .5, "max_delay": 2.0},
    429: {"max_elapsed": 300.0, "initial_delay": 1.0,
          "max_delay": 30.0},
    500: {"max_elapsed": 8.0, "initial_delay": .5, "max_delay": 2.0},
    502: {"max_elapsed": 8.0, "initial_delay": .5, "max_delay": 2.0},
    503: {"max_elapsed": 8.0, "initial_delay": .5, "max_delay": 2.0},
    504: {"max_elapsed": 8.0, "initial_delay": .5, "max_delay": 2.0},
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
              if key.lower() not in _HOP_HEADERS}
    result["Accept-Encoding"] = "identity"
    result["Content-Length"] = str(body_length)
    return result


def native_endpoint_for_path(request_path):
    """Return the native protocol name for one supported LLM path."""
    path = urlsplit(str(request_path or "/")).path.rstrip("/") or "/"
    if path.startswith("/v1/"):
        path = path[3:]
    return _NATIVE_PATHS.get(path)


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
                                policies=None, sleep=None,
                                monotonic=None,
                                jitter=None, on_retry=None):
    """Open an upstream response, hiding only configured transient statuses."""
    policies = RETRY_STATUS_POLICIES if policies is None else policies
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    jitter = jitter or (lambda delay: random.uniform(delay * .8, delay * 1.2))
    started, attempts = monotonic(), 0
    while True:
        conn = None
        try:
            conn = connect()
            conn.request(method, path, body, headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException):
            if conn:
                try:
                    conn.close()
                except (IOError, OSError):
                    pass
            attempts += 1
            delay = min(2.0, .5 * (2 ** (attempts - 1)))
            if attempts >= _MAX_CONNECT_ATTEMPTS:
                raise
            if on_retry:
                on_retry("connection", attempts, delay)
            sleep(delay)
            continue
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
        if retry_after is not None:
            retry_after = min(retry_after, float(policy.get("max_delay", 30)))
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


def _cleanup_agent_runtime_home(agent, owner):
    """Clean an owned adapter overlay plus obsolete API-home state."""
    runtime = agent.get("runtime") if isinstance(agent, dict) else None
    storage = runtime.get("storage") if isinstance(runtime, dict) else None
    home_info = storage.get("overlay") if isinstance(storage, dict) else None
    if not isinstance(home_info, dict) and isinstance(runtime, dict):
        home_info = runtime.get("home")  # short-lived full-home WIP records
    if isinstance(home_info, dict):
        try:
            from camc_pkg.runtime_env import remove_agent_runtime_home
            remove_agent_runtime_home(home_info, owner_id=owner)
        except (IOError, OSError, ValueError):
            pass

    task = agent.get("task") if isinstance(agent, dict) else None
    tool = ((task or {}).get("tool") if isinstance(task, dict) else None) or (
        agent.get("tool") if isinstance(agent, dict) else None)
    if tool == "codex" and owner:
        try:
            from camc_pkg.api_resolver import remove_codex_api_config_dir
            remove_codex_api_config_dir(owner)
        except (IOError, OSError, ValueError):
            pass


# Kept as a private compatibility alias for older bundled callers.
_cleanup_agent_codex_home = _cleanup_agent_runtime_home


def stop_agent_api_proxy(agent, cleanup_home=True):
    """Terminate an owned proxy; overlay cleanup is lifecycle-owned.

    Monitor terminal paths call this only after tmux is gone and use the
    cleanup default.  CLI kill/rm explicitly defer cleanup until after their
    own tmux termination so open files cannot race recursive removal.
    """
    api = agent.get("api") if isinstance(agent, dict) else None
    owner = agent.get("id", "") if isinstance(agent, dict) else ""
    try:
        pid = int((api or {}).get("proxy_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
            marker = "--owner\x00%s\x00" % owner
            if "_api_proxy\x00" in cmdline and marker in cmdline:
                os.kill(pid, signal.SIGTERM)
        except (IOError, OSError):
            pass
    if cleanup_home:
        _cleanup_agent_runtime_home(agent, owner)


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
    """Map client effort names for Responses and Claude Messages payloads."""
    if not isinstance(payload, dict) or not isinstance(mapping, dict):
        return False
    normalized = False
    for field in ("reasoning", "output_config"):
        config = payload.get(field)
        if not isinstance(config, dict):
            continue
        effort = str(config.get("effort") or "").strip().lower()
        mapped = str(mapping.get(effort) or "").strip().lower()
        if not mapped or mapped == effort:
            continue
        config["effort"] = mapped
        normalized = True
    return normalized


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


def extract_usage(body):
    """Observe the last JSON/SSE usage object without changing response bytes."""
    if not body:
        return {}
    objects = []
    try:
        objects.append(json.loads(bytes(body).decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        for raw in bytes(body).splitlines():
            if not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                objects.append(json.loads(data.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                continue
    usage = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        candidate = obj.get("usage")
        response = obj.get("response")
        if not isinstance(candidate, dict) and isinstance(response, dict):
            candidate = response.get("usage")
        if isinstance(candidate, dict):
            usage = dict(candidate)
    return usage


def usage_log_shape(usage, complete=True):
    """Flatten only token counters for body- and credential-free logging."""
    if not isinstance(usage, dict):
        usage = {}
    fields = {"complete": int(bool(complete))}
    aliases = (
        ("input_tokens", ("input_tokens", "prompt_tokens")),
        ("output_tokens", ("output_tokens", "completion_tokens")),
        ("total_tokens", ("total_tokens",)),
        ("cache_read_input_tokens", ("cache_read_input_tokens",)),
        ("cache_creation_input_tokens", ("cache_creation_input_tokens",)),
    )
    for key, candidates in aliases:
        value = next((usage.get(name) for name in candidates
                      if isinstance(usage.get(name), (int, float)) and
                      not isinstance(usage.get(name), bool)), None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[key] = value
    details = (usage.get("input_tokens_details") or
               usage.get("prompt_tokens_details"))
    if isinstance(details, dict):
        value = details.get("cached_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields["cached_tokens"] = value
    return ",".join("%s=%s" % (key, fields[key]) for key in sorted(fields))


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
        if urlsplit(self.path).path.rstrip("/") in ("/health", "/healthz"):
            raw = json.dumps({"ok": True, "status": "ok",
                              "route": "camc_proxy",
                              "mode": "native-endpoints-only"},
                             separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        started = time.time()
        endpoint = native_endpoint_for_path(self.path)
        available = self.server.available_endpoints
        if not endpoint or (available and endpoint not in available):
            raw = json.dumps({"error": {"type": "invalid_request_error",
                                        "message": "native endpoint unavailable; protocol conversion is disabled"}},
                             separators=(",", ":")).encode("utf-8")
            self.send_response(400 if endpoint else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
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
            input_types = []
            had_previous = False
            expanded = normalized = text_normalized = False
            if endpoint == "responses":
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
            model_changed = payload.get("model") != self.server.upstream_model
            payload["model"] = self.server.upstream_model
            if (expanded or normalized or text_normalized or
                    reasoning_normalized or model_changed):
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
            try:
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() in _HOP_HEADERS or key.lower() in ("server", "date"):
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
            except OSError:
                write_proxy_log(
                    log_file, transaction_id, "request_complete", self.command,
                    self.path, self.server.upstream_model, response.status,
                    (time.time() - started) * 1000, "ClientDisconnect",
                    usage_log_shape({}, complete=False))
                return
            captured = bytearray()
            complete = True
            while True:
                chunk = read_upstream_response(response, 65536)
                if not chunk:
                    break
                captured.extend(chunk)
                if len(captured) > _MAX_RESPONSE_CAPTURE:
                    del captured[:-_MAX_RESPONSE_CAPTURE]
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    complete = False
                    self.close_connection = True
                    break
            usage = extract_usage(bytes(captured))
            write_proxy_log(
                log_file, transaction_id, "request_complete", self.command,
                self.path, self.server.upstream_model, response.status,
                (time.time() - started) * 1000,
                "" if complete else "ClientDisconnect",
                usage_log_shape(usage, complete=complete))
            if response.status == 200 and endpoint == "responses":
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
                reasoning_mapping=None, available_endpoints=None):
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
    server.available_endpoints = frozenset(
        ("completions", "messages", "responses")
        if available_endpoints is None else available_endpoints)
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
    parser.add_argument("--available-endpoints", default="")
    parser.add_argument("--reasoning-map", default="{}")
    args = parser.parse_args(argv)
    # The proxy belongs to the agent record, not to the short-lived `run`
    # invocation that launched it. A terminal/session teardown must not take
    # the proxy down before `rm` can stop it by its recorded owner PID.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        reasoning_mapping = json.loads(args.reasoning_map)
    except ValueError:
        reasoning_mapping = {}
    try:
        available_endpoints = (json.loads(args.available_endpoints)
                               if args.available_endpoints else None)
    except ValueError:
        available_endpoints = None
    server = bind_server(args.port, args.upstream_url, args.upstream_model,
                         args.alias, args.owner, reasoning_mapping,
                         available_endpoints)
    print(server.server_address[1], flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
