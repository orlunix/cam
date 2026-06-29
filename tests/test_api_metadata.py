"""Tests for API model metadata sync and projections."""

import json

import pytest

from camc_pkg.api_metadata import (
    apply_metadata_fallbacks,
    claude_context_env_overrides,
    codex_catalog_model,
    litellm_cost_map_url,
    load_codex_catalog_template,
    merge_api_metadata,
    metadata_from_cost_entry,
    openai_model_object,
    sync_metadata_in_data,
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


class TestOpenAiModelObject:
    def test_includes_token_limits(self):
        obj = openai_model_object("glm-5.2", {"context_window": 1048576, "max_output_tokens": 8192})
        assert obj["max_input_tokens"] == 1048576
        assert obj["max_output_tokens"] == 8192


class TestClaudeContextEnv:
    def test_compact_window_from_api_name(self):
        env = claude_context_env_overrides("glm-5.2")
        assert env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576"}

    def test_compact_window_from_metadata(self):
        env = claude_context_env_overrides(
            "glm-5.1",
            {"context_window": 1048576},
        )
        assert env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1048576"}


class TestCodexCatalog:
    def test_from_official_template(self):
        template = load_codex_catalog_template()
        if template is None:
            pytest.skip("~/.codex/models_cache.json not available")
        row = codex_catalog_model("glm-5.1", apply_metadata_fallbacks("glm-5.1", {}), template=template)
        assert row["slug"] == "glm-5.1"
        assert row["context_window"] == 202752
        assert row["shell_type"] in ("shell_command", "default")
        assert row["priority"] == 1000
        assert "base_instructions" in row
        assert row["supported_reasoning_levels"] == []
        assert row["supports_reasoning_summaries"] is False
