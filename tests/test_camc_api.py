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
        assert data["version"] == "1.0.5"
        assert "deepseek-v4-flash" in data["apis"]
        assert data["apis"]["deepseek-v4-flash"]["model"] == (
            "nvidia/deepseek-ai/eccn-deepseek-v4-flash"
        )
        assert "kimi-k3" in data["apis"]
        assert not {"kimi-k3-low", "kimi-k3-high", "kimi-k3-max"}.intersection(
            data["apis"]
        )
        assert data["apis"]["kimi-k3"]["model"] == (
            "nvidia/moonshotai/eccn-kimi-k3"
        )
        assert all(
            entry["model"].startswith("nvidia/")
            for entry in data["apis"].values()
        )
        assert "glm-5.2" in data["apis"]
        assert data["apis"]["glm-5.2"]["model"] == "nvidia/zai-org/eccn-glm-5.2"
        assert "minimax-m3" in data["apis"]
        assert "default" not in data
        assert "default_provider" not in data
        assert "_templates" not in data
        assert data["apis"]["glm-5.2"]["available_endpoints"] == [
            "completions", "messages", "responses",
        ]
        assert "messages" not in data["apis"]["kimi-k3"]["available_endpoints"]
        for entry in data["apis"].values():
            assert entry["reasoning"]["mapping"]["claude"]["max"] == "max"

    def test_kimi_k3_profiles_use_unified_standard_model(self, api_models_file):
        data = ensure_ready()

        assert data["apis"]["kimi-k3"]["model"] == (
            "nvidia/moonshotai/eccn-kimi-k3"
        )
        assert all("kimi-k3-" not in entry.get("model", "")
                   for entry in data["apis"].values())

    def test_schema_version_refreshes_missing_or_older_json(self, api_models_file):
        for version in (None, 1, "1.0.0"):
            data = _default_seed()
            if version is None:
                data.pop("version", None)
            else:
                data["version"] = version
            with open(api_models_file, "w") as f:
                json.dump(data, f)
            refreshed = ensure_ready()
            assert refreshed["version"] == "1.0.5"
            with open(api_models_file) as f:
                assert json.load(f)["version"] == "1.0.5"

    def test_schema_version_does_not_downgrade_newer_json(self, api_models_file):
        data = _default_seed()
        data["version"] = "1.0.5"
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        assert ensure_ready()["version"] == "1.0.5"

    def test_alias_resolution(self, api_models_file):
        data = ensure_ready()
        assert resolve_api_name(data, "glm") == "glm-5.2"
        assert resolve_api_name(data, "GLM52") == "glm-5.2"

    def test_list_hides_disabled_by_default(self, api_models_file):
        data = ensure_ready()
        rows = list_apis(data, show_all=False)
        assert rows == []
        data["apis"]["glm-5.2"]["enabled"] = True
        rows = list_apis(data, show_all=False)
        assert len(rows) == 1
        assert rows[0]["available_endpoints"] == [
            "completions", "messages", "responses",
        ]

    def test_merge_curated_removes_old_proxy_schema(self, api_models_file):
        from camc_pkg.api_store import merge_curated_apis
        data = _default_seed()
        data["default"] = "glm-5.2"
        data["default_provider"] = "inference-hub"
        data["providers"]["inference-hub"]["translator"] = "embedded"
        data["providers"]["inference-hub"]["endpoints"] = {}
        data["apis"]["glm-5.2"]["url"] = "http://127.0.0.1:1234"
        merge_curated_apis(data)
        assert "default" not in data
        assert "default_provider" not in data
        assert "translator" not in data["providers"]["inference-hub"]
        assert "endpoints" not in data["providers"]["inference-hub"]
        assert "url" not in data["apis"]["glm-5.2"]

    def test_curated_merge_does_not_coerce_scalar_defaults(self, api_models_file):
        """The current schema is list-based; legacy scalars are not migrated."""
        from camc_pkg.api_store import merge_curated_apis
        data = _default_seed()
        data["defaults"] = {
            "claude": "deepseek-v4-flash",
            "codex": ["kimi-k3-max"],
        }
        merge_curated_apis(data)
        assert data["defaults"]["claude"] == "deepseek-v4-flash"
        assert data["defaults"]["codex"] == ["kimi-k3"]

    def test_custom_reasoning_profile_survives_curated_merge(self, api_models_file):
        from camc_pkg.api_store import merge_curated_apis
        data = _default_seed()
        data["apis"]["glm-5.2"]["reasoning"] = {
            "supported": ["low", "max"],
            "mapping": {"claude": {"low": "low", "max": "max"}},
            "verified": True,
            "source": "test",
        }
        merge_curated_apis(data)
        assert data["apis"]["glm-5.2"]["reasoning"]["mapping"]["claude"]["max"] == "max"
        assert data["apis"]["glm-5.2"]["reasoning"]["verified"] is True

    def test_curated_merge_keeps_provider_direct_schema(self, api_models_file):
        from camc_pkg.api_store import merge_curated_apis
        data = _default_seed()
        data["providers"]["inference-hub"]["client_base_url"] = "old"
        merge_curated_apis(data)
        provider = data["providers"]["inference-hub"]
        assert provider["base_url"] == "https://inference-api.nvidia.com"
        assert "client_base_url" not in provider

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
    def test_native_messages_plan_for_claude(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "glm-5.2")
        assert plan["mode"] == "proxy"
        assert plan["env"]["ANTHROPIC_BASE_URL"] == (
            "https://inference-api.nvidia.com"
        )
        assert plan["env"]["ANTHROPIC_MODEL"] == "claude-sonnet-5-glm52"
        assert plan["model"] == "nvidia/zai-org/eccn-glm-5.2"
        assert plan["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1048576"
        assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in plan["env"]

    def test_native_messages_plan_uses_full_upstream_model_id_for_claude(
        self, api_models_file
    ):
        data = ensure_ready()
        data["apis"]["deepseek-v4-flash"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "deepseek-v4-flash")
        assert plan["env"]["ANTHROPIC_MODEL"] == "claude-haiku-5-dsv4f"
        assert plan["model"] == "nvidia/deepseek-ai/eccn-deepseek-v4-flash"

    def test_native_messages_plan_uses_claude_client_root_url(self, api_models_file):
        data = ensure_ready()
        data["apis"]["deepseek-v4-flash"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "deepseek-v4-flash")
        assert plan["env"]["ANTHROPIC_BASE_URL"] == (
            "https://inference-api.nvidia.com"
        )

    def test_native_responses_plan_for_codex(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("codex", "glm-5.2")
        assert plan["mode"] == "proxy"
        assert plan["env"]["_API_USE_RESOLVED_TOKEN"] == "1"

    def test_native_api_plan_uses_json_base_url_suffix(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)

        claude = resolve_run_plan("claude", "glm-5.2")
        codex = resolve_run_plan("codex", "glm-5.2")
        assert claude["local_base_url"] == "https://inference-api.nvidia.com"
        assert codex["local_base_url"] == "https://inference-api.nvidia.com/v1"
        assert claude["api_endpoint"] == "anthropic_messages"
        assert codex["api_endpoint"] == "openai_responses"

    def test_disabled_api_rejected(self, api_models_file):
        ensure_ready()
        with pytest.raises(ValueError, match="disabled"):
            resolve_run_plan("claude", "glm-5.2")

    def test_codex_api_direct_plan(self, api_models_file):
        """Codex API profiles use the native Responses endpoint."""
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("codex", "glm-5.2")

    def test_cursor_api_not_supported(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True
        data["apis"]["glm-5.2"]["available_endpoints"] = ["completions"]
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="only supported for Claude and Codex"):
            resolve_run_plan("cursor", "glm-5.2")

    def test_codex_api_home_is_fully_isolated(self):
        from camc_pkg import api_resolver
        assert os.path.basename(api_resolver.CODEX_API_CONFIG_DIR) == ".codex-api"
        assert os.path.basename(os.path.dirname(api_resolver.CODEX_API_CONFIG_DIR)) != ".cam"

    def test_codex_api_config_writer_keeps_home_isolated(self, tmp_path, monkeypatch):
        from camc_pkg import api_resolver
        home = tmp_path / ".codex-api"
        monkeypatch.setattr(api_resolver, "CODEX_API_CONFIG_DIR", str(home))
        monkeypatch.setattr(
            "camc_pkg.api_metadata.resolve_api_metadata",
            lambda _name: {"context_window": 1024},
        )

        def _write_catalog(path, _name, _metadata, **_kwargs):
            with open(path, "w") as handle:
                handle.write("{}\n")

        monkeypatch.setattr(
            "camc_pkg.api_metadata.write_codex_model_catalog", _write_catalog)
        result = api_resolver.ensure_codex_api_config_dir(
            "https://inference-api.nvidia.com/v1", "glm-5.2")
        assert result == str(home)
        assert (home / "config.toml").is_file()
        assert "CODEX_HOME" not in (home / "config.toml").read_text()
        assert (home / "camc-model-catalog.json").is_file()

    def test_codex_api_config_uses_full_upstream_model_id(self, tmp_path, monkeypatch):
        from camc_pkg import api_resolver
        home = tmp_path / ".codex-api"
        monkeypatch.setattr(api_resolver, "CODEX_API_CONFIG_DIR", str(home))
        monkeypatch.setattr(
            "camc_pkg.api_metadata.resolve_api_metadata",
            lambda _name: {"context_window": 1024},
        )
        monkeypatch.setattr(
            "camc_pkg.api_metadata.write_codex_model_catalog",
            lambda path, _name, _metadata, **_kwargs: open(path, "w").write("{}\n"),
        )

        api_resolver.ensure_codex_api_config_dir(
            "https://inference-api.nvidia.com/v1",
            "deepseek-v4-flash",
            model_id="nvidia/deepseek-ai/eccn-deepseek-v4-flash",
        )

        config = (home / "config.toml").read_text()
        assert 'model = "nvidia/deepseek-ai/eccn-deepseek-v4-flash"' in config

    def test_codex_api_reuses_login_skills_with_idempotent_link(self, tmp_path, monkeypatch):
        from camc_pkg import api_resolver
        login_skills = tmp_path / ".codex" / "skills"
        api_home = tmp_path / ".codex-api"
        login_skills.mkdir(parents=True)
        (login_skills / "example").mkdir()
        monkeypatch.setattr(api_resolver, "CODEX_LOGIN_SKILLS_DIR", str(login_skills))
        monkeypatch.setattr(api_resolver, "CODEX_API_CONFIG_DIR", str(api_home))

        first = api_resolver._ensure_codex_skills_link()
        second = api_resolver._ensure_codex_skills_link()
        link = api_home / "skills"
        assert first == "linked"
        assert second == "linked"
        assert link.is_symlink()
        assert os.path.realpath(str(link)) == os.path.realpath(str(login_skills))

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
            "base_url": "https://gw.example",
            "catalog_path": "",
        }
        data["apis"]["my-custom"] = {
            "provider": "my-gw",
            "model": "my-model",
            "enabled": True,
            "allow_run": True,
            "aliases": [],
            "available_endpoints": ["messages"],
            "client_models": {"claude": "my-model"},
        }
        data["tool_supported_apis"]["claude"].append("my-custom")
        rebuild_aliases(data)
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        plan = resolve_run_plan("claude", "my-custom")
        assert plan["mode"] == "proxy"
        assert plan["local_base_url"] == "https://gw.example"


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
        data["default"] = "glm-5.2"
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        assert resolve_tool_default_api(data, "claude") is None
        name, source = resolve_run_api_name("claude", data=data)
        assert name is None
        assert source == "login"

    def test_codex_empty_means_login(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": ["glm-5.2"], "codex": []}
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        name, source = resolve_run_api_name("codex", data=data)
        assert name is None
        assert source == "login"

    def test_default_requires_enabled(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": ["glm-5.2"]}
        data["apis"]["glm-5.2"]["enabled"] = False
        data["apis"]["glm-5.2"]["enabled_reason"] = "id_not_on_key"
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        with pytest.raises(ValueError, match="disabled"):
            resolve_run_api_name("claude", data=data)

    def test_default_used_when_enabled(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": ["glm-5.2"]}
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        name, source = resolve_run_api_name("claude", data=data)
        assert name == "glm-5.2"
        assert source == "default"

    def test_multiple_tool_defaults_use_first_as_primary(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name, resolve_tool_default_api
        data = ensure_ready()
        data["defaults"] = {
            "codex": ["deepseek-v4-flash", "deepseek-v4-pro"],
        }
        data["apis"]["deepseek-v4-flash"]["enabled"] = True
        name, source = resolve_run_api_name("codex", data=data)
        assert name == "deepseek-v4-flash"
        assert source == "default"
        assert resolve_tool_default_api(data, "codex") == "deepseek-v4-flash"

    def test_seed_provider_has_token_file_without_duplicate_token_key(self, api_models_file):
        data = ensure_ready()
        provider = data["providers"]["inference-hub"]
        assert provider["token_file"] == "~/.my_tokens.yaml"
        assert "token_key" not in provider

    def test_kimi_k3_can_be_codex_default(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"codex": ["kimi-k3"]}
        data["apis"]["kimi-k3"]["enabled"] = True
        name, source = resolve_run_api_name("codex", data=data)
        assert name == "kimi-k3"
        assert source == "default"

    def test_no_default_api_skips_default(self, api_models_file):
        from camc_pkg.api_store import resolve_run_api_name
        data = ensure_ready()
        data["defaults"] = {"claude": ["glm-5.2"]}
        data["apis"]["glm-5.2"]["enabled"] = True
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
        set_tool_default_api(data, "codex", "glm-5.2")
        data = ensure_ready()
        assert resolve_tool_default_api(data, "codex") == "glm-5.2"
        clear_tool_default_api(data, "codex")
        data = ensure_ready()
        assert resolve_tool_default_api(data, "codex") is None

    def test_default_show_json(self, api_models_file, capsys):
        from argparse import Namespace
        from camc_pkg import cli as camc_cli

        data = ensure_ready()
        data["defaults"] = {"claude": ["glm-5.2"]}
        data["apis"]["glm-5.2"]["enabled"] = True
        with open(api_models_file, "w") as f:
            json.dump(data, f)
        camc_cli.cmd_api_default_show(Namespace(json=True))
        out = json.loads(capsys.readouterr().out)
        claude = next(r for r in out if r["tool"] == "claude")
        codex = next(r for r in out if r["tool"] == "codex")
        assert claude["api"] == "glm-5.2"
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
        data["defaults"] = {"claude": ["glm-5.2"]}
        data["apis"]["glm-5.2"]["enabled"] = True
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
        assert any(r["tool"] == "claude" and r["api"] == "glm-5.2" for r in rows)
