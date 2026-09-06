"""Focused tests for the 2026-06-23 PDX codex-regression hotfix.

Two narrow configuration contracts pinned:

  1. The default Codex ``ready_pattern`` accepts U+276F ❯, U+203A ›,
     AND ASCII '>' so the launch loop sees readiness across Codex
     build variants.

  2. Stale ``~/.cam/configs/codex.toml`` that overrides
     ``ready_pattern`` without ``›`` is normalized after merge by
     ``_load_config("codex")``.

The former source-mirroring launch-command tests are replaced by the public
``camc run`` contract in ``tests/baseline/test_run_contract.py``.

No new dependencies; Python 3.6 compatible.
"""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ---------------------------------------------------------------------------
# Codex ready_pattern accepts › (current Codex TUI cursor)
# ---------------------------------------------------------------------------

class TestCodexReadyPatternAcceptsAllCursorMarkers:
    def _load_pattern(self, fname):
        path = os.path.join(ROOT, "src", "cam", "adapters", "configs", fname)
        with open(path, "r") as f:
            text = f.read()
        m = re.search(r'^ready_pattern\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert m, "ready_pattern missing in %s" % fname
        return re.compile(m.group(1), re.MULTILINE)

    @pytest.mark.parametrize("source", ["codex.toml", "codex.boot.toml"])
    @pytest.mark.parametrize("char", ["❯", "›", ">"])
    def test_each_cursor_char_matches(self, source, char):
        pat = self._load_pattern(source)
        screen = "Some Codex prose\n%s " % char
        assert pat.search(screen), (
            "ready_pattern in %s did not accept %r — codex pane stays "
            "in initializing on this TUI variant" % (source, char))

    @pytest.mark.parametrize("source", ["codex.toml", "codex.boot.toml"])
    def test_prose_without_cursor_is_not_ready(self, source):
        pat = self._load_pattern(source)
        assert not pat.search("Some text without a cursor on any line\n"), (
            "ready_pattern matched a line that has no cursor at start")

    def test_runtime_adapter_loaded_codex_ready_pattern(self):
        from camc_pkg.adapters import AdapterConfig, _parse_toml
        path = os.path.join(ROOT, "src", "cam", "adapters", "configs",
                            "codex.toml")
        with open(path, "r") as f:
            cfg = AdapterConfig(_parse_toml(f.read()))
        assert cfg.ready_pattern is not None
        assert cfg.ready_pattern.search("\n› ")


# ---------------------------------------------------------------------------
# Defensive scrub: stale external ~/.cam/configs/codex.toml normalized
# ---------------------------------------------------------------------------

class TestExternalCodexReadyPatternScrub:
    def _seed_stale_external(self, tmp_path, body):
        (tmp_path / "codex.toml").write_text(body)

    def test_stale_external_overriding_with_no_arrow_is_scrubbed(
            self, tmp_path, monkeypatch):
        from camc_pkg import adapters
        monkeypatch.setattr(adapters, "CONFIGS_DIR", str(tmp_path))
        self._seed_stale_external(tmp_path, (
            "[launch]\n"
            'ready_pattern = "^\\\\s*[❯>]"\n'
            'ready_flags = ["MULTILINE"]\n'
        ))
        cfg = adapters._load_config("codex")
        assert cfg.ready_pattern is not None
        assert cfg.ready_pattern.search("\n› ")
        assert cfg.ready_pattern.search("\n❯ ")
        assert cfg.ready_pattern.search("\n> ")

    def test_external_with_arrow_already_present_is_left_alone(
            self, tmp_path, monkeypatch):
        from camc_pkg import adapters
        monkeypatch.setattr(adapters, "CONFIGS_DIR", str(tmp_path))
        self._seed_stale_external(tmp_path, (
            "[launch]\n"
            'ready_pattern = "^\\\\s*[❯›>]\\\\s"\n'
            'ready_flags = ["MULTILINE"]\n'
        ))
        cfg = adapters._load_config("codex")
        assert "›" in cfg.ready_pattern.pattern

    def test_no_external_falls_through_to_embedded(self, tmp_path, monkeypatch):
        from camc_pkg import adapters
        monkeypatch.setattr(adapters, "CONFIGS_DIR", str(tmp_path))
        cfg = adapters._load_config("codex")
        assert cfg.ready_pattern.search("\n› ")
        assert "›" in cfg.ready_pattern.pattern

    def test_claude_external_not_normalized_by_codex_scrub(
            self, tmp_path, monkeypatch):
        from camc_pkg import adapters
        monkeypatch.setattr(adapters, "CONFIGS_DIR", str(tmp_path))
        (tmp_path / "claude.toml").write_text(
            "[launch]\n"
            'ready_pattern = "^[XYZ]"\n'
            'ready_flags = ["MULTILINE"]\n')
        cfg = adapters._load_config("claude")
        assert "›" not in cfg.ready_pattern.pattern
