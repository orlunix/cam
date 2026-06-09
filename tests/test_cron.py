"""Focused tests for camc_pkg.cron (jobs.d / run worker scheme).

All disk paths, subprocess calls, and EventStore mirrors are monkey-patched
onto tmp_path / mocked runners — no live crontab mutation, no live tick
scheduling, and no writes to the real ~/.cam event log.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from camc_pkg import cron as _cron


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def home(monkeypatch, tmp_path):
    """Repoint every ~/.cam/cron-* path at tmp_path."""
    cam = tmp_path / ".cam"
    cron_dir = cam / "cron"
    cron_dir.mkdir(parents=True)
    monkeypatch.setattr(_cron, "CRON_DIR", str(cron_dir))
    monkeypatch.setattr(_cron, "CRON_JOBS_DIR", str(cron_dir / "jobs.d"))
    monkeypatch.setattr(_cron, "CRON_LEGACY_FILE", str(cron_dir / "jobs.json"))
    monkeypatch.setattr(_cron, "CRON_FILE", str(cron_dir / "jobs.json"))
    monkeypatch.setattr(_cron, "CRON_CONFIG_FILE", str(cron_dir / "config.json"))
    monkeypatch.setattr(_cron, "CRON_RUNS_FILE", str(cron_dir / "runs.jsonl"))
    monkeypatch.setattr(_cron, "CRON_STATE_FILE", str(cron_dir / "state.json"))
    monkeypatch.setattr(_cron, "CRON_LOCK_FILE", str(cron_dir / "tick.lock"))
    monkeypatch.setattr(_cron, "CRON_RUN_LOCK_FILE", str(cron_dir / "run.lock"))
    monkeypatch.setattr(_cron, "CRON_ARCHIVE_DIR", str(cron_dir / "archive"))
    monkeypatch.setattr(_cron, "CRON_LOGS_DIR", str(cron_dir / "logs"))
    monkeypatch.setattr(_cron, "CRON_HUMAN_LOG", str(cron_dir / "cron.log"))
    monkeypatch.setattr(_cron, "_emit_event", lambda *args, **kwargs: None)
    try:
        from camc_pkg import cli as _cli
        monkeypatch.setattr(_cli, "_emit_event", lambda *args, **kwargs: None)
    except Exception:
        pass
    return cron_dir


def _make_argv_cmd():
    return {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}


# ---------------------------------------------------------------------------
# Schedule parsers
# ---------------------------------------------------------------------------

class TestScheduleParsers:
    def test_every_minutes_and_hours(self):
        assert _cron.parse_every("30m") == {
            "type": "interval", "every_seconds": 1800,
        }
        assert _cron.parse_every("2h") == {
            "type": "interval", "every_seconds": 7200,
        }

    def test_every_rejects_garbage(self):
        for bad in ("0m", "-5m", "30s", "abc", "", None):
            with pytest.raises(ValueError):
                _cron.parse_every(bad)

    def test_daily_normalizes_hour(self):
        assert _cron.parse_daily("9:30") == {
            "type": "daily", "time": "09:30", "timezone": "local",
        }

    def test_daily_rejects_bad_time(self):
        for bad in ("24:00", "9:60", "1230", "noon", ""):
            with pytest.raises(ValueError):
                _cron.parse_daily(bad)

    def test_at_iso_with_offset(self):
        s = _cron.parse_at("2026-05-12T09:30:00-07:00")
        assert s["type"] == "once"
        assert s["run_at"].startswith("2026-05-12T09:30:00-07:00")

    def test_in_creates_future_run_at(self):
        s = _cron.parse_in("45m")
        assert s["type"] == "once"
        run_dt = datetime.fromisoformat(s["run_at"])
        delta = run_dt - datetime.now().astimezone()
        assert timedelta(minutes=44, seconds=50) < delta < timedelta(minutes=45, seconds=10)


# ---------------------------------------------------------------------------
# next_due_at computation
# ---------------------------------------------------------------------------

class TestNextDueAt:
    def test_initial_interval_lands_after_one_period(self):
        now = _cron._now_local()
        sched = _cron.parse_every("30m")
        sched["next_due_at"] = _cron._initial_next_due_at(sched, now)
        nda = datetime.fromisoformat(sched["next_due_at"])
        assert timedelta(minutes=29) < nda - now < timedelta(minutes=31)

    def test_initial_daily_next_occurrence(self):
        now = _cron._now_local()
        sched = _cron.parse_daily("00:01")
        nda_iso = _cron._initial_next_due_at(sched, now)
        nda = datetime.fromisoformat(nda_iso)
        assert nda > now

    def test_advance_interval_jumps_past_now(self):
        now = _cron._now_local()
        sched = {"type": "interval", "every_seconds": 60,
                 "next_due_at": _cron._iso(now - timedelta(minutes=5))}
        new = _cron.advance_next_due_at(sched, now)
        nda = datetime.fromisoformat(new["next_due_at"])
        assert nda > now

    def test_advance_once_clears_next_due(self):
        now = _cron._now_local()
        sched = {"type": "once",
                 "run_at": _cron._iso(now - timedelta(seconds=10)),
                 "next_due_at": _cron._iso(now - timedelta(seconds=10))}
        new = _cron.advance_next_due_at(sched, now)
        assert new["next_due_at"] is None


# ---------------------------------------------------------------------------
# Build job
# ---------------------------------------------------------------------------

class TestBuildJob:
    def test_default_ttl_resolves_to_expires_at(self, home):
        job = _cron.build_job(
            "j", _cron.parse_every("1h"), _make_argv_cmd())
        exp = job["policy"]["expires_at"]
        assert exp is not None
        # ~7 days out
        d = datetime.fromisoformat(exp) - datetime.now().astimezone()
        assert timedelta(days=6, hours=23) < d < timedelta(days=7, hours=1)

    def test_no_expire_clears_expires_at(self, home):
        job = _cron.build_job(
            "j", _cron.parse_every("1h"), _make_argv_cmd(),
            no_expire=True)
        assert job["policy"]["expires_at"] is None

    def test_explicit_expires_at_preserved(self, home):
        job = _cron.build_job(
            "j", _cron.parse_every("1h"), _make_argv_cmd(),
            expires_at="2099-01-01T00:00:00+00:00")
        assert job["policy"]["expires_at"] == "2099-01-01T00:00:00+00:00"

    def test_host_defaults_to_current_hostname(self, home):
        job = _cron.build_job(
            "j", _cron.parse_every("1h"), _make_argv_cmd())
        assert job["host"] == _cron._hostname()

    def test_schedule_has_next_due_at(self, home):
        job = _cron.build_job(
            "j", _cron.parse_every("30m"), _make_argv_cmd())
        assert job["schedule"].get("next_due_at")

    def test_state_initialized(self, home):
        job = _cron.build_job(
            "j", _cron.parse_every("30m"), _make_argv_cmd())
        st = job["state"]
        assert st["attempts"] == 0
        assert st["last_run_id"] is None
        assert st["last_status"] is None


# ---------------------------------------------------------------------------
# CronJobStore: jobs.d/ per-file storage
# ---------------------------------------------------------------------------

class TestCronJobStore:
    def test_add_creates_one_file_per_job(self, home):
        store = _cron.CronJobStore()
        job = _cron.build_job("a", _cron.parse_every("30m"), _make_argv_cmd())
        store.add(job)
        files = sorted(os.listdir(_cron.CRON_JOBS_DIR))
        assert files == ["%s.json" % job["id"]]

    def test_find_by_name_or_prefix(self, home):
        store = _cron.CronJobStore()
        j = _cron.build_job("hello", _cron.parse_every("30m"), _make_argv_cmd())
        store.add(j)
        assert store.find("hello")["id"] == j["id"]
        assert store.find(j["id"])["id"] == j["id"]
        # Unique prefix.
        if len(j["id"]) >= 4:
            assert store.find(j["id"][:4])["id"] == j["id"]

    def test_duplicate_name_rejected(self, home):
        store = _cron.CronJobStore()
        j1 = _cron.build_job("dup", _cron.parse_every("30m"), _make_argv_cmd())
        store.add(j1)
        j2 = _cron.build_job("dup", _cron.parse_every("30m"), _make_argv_cmd())
        with pytest.raises(_cron.DuplicateJobName):
            store.add(j2)
        assert len(os.listdir(_cron.CRON_JOBS_DIR)) == 1

    def test_remove_deletes_file(self, home):
        store = _cron.CronJobStore()
        j = _cron.build_job("x", _cron.parse_every("30m"), _make_argv_cmd())
        store.add(j)
        removed = store.remove("x")
        assert removed["id"] == j["id"]
        assert os.listdir(_cron.CRON_JOBS_DIR) == []

    def test_ambiguous_prefix_raises(self, home):
        store = _cron.CronJobStore()
        # Force two ids with shared prefix by writing manually.
        for jid in ("ab111111", "ab222222"):
            j = _cron.build_job("n_" + jid, _cron.parse_every("30m"),
                                _make_argv_cmd())
            j["id"] = jid
            store.save(j)
        with pytest.raises(_cron.AmbiguousJobKey):
            store.find("ab")

    def test_corrupt_job_file_reported(self, home):
        store = _cron.CronJobStore()
        j = _cron.build_job("ok", _cron.parse_every("30m"), _make_argv_cmd())
        store.add(j)
        bad = os.path.join(_cron.CRON_JOBS_DIR, "bad.json")
        with open(bad, "w") as f:
            f.write("not json {{{")
        assert store.is_corrupt()
        assert bad in store.corrupt_files()
        # Good job still readable.
        assert any(j["name"] == "ok" for j in store.jobs())

    def test_migrate_legacy_jobs_json(self, home):
        legacy_payload = {"version": 1, "jobs": [{
            "id": "legacy01", "name": "legacy",
            "enabled": True,
            "schedule": {"type": "interval", "every_seconds": 1800},
            "host": _cron._hostname(),
            "expires_at": None,
            "max_attempts": 3, "attempts": 0,
            "created_at": _cron._iso(_cron._now_local()),
            "command": _make_argv_cmd(),
        }]}
        with open(_cron.CRON_LEGACY_FILE, "w") as f:
            json.dump(legacy_payload, f)
        store = _cron.CronJobStore()
        migrated = store.migrate_legacy_if_present()
        assert migrated == 1
        jobs = store.jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == "legacy01"
        assert jobs[0]["policy"]["max_attempts"] == 3
        # legacy file renamed out of the way
        assert not os.path.exists(_cron.CRON_LEGACY_FILE)
        assert os.path.exists(_cron.CRON_LEGACY_FILE + ".migrated")


# ---------------------------------------------------------------------------
# is_due — next_due_at is source of truth
# ---------------------------------------------------------------------------

class TestIsDue:
    def test_due_when_next_due_at_in_past(self, home):
        j = _cron.build_job(
            "j", _cron.parse_every("30m"), _make_argv_cmd())
        j["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=1))
        due, due_at = _cron.is_due(j)
        assert due
        assert due_at == j["schedule"]["next_due_at"]

    def test_not_due_before_next_due_at(self, home):
        j = _cron.build_job(
            "j", _cron.parse_every("30m"), _make_argv_cmd())
        j["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() + timedelta(hours=1))
        due, _ = _cron.is_due(j)
        assert not due

    def test_disabled_not_due(self, home):
        j = _cron.build_job(
            "j", _cron.parse_every("30m"), _make_argv_cmd())
        j["enabled"] = False
        j["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=1))
        due, _ = _cron.is_due(j)
        assert not due


# ---------------------------------------------------------------------------
# Crontab block management
# ---------------------------------------------------------------------------

class _FakeCrontab(object):
    def __init__(self, current=""):
        self.current = current
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if args[:2] == ["crontab", "-l"]:
            if not self.current:
                return subprocess.CompletedProcess(
                    args, 1, "", "no crontab for tester\n")
            return subprocess.CompletedProcess(args, 0, self.current, "")
        if args[:2] == ["crontab", "-"]:
            self.current = kw.get("input", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError("unexpected subprocess call: %r" % (args,))


class TestCrontabBlock:
    def test_install_replaces_existing_block(self, home):
        ct = _FakeCrontab("# user line\n%s\n%s\n%s\n# user 2\n" % (
            _cron.CRON_BEGIN, "0 0 * * * old line", _cron.CRON_END))
        _cron.install_tick(camc_path="/usr/local/bin/camc", runner=ct)
        assert "old line" not in ct.current
        assert _cron.CRON_BEGIN in ct.current
        assert "# user line" in ct.current
        assert "# user 2" in ct.current

    def test_install_into_empty_crontab(self, home):
        ct = _FakeCrontab("")
        _cron.install_tick(camc_path="/usr/local/bin/camc", runner=ct)
        assert _cron.CRON_BEGIN in ct.current
        assert "/usr/local/bin/camc cron tick" in ct.current

    def test_remove_strips_block_preserves_other_lines(self, home):
        ct = _FakeCrontab("# user A\n%s\nLINE\n%s\n# user B\n" % (
            _cron.CRON_BEGIN, _cron.CRON_END))
        removed = _cron.remove_tick(runner=ct)
        assert removed
        assert _cron.CRON_BEGIN not in ct.current
        assert "# user A" in ct.current
        assert "# user B" in ct.current

    def test_ensure_tick_installs_when_jobs_exist(self, home):
        store = _cron.CronJobStore()
        store.add(_cron.build_job("j", _cron.parse_every("30m"), _make_argv_cmd()))
        ct = _FakeCrontab("")
        result = _cron.ensure_tick_if_needed(
            runner=ct, camc_path="/usr/local/bin/camc")
        assert result == "installed"
        assert _cron.CRON_BEGIN in ct.current

    def test_ensure_tick_removes_when_no_jobs(self, home):
        ct = _FakeCrontab("# u\n%s\nline\n%s\n" % (
            _cron.CRON_BEGIN, _cron.CRON_END))
        result = _cron.ensure_tick_if_needed(runner=ct)
        assert result == "removed"
        assert _cron.CRON_BEGIN not in ct.current

    def test_ensure_tick_refuses_to_touch_corrupt(self, home):
        ct = _FakeCrontab("")
        _cron.CronJobStore().add(_cron.build_job(
            "ok", _cron.parse_every("30m"), _make_argv_cmd()))
        # Add a corrupt file alongside.
        bad = os.path.join(_cron.CRON_JOBS_DIR, "bad.json")
        with open(bad, "w") as f:
            f.write("not json")
        result = _cron.ensure_tick_if_needed(runner=ct)
        assert isinstance(result, tuple)
        assert result[0] == "cron_json_corrupt"

    def test_crontab_path_is_short_and_stable(self, home, monkeypatch):
        # Inject an absurdly-long login PATH and confirm it does NOT
        # leak into the crontab block. Spec demands a short stable PATH.
        long_path = ":".join("/x/%d/bin" % i for i in range(40))
        monkeypatch.setenv("PATH", long_path)
        block = _cron._build_tick_block(camc_path="/usr/local/bin/camc")
        line = block.splitlines()[1]
        assert "/x/0/bin" not in line
        assert "/usr/local/bin:/usr/bin:/bin" in line


# ---------------------------------------------------------------------------
# Tick — schedules + queues + dispatches worker (NO inline execution)
# ---------------------------------------------------------------------------

def _seed_due_job(store, name, **overrides):
    j = _cron.build_job(name, _cron.parse_every("30m"), _make_argv_cmd())
    j["schedule"]["next_due_at"] = _cron._iso(
        _cron._now_local() - timedelta(minutes=1))
    j.update(overrides)
    store.save(j)
    return j


class TestTick:
    def test_tick_queues_and_advances_next_due_at(self, home):
        store = _cron.CronJobStore()
        j = _seed_due_job(store, "j1")
        spawned = []
        result = _cron.tick(spawn=lambda rid: spawned.append(rid) or 12345)
        assert result["status"] == "ok"
        assert result["queued"] == 1
        # next_due_at advanced
        reloaded = store.find(j["id"])
        new_nda = datetime.fromisoformat(reloaded["schedule"]["next_due_at"])
        assert new_nda > _cron._now_local()
        # run_queued recorded
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        queued = [e for e in events if e.get("event") == "run_queued"]
        assert len(queued) == 1
        assert queued[0]["job_id"] == j["id"]
        # spawn called with the same run_id
        assert spawned == [queued[0]["run_id"]]

    def test_tick_does_not_execute_inline(self, home):
        # Job command is `false` so an INLINE executor would record
        # a failure. With queueing semantics there must be no run_started
        # or run_failed produced by tick itself.
        store = _cron.CronJobStore()
        j = _seed_due_job(store, "j2")
        j["command"] = {"argv": ["false"], "cwd": ".", "timeout_seconds": 60}
        store.save(j)
        _cron.tick(spawn=lambda rid: 0)
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        evnames = [e.get("event") for e in events]
        assert "run_queued" in evnames
        assert "run_started" not in evnames
        assert "run_failed" not in evnames

    def test_tick_dedupes_same_due_at(self, home):
        store = _cron.CronJobStore()
        j = _seed_due_job(store, "dedup")
        nda = j["schedule"]["next_due_at"]
        # First tick queues; second tick on the SAME due_at must not re-queue.
        # Pin schedule.next_due_at back so the second tick sees it as still due.
        spawned = []
        _cron.tick(spawn=lambda rid: spawned.append(rid))
        # roll next_due_at back so it's "due" again but with same due_at value
        j2 = store.find(j["id"])
        j2["schedule"]["next_due_at"] = nda
        store.save(j2)
        _cron.tick(spawn=lambda rid: spawned.append(rid))
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        queued = [e for e in events if e.get("event") == "run_queued"
                  and e.get("job_id") == j["id"]]
        assert len(queued) == 1

    def test_tick_skipped_when_lock_held(self, home, monkeypatch):
        monkeypatch.setattr(_cron, "_acquire_lock", lambda path: None)
        result = _cron.tick(spawn=lambda rid: None)
        assert result["status"] == "skipped_locked"

    def test_host_mismatch_skips_with_event(self, home, monkeypatch):
        store = _cron.CronJobStore()
        j = _seed_due_job(store, "other", host="some-other-host-xyz")
        monkeypatch.setattr(_cron, "_hostname",
                            lambda: "this-is-the-current-host")
        _cron.tick(spawn=lambda rid: None)
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "job_skipped_host"
                   and e.get("job_id") == j["id"] for e in events)

    def test_tick_writes_state_heartbeat(self, home):
        _cron.tick(spawn=lambda rid: None)
        assert os.path.exists(_cron.CRON_STATE_FILE)
        with open(_cron.CRON_STATE_FILE) as f:
            state = json.load(f)
        assert state["last_tick_status"] == "ok"

    def test_tick_respects_global_enabled_false(self, home):
        cfg = dict(_cron.DEFAULT_CONFIG)
        cfg["enabled"] = False
        with open(_cron.CRON_CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        result = _cron.tick(spawn=lambda rid: None)
        assert result["status"] == "disabled"

    def test_tick_fails_closed_on_corrupt_config(self, home):
        with open(_cron.CRON_CONFIG_FILE, "w") as f:
            f.write("not json {{{")
        result = _cron.tick(spawn=lambda rid: None)
        assert result["status"] == "error"

    def test_tick_fails_closed_on_corrupt_job_file(self, home):
        _cron.CronJobStore().add(_cron.build_job(
            "ok", _cron.parse_every("30m"), _make_argv_cmd()))
        bad = os.path.join(_cron.CRON_JOBS_DIR, "bad.json")
        with open(bad, "w") as f:
            f.write("nope")
        result = _cron.tick(spawn=lambda rid: None)
        assert result["status"] == "error"

    def test_tick_recycles_expired_jobs(self, home):
        store = _cron.CronJobStore()
        j = _cron.build_job(
            "exp", _cron.parse_every("30m"), _make_argv_cmd())
        j["policy"]["expires_at"] = _cron._iso(
            _cron._now_local() - timedelta(seconds=1))
        store.save(j)
        _cron.tick(spawn=lambda rid: None)
        assert store.find(j["id"]) is None
        # archive written
        assert os.listdir(_cron.CRON_ARCHIVE_DIR)


# ---------------------------------------------------------------------------
# cron_run worker
# ---------------------------------------------------------------------------

class TestCronRun:
    def _seed_queued(self, store, command, run_id=None):
        j = _cron.build_job(
            "wrk", _cron.parse_every("30m"), command)
        j["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=1))
        store.save(j)
        rid = run_id or _cron._new_run_id()
        _cron._append_runs({
            "event": "run_queued", "run_id": rid,
            "job_id": j["id"], "job_name": j["name"],
            "due_at": j["schedule"]["next_due_at"],
            "host": _cron._hostname(),
        })
        return j, rid

    def test_run_executes_argv_and_records_success(self, home):
        store = _cron.CronJobStore()
        j, rid = self._seed_queued(store, _make_argv_cmd())  # `true`
        r = _cron.cron_run(rid)
        assert r["status"] == "succeeded"
        assert r["exit_code"] == 0
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        evnames = [e.get("event") for e in events]
        assert "run_started" in evnames
        assert "run_succeeded" in evnames
        log = os.path.join(_cron.CRON_LOGS_DIR, "%s.log" % rid)
        assert os.path.exists(log)

    def test_run_records_failure_for_nonzero(self, home):
        store = _cron.CronJobStore()
        j, rid = self._seed_queued(
            store, {"argv": ["false"], "cwd": ".", "timeout_seconds": 60})
        r = _cron.cron_run(rid)
        assert r["status"] == "run_failed"
        reloaded = store.find(j["id"])
        assert reloaded["state"]["attempts"] == 1
        assert reloaded["state"]["last_status"] == "failed"

    def test_run_timeout_records_run_timed_out(self, home):
        store = _cron.CronJobStore()
        j, rid = self._seed_queued(
            store, {"argv": ["sleep", "60"], "cwd": ".",
                    "timeout_seconds": 1})
        r = _cron.cron_run(rid)
        assert r["status"] == "run_timed_out"
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "run_timed_out" for e in events)

    def test_run_refuses_missing_run_id(self, home):
        r = _cron.cron_run("rNOPE12345")
        assert r["status"] == "missing"

    def test_run_refuses_already_terminal(self, home):
        store = _cron.CronJobStore()
        j, rid = self._seed_queued(store, _make_argv_cmd())
        _cron.cron_run(rid)  # first run terminal
        before = sum(1 for _ in _cron._scan_runs())
        r = _cron.cron_run(rid)
        assert r["status"] == "terminal"
        after = sum(1 for _ in _cron._scan_runs())
        assert before == after  # no new events

    def test_once_job_recycled_after_success(self, home):
        store = _cron.CronJobStore()
        j = _cron.build_job("once", {
            "type": "once",
            "run_at": _cron._iso(_cron._now_local() - timedelta(seconds=1)),
        }, _make_argv_cmd())
        # build_job set next_due_at = run_at for once jobs.
        store.save(j)
        rid = _cron._new_run_id()
        _cron._append_runs({
            "event": "run_queued", "run_id": rid,
            "job_id": j["id"], "job_name": j["name"],
            "due_at": j["schedule"]["next_due_at"],
            "host": _cron._hostname(),
        })
        _cron.cron_run(rid)
        assert store.find(j["id"]) is None  # recycled

    def test_failure_recycle_after_max_attempts(self, home):
        store = _cron.CronJobStore()
        j = _cron.build_job(
            "fail", _cron.parse_every("30m"),
            {"argv": ["false"], "cwd": ".", "timeout_seconds": 60},
            max_attempts=2)
        j["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=1))
        store.save(j)
        for _ in range(2):
            rid = _cron._new_run_id()
            _cron._append_runs({
                "event": "run_queued", "run_id": rid,
                "job_id": j["id"], "job_name": j["name"],
                "due_at": j["schedule"]["next_due_at"],
                "host": _cron._hostname(),
            })
            _cron.cron_run(rid)
        assert store.find(j["id"]) is None  # recycled

    def test_run_skips_other_host_run(self, home, monkeypatch):
        store = _cron.CronJobStore()
        j, rid = self._seed_queued(store, _make_argv_cmd())
        # Override the queued record's host to a different host.
        _cron._append_runs({
            "event": "run_queued", "run_id": "rotherhost",
            "job_id": j["id"], "due_at": "x",
            "host": "some-other-machine-xyz",
        })
        monkeypatch.setattr(_cron, "_hostname",
                            lambda: "current-machine-abc")
        r = _cron.cron_run("rotherhost")
        assert r["status"] == "skipped_host"


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------

class TestCmdCron:
    def _add(self, **kw):
        defaults = dict(
            name="t", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["--", "true"],
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_add_writes_job_and_installs_block(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(self._add())
        assert ei.value.code == 0
        files = os.listdir(_cron.CRON_JOBS_DIR)
        assert len(files) == 1
        assert _cron.CRON_BEGIN in ct.current

    def test_add_duplicate_name_rejected(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        with pytest.raises(SystemExit):
            cli.cmd_cron_add(self._add(name="dup"))
        before = sorted(os.listdir(_cron.CRON_JOBS_DIR))
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(self._add(name="dup"))
        assert ei.value.code == 1
        after = sorted(os.listdir(_cron.CRON_JOBS_DIR))
        assert before == after

    def test_add_rejects_missing_command(self, home, monkeypatch):
        from camc_pkg import cli
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(self._add(cmd_argv=[]))
        assert ei.value.code == 1
        assert not os.path.exists(_cron.CRON_JOBS_DIR) or \
            not os.listdir(_cron.CRON_JOBS_DIR)

    def test_add_argv_preserves_embedded_separator_tokens(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        argv = ["--", "echo", "--", "after-sep"]
        with pytest.raises(SystemExit):
            cli.cmd_cron_add(self._add(cmd_argv=argv))
        jobs = _cron.CronJobStore().jobs()
        assert jobs[0]["command"]["argv"] == ["echo", "--", "after-sep"]

    def test_add_shell_mode_stores_shell(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        with pytest.raises(SystemExit):
            cli.cmd_cron_add(self._add(shell_cmd="echo hi", cmd_argv=[]))
        jobs = _cron.CronJobStore().jobs()
        assert jobs[0]["command"]["shell"] == "echo hi"

    def test_list_empty_human(self, home, capsys):
        from camc_pkg import cli
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(argparse.Namespace(json_out=False))
        assert ei.value.code == 0
        captured = capsys.readouterr()
        assert "No cron jobs" in captured.out

    def test_list_empty_json(self, home, capsys):
        from camc_pkg import cli
        with pytest.raises(SystemExit):
            cli.cmd_cron_list(argparse.Namespace(json_out=True))
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == {"count": 0, "jobs": []}

    def test_list_json_includes_fields(self, home, capsys):
        from camc_pkg import cli
        _cron.CronJobStore().add(_cron.build_job(
            "j", _cron.parse_every("30m"), _make_argv_cmd()))
        with pytest.raises(SystemExit):
            cli.cmd_cron_list(argparse.Namespace(json_out=True))
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["count"] == 1
        job = payload["jobs"][0]
        assert set(["id", "name", "enabled", "kind", "schedule", "host",
                    "expires_at", "max_attempts", "attempts",
                    "last_status", "last_due_at",
                    "last_run_id"]).issubset(set(job.keys()))

    def test_list_fails_closed_on_corrupt_job_file(self, home, capsys):
        from camc_pkg import cli
        bad = os.path.join(_cron.CRON_JOBS_DIR, "bad.json")
        os.makedirs(_cron.CRON_JOBS_DIR)
        with open(bad, "w") as f:
            f.write("not json")
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(argparse.Namespace(json_out=False))
        assert ei.value.code == 1

    def test_rm_archives_and_removes_block_when_last(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        with pytest.raises(SystemExit):
            cli.cmd_cron_add(self._add(name="bye"))
        assert _cron.CRON_BEGIN in ct.current
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_rm(argparse.Namespace(id_or_name="bye"))
        assert ei.value.code == 0
        assert _cron.CronJobStore().jobs() == []
        assert _cron.CRON_BEGIN not in ct.current
        assert os.listdir(_cron.CRON_ARCHIVE_DIR)
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "job_removed" for e in events)

    def test_rm_removes_block_when_only_other_host_jobs_remain(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_hostname", lambda: "thishost")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        store = _cron.CronJobStore()
        mine = _cron.build_job("mine", _cron.parse_every("30m"),
                               _make_argv_cmd(), host="thishost")
        other = _cron.build_job("other", _cron.parse_every("30m"),
                                _make_argv_cmd(), host="otherhost")
        store.add(mine)
        store.add(other)
        _cron.install_tick()
        assert _cron.CRON_BEGIN in ct.current
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_rm(argparse.Namespace(id_or_name="mine"))
        assert ei.value.code == 0
        remaining = _cron.CronJobStore().jobs()
        assert [j["name"] for j in remaining] == ["other"]
        assert _cron.CRON_BEGIN not in ct.current


# ---------------------------------------------------------------------------
# Standalone smoke tests (built dist/camc)
# ---------------------------------------------------------------------------

class TestStandaloneSmoke:
    def test_standalone_camc_cron_list_smoke(self, tmp_path):
        camc = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "src", "camc")
        if not os.path.isfile(camc):
            pytest.skip("standalone src/camc not built")
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        r = subprocess.run(
            [camc, "cron", "list", "--json"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, env=env,
            timeout=15)
        assert r.returncode == 0, (
            "standalone `cron list --json` failed:\nstdout=%s\nstderr=%s"
            % (r.stdout, r.stderr))
        payload = json.loads(r.stdout)
        assert payload == {"count": 0, "jobs": []}

    def test_standalone_camc_cron_run_help(self, tmp_path):
        camc = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "src", "camc")
        if not os.path.isfile(camc):
            pytest.skip("standalone src/camc not built")
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        # Missing run_id should fail clearly
        r = subprocess.run(
            [camc, "cron", "run"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, env=env,
            timeout=15)
        # argparse will exit 2 for missing positional
        assert r.returncode != 0
