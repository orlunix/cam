"""Focused tests for TOML-only auto-confirm customization.

Covers:
  * `install_default_configs()` — template install on init/upgrade path.
    Missing-file create, no-overwrite, --force semantics.
  * `_load_config(tool)` actually merges a user-side
    `~/.cam/configs/<tool>.toml` on top of the embedded defaults so a
    custom `[[confirm]]` rule reaches `should_auto_confirm()`.
  * `cmd_heal --upgrade` invokes `install_default_configs()` and
    refuses to clobber existing user files.
"""

import argparse
import os

import pytest

from camc_pkg import adapters as _adapters
from camc_pkg import cli as _cli
from camc_pkg.detection import should_auto_confirm


# ---------------------------------------------------------------------------
# install_default_configs — template install on init/upgrade
# ---------------------------------------------------------------------------

class TestInstallDefaultConfigs:
    def test_creates_template_for_every_supported_tool(self, tmp_path):
        results = _adapters.install_default_configs(target_dir=str(tmp_path))
        # claude / codex / cursor are the supported tools today.
        expected = set(_adapters._EMBEDDED_CONFIGS.keys())
        assert set(results.keys()) == expected
        for fname in expected:
            assert (tmp_path / fname).exists(), \
                "missing template: %s" % fname
            assert results[fname] == "created"

    def test_does_not_overwrite_user_edited_file_without_force(self, tmp_path):
        # Pre-seed claude.toml with a user edit.
        custom_text = '# my edits\n[[confirm]]\npattern="Hello"\nresponse="y"\n'
        target = tmp_path / "claude.toml"
        target.write_text(custom_text)

        results = _adapters.install_default_configs(target_dir=str(tmp_path))
        assert results["claude.toml"] == "skipped_exists"
        assert target.read_text() == custom_text, \
            "user edit was clobbered"

    def test_force_overwrites_existing_files(self, tmp_path):
        target = tmp_path / "claude.toml"
        target.write_text("# stale custom content\n")
        results = _adapters.install_default_configs(
            target_dir=str(tmp_path), force=True)
        assert results["claude.toml"] == "overwritten"
        # Now matches the embedded snapshot.
        assert target.read_text() == _adapters._EMBEDDED_CONFIGS["claude.toml"]

    def test_partial_template_existing_partial_missing(self, tmp_path):
        # Pre-seed only claude.toml; codex.toml + cursor.toml should be
        # freshly created on the same call.
        (tmp_path / "claude.toml").write_text("# user-owned\n")
        results = _adapters.install_default_configs(target_dir=str(tmp_path))
        assert results["claude.toml"] == "skipped_exists"
        assert results["codex.toml"] == "created"
        assert results["cursor.toml"] == "created"


# ---------------------------------------------------------------------------
# _load_config — verify external [[confirm]] rules actually take effect
# ---------------------------------------------------------------------------

class TestCustomConfirmRuleLoads:
    def test_custom_confirm_rule_reaches_should_auto_confirm(
            self, tmp_path, monkeypatch):
        # Redirect the external-config dir so we don't touch real ~/.cam.
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(tmp_path))
        # Custom TOML adds ONE [[confirm]] rule on top of claude defaults.
        custom = (
            "# user customization\n"
            "[[confirm]]\n"
            'pattern    = "Please confirm: launch the rockets\\\\?"\n'
            'flags      = ["IGNORECASE"]\n'
            'response   = "y"\n'
            "send_enter = true\n"
        )
        (tmp_path / "claude.toml").write_text(custom)

        cfg = _adapters._load_config("claude")
        # The user rule must be present on the loaded adapter.
        rule_patterns = [r[0].pattern for r in cfg.confirm_rules]
        assert any("launch the rockets" in p for p in rule_patterns), (
            "custom rule did not load into adapter.confirm_rules; saw: %r"
            % rule_patterns)

        # And should_auto_confirm should fire on a screen that matches it.
        # Do not append a bare input cursor — that blocks confirm via the
        # input-box guard (has_input_cursor).
        screen = (
            "Working on the build...\n"
            "Please confirm: launch the rockets?\n"
            "(y/n)\n"
        )
        out = should_auto_confirm(screen, cfg)
        assert out is not None, "custom rule did not fire"
        response, send_enter, _pat_str, _matched = out
        assert response == "y"
        assert send_enter is True

    def test_full_template_does_not_duplicate_readiness_version_args(
            self, tmp_path, monkeypatch):
        """Regression for the install_default_configs() / _merge_toml
        interaction: after ``camc heal --upgrade`` writes a full
        embedded snapshot to ~/.cam/configs/claude.toml, _load_config
        used to APPEND every list field and emit
        readiness.version_args = ['--version', '--version']. The
        narrowed merge must override non-extension lists, leaving
        readiness.version_args = ['--version']."""
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(tmp_path))
        # Simulate exactly what install_default_configs() writes: the
        # full embedded TOML, verbatim.
        (tmp_path / "claude.toml").write_text(
            _adapters._EMBEDDED_CONFIGS["claude.toml"])

        cfg = _adapters._load_config("claude")
        assert cfg.readiness is not None
        va = cfg.readiness["version_args"]
        assert va == ["--version"], (
            "readiness.version_args duplicated on merge: %r" % va)
        # required_files / optional_files / optional_env must also stay
        # single-shot (not doubled) so adapter-owned readiness keeps
        # working after heal --upgrade.
        rf = [r["path"] for r in cfg.readiness["required_files"]]
        assert len(rf) == len(set(rf)), \
            "readiness.required_files duplicated on merge: %r" % rf
        of = [o["path"] for o in cfg.readiness["optional_files"]]
        assert len(of) == len(set(of)), \
            "readiness.optional_files duplicated on merge: %r" % of
        oe = [o["name"] for o in cfg.readiness["optional_env"]]
        assert len(oe) == len(set(oe)), \
            "readiness.optional_env duplicated on merge: %r" % oe

    def test_full_template_does_not_duplicate_state_patterns(
            self, tmp_path, monkeypatch):
        """State patterns are also a list under a non-`confirm` key.
        They must override (not double) when a full embedded snapshot
        is present as the user-side template."""
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(tmp_path))
        (tmp_path / "claude.toml").write_text(
            _adapters._EMBEDDED_CONFIGS["claude.toml"])

        cfg = _adapters._load_config("claude")
        # state_patterns is parsed into a list of (name, pattern) tuples
        # whose 'name' values must remain unique-per-occurrence; in
        # particular, the embedded set must not appear twice.
        state_names = [name for name, _pat in cfg.state_patterns]
        # The embedded claude.toml declares 4 distinct states; with the
        # old buggy merge, we'd see each appear twice (8 entries).
        assert len(state_names) == len(set(state_names)), (
            "state_patterns duplicated on merge: %r" % state_names)

    def test_full_template_plus_custom_confirm_appends_only_confirm(
            self, tmp_path, monkeypatch):
        """confirm IS the user-extension list — extras under [[confirm]]
        must still append. version_args / state_patterns / others must
        NOT."""
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(tmp_path))
        base = _adapters._EMBEDDED_CONFIGS["claude.toml"]
        extra = (
            "\n\n# user customization on top of the full template\n"
            "[[confirm]]\n"
            'pattern    = "Custom: launch the rockets\\\\?"\n'
            'flags      = ["IGNORECASE"]\n'
            'response   = "y"\n'
            "send_enter = true\n"
        )
        (tmp_path / "claude.toml").write_text(base + extra)

        cfg = _adapters._load_config("claude")
        # readiness untouched.
        assert cfg.readiness["version_args"] == ["--version"]
        # custom confirm rule reached the loaded adapter.
        patterns = [r[0].pattern for r in cfg.confirm_rules]
        assert any("launch the rockets" in p for p in patterns), \
            "custom [[confirm]] rule did not append: %r" % patterns
        # Embedded confirm rules also still present (i.e. APPEND, not
        # OVERRIDE) — pick one stable marker from embedded claude.toml.
        assert any("Yes|Allow" in p for p in patterns), \
            "embedded confirm rules dropped on merge: %r" % patterns

    def test_external_config_does_not_drop_embedded_defaults(
            self, tmp_path, monkeypatch):
        # Custom file with one extra rule — the embedded confirm rules
        # (e.g. Claude's "Do you want to proceed?") must still match.
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(tmp_path))
        (tmp_path / "claude.toml").write_text(
            "[[confirm]]\npattern=\"My rule\"\nresponse=\"y\"\n")
        cfg = _adapters._load_config("claude")
        screen = "❯ 1. Yes  2. No"
        out = should_auto_confirm(screen, cfg)
        assert out is not None, "embedded rule was dropped after merge"

    def test_stale_cursor_trust_rule_scrubbed_on_merge(self, tmp_path, monkeypatch):
        """Stale ~/.cam/configs/cursor.toml must not re-append boot-only rules."""
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(tmp_path))
        stale = (
            "[[confirm]]\n"
            'pattern = "^\\\\W*\\\\[a\\\\]\\\\s*Trust this workspace"\n'
            "response = \"a\"\n"
            "send_enter = false\n"
            "[[confirm]]\n"
            'pattern = "My custom runtime rule"\n'
            "response = \"x\"\n"
            "send_enter = false\n"
        )
        (tmp_path / "cursor.toml").write_text(stale)

        cfg = _adapters._load_config("cursor")
        patterns = [r[0].pattern for r in cfg.confirm_rules]
        assert not any("Trust this workspace" in p for p in patterns), (
            "retired trust rule was merged: %r" % patterns)
        assert any("My custom runtime rule" in p for p in patterns), (
            "custom rule was dropped: %r" % patterns)


# ---------------------------------------------------------------------------
# heal --upgrade installs templates without clobbering edits
# ---------------------------------------------------------------------------

class TestHealUpgradeInstallsTemplates:
    def test_upgrade_creates_missing_templates(self, tmp_path, monkeypatch):
        # Redirect every disk path heal touches into tmp.
        cam_dir = tmp_path / ".cam"
        configs_dir = cam_dir / "configs"
        cam_dir.mkdir()
        # adapters.CONFIGS_DIR is what install_default_configs() reads
        # when target_dir is omitted (i.e. cmd_heal's caller path).
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(configs_dir))
        # cmd_heal also scans /tmp for orphan sockets — neuter that.
        _real_listdir = os.listdir
        monkeypatch.setattr(_cli.os, "listdir",
                            lambda p: [] if p == "/tmp"
                            else _real_listdir(p))
        # An empty agent store keeps the heal body short.
        from camc_pkg import storage as _storage
        monkeypatch.setattr(_storage, "AGENTS_FILE",
                            str(cam_dir / "agents.json"))
        # Cron tick hook short-circuit.
        from camc_pkg import cron as _cron
        monkeypatch.setattr(_cron, "ensure_tick_if_needed",
                            lambda *a, **kw: "noop")
        # Don't actually kill anything when --upgrade fires.
        monkeypatch.setattr(_cli, "_kill_all_monitors", lambda: 0)

        _cli.cmd_heal(argparse.Namespace(upgrade=True))

        for fname in _adapters._EMBEDDED_CONFIGS.keys():
            assert (configs_dir / fname).exists(), \
                "upgrade did not create %s" % fname

    def test_upgrade_does_not_overwrite_existing_user_files(
            self, tmp_path, monkeypatch):
        cam_dir = tmp_path / ".cam"
        configs_dir = cam_dir / "configs"
        configs_dir.mkdir(parents=True)
        original = '# user-owned\n[[confirm]]\npattern="X"\nresponse="y"\n'
        (configs_dir / "claude.toml").write_text(original)

        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(configs_dir))
        _real_listdir = os.listdir
        monkeypatch.setattr(_cli.os, "listdir",
                            lambda p: [] if p == "/tmp"
                            else _real_listdir(p))
        from camc_pkg import storage as _storage
        monkeypatch.setattr(_storage, "AGENTS_FILE",
                            str(cam_dir / "agents.json"))
        from camc_pkg import cron as _cron
        monkeypatch.setattr(_cron, "ensure_tick_if_needed",
                            lambda *a, **kw: "noop")
        monkeypatch.setattr(_cli, "_kill_all_monitors", lambda: 0)

        _cli.cmd_heal(argparse.Namespace(upgrade=True))

        assert (configs_dir / "claude.toml").read_text() == original, \
            "heal --upgrade clobbered a user-edited file"
        # And siblings WERE created.
        assert (configs_dir / "codex.toml").exists()
        assert (configs_dir / "cursor.toml").exists()


# ---------------------------------------------------------------------------
# Sanity: cmd_init uses the same installer and respects --force
# ---------------------------------------------------------------------------

class TestCmdInitDelegatesToInstaller:
    def test_cmd_init_writes_templates_via_helper(self, tmp_path, monkeypatch,
                                                    capsys):
        cam_dir = tmp_path / ".cam"
        configs_dir = cam_dir / "configs"
        monkeypatch.setattr(_cli, "CAM_DIR", str(cam_dir))
        monkeypatch.setattr(_cli, "CONFIGS_DIR", str(configs_dir))
        monkeypatch.setattr(_adapters, "CONFIGS_DIR", str(configs_dir))
        # cmd_init also touches CONTEXT_FILE via _load_default_context;
        # redirect that to tmp too.
        monkeypatch.setattr(_cli, "CONTEXT_FILE", str(cam_dir / "context.json"))
        # _load_default_context() reaches into PROJECT_ROOT; stub a no-op.
        monkeypatch.setattr(_cli, "_load_default_context",
                            lambda: {"name": "default"})

        _cli.cmd_init(argparse.Namespace(force=False))
        out = capsys.readouterr().out
        for fname in _adapters._EMBEDDED_CONFIGS.keys():
            assert (configs_dir / fname).exists()
            assert fname in out
