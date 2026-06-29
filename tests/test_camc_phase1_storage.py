"""Phase 1 home-quota protection for camc storage.

The feature is deliberately best-effort: failures must never break camc
commands, and source directories must not be replaced unless copying
succeeds first.
"""

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import cli  # noqa: E402


def _write(path, text):
    parent = os.path.dirname(str(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(str(path), "w") as f:
        f.write(text)


class TestPhase1Storage:
    def test_explicit_camc_data_dir_moves_logs_and_symlinks_back(
            self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        data = tmp_path / "data"
        logs = home / ".cam" / "logs"
        logs.mkdir(parents=True)
        _write(logs / "monitor-a.log", "hello")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CAMC_DATA_DIR", str(data))

        cli._ensure_phase1_storage()

        assert os.path.islink(str(logs))
        assert os.readlink(str(logs)) == str(data / ".cam" / "logs")
        with open(str(data / ".cam" / "logs" / "monitor-a.log")) as f:
            assert f.read() == "hello"

    def test_moves_cron_heavy_dirs_only_when_present(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        data = tmp_path / "data"
        cron_logs = home / ".cam" / "cron" / "logs"
        cron_archive = home / ".cam" / "cron" / "archive"
        cron_logs.mkdir(parents=True)
        cron_archive.mkdir(parents=True)
        _write(cron_logs / "r1.log", "run")
        _write(cron_archive / "j1.json", "{}")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CAMC_DATA_DIR", str(data))

        cli._ensure_phase1_storage()

        assert os.path.islink(str(cron_logs))
        assert os.path.islink(str(cron_archive))
        assert os.path.exists(str(data / ".cam" / "cron" / "logs" / "r1.log"))
        assert os.path.exists(str(data / ".cam" / "cron" / "archive" / "j1.json"))

    def test_copy_failure_leaves_source_directory_intact(
            self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        data = tmp_path / "data"
        logs = home / ".cam" / "logs"
        logs.mkdir(parents=True)
        _write(logs / "monitor-a.log", "hello")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CAMC_DATA_DIR", str(data))

        def boom(src, dst):
            raise IOError("copy failed")

        monkeypatch.setattr(cli, "_copy_tree_contents", boom)
        cli._ensure_phase1_storage()

        assert os.path.isdir(str(logs))
        assert not os.path.islink(str(logs))
        with open(str(logs / "monitor-a.log")) as f:
            assert f.read() == "hello"

    def test_main_continues_if_phase1_storage_raises(
            self, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "argv", ["camc", "list"])
        monkeypatch.setattr(cli, "_ensure_logs_on_scratch", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(cli, "cmd_list", lambda args: calls.append(args))

        cli.main()

        assert len(calls) == 1
