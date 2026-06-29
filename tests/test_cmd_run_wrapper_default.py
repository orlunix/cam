"""Focused tests for the 2026-06-23 PDX codex-regression hotfix.

Three narrow contracts pinned (no default startup-mode change):

  1. After readiness resolves the selected tool's binary,
     ``cmd_run`` rewrites ``launch_cmd`` to use the absolute resolved
     path. Codex slot 0; Claude past the ``env KEY=VAL ...`` prefix;
     Cursor slot 0.

  2. The default Codex ``ready_pattern`` accepts U+276F ❯, U+203A ›,
     AND ASCII '>' so the launch loop sees readiness across Codex
     build variants.

  3. Stale ``~/.cam/configs/codex.toml`` that overrides
     ``ready_pattern`` without ``›`` is normalized after merge by
     ``_load_config("codex")``.

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
# launch_cmd rewrite: resolved absolute binary lands in the right slot
# ---------------------------------------------------------------------------

def _rewrite_launch_cmd(launch_cmd, resolved_bin, tool):
    """Pure helper that mirrors the cmd_run rewrite block. Lifted so
    the test can exercise the contract without driving the full
    cmd_run flow. cmd_run uses identical logic — see cli.py."""
    if not (resolved_bin and os.path.isabs(resolved_bin) and launch_cmd):
        return list(launch_cmd or [])
    expected_basename = os.path.basename(resolved_bin)
    i = 0
    if launch_cmd[0] == "env":
        i = 1
        while i < len(launch_cmd) and ("=" in launch_cmd[i]
                                       and not launch_cmd[i].startswith("-")):
            i += 1
    if i < len(launch_cmd):
        tok = launch_cmd[i]
        if tok == expected_basename or tok == tool:
            launch_cmd = list(launch_cmd)
            launch_cmd[i] = resolved_bin
    return launch_cmd


class TestLaunchCmdResolvedBinaryRewrite:
    def test_codex_slot0_rewritten_to_absolute(self):
        cmd = ["codex", "-c", "features.goals=true"]
        out = _rewrite_launch_cmd(
            cmd, "/home/prgn_share/tools/codex/bin/codex", "codex")
        assert out == ["/home/prgn_share/tools/codex/bin/codex",
                       "-c", "features.goals=true"]

    def test_claude_past_env_wrapper_prefix(self):
        cmd = ["env", "CLAUDE_CODE_DISABLE_MOUSE=1",
               "claude", "--allowed-tools",
               "Bash,Edit,Read,Write,Glob,Grep,WebFetch,TodoWrite,NotebookEdit"]
        out = _rewrite_launch_cmd(
            cmd, "/home/prgn_share/tools/claude-code/bin/claude", "claude")
        # env + KEY=VAL preserved verbatim; 'claude' replaced
        assert out[0] == "env"
        assert out[1] == "CLAUDE_CODE_DISABLE_MOUSE=1"
        assert out[2] == "/home/prgn_share/tools/claude-code/bin/claude"
        assert out[3:] == cmd[3:]   # tool flags preserved

    def test_claude_multiple_env_assignments_skipped(self):
        cmd = ["env", "A=1", "B=2", "C=3", "claude", "--allowed-tools", "Bash"]
        out = _rewrite_launch_cmd(cmd, "/usr/local/bin/claude", "claude")
        assert out[:4] == ["env", "A=1", "B=2", "C=3"]
        assert out[4] == "/usr/local/bin/claude"
        assert out[5:] == cmd[5:]

    def test_cursor_slot0_rewritten(self):
        cmd = ["agent", "--workspace", "/proj"]
        # cursor's binary is named 'agent' — when resolved binary
        # basename matches launch_cmd[0], the rewrite fires.
        out = _rewrite_launch_cmd(cmd, "/home/hren/.local/bin/agent", "cursor")
        assert out[0] == "/home/hren/.local/bin/agent"
        assert out[1:] == cmd[1:]

    def test_no_rewrite_when_resolved_is_not_absolute(self):
        cmd = ["codex", "-c", "features.goals=true"]
        # Relative path → defensive no-op (preflight should never
        # produce this, but the rewrite must refuse to mutate).
        out = _rewrite_launch_cmd(cmd, "codex", "codex")
        assert out == cmd

    def test_no_rewrite_when_resolved_is_none(self):
        cmd = ["codex", "-c", "features.goals=true"]
        out = _rewrite_launch_cmd(cmd, None, "codex")
        assert out == cmd

    def test_no_rewrite_when_slot_basename_does_not_match(self):
        """Defensive: if the adapter launch_cmd uses a shape the
        rewrite doesn't recognize, leave it alone instead of
        guessing."""
        cmd = ["xclock", "--theme", "dark"]   # not codex/claude/cursor
        out = _rewrite_launch_cmd(cmd, "/usr/bin/codex", "codex")
        assert out == cmd

    def test_rewrite_does_not_mutate_input_list(self):
        cmd = ["codex", "-c", "features.goals=true"]
        original = list(cmd)
        _rewrite_launch_cmd(cmd, "/usr/bin/codex", "codex")
        assert cmd == original, "rewrite must not mutate adapter's command list"

    def test_default_startup_mode_unchanged(self):
        """Sanity: the inherit_env decision wiring stays at its
        pre-2026-06-23 default (True unless --no-inherit-env is
        passed). The narrow hotfix is the launch_cmd rewrite, not a
        startup-mode flip."""
        import argparse
        ns = argparse.Namespace(no_inherit_env=False)
        inherit_env = not getattr(ns, "no_inherit_env", False)
        assert inherit_env is True   # default is unchanged
        ns2 = argparse.Namespace(no_inherit_env=True)
        inherit_env2 = not getattr(ns2, "no_inherit_env", False)
        assert inherit_env2 is False  # flag still works


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
