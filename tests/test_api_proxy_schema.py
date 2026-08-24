"""Focused contracts for proxy-backed API routing."""

import json

import pytest

from camc_pkg.api_metadata import codex_catalog_models
from camc_pkg.api_resolver import resolve_run_plan
from camc_pkg.api_store import ensure_ready, resolve_api_name


@pytest.fixture
def api_models_file(monkeypatch, tmp_path):
    path = tmp_path / "api-models.json"
    monkeypatch.setattr("camc_pkg.api_store.API_MODELS_FILE", str(path))
    monkeypatch.setattr("camc_pkg.api_store.CAM_DIR", str(tmp_path))
    return path


def test_seed_declares_versioned_tool_api_routing(api_models_file):
    data = ensure_ready()

    assert data["version"] == "1.0.5"
    assert data["tool_supported_endpoints"] == {
        "claude": ["messages"],
        "codex": ["responses"],
    }
    assert data["tool_base_url_suf"] == {"claude": "", "codex": "/v1"}
    assert "claude-opus-5-k3" not in data["tool_supported_apis"]["claude"]
    assert "kimi-k3" in data["tool_supported_apis"]["codex"]
    assert data["apis"]["kimi-k3"]["tool_capabilities"]["codex"] == {
        "supports_search_tool": False,
    }
    assert data["apis"]["glm-5.2"]["reasoning"]["mapping"]["codex"] == {
        "none": "none", "low": "high", "medium": "high",
        "high": "high", "xhigh": "max", "max": "max",
    }
    assert data["apis"]["glm-5.2"]["reasoning"]["default"]["codex"] == "high"


def test_kimi_k3_codex_effort_mapping_is_seeded_and_versioned(api_models_file):
    data = ensure_ready()
    expected = {
        "none": "low", "low": "low", "medium": "high",
        "high": "high", "xhigh": "max", "max": "max",
    }

    assert data["version"] == "1.0.5"
    reasoning = data["apis"]["kimi-k3"]["reasoning"]
    assert reasoning["mapping"]["codex"] == expected
    assert reasoning["default"]["codex"] == "high"


def test_kimi_k3_is_not_advertised_for_claude_without_messages(api_models_file):
    data = ensure_ready()
    data["apis"]["kimi-k3"]["enabled"] = True
    with open(api_models_file, "w") as handle:
        json.dump(data, handle)

    with pytest.raises(ValueError, match="not supported for tool 'claude'"):
        resolve_run_plan("claude", "kimi-k3")


def test_codex_catalog_uses_upstream_slug_and_disables_search(api_models_file):
    data = ensure_ready()
    data["apis"]["kimi-k3"]["enabled"] = True

    rows = codex_catalog_models("kimi-k3", data=data,
                                template={"shell_type": "default"},
                                require_endpoint="responses")

    assert rows[0]["slug"] == "nvidia/moonshotai/eccn-kimi-k3"
    assert rows[0]["supports_search_tool"] is False


def test_legacy_k3_phase_profiles_migrate_to_one_canonical_profile(api_models_file):
    """Retired low/high/max profiles must not survive an API refresh."""
    data = ensure_ready()
    data["version"] = "1.0.4"
    canonical = data["apis"].pop("kimi-k3")
    for phase in ("low", "high", "max"):
        data["apis"]["kimi-k3-" + phase] = dict(canonical)
    data["apis"]["kimi-k3-max"]["enabled"] = True
    with open(api_models_file, "w") as handle:
        json.dump(data, handle)

    refreshed = ensure_ready()

    assert refreshed["version"] == "1.0.5"
    assert "kimi-k3" in refreshed["apis"]
    assert not {"kimi-k3-low", "kimi-k3-high", "kimi-k3-max"}.intersection(
        refreshed["apis"]
    )
    assert refreshed["apis"]["kimi-k3"]["model"] == (
        "nvidia/moonshotai/eccn-kimi-k3"
    )
    assert "kimi-k3" in refreshed["tool_supported_apis"]["codex"]
    assert resolve_api_name(refreshed, "kimi-k3-max") == "kimi-k3"
