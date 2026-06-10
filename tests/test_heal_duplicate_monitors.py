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
        from camc_pkg import cron as _cron
        import socket as _sock

        # ---- Redirect ~/.cam/* into tmp_path ----
        agents_file = tmp_path / "agents.json"
        monkeypatch.setattr(_storage, "AGENTS_FILE", str(agents_file))
        # Heal also touches cron.ensure_tick_if_needed → stub it out.
        monkeypatch.setattr(_cron, "ensure_tick_if_needed",
                            lambda *a, **kw: "noop")
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
            "status": "running",
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
