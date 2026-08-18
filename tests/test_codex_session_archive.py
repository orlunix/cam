"""Regression tests for Codex rollout binding and archive collection."""

from __future__ import annotations

import builtins
import io
import inspect
import json
import os
import tarfile
from argparse import Namespace

import pytest

import camc_pkg.cli as cli
from camc_pkg.storage import AgentStore


UUID = "019f6919-3328-7d53-bc32-71523711d9ae"
STARTED = "2026-08-05T10:00:00Z"


def test_iso_parser_is_python36_compatible():
    source = inspect.getsource(cli._parse_iso_timestamp)
    assert "datetime.fromisoformat" not in source
    assert cli._parse_iso_timestamp(
        "2026-08-13T23:09:30.975Z").isoformat() == (
            "2026-08-13T23:09:30.975000+00:00")
    assert cli._parse_iso_timestamp(
        "2026-08-13T16:09:30-07:00").isoformat() == (
            "2026-08-13T23:09:30+00:00")


def _make_rollout(tmp_path, *, cwd, session_id=UUID):
    path = (
        tmp_path / "sessions" / "2026" / "08" / "05"
        / f"rollout-2026-08-05T10-00-01-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "type": "session_meta",
        "timestamp": STARTED,
        "payload": {
            "session_id": session_id,
            "cwd": cwd,
            "timestamp": STARTED,
        },
    }) + "\n", encoding="utf-8")
    return path


def _fake_proc_fd(monkeypatch, pid, rollout, codex_home):
    """Make one fake /proc fd and CODEX_HOME environment entry."""
    real_open = builtins.open
    real_listdir = os.listdir
    real_readlink = os.readlink

    def fake_open(path, *args, **kwargs):
        if path == f"/proc/{pid}/environ":
            return io.BytesIO(f"CODEX_HOME={codex_home}\0".encode())
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(
        os, "listdir",
        lambda path: ["9"] if path == f"/proc/{pid}/fd" else real_listdir(path),
    )
    monkeypatch.setattr(
        os, "readlink",
        lambda path: str(rollout) if path == f"/proc/{pid}/fd/9" else real_readlink(path),
    )


def test_validated_codex_rollout_requires_pid_fd_uuid_meta_cwd_and_start(
    monkeypatch, tmp_path,
):
    rollout = _make_rollout(tmp_path, cwd="/work")
    _fake_proc_fd(monkeypatch, 4321, rollout, tmp_path)

    assert cli._validated_codex_rollout(4321, "/work", STARTED) == (
        UUID,
        str(rollout),
    )


def test_validated_codex_rollout_rejects_wrong_metadata_cwd(monkeypatch, tmp_path):
    rollout = _make_rollout(tmp_path, cwd="/other")
    _fake_proc_fd(monkeypatch, 4321, rollout, tmp_path)

    assert cli._validated_codex_rollout(4321, "/work", STARTED) is None


def _codex_agent(agent_id, session="cam-agent-a"):
    return {
        "id": agent_id,
        "session_id": "",
        "task": {"tool": "codex"},
        "tmux_session": session,
        "context_path": "/work",
        "started_at": STARTED,
    }


def test_codex_binder_updates_only_the_matching_agent(monkeypatch, tmp_path):
    store = AgentStore(str(tmp_path / "agents.json"))
    store.save(_codex_agent("agent-a"))
    store.save(_codex_agent("agent-b", "cam-agent-b"))
    monkeypatch.setattr(cli, "_find_codex_pids_for_session", lambda _session: [4321])
    monkeypatch.setattr(
        cli, "_validated_codex_rollout", lambda *_args: (UUID, "/tmp/rollout.jsonl"),
    )

    assert cli._bind_codex_session_once(
        "agent-a", "cam-agent-a", "/work", STARTED, store=store,
    ) is True
    assert store.get("agent-a")["session_id"] == UUID
    assert store.get("agent-a")["session_path"] == "/tmp/rollout.jsonl"
    assert store.get("agent-a")["session_binding"] == "bound"
    assert store.get("agent-b")["session_id"] == ""


def test_codex_binder_never_updates_when_pid_evidence_is_missing(monkeypatch, tmp_path):
    store = AgentStore(str(tmp_path / "agents.json"))
    store.save(_codex_agent("agent-a"))
    monkeypatch.setattr(cli, "_find_codex_pids_for_session", lambda _session: [4321])
    monkeypatch.setattr(cli, "_validated_codex_rollout", lambda *_args: None)

    assert cli._bind_codex_session_once(
        "agent-a", "cam-agent-a", "/work", STARTED, store=store,
    ) is False
    assert store.get("agent-a")["session_id"] == ""
    assert store.get("agent-a").get("session_path") is None


def test_codex_binder_is_spawned_detached_without_waiting(monkeypatch):
    calls = []

    class FakeProcess:
        pass

    monkeypatch.setattr(cli.sys, "argv", ["/opt/camc"])
    monkeypatch.setattr(
        cli.subprocess, "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or FakeProcess(),
    )

    cli._spawn_codex_session_binder("agent-a", "cam-agent-a", "/work", STARTED)

    argv, kwargs = calls[0]
    assert argv == [
        "/opt/camc", "_bind_codex_session", "agent-a", "cam-agent-a",
        "/work", STARTED,
    ]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is cli.subprocess.DEVNULL
    assert kwargs["stderr"] is cli.subprocess.DEVNULL


def test_codex_binder_keeps_polling_past_the_old_five_second_window(monkeypatch):
    attempts = []

    def eventually_binds(*_args, **_kwargs):
        attempts.append(None)
        return len(attempts) == 21

    monkeypatch.setattr(cli, "_bind_codex_session_once", eventually_binds)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    assert cli._run_codex_session_binder("agent-a", "cam-agent-a", "/work", STARTED) == 0
    assert len(attempts) == 21


class _NoEvents:
    def read(self, **_kwargs):
        return []


def _archive_members(path):
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }


def _archive_codex_agent(store, *, session_id, session_path, workdir):
    store.save({
        "id": "agent-a",
        "session_id": session_id,
        "session_path": session_path,
        "task": {"name": "codex-agent", "tool": "codex"},
        "context_path": workdir,
        "tmux_session": "",
    })


def test_archive_includes_only_the_validated_codex_rollout(monkeypatch, tmp_path):
    rollout = _make_rollout(tmp_path, cwd="/work")
    store = AgentStore(str(tmp_path / "agents.json"))
    _archive_codex_agent(
        store, session_id=UUID, session_path=str(rollout), workdir="/work",
    )
    monkeypatch.setattr(cli, "AgentStore", lambda: store)
    monkeypatch.setattr(cli, "EventStore", _NoEvents)

    out_dir = tmp_path / "archives"
    cli.cmd_archive(Namespace(id="agent-a", output=str(out_dir), session_id=None))

    archives = list(out_dir.glob("*.tar.gz"))
    assert len(archives) == 1
    assert _archive_members(archives[0])["codex/session.jsonl"] == rollout.read_bytes()


def test_unbound_codex_archive_never_uses_claude_session_guessing(
    monkeypatch, tmp_path,
):
    store = AgentStore(str(tmp_path / "agents.json"))
    _archive_codex_agent(store, session_id="", session_path="", workdir="/work")
    monkeypatch.setattr(cli, "AgentStore", lambda: store)
    monkeypatch.setattr(cli, "EventStore", _NoEvents)
    monkeypatch.setattr(
        cli, "_find_session_id",
        lambda *_args, **_kwargs: pytest.fail("Codex must not use Claude discovery"),
    )

    out_dir = tmp_path / "archives"
    cli.cmd_archive(Namespace(id="agent-a", output=str(out_dir), session_id=None))

    archives = list(out_dir.glob("*.tar.gz"))
    assert len(archives) == 1
    assert "codex/session.jsonl" not in _archive_members(archives[0])
