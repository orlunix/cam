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
        assert "openai-chat-gateway" in data.get("_templates", {})

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
        assert plan["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "202752"
        assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in plan["env"]

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

    def test_codex_api_proxy_plan(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("codex", "glm-5.1")
        assert plan["mode"] == "proxy"
        assert plan["route"] == "completions_to_responses"
        assert plan["env"]["CAMC_CODEX_API_KEY"] == "sk-camc-local"
        from camc_pkg.api_resolver import ensure_codex_api_config_dir
        catalog_path = ensure_codex_api_config_dir("/tmp/v1", "glm-5.1")
        assert catalog_path.endswith("codex-api")
        cfg = open(os.path.join(catalog_path, "config.toml")).read()
        assert "model_catalog_json" in cfg
        assert "camc-model-catalog.json" in cfg
        cat = json.load(open(os.path.join(catalog_path, "camc-model-catalog.json")))
        assert cat["models"][0]["slug"] == "glm-5.1"
        assert cat["models"][0]["context_window"] == 202752

    def test_cursor_api_not_supported(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="not supported for tool 'cursor'"):
            resolve_run_plan("cursor", "glm-5.1")

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

    def test_allow_run_custom_api(self, api_models_file):
        data = ensure_ready()
        data["providers"]["my-gw"] = {
            "display_name": "Gateway",
            "auth_key": "my_gw",
            "env_names": ["MY_GW_KEY"],
            "base_url": "https://gw.example/v1",
            "upstream_protocol": "openai_chat_completions",
            "translator": "embedded",
            "catalog_path": "",
            "endpoints": {"openai_chat_completions": "/chat/completions"},
        }
        data["apis"]["my-custom"] = {
            "provider": "my-gw",
            "model": "my-model",
            "enabled": True,
            "allow_run": True,
            "aliases": [],
        }
        rebuild_aliases(data)
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "my-custom")
        assert plan["translator"] == "embedded"
        assert plan["mode"] == "proxy"

    def test_external_translator_plan(self, api_models_file):
        data = ensure_ready()
        data["providers"]["cc-switch"] = {
            "display_name": "CC Switch",
            "auth_key": "cc_switch",
            "env_names": ["OPENAI_API_KEY"],
            "base_url": "http://127.0.0.1:15721",
            "client_base_url": "http://127.0.0.1:15721",
            "upstream_protocol": "anthropic_messages",
            "translator": "external",
            "catalog_path": "",
            "endpoints": {"anthropic_messages": ""},
        }
        data["apis"]["via-cc"] = {
            "provider": "cc-switch",
            "model": "glm-5.1",
            "enabled": True,
            "allow_run": True,
            "aliases": [],
        }
        rebuild_aliases(data)
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "via-cc")
        assert plan["translator"] == "external"
        assert plan["mode"] == "external"
        assert plan["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:15721"
        assert plan["route"] is None


class TestApiProxyStart:
    def test_api_plan_sets_model_alias_without_default_override(self, api_models_file, monkeypatch):
        """--api NAME must use plan name, not glm-5.1 default."""
        from argparse import Namespace
        from camc_pkg import cli as camc_cli

        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)

        captured = {}

        def _fake_ensure(plan, token):
            captured["plan"] = plan
            return 18324, {"pid": 1}

        monkeypatch.setattr("camc_pkg.proxy.manager.ensure_proxy", _fake_ensure)
        monkeypatch.setenv("INFERENCE_HUB_TOKEN", "test-token")

        args = Namespace(
            route="completions_to_messages",
            port=None,
            api_name="glm-5.1",
            upstream_url=None,
            upstream_model=None,
            model_alias=None,
            debug=False,
        )
        camc_cli.cmd_api_proxy_start(args)
        plan = captured["plan"]
        assert plan["name"] == "glm-5.1"
        assert plan["route"] == "completions_to_messages"
        assert plan["proxy_port"] == 18324
        assert plan["tool_protocol"] == "anthropic_messages"

    def test_api_proxy_start_responses_route_uses_codex_plan(self, api_models_file, monkeypatch):
        """completions_to_responses --api must build Codex/openai_responses plan."""
        from argparse import Namespace
        from camc_pkg import cli as camc_cli

        data = ensure_ready()
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)

        captured = {}

        def _fake_ensure(plan, token):
            captured["plan"] = plan
            return 18325, {"pid": 1}

        monkeypatch.setattr("camc_pkg.proxy.manager.ensure_proxy", _fake_ensure)
        monkeypatch.setenv("INFERENCE_HUB_TOKEN", "test-token")

        args = Namespace(
            route="completions_to_responses",
            port=None,
            api_name="glm-5.1",
            upstream_url=None,
            upstream_model=None,
            model_alias=None,
            debug=False,
        )
        camc_cli.cmd_api_proxy_start(args)
        plan = captured["plan"]
        assert plan["name"] == "glm-5.1"
        assert plan["route"] == "completions_to_responses"
        assert plan["proxy_port"] == 18325
        assert plan["tool_protocol"] == "openai_responses"


class TestApiDefaults:
    def test_seed_does_not_auto_apply_run_default(self, api_models_file):
        from camc_pkg.api_store import _default_seed, resolve_run_api_name
        data = _default_seed()
        name, source = resolve_run_api_name("claude", data=data)
        assert name is None
        assert source == "login"
        assert data.get("defaults") in (None, {})

    def test_legacy_top_level_default_stays_login(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name, resolve_tool_default_api
        data = ensure_ready()
        data.pop("defaults", None)
        data["default"] = "glm-5.1"
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        assert resolve_tool_default_api(data, "claude") is None
        name, source = resolve_run_api_name("claude", data=data)
        assert name is None
        assert source == "login"

    def test_codex_empty_means_login(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": "glm-5.1", "codex": None}
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        name, source = resolve_run_api_name("codex", data=data)
        assert name is None
        assert source == "login"

    def test_default_requires_enabled(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": "glm-5.1"}
        data["apis"]["glm-5.1"]["enabled"] = False
        data["apis"]["glm-5.1"]["enabled_reason"] = "id_not_on_key"
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="disabled"):
            resolve_run_api_name("claude", data=data)

    def test_default_used_when_enabled(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": "glm-5.1"}
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        name, source = resolve_run_api_name("claude", data=data)
        assert name == "glm-5.1"
        assert source == "default"

    def test_no_default_api_skips_default(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": "glm-5.1"}
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        name, source = resolve_run_api_name("claude", no_default_api=True, data=data)
        assert name is None
        assert source == "login"

    def test_set_and_clear_default(self, api_models_file):
        from camc_pkg.api_store import (
            clear_tool_default_api,
            resolve_tool_default_api,
            set_tool_default_api,
        )
        data = ensure_ready()
        set_tool_default_api(data, "codex", "glm-5.1")
        data = ensure_ready()
        assert resolve_tool_default_api(data, "codex") == "glm-5.1"
        clear_tool_default_api(data, "codex")
        data = ensure_ready()
        assert resolve_tool_default_api(data, "codex") is None

    def test_default_show_json(self, api_models_file, capsys):
        from argparse import Namespace
        from camc_pkg import cli as camc_cli

        data = ensure_ready()
        data["defaults"] = {"claude": "glm-5.1"}
        data["apis"]["glm-5.1"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        camc_cli.cmd_api_default_show(Namespace(json=True))
        out = json.loads(capsys.readouterr().out)
        claude = next(r for r in out if r["tool"] == "claude")
        codex = next(r for r in out if r["tool"] == "codex")
        assert claude["api"] == "glm-5.1"
        assert claude["enabled"] is True
        assert codex["mode"] == "login"

    def test_default_show_accepts_json_flag(self, tmp_path):
        import subprocess
        import sys

        from camc_pkg.api_store import _default_seed

        cam_dir = tmp_path / ".cam"
        cam_dir.mkdir()
        path = cam_dir / "api-models.json"
        data = _default_seed()
        data["defaults"] = {"claude": "glm-5.1"}
        data["apis"]["glm-5.1"]["enabled"] = True
        path.write_text(json.dumps(data) + "\n")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env["PYTHONPATH"] = os.path.join(repo, "src")
        out = subprocess.check_output(
            [sys.executable, "-m", "camc_pkg", "api", "default", "show", "--json"],
            env=env,
            cwd=repo,
            stderr=subprocess.STDOUT,
        ).decode()
        rows = json.loads(out)
        assert any(r["tool"] == "claude" and r["api"] == "glm-5.1" for r in rows)
