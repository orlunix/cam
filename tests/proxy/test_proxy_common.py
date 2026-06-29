"""Integration tests for API proxy model resolution (production: camc_pkg.proxy.common)."""

from camc_pkg.proxy.common import resolve_upstream_model, text_from_content


def test_resolve_short_api_name_to_ihub_id():
    got = resolve_upstream_model(
        "deepseek-v4-pro",
        "models/zai-org/dep-glm-5.1",
    )
    assert got == "models/deepseek-ai/dep-deepseek-v4-pro"


def test_resolve_full_path_passthrough():
    full = "models/moonshotai/dep-kimi-k2.6"
    assert resolve_upstream_model(full, "models/zai-org/dep-glm-5.1") == full


def test_resolve_falls_back_to_default_upstream():
    default = "models/qwen/dep-qwen3-5-397b-a17b"
    assert resolve_upstream_model("", default) == default


def test_resolve_glm_legacy_alias():
    assert resolve_upstream_model(
        "glm-5.1",
        "",
    ) == "models/zai-org/dep-glm-5.2"


def test_text_from_content_responses_input_text():
  content = [{"type": "input_text", "text": "fix the bucket bug"}]
  assert text_from_content(content) == "fix the bucket bug"


def test_text_from_content_responses_output_text():
  content = [{"type": "output_text", "text": "done"}]
  assert text_from_content(content) == "done"
