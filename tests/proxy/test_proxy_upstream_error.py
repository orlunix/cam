"""Upstream HTTP errors must propagate before SSE headers are sent."""

import io
import json
import urllib.error

from camc_pkg.proxy.messages import MessagesHandler, MessagesServer


class _Dbg(object):
    def log(self, *a, **kw):
        pass


class _CaptureHandler(MessagesHandler):
    status = None
    content_type = None
    body = b""

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        if keyword.lower() == "content-type":
            self.content_type = value

    def end_headers(self):
        pass

    def _send_bytes(self, body, content_type, status=200):
        self.status = status
        self.content_type = content_type
        self.body = body


def test_streaming_upstream_error_returns_http_status(monkeypatch):
    def _raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "http://upstream/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            None,
        )

    monkeypatch.setattr(
        "camc_pkg.proxy.messages.call_chat_completions", _raise_http_error)

    payload = json.dumps({
        "model": "glm-5.1",
        "max_tokens": 16,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")

    srv = MessagesServer(
        ("127.0.0.1", 0),
        _CaptureHandler,
        api_key="test-key",
        model_alias="glm-5.1",
        upstream_model="glm-5.1",
        upstream_url="http://upstream/v1/chat/completions",
        timeout=5.0,
        debug=_Dbg(),
    )
    handler = _CaptureHandler.__new__(_CaptureHandler)
    handler.server = srv
    handler.path = "/v1/messages"
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"

    handler.do_POST()

    assert handler.status == 401
    assert handler.content_type == "application/json"
    body = json.loads(handler.body.decode("utf-8"))
    assert body["error"]["type"] == "upstream_error"
