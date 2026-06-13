"""Focused smoke tests for CAM-DESK-REMOTE-012 token injection.

Covers:
  * Desktop-style /client REST-over-WS frame with NO Authorization
    is mutated to carry the configured api_token bearer.
  * Old frame that already has Authorization is preserved.
  * HTTP /api/* injection still works (the original path).
  * Non-/api paths and non-JSON frames are NEVER touched.

These exercise the shared ``_inject_api_token_into_headers`` helper
the relay uses on both HTTP _proxy_api and WS _handle_client paths.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
RELAY_SRC = os.path.join(REPO_ROOT, "relay")
if RELAY_SRC not in sys.path:
    sys.path.insert(0, RELAY_SRC)

from relay import Relay  # noqa: E402


# ---------------------------------------------------------------------------
# _inject_api_token_into_headers — the single decision point
# ---------------------------------------------------------------------------

class TestInjectHelper:
    def test_no_api_token_no_injection(self):
        r = Relay(token="rt", api_token=None)
        h = r._inject_api_token_into_headers({}, "/api/agents")
        assert "authorization" not in {k.lower() for k in h}

    def test_api_path_no_auth_header_injects(self):
        r = Relay(token="rt", api_token="SECRET_API_TOKEN")
        h = r._inject_api_token_into_headers({}, "/api/agents")
        assert h.get("authorization") == "Bearer SECRET_API_TOKEN"

    def test_api_path_with_lowercase_auth_preserved(self):
        r = Relay(token="rt", api_token="SECRET")
        old = {"authorization": "Bearer OLDCLIENT"}
        h = r._inject_api_token_into_headers(old, "/api/agents")
        assert h.get("authorization") == "Bearer OLDCLIENT"

    def test_api_path_with_canonical_Authorization_preserved(self):
        """Old clients sometimes send the canonical capitalization
        ('Authorization' rather than 'authorization'). The injector
        must match case-insensitively and leave the bearer alone."""
        r = Relay(token="rt", api_token="SECRET")
        old = {"Authorization": "Bearer OLDCLIENT"}
        h = r._inject_api_token_into_headers(old, "/api/agents")
        # Old key preserved, no second authorization added.
        assert h.get("Authorization") == "Bearer OLDCLIENT"
        assert "authorization" not in h or h["authorization"] == "Bearer OLDCLIENT"

    def test_non_api_path_not_injected(self):
        r = Relay(token="rt", api_token="SECRET")
        for path in ("/_relay/status", "/health", "/static/index.html",
                     "/", "/apiv2/foo", ""):
            h = r._inject_api_token_into_headers({}, path)
            assert "authorization" not in {k.lower() for k in h}, \
                "non-api path %r got injected" % path

    def test_exact_api_root_injected(self):
        # /api by itself counts (Desktop never hits it but the
        # semantics should be consistent).
        r = Relay(token="rt", api_token="SECRET")
        h = r._inject_api_token_into_headers({}, "/api")
        assert h.get("authorization") == "Bearer SECRET"

    def test_helper_does_not_mutate_input(self):
        """The helper must copy before mutating so the caller's dict
        is left untouched."""
        r = Relay(token="rt", api_token="SECRET")
        old = {"x-other": "y"}
        h = r._inject_api_token_into_headers(old, "/api/agents")
        # Original dict unchanged
        assert "authorization" not in old
        # Returned dict has the injection
        assert h.get("authorization") == "Bearer SECRET"
        assert h.get("x-other") == "y"


# ---------------------------------------------------------------------------
# /client REST-over-WS injection (the Desktop request path)
# ---------------------------------------------------------------------------

class TestClientFrameInjection:
    """Exercise the JSON path that _handle_client uses, without
    spinning up actual sockets. We invoke the same helper the
    inline frame-rewrite branch calls."""

    def _build_frame(self, path, headers=None):
        return {
            "id": "req-1", "method": "GET", "path": path,
            "headers": headers or {}, "body": "",
        }

    def test_desktop_frame_no_auth_gets_injected(self):
        r = Relay(token="rt", api_token="DESKTOP_TOKEN")
        msg = self._build_frame("/api/agents")
        new_headers = r._inject_api_token_into_headers(
            msg["headers"], msg["path"])
        msg["headers"] = new_headers
        assert msg["headers"].get("authorization") == "Bearer DESKTOP_TOKEN"

    def test_old_client_frame_with_auth_preserved(self):
        r = Relay(token="rt", api_token="NEWTOKEN")
        msg = self._build_frame("/api/agents",
                                 {"authorization": "Bearer LEGACY"})
        new_headers = r._inject_api_token_into_headers(
            msg["headers"], msg["path"])
        assert new_headers.get("authorization") == "Bearer LEGACY"

    def test_relay_internal_status_path_not_touched(self):
        r = Relay(token="rt", api_token="SECRET")
        msg = self._build_frame("/_relay/status")
        new_headers = r._inject_api_token_into_headers(
            msg["headers"], msg["path"])
        assert "authorization" not in new_headers
