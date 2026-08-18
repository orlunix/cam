"""Runtime compatibility contracts for the API proxy launcher."""

from camc_pkg import api_proxy, cli
from pathlib import Path


def test_api_proxy_has_no_inline_stripped_package_import():
    source = Path("src/camc_pkg/api_proxy.py").read_text()

    assert "from camc_pkg import CAM_DIR" not in source


def test_proxy_does_not_duplicate_upstream_v1_prefix():
    assert api_proxy.upstream_request_path(
        "https://inference-api.nvidia.com/v1", "/v1/responses") == "/v1/responses"


def test_proxy_requests_uncompressed_upstream_for_response_cache():
    headers = api_proxy.upstream_request_headers({
        "Authorization": "Bearer secret",
        "Accept-Encoding": "gzip, br",
        "Connection": "keep-alive",
    }, 123)

    assert headers["Accept-Encoding"] == "identity"
    assert headers["Content-Length"] == "123"
    assert "Connection" not in headers


def test_api_proxy_uses_python36_compatible_text_pipe(monkeypatch):
    captured = {}

    class FakeStdout(object):
        def readline(self):
            return "18432\n"

    class FakeProcess(object):
        pid = 99
        stdout = FakeStdout()

    def fake_popen(_command, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    plan = {
        "upstream_base_url": "https://inference-api.nvidia.com",
        "model": "nvidia/test",
        "client_model": "nvidia/test",
        "env": {},
    }

    _proc, port = cli._start_api_proxy(plan, "deadbeef")

    assert port == 18432
    assert captured["universal_newlines"] is True
    assert "text" not in captured


def test_debug_api_proxy_launch_uses_owner_without_log_file(monkeypatch, tmp_path):
    captured = {}

    class FakeStdout(object):
        def readline(self):
            return "18433\n"

    class FakeProcess(object):
        pid = 100
        stdout = FakeStdout()

    def fake_popen(command, **_kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "LOGS_DIR", str(tmp_path))
    plan = {
        "upstream_base_url": "https://inference-api.nvidia.com/v1",
        "model": "nvidia/test",
        "client_model": "nvidia/test",
        "debug_api_proxy": True,
    }

    cli._start_api_proxy(plan, "deadbeef")

    assert "--owner" in captured["command"]
    assert "--log-file" not in captured["command"]


def test_debug_proxy_log_has_no_request_body_or_auth(tmp_path):
    path = tmp_path / "proxy.log"

    api_proxy.write_proxy_log(
        str(path), "tx-1", "response", "POST", "/v1/responses?secret=no",
        "nvidia/test", 200, 17, "")

    text = path.read_text()
    assert "method=POST path=/v1/responses model=nvidia/test status=200 duration_ms=17" in text
    assert "secret=no" not in text
    assert "Authorization" not in text


def test_debug_proxy_log_correlates_request_and_response(tmp_path):
    path = tmp_path / "proxy.log"

    api_proxy.write_proxy_log(
        str(path), "tx-1", "request", "POST", "/v1/responses?secret=no",
        "nvidia/test", "-", 0, "")
    api_proxy.write_proxy_log(
        str(path), "tx-1", "response", "POST", "/v1/responses",
        "nvidia/test", 200, 17, "")

    rows = path.read_text().splitlines()
    assert "tx=tx-1 event=request method=POST path=/v1/responses" in rows[0]
    assert "tx=tx-1 event=response method=POST path=/v1/responses" in rows[1]


def test_proxy_debug_switch_is_read_from_json_at_request_time(monkeypatch, tmp_path):
    model_file = tmp_path / "api-models.json"
    monkeypatch.setattr(api_proxy, "API_MODELS_FILE", str(model_file))
    model_file.write_text('{"debug_api_proxy": true}\n')

    assert api_proxy.debug_log_file_for_owner("deadbeef") == str(
        tmp_path / "logs" / "api-proxy-deadbeef.log")

    model_file.write_text('{"debug_api_proxy": false}\n')
    assert api_proxy.debug_log_file_for_owner("deadbeef") == ""


def test_tool_continuation_is_expanded_from_all_cached_output_items():
    reasoning = {"type": "reasoning", "encrypted_content": "opaque"}
    call = {"type": "custom_tool_call", "call_id": "call-1",
            "name": "read_file", "input": "opaque"}
    cache = {"resp-1": [reasoning, call]}
    payload = {
        "model": "nvidia/test",
        "previous_response_id": "resp-1",
        "input": [{"type": "custom_tool_call_output", "call_id": "call-1",
                   "output": "unit_ok"}],
    }

    assert api_proxy.expand_tool_continuation(payload, cache) is True
    assert "previous_response_id" not in payload
    assert payload["input"] == [reasoning, call, {
        "type": "custom_tool_call_output", "call_id": "call-1",
        "output": "unit_ok",
    }]


def test_unknown_tool_continuation_is_left_unchanged():
    payload = {"previous_response_id": "unknown",
               "input": [{"type": "function_call_output",
                          "call_id": "call-1", "output": "unit_ok"}]}

    assert api_proxy.expand_tool_continuation(payload, {}) is False
    assert payload["previous_response_id"] == "unknown"


def test_tool_history_drops_reasoning_items_for_ih_compatibility():
    payload = {"input": [
        {"type": "message", "role": "user", "content": []},
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "call-1"},
        {"type": "message", "role": "assistant", "content": []},
        {"type": "function_call_output", "call_id": "call-1",
         "output": "unit_ok"},
    ]}

    assert api_proxy.normalize_tool_history(payload) is True
    assert [item["type"] for item in payload["input"]] == [
        "message", "function_call", "function_call_output"]


def test_responses_text_format_defaults_are_made_explicit_without_overwrite():
    missing = {"text": {"verbosity": "low"}}
    explicit = {"text": {"verbosity": "low", "format": {
        "type": "json_schema", "name": "result", "schema": {},
    }}}
    absent = {"model": "nvidia/test"}

    assert api_proxy.normalize_response_text_format(missing) is True
    assert missing["text"] == {
        "verbosity": "low", "format": {"type": "text"},
    }
    assert api_proxy.normalize_response_text_format(missing) is False
    assert api_proxy.normalize_response_text_format(explicit) is False
    assert explicit["text"]["format"]["type"] == "json_schema"
    assert api_proxy.normalize_response_text_format(absent) is False
    assert absent == {"model": "nvidia/test"}


def test_codex_reasoning_effort_uses_json_mapping():
    payload = {"reasoning": {"effort": "low", "summary": "auto"}}
    mapping = {"low": "high", "high": "high", "max": "max"}

    assert api_proxy.normalize_reasoning_effort(payload, mapping) is True
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert api_proxy.normalize_reasoning_effort(payload, mapping) is False
    assert api_proxy.normalize_reasoning_effort(
        {"reasoning": {"effort": "low"}}, {}) is False


def test_response_output_is_extracted_from_json_and_sse():
    reasoning = {"type": "reasoning", "encrypted_content": "opaque"}
    call = {"type": "function_call", "call_id": "call-1",
            "name": "unit_ping", "arguments": "{}"}
    response = {"id": "resp-1", "object": "response",
                "output": [reasoning, call]}
    raw = __import__("json").dumps(response).encode("utf-8")
    assert api_proxy.extract_response_output(raw) == (
        "resp-1", [reasoning, call])

    event = {"type": "response.completed", "response": response}
    sse = ("event: response.completed\ndata: %s\n\n" %
           __import__("json").dumps(event)).encode("utf-8")
    assert api_proxy.extract_response_output(sse) == (
        "resp-1", [reasoning, call])


def test_response_call_cache_is_bounded():
    cache = api_proxy.ResponseCallCache(max_entries=2)
    cache.put("resp-1", [{"type": "function_call", "call_id": "1"}])
    cache.put("resp-2", [{"type": "function_call", "call_id": "2"}])
    cache.put("resp-3", [{"type": "function_call", "call_id": "3"}])

    assert cache.get("resp-1") is None
    assert cache.get("resp-2")[0]["call_id"] == "2"
    assert cache.get("resp-3")[0]["call_id"] == "3"


def test_upstream_429_is_retried_without_exposing_intermediate_response():
    class Response(object):
        def __init__(self, status, body=b"", retry_after=None):
            self.status = status
            self.body = body
            self.retry_after = retry_after
            self.reads = 0

        def read(self, _size=None):
            self.reads += 1
            body, self.body = self.body, b""
            return body

        def getheader(self, name):
            return self.retry_after if name.lower() == "retry-after" else None

    class Connection(object):
        def __init__(self, response):
            self.response = response
            self.closed = False

        def request(self, *_args):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    responses = [Response(429, b"busy", "2"), Response(429, b"busy"),
                 Response(200, b"ok")]
    connections = []
    sleeps = []

    def connect():
        conn = Connection(responses[len(connections)])
        connections.append(conn)
        return conn

    clock = [0.0]

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    conn, response = api_proxy.request_upstream_with_retry(
        connect, "POST", "/v1/responses", b"{}", {}, sleep=sleep,
        monotonic=lambda: clock[0], jitter=lambda delay: delay)

    assert response.status == 200
    assert sleeps == [2.0, 2.0]
    assert all(item.reads == 1 for item in responses[:2])
    assert all(conn.closed for conn in connections[:2])
    assert conn is connections[2]


def test_upstream_generic_internal_400_is_retried_but_bounded():
    class Response(object):
        def __init__(self, status, body=b""):
            self.status = status
            self.body = body

        def read(self, _size=None):
            body, self.body = self.body, b""
            return body

        def getheader(self, _name):
            return None

    class Connection(object):
        def __init__(self, response):
            self.response = response
            self.closed = False

        def request(self, *_args):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    body = (b'{"error":{"code":"invalid_argument",'
            b'"message":"an internal error occurred"}}')
    responses = [Response(400, body), Response(400, body), Response(200)]
    connections = []

    def connect():
        conn = Connection(responses[len(connections)])
        connections.append(conn)
        return conn

    clock = [0.0]
    sleeps = []
    conn, response = api_proxy.request_upstream_with_retry(
        connect, "POST", "/v1/responses", b"{}", {},
        sleep=lambda delay: (sleeps.append(delay),
                             clock.__setitem__(0, clock[0] + delay)),
        monotonic=lambda: clock[0], jitter=lambda delay: delay)

    assert response.status == 200
    assert len(connections) == 3
    assert sleeps == [1.0, 2.0]
    assert all(item.closed for item in connections[:2])
    conn.close()


def test_upstream_schema_400_is_not_retried():
    class Response(object):
        def __init__(self, status, body):
            self.status = status
            self.body = body

        def read(self, _size=None):
            body, self.body = self.body, b""
            return body

        def getheader(self, _name):
            return None

    class Connection(object):
        def __init__(self, response):
            self.response = response
            self.closed = False

        def request(self, *_args):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    response = Response(
        400, b'{"error":{"code":"invalid_argument",'
        b'"message":"missing field `format`"}}')
    connections = []

    def connect():
        conn = Connection(response)
        connections.append(conn)
        return conn

    conn, result = api_proxy.request_upstream_with_retry(
        connect, "POST", "/v1/responses", b"{}", {},
        sleep=lambda _delay: None)

    assert result.status == 400
    assert len(connections) == 1
    assert api_proxy.read_upstream_response(result) == (
        b'{"error":{"code":"invalid_argument",'
        b'"message":"missing field `format`"}}')
    conn.close()


def test_upstream_retry_policy_is_data_driven_and_other_errors_pass_through():
    class Response(object):
        def __init__(self, status):
            self.status = status

        def read(self, _size=None):
            return b""

        def getheader(self, _name):
            return None

    class Connection(object):
        def __init__(self, status):
            self.response = Response(status)
            self.closed = False

        def request(self, *_args):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    statuses = [503, 200]
    calls = []

    def connect():
        conn = Connection(statuses[len(calls)])
        calls.append(conn)
        return conn

    clock = [0.0]
    conn, response = api_proxy.request_upstream_with_retry(
        connect, "POST", "/", b"", {},
        policies={503: {"max_elapsed": 10, "initial_delay": 1,
                        "max_delay": 1}},
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
        monotonic=lambda: clock[0], jitter=lambda delay: delay)
    assert response.status == 200
    assert len(calls) == 2
    conn.close()

    calls[:] = []
    statuses[:] = [500]
    conn, response = api_proxy.request_upstream_with_retry(
        connect, "POST", "/", b"", {}, sleep=lambda _delay: None)
    assert response.status == 500
    assert len(calls) == 1
    conn.close()


def test_codex_api_launch_overrides_project_local_model(monkeypatch, tmp_path):
    config = type("Config", (), {
        "command": ["codex", "-c", "features.goals=true"],
    })()
    monkeypatch.setattr(cli, "_load_config", lambda _tool: config)

    command = cli._build_command(config, "", str(tmp_path))
    api_plan = {"model": "nvidia/moonshotai/eccn-kimi-k3"}
    command += cli._codex_api_model_override(api_plan)

    assert command[-2:] == [
        "-c",
        'model="nvidia/moonshotai/eccn-kimi-k3"',
    ]
