"""Retired commands stay absent in source and both standalone entry points."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", params=["source", "dist", "fresh-build"])
def cli_entry(request, tmp_path_factory):
    if request.param == "source":
        return [sys.executable, "-m", "camc_pkg"]
    if request.param == "dist":
        return ["/bin/sh", str(ROOT / "dist/camc")]
    spec = importlib.util.spec_from_file_location("build_camc", ROOT / "build_camc.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    artifact = tmp_path_factory.mktemp("camc-cli-build") / "camc"
    artifact.write_text(builder.build(), encoding="utf-8")
    return ["/bin/sh", str(artifact)]


@pytest.mark.parametrize("command", ["apply", "machine", "context", "sync", "db-migrate", "skills add", "skills rm"])
def test_retired_commands_are_rejected_without_touching_registries(
        cli_entry, command, tmp_path):
    cam_dir = tmp_path / ".cam"
    cam_dir.mkdir()
    contents = b'[{"name":"existing","host":"example.test"}]\n'
    for name in ("machines.json", "contexts.json"):
        (cam_dir / name).write_bytes(contents)
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(ROOT / "src"),
               CAMC_PRELUDE_DISABLE="all")

    result = subprocess.run(cli_entry + command.split(), env=env, cwd=str(tmp_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=15)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "invalid choice: '%s'" % command.split()[-1] in result.stderr
    for name in ("machines.json", "contexts.json"):
        assert (cam_dir / name).read_bytes() == contents


def test_run_still_accepts_explicit_path_and_launcher(cli_entry, tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(ROOT / "src"),
               CAMC_PRELUDE_DISABLE="all")
    result = subprocess.run(cli_entry + ["run", "--help"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--path" in result.stdout
    assert "--tool-launcher" in result.stdout
