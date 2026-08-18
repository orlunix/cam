"""Focused contract tests for proxy-backed API profile routing."""

import json
import os
import subprocess
import sys

import pytest

from camc_pkg.api_resolver import (
    apply_resolved_token,
    resolve_run_plan,
)
from camc_pkg.api_routing import build_routing_plan
from camc_pkg.api_store import _default_seed, ensure_ready


def test_curated_claude_client_aliases_are_plain_and_unambiguous():
    seed = _default_seed()
    expected = {
        "deepseek-v4-flash": "claude-haiku-5-dsv4f",
        "deepseek-v4-pro": "claude-opus-5-dsv4p",
        "glm-5.2": "claude-sonnet-5-glm52",
        "kimi-k2.6": "claude-sonnet-5-kimi26",
    }
    for key, alias in expected.items():
        entry = seed["apis"][key]
        assert alias in entry["aliases"]
        assert "claude:proxy:" not in " ".join(entry["aliases"])


def test_claude_proxy_plan_keeps_upstream_model_and_marks_proxy(tmp_path, monkeypatch):
    models = tmp_path / "api-models.json"
    monkeypatch.setattr("camc_pkg.api_store.API_MODELS_FILE", str(models))
    monkeypatch.setattr("camc_pkg.api_store.CAM_DIR", str(tmp_path))
    data = ensure_ready()
    data["apis"]["deepseek-v4-flash"]["enabled"] = True
    models.write_text(json.dumps(data))
    plan = resolve_run_plan("claude", "deepseek-v4-flash")
    assert plan["client_model"] == "claude-haiku-5-dsv4f"
    assert plan["proxy_required"] is True
    assert plan["model"] == "nvidia/deepseek-ai/eccn-deepseek-v4-flash"


def test_claude_client_alias_is_not_allowed_for_codex(tmp_path, monkeypatch):
    models = tmp_path / "api-models.json"
    monkeypatch.setattr("camc_pkg.api_store.API_MODELS_FILE", str(models))
    monkeypatch.setattr("camc_pkg.api_store.CAM_DIR", str(tmp_path))
    data = ensure_ready()
    data["apis"]["deepseek-v4-flash"]["enabled"] = True
    models.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not supported for tool"):
        resolve_run_plan("codex", "claude-haiku-5-dsv4f")


def test_alias_proxy_requires_a_real_ephemeral_port(monkeypatch):
    from camc_pkg import api_proxy

    class FakeServer(object):
        server_address = ("127.0.0.1", 18432)

    monkeypatch.setattr(api_proxy, "ThreadingHTTPServer",
                        lambda *_args: FakeServer())
    server = api_proxy.bind_server(0)
    assert server.server_address[1] == 18432


def test_seed_has_one_direct_base_url_and_no_proxy_schema():
    seed = _default_seed()
    assert "default_provider" not in seed
    assert "default" not in seed
    provider = seed["providers"]["inference-hub"]
    assert provider["base_url"] == "https://inference-api.nvidia.com"
    assert "client_base_url" not in provider
    assert "upstream_protocol" not in provider
    assert "translator" not in provider
    assert "endpoints" not in provider
    assert provider["catalog_path"] == "/v1/models"
    for entry in seed["apis"].values():
        assert "url" not in entry
        assert "client_url" not in entry


def test_proxy_routing_requires_native_tool_endpoint():
    provider = {
        "base_url": "https://inference-api.nvidia.com",
    }
    entry = {
        "available_endpoints": ["completions"],
        "model": "nvidia/example/model",
    }
    with pytest.raises(ValueError, match="native.*messages"):
        build_routing_plan("claude", provider, entry)


def test_run_plan_always_has_proxy_controls(tmp_path, monkeypatch):
    models = tmp_path / "api-models.json"
    monkeypatch.setattr("camc_pkg.api_store.API_MODELS_FILE", str(models))
    monkeypatch.setattr("camc_pkg.api_store.CAM_DIR", str(tmp_path))
    data = ensure_ready()
    data["apis"]["glm-5.2"]["enabled"] = True
    models.write_text(json.dumps(data))
    plan = resolve_run_plan("claude", "glm-5.2")
    assert plan["mode"] == "proxy"
    assert "route" not in plan
    assert plan["proxy_required"] is True
    assert plan["local_base_url"] == "https://inference-api.nvidia.com"


def test_claude_direct_api_uses_anthropic_auth_token():
    env = {
        "ANTHROPIC_BASE_URL": "https://inference-api.nvidia.com",
        "ANTHROPIC_AUTH_TOKEN": "",
    }
    resolved = apply_resolved_token("claude", env, "inference-token")
    assert resolved["ANTHROPIC_AUTH_TOKEN"] == "inference-token"
    assert "ANTHROPIC_API_KEY" not in resolved


def test_cli_exposes_no_proxy_flags_or_subcommand():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    help_result = subprocess.run(
        [sys.executable, "-m", "camc_pkg", "run", "--help"],
        env=env, text=True, capture_output=True, check=True)
    assert "--no-api-proxy" not in help_result.stdout
    assert "--proxy-debug" not in help_result.stdout
    proxy_result = subprocess.run(
        [sys.executable, "-m", "camc_pkg", "api", "proxy"],
        env=env, text=True, capture_output=True)
    assert proxy_result.returncode != 0
