"""Tests for api_token.resolve_token — case-insensitive, robust lookup."""

import os

import pytest

from camc_pkg import api_token as tok


@pytest.fixture
def token_paths(monkeypatch, tmp_path):
    token_env = tmp_path / "token.env"
    legacy_env = tmp_path / "inference-hub.env"
    yaml_path = tmp_path / "my_tokens.yaml"
    monkeypatch.setattr(tok, "TOKEN_ENV_FILE", str(token_env))
    monkeypatch.setattr(tok, "LEGACY_TOKEN_ENV", str(legacy_env))
    monkeypatch.setattr(tok, "MY_TOKENS_YAML", str(yaml_path))
    return {
        "token_env": token_env,
        "legacy_env": legacy_env,
        "yaml": yaml_path,
    }


class TestResolveToken:
    def test_cli_token_wins(self):
        token, src = tok.resolve_token(
            "inference_hub", ["INFERENCE_HUB_TOKEN"], cli_token="from-cli")
        assert token == "from-cli"
        assert src == "cli"

    def test_env_exact_name(self, monkeypatch):
        monkeypatch.setenv("INFERENCE_HUB_TOKEN", "from-env")
        token, src = tok.resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == "from-env"
        assert src == "env:INFERENCE_HUB_TOKEN"

    def test_env_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("INFERENCE_HUB_TOKEN", raising=False)
        monkeypatch.setenv("inference_hub_token", "lower-env")
        token, src = tok.resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == "lower-env"
        assert src == "env:inference_hub_token"

    def test_token_env_case_insensitive(self, token_paths):
        token_paths["token_env"].write_text(
            "export INFERENCE_HUB_TOKEN=from-file\n")
        token, src = tok.resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == "from-file"
        assert src == "token.env:INFERENCE_HUB_TOKEN"

    def test_token_env_hyphen_auth_key(self, token_paths):
        token_paths["token_env"].write_text("inference-hub=from-hyphen\n")
        token, src = tok.resolve_token("inference_hub", [])
        assert token == "from-hyphen"
        assert src == "token.env:inference-hub"

    def test_yaml_case_insensitive(self, token_paths):
        token_paths["yaml"].write_text("INFERENCE_HUB: yaml-upper\n")
        token, src = tok.resolve_token("inference_hub", ["MISSING_VAR"])
        assert token == "yaml-upper"
        assert src == "my_tokens.yaml:INFERENCE_HUB"

    def test_yaml_hyphen_key(self, token_paths):
        token_paths["yaml"].write_text("inference-hub: yaml-hyphen\n")
        token, src = tok.resolve_token("inference_hub", [])
        assert token == "yaml-hyphen"
        assert src == "my_tokens.yaml:inference-hub"

    def test_precedence_env_before_yaml(self, token_paths, monkeypatch):
        monkeypatch.setenv("INFERENCE_HUB_TOKEN", "env-wins")
        token_paths["yaml"].write_text("inference_hub: yaml-loses\n")
        token, src = tok.resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == "env-wins"
        assert src == "env:INFERENCE_HUB_TOKEN"

    def test_precedence_token_env_before_yaml(self, token_paths):
        token_paths["token_env"].write_text("INFERENCE_HUB_TOKEN=file-wins\n")
        token_paths["yaml"].write_text("inference_hub: yaml-loses\n")
        token, src = tok.resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == "file-wins"
        assert src == "token.env:INFERENCE_HUB_TOKEN"

    def test_missing_returns_none(self, token_paths, monkeypatch):
        for name in ("INFERENCE_HUB_TOKEN", "INFERENCE_HUB_API_KEY", "inference_hub"):
            monkeypatch.delenv(name, raising=False)
        token, src = tok.resolve_token("inference_hub", ["INFERENCE_HUB_TOKEN"])
        assert token == ""
        assert src == "none"
