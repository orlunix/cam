"""Tests for camc_pkg.cli session ID extraction (4-layer fallback).

Covers the bug where `camc heal`/`camc add` adopted agents without recording
their Claude session UUID, causing `camc reboot` to resume the wrong session.
"""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import pytest

from camc_pkg.cli import (
    _extract_session_from_cmdline,
    _extract_session_from_fd,
    _extract_session_from_project_dir,
    _extract_session_from_jsonl,
    _find_session_id,
    _find_session_id_pid,
    _project_dirs_for_workdir,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    """Redirect ~ to a tmp dir so tests don't touch the real ~/.claude/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # os.path.expanduser reads HOME on POSIX, but be defensive
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1) if p.startswith("~") else p)
    return tmp_path


def _mk_project(tmp_home, workdir, session_uuid, with_subdir=True, with_jsonl=False):
    """Create a ~/.claude/projects/<enc>/ fixture with optional subdir/jsonl."""
    enc = "-" + workdir.strip("/").replace("/", "-")
    proj = tmp_home / ".claude" / "projects" / enc
    proj.mkdir(parents=True, exist_ok=True)
    if with_subdir:
        sub = proj / session_uuid
        sub.mkdir()
        (sub / "subagents").mkdir()
    if with_jsonl:
        (proj / f"{session_uuid}.jsonl").write_text("{}\n")
    return proj


# ---------------------------------------------------------------------------
# Layer 1a: /proc/<pid>/cmdline
# ---------------------------------------------------------------------------


def _fake_cmdline(monkeypatch, pid, parts):
    """Install a fake /proc/<pid>/cmdline reader returning the given argv."""
    real_open = open
    payload = ("\x00".join(parts) + "\x00").encode()

    class FakeFile:
        def __init__(self, data):
            self._data = data
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_open(path, *a, **kw):
        if path == f"/proc/{pid}/cmdline":
            return FakeFile(payload)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)


class TestExtractFromCmdline:
    def test_session_id_space_form(self, monkeypatch):
        uid = "0d27cb26-0000-0000-0000-000000000000"
        _fake_cmdline(monkeypatch, 42, ["claude", "--session-id", uid])
        assert _extract_session_from_cmdline(42) == uid

    def test_resume_form(self, monkeypatch):
        uid = "12345678-1234-1234-1234-123456789012"
        _fake_cmdline(monkeypatch, 42, ["claude", "--resume", uid])
        assert _extract_session_from_cmdline(42) == uid

    def test_session_id_equals_form(self, monkeypatch):
        uid = "abcdef12-1111-2222-3333-444455556666"
        _fake_cmdline(monkeypatch, 42, ["claude", f"--session-id={uid}"])
        assert _extract_session_from_cmdline(42) == uid

    def test_ignores_non_uuid_value(self, monkeypatch):
        _fake_cmdline(monkeypatch, 42, ["claude", "--session-id", "not-a-uuid"])
        assert _extract_session_from_cmdline(42) is None

    def test_no_pid_returns_none(self):
        assert _extract_session_from_cmdline(None) is None
        assert _extract_session_from_cmdline(0) is None

    def test_missing_pid_returns_none(self):
        # Very unlikely pid
        assert _extract_session_from_cmdline(999_999_999) is None


# ---------------------------------------------------------------------------
# _find_session_id_pid — PID-authoritative only, never falls through to workdir
# ---------------------------------------------------------------------------


class TestFindSessionIdPidOnly:
    """Backfill must not propagate workdir-based guesses across agents that
    happen to share a cwd. _find_session_id_pid returns None rather than
    falling back to project-dir scanning.
    """

    def test_returns_cmdline_uuid(self, monkeypatch):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_cmdline",
                            lambda pid: uid if pid else None)
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_fd",
                            lambda pid: None)
        assert _find_session_id_pid(123) == uid

    def test_falls_back_to_fd_when_cmdline_empty(self, monkeypatch):
        uid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_cmdline",
                            lambda pid: None)
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_fd",
                            lambda pid: uid if pid else None)
        assert _find_session_id_pid(123) == uid

    def test_returns_none_when_both_pid_layers_empty(self, tmp_home, monkeypatch):
        # Even with a project dir that would match under workdir-based layers,
        # _find_session_id_pid must not consult it.
        _mk_project(tmp_home, "/home/hren", "99999999-9999-9999-9999-999999999999")
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_cmdline",
                            lambda pid: None)
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_fd",
                            lambda pid: None)
        assert _find_session_id_pid(123) is None

    def test_no_pid_returns_none(self):
        assert _find_session_id_pid(None) is None
        assert _find_session_id_pid(0) is None


# ---------------------------------------------------------------------------
# Layer 1b: /proc/<pid>/fd
# ---------------------------------------------------------------------------


class TestExtractFromFd:
    def test_finds_uuid_in_fd_target(self, tmp_path, monkeypatch):
        """fd target contains .claude/tasks/<uuid>/.lock → UUID returned."""
        fd_dir = tmp_path / "proc" / "12345" / "fd"
        fd_dir.mkdir(parents=True)
        # Create a fake fd symlink
        target = "/home/hren/.claude/tasks/0f845ea5-6f07-4fc2-bf47-0d4531f8c0a3/.lock"
        (fd_dir / "7").symlink_to(target)
        # Another unrelated fd
        (fd_dir / "3").symlink_to("/dev/null")

        # Monkeypatch os.listdir / os.readlink to pretend /proc/12345/fd lives here
        real_listdir = os.listdir
        real_readlink = os.readlink

        def fake_listdir(path):
            if path == "/proc/12345/fd":
                return real_listdir(str(fd_dir))
            return real_listdir(path)

        def fake_readlink(path):
            if path.startswith("/proc/12345/fd/"):
                fd = path.rsplit("/", 1)[1]
                return real_readlink(str(fd_dir / fd))
            return real_readlink(path)

        monkeypatch.setattr(os, "listdir", fake_listdir)
        monkeypatch.setattr(os, "readlink", fake_readlink)

        assert (
            _extract_session_from_fd(12345)
            == "0f845ea5-6f07-4fc2-bf47-0d4531f8c0a3"
        )

    def test_no_pid_returns_none(self):
        assert _extract_session_from_fd(None) is None
        assert _extract_session_from_fd(0) is None

    def test_nonexistent_pid_returns_none(self):
        # Very unlikely PID that won't exist
        assert _extract_session_from_fd(999_999_999) is None

    def test_no_matching_fd_returns_none(self, tmp_path, monkeypatch):
        fd_dir = tmp_path / "proc" / "22222" / "fd"
        fd_dir.mkdir(parents=True)
        (fd_dir / "1").symlink_to("/dev/pts/0")

        real_listdir, real_readlink = os.listdir, os.readlink
        monkeypatch.setattr(os, "listdir",
                            lambda p: real_listdir(str(fd_dir)) if p == "/proc/22222/fd" else real_listdir(p))
        monkeypatch.setattr(os, "readlink",
                            lambda p: real_readlink(str(fd_dir / p.rsplit('/', 1)[1])) if p.startswith("/proc/22222/fd/") else real_readlink(p))

        assert _extract_session_from_fd(22222) is None


# ---------------------------------------------------------------------------
# Layer 2: project-dir session subdirectory
# ---------------------------------------------------------------------------


class TestExtractFromProjectDir:
    def test_returns_newest_uuid_subdir(self, tmp_home):
        _mk_project(tmp_home, "/home/hren", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        _mk_project(tmp_home, "/home/hren", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        # Make bbbb newer
        newer = tmp_home / ".claude/projects/-home-hren/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        os.utime(newer, (10_000_000_000, 10_000_000_000))

        assert (
            _extract_session_from_project_dir("/home/hren")
            == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )

    def test_ignores_memory_subdir(self, tmp_home):
        proj = tmp_home / ".claude/projects/-home-hren"
        proj.mkdir(parents=True)
        (proj / "memory").mkdir()
        # Only non-matching, should return None
        assert _extract_session_from_project_dir("/home/hren") is None

    def test_no_project_dir_returns_none(self, tmp_home):
        assert _extract_session_from_project_dir("/nope") is None

    def test_handles_dot_encoding_variant(self, tmp_home):
        """Project dir names with dots encoded as dashes are also checked."""
        enc_dash = "-home-scratch-hren_gpu_1"
        proj = tmp_home / ".claude/projects" / enc_dash
        proj.mkdir(parents=True)
        uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        (proj / uuid).mkdir()

        # Input uses dots; helper should also try dash-for-dot encoding.
        assert (
            _extract_session_from_project_dir("/home/scratch.hren_gpu_1")
            == uuid
        )

    def test_handles_canonical_dot_underscore_encoding(self, tmp_home):
        """Canonical Claude encoding replaces / . _ all with dashes."""
        # /home/scratch.hren_gpu_1/test/fn211 → -home-scratch-hren-gpu-1-test-fn211
        enc = "-home-scratch-hren-gpu-1-test-fn211"
        proj = tmp_home / ".claude/projects" / enc
        proj.mkdir(parents=True)
        uuid = "11112222-3333-4444-5555-666677778888"
        (proj / uuid).mkdir()

        assert (
            _extract_session_from_project_dir("/home/scratch.hren_gpu_1/test/fn211")
            == uuid
        )


# ---------------------------------------------------------------------------
# Layer 3: .jsonl fallback
# ---------------------------------------------------------------------------


class TestExtractFromJsonl:
    def test_newest_jsonl_wins(self, tmp_home):
        proj = tmp_home / ".claude/projects/-home-hren"
        proj.mkdir(parents=True)
        old = proj / "11111111-1111-1111-1111-111111111111.jsonl"
        new = proj / "22222222-2222-2222-2222-222222222222.jsonl"
        old.write_text("{}")
        new.write_text("{}")
        os.utime(old, (1, 1))
        os.utime(new, (10_000_000_000, 10_000_000_000))

        assert (
            _extract_session_from_jsonl("/home/hren")
            == "22222222-2222-2222-2222-222222222222"
        )


# ---------------------------------------------------------------------------
# _find_session_id — priority ordering
# ---------------------------------------------------------------------------


class TestFindSessionIdPriority:
    AGENT_ID = "abc12345"

    def _no_pid_signals(self, monkeypatch):
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_cmdline", lambda pid: None)
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_fd", lambda pid: None)

    def test_cmdline_wins_over_everything(self, tmp_home, monkeypatch):
        """Layer 1a (cmdline) beats fd + project dir when all three exist."""
        cmd_uuid = "11111111-1111-1111-1111-111111111111"
        fd_uuid = "22222222-2222-2222-2222-222222222222"
        proj_uuid = "33333333-3333-3333-3333-333333333333"
        _mk_project(tmp_home, "/home/hren", proj_uuid)

        monkeypatch.setattr("camc_pkg.cli._extract_session_from_cmdline",
                            lambda pid: cmd_uuid if pid else None)
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_fd",
                            lambda pid: fd_uuid if pid else None)

        assert _find_session_id(self.AGENT_ID, 123, "/home/hren") == cmd_uuid

    def test_fd_wins_over_project_dir(self, tmp_home, monkeypatch):
        fd_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        proj_uuid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        _mk_project(tmp_home, "/home/hren", proj_uuid)

        monkeypatch.setattr("camc_pkg.cli._extract_session_from_cmdline",
                            lambda pid: None)
        monkeypatch.setattr("camc_pkg.cli._extract_session_from_fd",
                            lambda pid: fd_uuid if pid else None)
        assert _find_session_id(self.AGENT_ID, 123, "/home/hren") == fd_uuid

    def test_project_dir_when_no_pid_signals(self, tmp_home, monkeypatch):
        proj_uuid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        _mk_project(tmp_home, "/home/hren", proj_uuid)
        self._no_pid_signals(monkeypatch)

        assert _find_session_id(self.AGENT_ID, 123, "/home/hren") == proj_uuid

    def test_jsonl_when_no_subdir(self, tmp_home, monkeypatch):
        proj = tmp_home / ".claude/projects/-home-hren"
        proj.mkdir(parents=True)
        uuid = "99999999-9999-9999-9999-999999999999"
        (proj / f"{uuid}.jsonl").write_text("{}")
        self._no_pid_signals(monkeypatch)

        assert _find_session_id(self.AGENT_ID, 0, "/home/hren") == uuid

    def test_synthetic_last_resort(self, tmp_home, monkeypatch):
        self._no_pid_signals(monkeypatch)
        out = _find_session_id(self.AGENT_ID, 0, "/nowhere")
        assert out == f"{self.AGENT_ID}-0000-0000-0000-000000000000"

    def test_no_agent_id_and_nothing_found_returns_none(self, tmp_home, monkeypatch):
        self._no_pid_signals(monkeypatch)
        assert _find_session_id("", 0, "/nowhere") is None

    def test_pane_cwd_beats_stored_workdir(self, tmp_home, monkeypatch):
        """Pane CWD is tried before context_path — picks up post-`cd` session."""
        stored_uuid = "aaaa0000-1111-2222-3333-444455556666"
        live_uuid = "bbbb0000-1111-2222-3333-444455556666"
        _mk_project(tmp_home, "/home/hren", stored_uuid)
        _mk_project(tmp_home, "/home/hren/sub", live_uuid)

        self._no_pid_signals(monkeypatch)
        monkeypatch.setattr(
            "camc_pkg.cli._get_tmux_pane_cwd",
            lambda session: "/home/hren/sub",
        )

        out = _find_session_id(
            self.AGENT_ID, 0, workdir="/home/hren", tmux_session="cam-abc",
        )
        assert out == live_uuid

    def test_falls_back_to_stored_workdir_when_pane_cwd_empty(self, tmp_home, monkeypatch):
        stored_uuid = "cccc0000-1111-2222-3333-444455556666"
        _mk_project(tmp_home, "/home/hren", stored_uuid)

        self._no_pid_signals(monkeypatch)
        monkeypatch.setattr("camc_pkg.cli._get_tmux_pane_cwd", lambda session: None)

        out = _find_session_id(
            self.AGENT_ID, 0, workdir="/home/hren", tmux_session="cam-abc",
        )
        assert out == stored_uuid


# ---------------------------------------------------------------------------
# _project_dirs_for_workdir — encoding variants
# ---------------------------------------------------------------------------


class TestProjectDirsForWorkdir:
    def test_emits_all_three_variants_for_dots_and_underscores(self):
        """Canonical (/ . _ → -), conservative (/ . → -), minimal (/ → -)."""
        dirs = _project_dirs_for_workdir("/home/scratch.hren_gpu_1")
        names = [os.path.basename(d) for d in dirs]
        assert "-home-scratch-hren-gpu-1" in names  # canonical
        assert "-home-scratch-hren_gpu_1" in names  # dots only
        assert "-home-scratch.hren_gpu_1" in names  # minimal

    def test_canonical_first(self):
        """Canonical encoding is tried first (picked up before legacy)."""
        dirs = _project_dirs_for_workdir("/home/scratch.hren_gpu_1")
        assert os.path.basename(dirs[0]) == "-home-scratch-hren-gpu-1"

    def test_no_dup_when_no_dots_or_underscores(self):
        dirs = _project_dirs_for_workdir("/home/hren")
        assert len(dirs) == 1
        assert dirs[0].endswith("/-home-hren")

    def test_complex_path_from_task_doc(self):
        """Exact example from the task doc."""
        dirs = _project_dirs_for_workdir("/home/scratch.hren_gpu_1/test/fn211")
        names = [os.path.basename(d) for d in dirs]
        assert "-home-scratch-hren-gpu-1-test-fn211" in names


# ---------------------------------------------------------------------------
# Session-file check in cmd_heal uses the canonical encoding
# ---------------------------------------------------------------------------


class TestSessionFileCheckEncoding:
    """Regression: the session-file existence check in heal used to slug via
    ctx_path.replace('/', '-'), which produced false 'missing' warnings for
    every path containing a dot or underscore. It should now go through
    _project_dirs_for_workdir and find the file under the canonical name."""

    def test_finds_jsonl_via_canonical_encoding(self, tmp_home):
        # File lives under the canonical encoding
        uuid = "deadbeef-0000-0000-0000-000000000000"
        proj = tmp_home / ".claude/projects/-home-scratch-hren-gpu-1-test"
        proj.mkdir(parents=True)
        (proj / f"{uuid}.jsonl").write_text("{}")

        # The heal check builds the path via _project_dirs_for_workdir now,
        # so the same helper should surface this directory first.
        dirs = _project_dirs_for_workdir("/home/scratch.hren_gpu_1/test")
        first = dirs[0]
        found = os.path.exists(os.path.join(first, f"{uuid}.jsonl"))
        assert found, f"expected file under {first}"
