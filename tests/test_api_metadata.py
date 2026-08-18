"""Tests for API model metadata sync and projections."""

import json

import pytest

from camc_pkg.api_metadata import (
    apply_metadata_fallbacks,
    claude_context_env_overrides,
    codex_catalog_models,
    codex_catalog_model,
    litellm_cost_map_url,
    load_codex_catalog_template,
    merge_api_metadata,
    metadata_from_cost_entry,
    normalize_available_endpoints,
    normalize_reasoning_profile,
    resolve_claude_effort,
    sync_metadata_in_data,
    write_codex_model_catalog,
)
from camc_pkg.api_store import ensure_ready


@pytest.fixture
def api_models_file(monkeypatch, tmp_path):
    path = tmp_path / "api-models.json"
    monkeypatch.setattr("camc_pkg.api_store.API_MODELS_FILE", str(path))
    monkeypatch.setattr("camc_pkg.api_store.CAM_DIR", str(tmp_path))
    return str(path)


class TestLitellmCostMapUrl:
    def test_strips_v1_suffix(self):
        url = litellm_cost_map_url({
            "base_url": "https://inference-api.nvidia.com/v1",
        })
        assert url == "https://inference-api.nvidia.com/public/litellm_model_cost_map"


class TestMetadataFromCostEntry:
    def test_maps_input_tokens(self):
        meta = metadata_from_cost_entry({
            "max_input_tokens": 202752,
            "mode": "chat",
        })
        assert meta["context_window"] == 202752
        assert meta["source"] == "litellm_cost_map"

    def test_supports_flags(self):
        meta = metadata_from_cost_entry({
            "max_input_tokens": 1000,
            "supports_function_calling": True,
            "supports_reasoning": False,
        })
        assert meta["supports_tools"] is True
        assert meta["supports_reasoning"] is False


class TestMergeApiMetadata:
    def test_deepseek_flash_uses_curated_fallback(self):
        meta = merge_api_metadata(
            "deepseek-v4-flash",
            "nvidia/deepseek-ai/eccn-deepseek-v4-flash",
            {},
        )
        assert meta["context_window"] == 1048576
        assert meta["supports_reasoning"] is True

    def test_kimi_k3_uses_curated_fallback(self):
        meta = merge_api_metadata(
            "kimi-k3",
            "nvidia/moonshotai/eccn-kimi-k3",
            {},
        )
        assert meta["context_window"] == 1048576
        assert meta["supports_reasoning"] is True

    def test_cost_map_plus_fallback(self):
        cost = {"nvidia/zai-org/eccn-glm-5.2": {"max_input_tokens": 1048576, "mode": "chat"}}
        meta = merge_api_metadata("glm-5.2", "nvidia/zai-org/eccn-glm-5.2", cost)
        assert meta["context_window"] == 1048576
        assert meta["max_output_tokens"] == 131072
        assert meta["reasoning_levels"] == []

    def test_missing_cost_map_uses_curated(self):
        meta = merge_api_metadata("glm-5.2", "nvidia/zai-org/eccn-glm-5.2", {})
        assert meta["context_window"] == 1048576


class TestSyncMetadataInData:
    def test_writes_all_apis(self, api_models_file):
        data = ensure_ready()
        count = sync_metadata_in_data(data, data["providers"]["inference-hub"], cost_map={
            "nvidia/zai-org/eccn-glm-5.2": {"max_input_tokens": 1048576},
        })
        assert count == len(data["apis"])
        assert data["apis"]["glm-5.2"]["metadata"]["context_window"] == 1048576

    def test_metadata_sync_keeps_reasoning_profile(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["reasoning"]["verified"] = True
        sync_metadata_in_data(data, data["providers"]["inference-hub"], cost_map={})
        assert data["apis"]["glm-5.2"]["reasoning"]["verified"] is True
        assert data["apis"]["glm-5.2"]["reasoning"]["mapping"]["claude"]["max"] == "max"


class TestClaudeContextEnv:
    def test_compact_window_from_api_name(self):
        env = claude_context_env_overrides("glm-5.2")
        assert env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576"}


class TestClaudeReasoningProfile:
    def test_default_profile_exposes_full_claude_effort_range(self):
        profile = normalize_reasoning_profile(None)
        assert profile["supported"] == ["low", "medium", "high", "xhigh", "max"]
        assert profile["mapping"]["claude"]["max"] == "max"

    def test_max_effort_is_not_silently_downgraded(self):
        profile = normalize_reasoning_profile({
            "supported": ["low", "medium", "high", "xhigh", "max"],
            "mapping": {"claude": {
                "low": "low", "medium": "medium", "high": "high",
                "xhigh": "xhigh", "max": "max",
            }},
        })
        assert resolve_claude_effort(profile, "max") == "max"

    def test_available_endpoint_names_are_normalized(self):
        assert normalize_available_endpoints([
            "messages", "MESSAGES", "completions", "unknown", "responses",
        ]) == ["messages", "completions", "responses"]

    def test_missing_claude_mapping_is_rejected(self):
        profile = normalize_reasoning_profile({"supported": ["low"]})
        with pytest.raises(ValueError, match="no Claude effort mapping"):
            resolve_claude_effort(profile, "max")

    def test_compact_window_from_metadata(self):
        env = claude_context_env_overrides(
            "glm-5.2",
            {"context_window": 1048576},
        )
        assert env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576"}


class TestCodexCatalog:
    def test_glm_catalog_uses_json_codex_reasoning_mapping(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True

        row = codex_catalog_models(
            "glm-5.2", data=data,
            template={"supported_reasoning_levels": []},
            require_endpoint="responses",
        )[0]

        assert row["default_reasoning_level"] == "high"
        assert [level["effort"] for level in row["supported_reasoning_levels"]] == [
            "none", "low", "medium", "high", "xhigh", "max",
        ]

    def test_catalog_refresh_keeps_json_codex_reasoning_mapping(
            self, api_models_file, monkeypatch, tmp_path):
        data = ensure_ready()
        data["apis"]["kimi-k3"]["enabled"] = True
        with open(api_models_file, "w") as handle:
            json.dump(data, handle)
        monkeypatch.setattr(
            "camc_pkg.api_metadata.load_codex_catalog_template",
            lambda: {"supported_reasoning_levels": []},
        )

        catalog_path = tmp_path / "camc-model-catalog.json"
        write_codex_model_catalog(
            str(catalog_path), "kimi-k3",
            metadata={"context_window": 1048576},
            require_endpoint="responses",
        )

        row = json.loads(catalog_path.read_text())["models"][0]
        assert [level["effort"] for level in row["supported_reasoning_levels"]] == [
            "none", "low", "medium", "high", "xhigh", "max",
        ]
        assert row["default_reasoning_level"] == "high"

    def test_catalog_includes_all_enabled_response_profiles(self, api_models_file):
        data = ensure_ready()
        data["apis"]["glm-5.2"]["enabled"] = True
        data["apis"]["deepseek-v4-flash"]["enabled"] = True
        data["apis"]["kimi-k3"]["enabled"] = True
        rows = codex_catalog_models(
            "glm-5.2",
            data=data,
            template={"shell_type": "default"},
            require_endpoint="responses",
        )
        assert [row["slug"] for row in rows] == [
            "nvidia/zai-org/eccn-glm-5.2",
            "nvidia/deepseek-ai/eccn-deepseek-v4-flash",
            "nvidia/moonshotai/eccn-kimi-k3",
        ]

    def test_catalog_has_one_profile_for_unified_kimi_model(
            self, api_models_file):
        data = ensure_ready()
        data["apis"]["kimi-k3"]["enabled"] = True
        rows = codex_catalog_models(
            "kimi-k3", data=data,
            template={"shell_type": "default"},
            require_endpoint="responses",
        )

        slugs = [row["slug"] for row in rows]
        assert slugs.count("nvidia/moonshotai/eccn-kimi-k3") == 1

    def test_from_official_template(self):
        template = load_codex_catalog_template()
        if template is None:
            pytest.skip("~/.codex/models_cache.json not available")
        row = codex_catalog_model("glm-5.2", apply_metadata_fallbacks("glm-5.2", {}), template=template)
        assert row["slug"] == "glm-5.2"
        assert row["context_window"] == 1048576
        assert row["shell_type"] in ("shell_command", "default")
        assert row["priority"] == 1000
        assert "base_instructions" in row
        assert row["supported_reasoning_levels"] == []
        assert row["supports_reasoning_summaries"] is False
