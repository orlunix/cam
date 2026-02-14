"""Tests for the CAM API Server.

Uses FastAPI's TestClient for synchronous HTTP testing and
verifies REST endpoints, auth, WebSocket events, and config.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cam.api.server import create_app
from cam.core.models import (
    Agent,
    AgentEvent,
    AgentState,
    AgentStatus,
    MachineConfig,
    TaskDefinition,
    TransportType,
)


@pytest.fixture
def app_and_token(tmp_path):
    """Create a test app with temp database and return (TestClient, token)."""
    overrides = {
        "paths": {"data_dir": str(tmp_path)},
        "server": {"auth_token": "test-token-123"},
    }
    app = create_app(overrides=overrides)

    with TestClient(app) as client:
        yield client, "test-token-123"


@pytest.fixture
def client(app_and_token):
    """Just the TestClient."""
    return app_and_token[0]


@pytest.fixture
def auth_headers(app_and_token):
    """Auth headers dict."""
    return {"Authorization": f"Bearer {app_and_token[1]}"}


@pytest.fixture
def authed(app_and_token):
    """Return (client, headers) tuple."""
    client, token = app_and_token
    return client, {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────
# Health endpoint (no auth)
# ──────────────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_no_auth_required(self, client):
        resp = client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "uptime_seconds" in data
        assert data["agents_running"] == 0

    def test_health_returns_version(self, client):
        from cam import __version__

        resp = client.get("/api/system/health")
        assert resp.json()["version"] == __version__


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_auth_returns_401(self, client):
        resp = client.get("/api/agents")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, client):
        resp = client.get(
            "/api/agents", headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code == 401

    def test_valid_token_returns_200(self, authed):
        client, headers = authed
        resp = client.get("/api/agents", headers=headers)
        assert resp.status_code == 200

    def test_no_bearer_prefix_returns_401(self, client):
        resp = client.get(
            "/api/agents", headers={"Authorization": "test-token-123"}
        )
        assert resp.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# Agents
# ──────────────────────────────────────────────────────────────────────


class TestAgents:
    def test_list_agents_empty(self, authed):
        client, headers = authed
        resp = client.get("/api/agents", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["agents"] == []
        assert data["count"] == 0

    def test_get_agent_not_found(self, authed):
        client, headers = authed
        resp = client.get("/api/agents/nonexistent-id", headers=headers)
        assert resp.status_code == 404

    def test_run_agent_requires_context(self, authed):
        client, headers = authed
        resp = client.post(
            "/api/agents",
            json={"tool": "claude", "prompt": "do something"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "Context" in resp.json()["detail"]

    def test_run_agent_context_not_found(self, authed):
        client, headers = authed
        resp = client.post(
            "/api/agents",
            json={
                "tool": "claude",
                "prompt": "do something",
                "context": "nonexistent",
            },
            headers=headers,
        )
        assert resp.status_code == 404

    def test_stop_agent_not_found(self, authed):
        client, headers = authed
        resp = client.delete("/api/agents/nonexistent", headers=headers)
        assert resp.status_code == 404

    def test_logs_agent_not_found(self, authed):
        client, headers = authed
        resp = client.get("/api/agents/nonexistent/logs", headers=headers)
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Contexts
# ──────────────────────────────────────────────────────────────────────


class TestContexts:
    def test_list_contexts_empty(self, authed):
        client, headers = authed
        resp = client.get("/api/contexts", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["contexts"] == []
        assert data["count"] == 0

    def test_create_and_get_context(self, authed):
        client, headers = authed

        # Create
        resp = client.post(
            "/api/contexts",
            json={"name": "test-ctx", "path": "/tmp/test-project"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-ctx"
        assert data["path"] == "/tmp/test-project"

        # Get by name
        resp = client.get("/api/contexts/test-ctx", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-ctx"

    def test_create_context_duplicate_fails(self, authed):
        client, headers = authed

        client.post(
            "/api/contexts",
            json={"name": "dup-ctx", "path": "/tmp/dup"},
            headers=headers,
        )
        resp = client.post(
            "/api/contexts",
            json={"name": "dup-ctx", "path": "/tmp/dup2"},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_delete_context(self, authed):
        client, headers = authed

        client.post(
            "/api/contexts",
            json={"name": "del-ctx", "path": "/tmp/del"},
            headers=headers,
        )
        resp = client.delete("/api/contexts/del-ctx", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify deleted
        resp = client.get("/api/contexts/del-ctx", headers=headers)
        assert resp.status_code == 404

    def test_delete_context_not_found(self, authed):
        client, headers = authed
        resp = client.delete("/api/contexts/nonexistent", headers=headers)
        assert resp.status_code == 404

    def test_list_contexts_after_create(self, authed):
        client, headers = authed

        client.post(
            "/api/contexts",
            json={"name": "list-ctx", "path": "/tmp/list"},
            headers=headers,
        )
        resp = client.get("/api/contexts", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        names = [c["name"] for c in data["contexts"]]
        assert "list-ctx" in names


# ──────────────────────────────────────────────────────────────────────
# Config endpoint
# ──────────────────────────────────────────────────────────────────────


class TestConfig:
    def test_config_requires_auth(self, client):
        resp = client.get("/api/system/config")
        assert resp.status_code == 401

    def test_config_excludes_secrets(self, authed):
        client, headers = authed
        resp = client.get("/api/system/config", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        # auth_token should be stripped
        assert "auth_token" not in data.get("server", {})
        # security section should be stripped
        assert "security" not in data


# ──────────────────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────────────────


class TestWebSocket:
    def test_ws_no_token_rejected(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws"):
                pass

    def test_ws_wrong_token_rejected(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws?token=wrong"):
                pass

    def test_ws_valid_token_connects(self, app_and_token):
        client, token = app_and_token
        with client.websocket_connect(f"/api/ws?token={token}") as ws:
            # Connection established — just close cleanly
            pass

    def test_ws_receives_event(self, app_and_token):
        client, token = app_and_token
        with client.websocket_connect(f"/api/ws?token={token}") as ws:
            # Publish an event on the EventBus
            state = client.app.state.server
            event = AgentEvent(
                agent_id="test-agent-id",
                event_type="test_event",
                detail={"key": "value"},
            )
            state.event_bus.publish(event)

            # Should receive it on the WebSocket
            data = ws.receive_json()
            assert data["type"] == "event"
            assert data["agent_id"] == "test-agent-id"
            assert data["event_type"] == "test_event"
            assert data["detail"]["key"] == "value"

    def test_ws_agent_id_filter(self, app_and_token):
        client, token = app_and_token
        with client.websocket_connect(
            f"/api/ws?token={token}&agent_id=target-agent"
        ) as ws:
            state = client.app.state.server

            # Event for different agent — should be filtered
            other_event = AgentEvent(
                agent_id="other-agent",
                event_type="other",
                detail={},
            )
            state.event_bus.publish(other_event)

            # Event for target agent — should arrive
            target_event = AgentEvent(
                agent_id="target-agent",
                event_type="target",
                detail={"hit": True},
            )
            state.event_bus.publish(target_event)

            data = ws.receive_json()
            assert data["agent_id"] == "target-agent"
            assert data["event_type"] == "target"
