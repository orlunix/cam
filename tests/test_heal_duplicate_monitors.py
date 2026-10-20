"""Focused tests for `cmd_heal`'s duplicate-monitor cleanup.

Covers two helpers added in src/camc_pkg/cli.py:
  * `_find_monitor_pids(agent_id)` — best-effort `ps` enumeration.
  * `_cleanup_duplicate_monitors(agent_id, record_pid, pidfile_pid)` —
    keeper preference + SIGTERM of older extras.

No live `ps` / `os.kill` calls are made: subprocess + os.kill are
monkey-patched. Python 3.6+, stdlib only.
"""

import os
import signal
import subprocess

import pytest

from camc_pkg import cli as _cli


@pytest.mark.parametrize("status", ["stopped", "completed", "failed"])
@pytest.mark.parametrize("other_monitor", [None, "alive", "dead"])
def test_heal_preserves_terminal_agents_with_live_shells(
        tmp_path, monkeypatch, status, other_monitor):
    from types import SimpleNamespace
    from camc_pkg.storage import AgentStore
    store = AgentStore(str(tmp_path / "agents.json"))
    terminal = {
        "id": "dead1234", "status": status, "state": "idle",
        "hostname": _cli._sock.gethostname(), "tmux_session": "cam-dead1234",
        "completed_at": "2026-10-14T00:00:00Z", "exit_reason": "retained shell",
    }
    store.save(terminal)
    if other_monitor:
        store.save(dict(terminal, id="beef1234", status="running", pid=123456,
                        tmux_session="cam-beef1234", completed_at=None))
    monkeypatch.setattr(_cli, "AgentStore", lambda: store)
    monkeypatch.setattr(_cli, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(_cli, "SOCKETS_DIR", str(tmp_path / "sockets"))
    os.mkdir(_cli.SOCKETS_DIR)
    open(os.path.join(_cli.SOCKETS_DIR, "cam-dead1234.sock"), "w").close()
    monkeypatch.setattr(_cli, "tmux_session_exists", lambda _s: True)
    monkeypatch.setattr(_cli, "_cleanup_duplicate_monitors", lambda *_a: (123456 if other_monitor == "alive" else None, []))
    monkeypatch.setattr(_cli, "_find_claude_pid", lambda _s: None)
    monkeypatch.setattr(_cli, "EventStore", lambda: SimpleNamespace(rotate=lambda **_kw: 0))
    monkeypatch.setattr(_cli, "has_local_cron_work", lambda: False)
    # Exercise the orphan scan with the retained terminal session too.
    monkeypatch.setattr(_cli.os, "listdir", lambda p:
                        ["cam-dead1234.sock"] if p == _cli.SOCKETS_DIR else [])
    def probe_pid(pid, sig):
        assert (pid, sig) == (123456, 0)
        if other_monitor == "dead":
            raise ProcessLookupError()
    monkeypatch.setattr(_cli.os, "kill", probe_pid)
    spawned = []
    monkeypatch.setattr(_cli.subprocess, "Popen", lambda argv, **_kw:
                        spawned.append(argv) or SimpleNamespace(pid=234567))

    _cli._do_heal()

    from camc_pkg.storage import normalize_agent
    assert store.get(terminal["id"]) == normalize_agent(terminal)
    assert [argv[-1] for argv in spawned] == (["beef1234"] if other_monitor == "dead" else [])
    if other_monitor:
        assert store.get("beef1234")["status"] == "alive"
        assert store.get("beef1234")["pid"] == (234567 if other_monitor == "dead" else 123456)


@pytest.mark.parametrize("orphan_home", [None, "current", "other", "unavailable"])
@pytest.mark.parametrize("restart", [False, True])
def test_heal_with_no_running_agents_repairs_cron_and_adopts_only_owned_orphans(
        tmp_path, monkeypatch, orphan_home, restart):
    from types import SimpleNamespace
    from camc_pkg.storage import AgentStore
    store = AgentStore(str(tmp_path / "agents.json"))
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    if orphan_home:
        (sockets / "cam-abcd1234.sock").touch()
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setattr(_cli, "AgentStore", lambda: store)
    monkeypatch.setattr(_cli, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(_cli, "SOCKETS_DIR", str(sockets))
    listdir = os.listdir
    monkeypatch.setattr(_cli.os, "listdir", lambda p:
                        listdir(p) if str(p) == str(sockets) else [])
    monkeypatch.setattr(_cli, "tmux_session_exists", lambda _s: True)
    def query(argv, **_kw):
        if argv[-2:] == ["show-environment", "HOME"]:
            if orphan_home == "unavailable":
                return 1, ""
            return 0, "HOME=" + (home if orphan_home == "current" else "/other/home")
        return 0, str(tmp_path)
    monkeypatch.setattr(_cli, "_run", query)
    monkeypatch.setattr(_cli, "_find_claude_pid", lambda _s: None)
    monkeypatch.setattr(_cli, "_find_session_id", lambda *_a, **_kw: "")
    spawned = []
    monkeypatch.setattr(_cli.subprocess, "Popen", lambda argv, **_kw:
                        spawned.append(argv) or SimpleNamespace(pid=234567))
    maintenance = []
    monkeypatch.setattr(_cli, "EventStore", lambda: SimpleNamespace(
        rotate=lambda **_kw: maintenance.append("events") or 0))
    monkeypatch.setattr(_cli, "has_local_cron_work", lambda: not restart)
    monkeypatch.setattr(_cli, "cron_scheduler_present", lambda: restart)
    def repair(**kwargs):
        maintenance.append(kwargs)
        return {"status": "running"}
    monkeypatch.setattr(_cli, "ensure_cron_scheduler", repair)

    _cli._do_heal(restart_cron=restart)

    assert maintenance == ["events", {"wait": True, "restart": restart}]
    assert [r["id"] for r in store.list()] == (["abcd1234"] if orphan_home == "current" else [])
    assert len(spawned) == (1 if orphan_home == "current" else 0)


# ---------------------------------------------------------------------------
# _find_monitor_pids
# ---------------------------------------------------------------------------

class TestFindMonitorPids:
    def _patch_ps_extended(self, monkeypatch, text):
        calls = []
        def fake(cmd, **kw):
            calls.append(list(cmd))
            return text.encode("utf-8")
        monkeypatch.setattr(_cli.subprocess, "check_output", fake)
        return calls

    def test_finds_two_distinct_monitors(self, monkeypatch):
        ps = (
            "  101 120 python camc _monitor abc12345\n"
            "  202  30 python camc _monitor abc12345\n"
            "  303 999 python camc _monitor other999\n"
            "  404  10 grep _monitor abc12345\n"
        )
        self._patch_ps_extended(monkeypatch, ps)
        pids = _cli._find_monitor_pids("abc12345")
        assert sorted(pids) == [(101, 120), (202, 30)]

    def test_skips_current_process(self, monkeypatch):
        my = os.getpid()
        ps = "%d  50 python camc _monitor abc12345\n  777 5 camc _monitor abc12345\n" % my
        self._patch_ps_extended(monkeypatch, ps)
        pids = _cli._find_monitor_pids("abc12345")
        assert pids == [(777, 5)]

    def test_returns_empty_on_no_match(self, monkeypatch):
        ps = "1 0 init\n2 0 kthreadd\n"
        self._patch_ps_extended(monkeypatch, ps)
        assert _cli._find_monitor_pids("zzz99999") == []

    def test_token_boundary_avoids_substring_collision(self, monkeypatch):
        ps = (
            "  101 50 python camc _monitor abc12345\n"
            "  202 60 python camc _monitor abc123456789\n"   # superset id
        )
        self._patch_ps_extended(monkeypatch, ps)
        pids = _cli._find_monitor_pids("abc12345")
        # only the exact-token line — the longer id is NOT a substring match.
        assert pids == [(101, 50)]

    def test_falls_back_to_ps_aux_on_extended_failure(self, monkeypatch):
        # First call (extended) raises; second call (aux) returns data.
        calls = {"n": 0}
        def fake(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("no etimes support")
            return (
                "user  111 0.0 0.0 0 0 ? S 00:00 0:00 python camc _monitor abc12345\n"
                "user  222 0.0 0.0 0 0 ? S 00:00 0:00 python camc _monitor abc12345\n"
            ).encode("utf-8")
        monkeypatch.setattr(_cli.subprocess, "check_output", fake)
        pids = _cli._find_monitor_pids("abc12345")
        # etimes is None in the fallback path.
        assert sorted(pids) == [(111, None), (222, None)]

    def test_returns_empty_when_all_ps_calls_fail(self, monkeypatch):
        def fake(cmd, **kw):
            raise OSError("ps missing")
        monkeypatch.setattr(_cli.subprocess, "check_output", fake)
        assert _cli._find_monitor_pids("abc12345") == []

    def test_returns_empty_when_agent_id_empty(self):
        assert _cli._find_monitor_pids("") == []
        assert _cli._find_monitor_pids(None) == []


# ---------------------------------------------------------------------------
# _cleanup_duplicate_monitors
# ---------------------------------------------------------------------------

class _RecKill(object):
    """Record os.kill calls; raise on a pre-seeded missing-pid set."""
    def __init__(self, missing=()):
        self.calls = []
        self.missing = set(missing)

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if pid in self.missing:
            raise ProcessLookupError(pid)


class TestCleanupDuplicateMonitors:
    def test_no_monitor_is_no_op(self):
        rk = _RecKill()
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", 123, 456,
            _find=lambda _aid: [],
            _kill=rk)
        assert kept is None
        assert killed == []
        assert rk.calls == []

    def test_single_monitor_is_no_op(self):
        rk = _RecKill()
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", 999, None,
            _find=lambda _aid: [(101, 30)],
            _kill=rk)
        assert kept == 101
        assert killed == []
        assert rk.calls == []

    def test_keeps_record_pid_when_in_set(self):
        rk = _RecKill()
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", 200, 999,
            _find=lambda _aid: [(100, 50), (200, 30), (300, 10)],
            _kill=rk)
        assert kept == 200
        assert set(killed) == {100, 300}
        assert {sig for _p, sig in rk.calls} == {signal.SIGTERM}

    def test_keeps_pidfile_pid_when_record_pid_absent(self):
        rk = _RecKill()
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", 999, 300,                       # 999 NOT in live set
            _find=lambda _aid: [(100, 50), (200, 30), (300, 10)],
            _kill=rk)
        assert kept == 300
        assert set(killed) == {100, 200}

    def test_keeps_newest_when_neither_canonical(self):
        rk = _RecKill()
        # Lowest etimes (3s) is the newest.
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", 999, 888,
            _find=lambda _aid: [(100, 50), (200, 30), (300, 3)],
            _kill=rk)
        assert kept == 300
        assert set(killed) == {100, 200}

    def test_newest_falls_back_to_highest_pid_when_etimes_unknown(self):
        rk = _RecKill()
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", None, None,
            _find=lambda _aid: [(100, None), (200, None), (300, None)],
            _kill=rk)
        assert kept == 300
        assert set(killed) == {100, 200}

    def test_dead_pid_during_kill_is_swallowed(self):
        rk = _RecKill(missing={100})           # SIGTERM 100 -> race lost
        kept, killed = _cli._cleanup_duplicate_monitors(
            "aid", 300, None,
            _find=lambda _aid: [(100, 50), (200, 40), (300, 10)],
            _kill=rk)
        assert kept == 300
        # 100 was "missing" — it should NOT appear in killed.
        assert killed == [200]

    def test_sigterm_not_sigkill(self):
        rk = _RecKill()
        _cli._cleanup_duplicate_monitors(
            "aid", 200, None,
            _find=lambda _aid: [(100, 50), (200, 30)],
            _kill=rk)
        for _pid, sig in rk.calls:
            assert sig == signal.SIGTERM
            assert sig != signal.SIGKILL


# ---------------------------------------------------------------------------
# cmd_heal adoption path: a single live but untracked monitor must be
# adopted (record pid bumped, no new monitor spawned). This is the
# REVIEW=needs_fix scenario from cam-review#d123bdd9.
# ---------------------------------------------------------------------------

class TestHealAdoptsLiveUntrackedMonitor:
    def test_single_live_monitor_is_adopted_without_spawn(
            self, tmp_path, monkeypatch, capsys):
        import json as _json
        from camc_pkg import storage as _storage
        import socket as _sock

        # ---- Redirect ~/.cam/* into tmp_path ----
        agents_file = tmp_path / "agents.json"
        monkeypatch.setattr(_storage, "AGENTS_FILE", str(agents_file))
        # Stub /tmp scan that the heal hook walks (preserve real listdir).
        _real_listdir = os.listdir
        monkeypatch.setattr(_cli.os, "listdir",
                            lambda p: [] if p == "/tmp"
                            else _real_listdir(p))

        # ---- Seed one running agent ----
        my_host = _sock.gethostname()
        aid = "abc12345"
        # record_pid points at a "stale" pid; we override os.kill so
        # kill(stale_pid, 0) raises ProcessLookupError (i.e. dead).
        stale_pid = 999111
        live_pid  = 999222   # what _find_monitor_pids will report
        agent = {
            "id": aid,
            "task": {"name": "live-untracked", "tool": "claude",
                     "auto_confirm": True},
            "context_path": str(tmp_path),
            "transport_type": "local",
            "status": "alive",
            "state": "idle",
            "tmux_session": "cam-" + aid,
            "pid": stale_pid,
            "hostname": my_host,
            "started_at": "2026-06-10T00:00:00Z",
        }
        with open(str(agents_file), "w") as f:
            _json.dump([agent], f)

        # ---- Force the heal-path conditions ----
        # tmux session is "alive" — ONLY for our seeded agent (otherwise
        # the orphan-adoption phase would pull in every cam-*.sock under
        # /tmp on this host).
        monkeypatch.setattr(_cli, "tmux_session_exists",
                            lambda s: s == "cam-" + aid)
        # Also keep the orphan-socket scan empty so this test isolates
        # phase 2 (running-agent monitor sweep).
        monkeypatch.setattr(_cli, "SOCKETS_DIR", str(tmp_path / "no-sockets"))
        # exactly one live monitor process, untracked by record/pidfile
        monkeypatch.setattr(_cli, "_find_monitor_pids",
                            lambda a_id: [(live_pid, 5)])

        # os.kill(stale,0) -> dead; os.kill(live,0) -> alive
        def fake_kill(pid, sig):
            if pid == stale_pid:
                raise ProcessLookupError(pid)
            if pid == live_pid:
                return None  # alive
            raise ProcessLookupError(pid)
        monkeypatch.setattr(_cli.os, "kill", fake_kill)

        # ---- Block any monitor respawn; record the call if it happens ----
        spawned = []
        class _NoSpawnPopen(object):
            def __init__(self, args, **kw):
                spawned.append(list(args))
                self.pid = 7777
        monkeypatch.setattr(_cli.subprocess, "Popen", _NoSpawnPopen)

        # ---- Run heal ----
        import argparse
        _cli.cmd_heal(argparse.Namespace(upgrade=False))

        # ---- Assertions ----
        # 1. No new monitor was spawned.
        monitor_spawns = [s for s in spawned
                          if any("_monitor" in str(p) for p in s)]
        assert monitor_spawns == [], (
            "heal spawned a duplicate monitor: %r" % monitor_spawns)
        # 2. The record pid was bumped to the live untracked monitor.
        with open(str(agents_file)) as f:
            persisted = _json.load(f)
        assert persisted[0]["pid"] == live_pid, (
            "expected pid -> %d, got %r" % (live_pid, persisted[0].get("pid")))
        # 3. The "duplicate monitors cleaned" line should NOT appear —
        #    no kills happened — but heal should report `ok`.
        out = capsys.readouterr().out
        assert "duplicate monitors cleaned" not in out
        assert ": ok" in out
