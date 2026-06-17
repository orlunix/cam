"""Tests for camc API profiles and resolver."""

import json
import os
import tempfile

import pytest

from camc_pkg.api_store import (
    ensure_ready,
    list_apis,
    rebuild_aliases,
    resolve_api_name,
    _default_seed,
)
from camc_pkg.api_resolver import resolve_run_plan
from camc_pkg.api_token import resolve_token


@pytest.fixture
def api_models_file(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "api-models.json")
        monkeypatch.setattr("camc_pkg.api_store.API_MODELS_FILE", path)
        monkeypatch.setattr("camc_pkg.api_store.CAM_DIR", tmp)
        yield path


class TestApiStore:
    def test_seed_creates_curated_apis(self, api_models_file):
        data = ensure_ready()
        assert "glm-5.1" in data["apis"]
        assert data["apis"]["glm-5.1"]["model"].startswith("nvidia/")

    def test_alias_resolution(self, api_models_file):
        data = ensure_ready()
        assert resolve_api_name(data, "glm") == "glm-5.1"
        assert resolve_api_name(data, "GLM-5.1") == "glm-5.1"

    def test_list_hides_disabled_by_default(self, api_models_file):
        data = ensure_ready()
        rows = list_apis(data, show_all=False)
        assert rows == []
        data["apis"]["glm-5.1"]["enabled"] = True
        rows = list_apis(data, show_all=False)
        assert len(rows) == 1

    def test_rebuild_aliases(self):
        data = _default_seed()
        data["apis"]["test-api"] = {
            "provider": "inference-hub",
            "model": "nvidia/foo/bar",
            "aliases": ["foo"],
        }
        rebuild_aliases(data)
        assert data["_aliases"]["foo"] == "test-api"


class TestApiToken:
    def test_cli_token_wins(self, monkeypatch):
        token, src = resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"], cli_token="abc")
        assert token == "abc"
        assert src == "cli"

    def test_env_token(self, monkeypatch):
        monkeypatch.setenv("INFERENCE_HUB_TOKEN", "from-env")
        token, src = resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == "from-env"
        assert src.startswith("env:")


class TestApiResolver:
    def test_proxy_plan_for_claude(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "glm-5.1")
        assert plan["mode"] == "proxy"
        assert plan["route"] == "completions_to_messages"
        assert plan["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert plan["env"]["ANTHROPIC_MODEL"] == "glm-5.1"

    def test_disabled_api_rejected(self, api_models_file):
        ensure_ready()
        with pytest.raises(ValueError, match="disabled"):
            resolve_run_plan("claude", "glm-5.1")

    def test_no_api_proxy_flag(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="no-api-proxy"):
            resolve_run_plan("claude", "glm-5.1", no_api_proxy=True)

    def test_codex_api_not_supported(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="not supported for tool 'codex'"):
            resolve_run_plan("codex", "glm-5.1")

    def test_non_curated_api_not_supported(self, api_models_file):
        data = ensure_ready()
        data["apis"]["my-custom"] = {
            "provider": "inference-hub",
            "model": "nvidia/foo/bar",
            "enabled": True,
            "aliases": [],
        }
        rebuild_aliases(data)
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="not supported for --api"):
            resolve_run_plan("claude", "my-custom")
