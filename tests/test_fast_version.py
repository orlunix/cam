"""Version queries must preserve output without initializing agent services."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from camc_pkg.fast_version import early_version_main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("version_build", ROOT / "build_camc.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    path = tmp_path_factory.mktemp("fast-version") / "camc"
    path.write_text(builder.build())
    path.chmod(0o755)
    return path


def test_shell_version_does_not_require_python_or_tmux(candidate, tmp_path):
    home = tmp_path / "home with spaces"
    configs = home / ".cam" / "configs"
    configs.mkdir(parents=True)
    (configs / "claude.toml").write_text("not parsed by version")
    env = dict(os.environ, HOME=str(home), PATH="/nonexistent")
    result = subprocess.run([str(candidate), "version"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    assert result.returncode == 0 and not result.stderr
    assert "  claude (config: %s)" % (configs / "claude.toml") in result.stdout
    assert "  codex (embedded)" in result.stdout
    assert "  pi (embedded)" in result.stdout
    assert not (home / ".cam" / "logs").exists()
    assert not (home / ".cam" / "agents.json").exists()


@pytest.mark.parametrize("flags", [[], ["--json"], ["-v"], ["--verbose", "--json"]])
def test_python_and_shell_version_output_match(candidate, flags):
    expected = subprocess.check_output([str(candidate), "version"])
    actual = subprocess.check_output([sys.executable, str(candidate)] + flags + ["version"])
    assert actual == expected


@pytest.mark.parametrize("argv", [[], ["run"], ["version", "unexpected"],
                                  ["version", "--help"], ["--host", "remote", "version"]])
def test_fast_version_does_not_consume_other_commands(argv, capsys):
    assert early_version_main(argv, version="test", config_names=[]) is None
    assert not capsys.readouterr().out


def test_source_version_skips_cli_and_capture_imports():
    program = """
import runpy, sys
sys.argv = ['camc', 'version']
try:
    runpy.run_module('camc_pkg', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
assert 'camc_pkg.cli' not in sys.modules
assert 'camc_pkg.transport' not in sys.modules
assert 'camc_pkg.fast_capture' not in sys.modules
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run([sys.executable, "-c", program], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr


def test_version_help_and_invalid_arguments_keep_parser_behavior(candidate):
    help_result = subprocess.run([str(candidate), "version", "--help"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert help_result.returncode == 0
    assert b"usage: camc version" in help_result.stdout
    error = subprocess.run([str(candidate), "version", "unexpected"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert error.returncode == 2
    assert b"unrecognized arguments" in error.stderr


def test_unset_home_falls_back_to_python_lookup(candidate):
    env = dict(os.environ)
    env.pop("HOME", None)
    shell = subprocess.check_output([str(candidate), "version"], env=env)
    python = subprocess.check_output([sys.executable, str(candidate), "version"], env=env)
    assert shell == python
