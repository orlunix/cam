"""Integration tests for API proxy model resolution (production: camc_pkg.proxy.common)."""

from camc_pkg.proxy.common import resolve_upstream_model


def test_resolve_short_api_name_to_ihub_id():
    got = resolve_upstream_model(
        "deepseek-v4-pro",
        "nvidia/zai-org/eccn-glm-5.1",
    )
    assert got == "nvidia/deepseek-ai/eccn-deepseek-v4-pro"


def test_resolve_full_path_passthrough():
    full = "nvidia/moonshotai/eccn-kimi-k2.6"
    assert resolve_upstream_model(full, "nvidia/zai-org/eccn-glm-5.1") == full


def test_resolve_falls_back_to_default_upstream():
    default = "nvidia/qwen/eccn-qwen3-5-397b-a17b"
    assert resolve_upstream_model("", default) == default


def test_resolve_glm_alias():
    assert resolve_upstream_model(
        "glm-5.1",
        "",
    ) == "nvidia/zai-org/eccn-glm-5.1"
