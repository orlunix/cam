"""Proxy GET /v1/models metadata enrichment."""

import io
import json

from camc_pkg.api_metadata import openai_models_list_response
from camc_pkg.proxy.messages import MessagesHandler, MessagesServer
from camc_pkg.proxy.common import ProxyLogger


class _CaptureHandler(MessagesHandler):
    status = None
    body = b""

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        pass

    def end_headers(self):
        pass

    def _send_bytes(self, body, content_type, status=200):
        self.status = status
        self.body = body


class TestProxyModelsEndpoint:
    def test_messages_models_includes_limits(self):
        server = MessagesServer(
            ("127.0.0.1", 0),
            _CaptureHandler,
            api_key="test",
            model_alias="glm-5.1",
            upstream_model="nvidia/zai-org/eccn-glm-5.1",
            upstream_url="https://example/v1/chat/completions",
            timeout=30,
            debug=ProxyLogger("test", False, "/dev/null"),
            model_metadata={"context_window": 202752, "max_output_tokens": 131072},
        )
        handler = _CaptureHandler.__new__(_CaptureHandler)
        handler.server = server
        handler.path = "/v1/models"
        handler.headers = {}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"

        handler.do_GET()

        assert handler.status == 200
        payload = json.loads(handler.body.decode("utf-8"))
        row = payload["data"][0]
        assert row["id"] == "glm-5.1"
        assert row["max_input_tokens"] == 202752
        assert row["max_output_tokens"] == 131072

    def test_openai_models_list_helper(self):
        payload = openai_models_list_response("glm-5.1", {"context_window": 1000})
        assert payload["data"][0]["max_input_tokens"] == 1000
