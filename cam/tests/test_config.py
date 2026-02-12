"""Tests for configuration system."""

from __future__ import annotations

import pytest

from cam.core.config import CamConfig, load_config, parse_duration


class TestParseDuration:
    def test_seconds(self):
        assert parse_duration("30s") == 30

    def test_minutes(self):
        assert parse_duration("5m") == 300

    def test_hours(self):
        assert parse_duration("2h") == 7200

    def test_days(self):
        assert parse_duration("1d") == 86400

    def test_plain_number(self):
        assert parse_duration("600") == 600

    def test_none(self):
        assert parse_duration(None) is None

    def test_empty(self):
        assert parse_duration("") is None


class TestCamConfig:
    def test_defaults(self):
        config = load_config()
        assert config.general.default_tool == "claude"
        assert config.monitor.poll_interval == 2
        assert config.retry.max_retries == 0

    def test_override(self):
        config = load_config(general={"default_tool": "codex"})
        assert config.general.default_tool == "codex"

    def test_nested_override(self):
        config = load_config(monitor={"poll_interval": 10})
        assert config.monitor.poll_interval == 10
        # Other monitor values should remain default
        assert config.monitor.idle_timeout == 300
