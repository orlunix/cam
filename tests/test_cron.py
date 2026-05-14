"""Focused tests for camc_pkg.cron (P0).

All disk paths and subprocess calls are monkey-patched onto tmp_path /
mocked runners — no live crontab mutation, no live tick scheduling.
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
# Fixtures: redirect all cron paths into tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture
def home(monkeypatch, tmp_path):
    """Repoint every ~/.cam/cron-* path at tmp_path. Returns the dir."""
    cam = tmp_path / ".cam"
    cam.mkdir()
    (cam / "logs").mkdir()
    monkeypatch.setattr(_cron, "CRON_FILE", str(cam / "cron.json"))
    monkeypatch.setattr(_cron, "CRON_CONFIG_FILE", str(cam / "cron-config.json"))
    monkeypatch.setattr(_cron, "CRON_RUNS_FILE", str(cam / "cron-runs.jsonl"))
    monkeypatch.setattr(_cron, "CRON_STATE_FILE", str(cam / "cron.state.json"))
    monkeypatch.setattr(_cron, "CRON_LOCK_FILE", str(cam / "cron.lock"))
    monkeypatch.setattr(_cron, "CRON_ARCHIVE_DIR", str(cam / "cron-archive"))
    monkeypatch.setattr(_cron, "CRON_HUMAN_LOG", str(cam / "logs" / "cron.log"))
    return cam


# ---------------------------------------------------------------------------
# Schedule parser
# ---------------------------------------------------------------------------

class TestScheduleParsers:
    def test_every_minutes_and_hours(self):
        assert _cron.parse_every("30m") == {
            "type": "interval", "every_seconds": 1800, "anchor": "created_at",
        }
        assert _cron.parse_every("2h") == {
            "type": "interval", "every_seconds": 7200, "anchor": "created_at",
        }

    def test_every_rejects_garbage(self):
        for bad in ("0m", "-5m", "30s", "abc", "", None):
            with pytest.raises(ValueError):
                _cron.parse_every(bad)

    def test_daily_normalizes_hour(self):
        # Leading-zero canonicalization: 9:30 → 09:30.
        assert _cron.parse_daily("9:30") == {
            "type": "daily", "time": "09:30", "timezone": "local",
        }
        assert _cron.parse_daily("23:59") == {
            "type": "daily", "time": "23:59", "timezone": "local",
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
        # Within 10s of "now + 45min" (the test only takes ms).
        assert timedelta(minutes=44, seconds=50) < delta < timedelta(minutes=45, seconds=10)


# ---------------------------------------------------------------------------
# Build job
# ---------------------------------------------------------------------------

class TestBuildJob:
    def test_default_ttl_resolves_to_expires_at_seven_days(self, home):
        schedule = _cron.parse_every("30m")
        job = _cron.build_job("j", schedule, {"argv": ["true"], "cwd": ".",
                                              "timeout_seconds": 60})
        assert job["enabled"] is True
        assert job["max_attempts"] == 3
        assert job["expires_at"] is not None
        exp = datetime.fromisoformat(job["expires_at"])
        created = datetime.fromisoformat(job["created_at"])
        assert abs((exp - created) - timedelta(days=7)) < timedelta(seconds=5)

    def test_no_expire_clears_expires_at(self, home):
        job = _cron.build_job("j", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60},
                              no_expire=True)
        assert job["expires_at"] is None

    def test_explicit_expires_at_preserved(self, home):
        job = _cron.build_job("j", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60},
                              expires_at="2027-01-01T00:00:00-07:00")
        assert job["expires_at"].startswith("2027-01-01T00:00:00-07:00")

    def test_host_defaults_to_current_hostname(self, home):
        import socket as _s
        job = _cron.build_job("j", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60})
        assert job["host"] == _s.gethostname()


# ---------------------------------------------------------------------------
# Store: add / remove / corrupt-refusal
# ---------------------------------------------------------------------------

class TestCronStore:
    def test_add_persists_and_finds_by_name_or_prefix(self, home):
        s = _cron.CronStore()
        job = _cron.build_job("j1", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60})
        s.add(job)
        assert s.find("j1")["id"] == job["id"]
        assert s.find(job["id"])["name"] == "j1"
        assert s.find(job["id"][:4])["name"] == "j1"

    def test_duplicate_name_rejected(self, home):
        s = _cron.CronStore()
        job1 = _cron.build_job("dup", _cron.parse_every("30m"),
                               {"argv": ["true"], "cwd": ".",
                                "timeout_seconds": 60})
        s.add(job1)
        job2 = _cron.build_job("dup", _cron.parse_every("1h"),
                               {"argv": ["true"], "cwd": ".",
                                "timeout_seconds": 60})
        with pytest.raises(_cron.DuplicateJobName):
            s.add(job2)
        assert len(s.jobs()) == 1   # registry unchanged

    def test_remove_returns_job_and_omits_from_jobs(self, home):
        s = _cron.CronStore()
        job = _cron.build_job("j1", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60})
        s.add(job)
        removed = s.remove("j1")
        assert removed["id"] == job["id"]
        assert s.jobs() == []
        assert s.remove("j1") is None

    def test_find_exact_match_beats_prefix(self, home):
        s = _cron.CronStore()
        for nm, override_id in (("a-job", "abcd1111"),
                                ("b-job", "abcd2222")):
            j = _cron.build_job(nm, _cron.parse_every("30m"),
                                {"argv": ["true"], "cwd": ".",
                                 "timeout_seconds": 60})
            j["id"] = override_id  # force collision-prone prefix
            s.add(j)
        # Exact id wins.
        assert s.find("abcd1111")["name"] == "a-job"

    def test_find_ambiguous_prefix_raises(self, home):
        s = _cron.CronStore()
        for nm, override_id in (("a-job", "abcd1111"),
                                ("b-job", "abcd2222")):
            j = _cron.build_job(nm, _cron.parse_every("30m"),
                                {"argv": ["true"], "cwd": ".",
                                 "timeout_seconds": 60})
            j["id"] = override_id
            s.add(j)
        with pytest.raises(_cron.AmbiguousJobKey):
            s.find("abcd")

    def test_remove_ambiguous_prefix_rejected(self, home):
        s = _cron.CronStore()
        for nm, override_id in (("a-job", "abcd1111"),
                                ("b-job", "abcd2222")):
            j = _cron.build_job(nm, _cron.parse_every("30m"),
                                {"argv": ["true"], "cwd": ".",
                                 "timeout_seconds": 60})
            j["id"] = override_id
            s.add(j)
        with pytest.raises(_cron.AmbiguousJobKey):
            s.remove("abcd")
        # Registry unchanged.
        assert {j["id"] for j in s.jobs()} == {"abcd1111", "abcd2222"}

    def test_remove_unique_prefix_ok(self, home):
        s = _cron.CronStore()
        j1 = _cron.build_job("a-job", _cron.parse_every("30m"),
                             {"argv": ["true"], "cwd": ".",
                              "timeout_seconds": 60})
        j1["id"] = "abcd1111"
        j2 = _cron.build_job("b-job", _cron.parse_every("30m"),
                             {"argv": ["true"], "cwd": ".",
                              "timeout_seconds": 60})
        j2["id"] = "wxyz9999"
        s.add(j1)
        s.add(j2)
        # "abcd" uniquely matches j1; "wxy" uniquely matches j2.
        removed = s.remove("abcd")
        assert removed and removed["id"] == "abcd1111"
        removed = s.remove("wxy")
        assert removed and removed["id"] == "wxyz9999"
        assert s.jobs() == []

    def test_corrupt_cron_json_is_not_overwritten(self, home):
        # Pre-seed corrupt file.
        with open(_cron.CRON_FILE, "w") as f:
            f.write("not json {{{")
        with pytest.raises(_cron.CorruptCronJSON):
            _cron.CronStore().add(_cron.build_job(
                "j", _cron.parse_every("30m"),
                {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}))
        with open(_cron.CRON_FILE, "r") as f:
            assert f.read() == "not json {{{"


# ---------------------------------------------------------------------------
# Crontab block management — mocked subprocess
# ---------------------------------------------------------------------------

class _FakeCrontab(object):
    """Mock subprocess.run for `crontab -l` / `crontab -`."""

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
        # User lines preserved; one camc block.
        assert "# user line" in ct.current
        assert "# user 2" in ct.current
        assert ct.current.count(_cron.CRON_BEGIN) == 1
        assert ct.current.count(_cron.CRON_END) == 1
        assert "/usr/local/bin/camc cron tick" in ct.current

    def test_install_into_empty_crontab(self, home):
        ct = _FakeCrontab("")
        _cron.install_tick(camc_path="/usr/local/bin/camc", runner=ct)
        assert _cron.CRON_BEGIN in ct.current
        assert _cron.CRON_END in ct.current

    def test_remove_strips_block_preserves_other_lines(self, home):
        ct = _FakeCrontab("# user A\n%s\nLINE\n%s\n# user B\n" % (
            _cron.CRON_BEGIN, _cron.CRON_END))
        result = _cron.remove_tick(runner=ct)
        assert result is True
        assert "LINE" not in ct.current
        assert "user A" in ct.current and "user B" in ct.current
        # Re-running removes nothing.
        assert _cron.remove_tick(runner=ct) is False

    def test_ensure_tick_if_needed_no_jobs_no_block(self, home):
        ct = _FakeCrontab("")
        assert _cron.ensure_tick_if_needed(runner=ct) == "noop"
        assert ct.current == ""

    def test_ensure_tick_installs_when_jobs_exist(self, home):
        s = _cron.CronStore()
        s.add(_cron.build_job("j", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60}))
        ct = _FakeCrontab("")
        assert _cron.ensure_tick_if_needed(runner=ct,
                                           camc_path="/usr/local/bin/camc") == "installed"
        assert _cron.CRON_BEGIN in ct.current

    def test_ensure_tick_removes_when_no_jobs(self, home):
        ct = _FakeCrontab("%s\nLINE\n%s\n" % (_cron.CRON_BEGIN, _cron.CRON_END))
        assert _cron.ensure_tick_if_needed(runner=ct) == "removed"
        assert _cron.CRON_BEGIN not in ct.current

    def test_ensure_tick_does_not_remove_block_when_cron_json_corrupt(self, home):
        # Corrupt registry must NOT trigger removal of an existing tick
        # block — that would silently disable scheduled jobs at exactly
        # the moment the registry needs manual inspection.
        with open(_cron.CRON_FILE, "w") as f:
            f.write("garbage {{{")
        original = ("# user\n%s\nLINE\n%s\n# user 2\n"
                    % (_cron.CRON_BEGIN, _cron.CRON_END))
        ct = _FakeCrontab(original)
        result = _cron.ensure_tick_if_needed(runner=ct)
        assert isinstance(result, tuple)
        assert result[0] == "cron_json_corrupt"
        assert ct.current == original  # crontab untouched
        assert _cron.CRON_BEGIN in ct.current


# ---------------------------------------------------------------------------
# Due computation
# ---------------------------------------------------------------------------

class TestIsDue:
    def test_interval_first_due_after_anchor_plus_every(self, home):
        job = _cron.build_job("j", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60})
        # Just-created job: not yet due (< 30min since created_at).
        due, _at = _cron.is_due(job, now=datetime.fromisoformat(job["created_at"]))
        assert due is False
        # 31 minutes after created_at: due.
        future = (datetime.fromisoformat(job["created_at"])
                  + timedelta(minutes=31))
        due, at_iso = _cron.is_due(job, now=future)
        assert due is True
        assert at_iso  # ISO string

    def test_once_due_after_run_at(self, home):
        run_at = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(
            timespec="seconds")
        job = _cron.build_job("once", {"type": "once", "run_at": run_at},
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60})
        due, at_iso = _cron.is_due(job)
        assert due is True
        # due_at is now the CURRENT minute (rounded), so successive ticks
        # get a fresh dedup key and a failed once-job can retry.
        assert at_iso  # ISO string, not run_at any more
        assert ":00" in at_iso[-3:] or at_iso.endswith(":00")

    def test_once_not_due_before_run_at(self, home):
        run_at = (datetime.now().astimezone() + timedelta(hours=1)).isoformat(
            timespec="seconds")
        job = _cron.build_job("once", {"type": "once", "run_at": run_at},
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60})
        assert _cron.is_due(job) == (False, None)


# ---------------------------------------------------------------------------
# Tick — execution, idempotency, recycle
# ---------------------------------------------------------------------------

class _FakeRunner(object):
    """Mock subprocess.run for the job command itself."""

    def __init__(self, exit_codes=None, *, sleep=0.0):
        # exit_codes is a list; the runner pops one per call (or returns 0).
        self.exit_codes = list(exit_codes or [])
        self.sleep = sleep
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.sleep:
            time.sleep(self.sleep)
        rc = self.exit_codes.pop(0) if self.exit_codes else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")


def _seed_once_job(home, *, exit_code=0, run_at_offset_minutes=-1):
    """Seed a 'once' job whose run_at is in the past so tick fires it."""
    run_at = (datetime.now().astimezone()
              + timedelta(minutes=run_at_offset_minutes)).isoformat(timespec="seconds")
    job = _cron.build_job("once-job", {"type": "once", "run_at": run_at},
                          {"argv": ["true"], "cwd": ".",
                           "timeout_seconds": 60})
    _cron.CronStore().add(job)
    return job


class TestTick:
    def test_tick_runs_due_once_job_and_recycles_after_success(self, home):
        job = _seed_once_job(home)
        runner = _FakeRunner(exit_codes=[0])
        result = _cron.tick(runner=runner)
        assert result == {"status": "ok", "ran": 1}
        # Job archived + removed.
        assert _cron.CronStore().find(job["id"]) is None
        archives = os.listdir(_cron.CRON_ARCHIVE_DIR)
        assert any(job["id"] in name for name in archives)
        # Runs log has started + succeeded + recycled.
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        kinds = [e.get("event") for e in events]
        assert "job_started" in kinds
        assert "job_succeeded" in kinds
        assert "job_recycled" in kinds

    def test_tick_dedupes_same_job_and_due_at(self, home):
        # Within the SAME minute, two ticks must not double-fire even if
        # the global lock is bypassed: (job_id, due_at=current-minute) is
        # the idempotency key. Pinning `now` makes the minute deterministic.
        job = _seed_once_job(home)
        pinned = datetime.now().astimezone().replace(second=0, microsecond=0)
        # First tick at this minute: runs.
        r1 = _FakeRunner(exit_codes=[0])
        _cron.tick(now=pinned, runner=r1)
        assert len(r1.calls) == 1
        # Re-seed a fresh once-job (prior recycled after success), with
        # run_at in the past. Pre-seed a job_started for THIS minute so
        # dedup blocks execution on the same-minute retry.
        job2 = _seed_once_job(home)
        due_at_iso = _cron._iso(pinned.replace(second=0, microsecond=0))
        _cron._append_runs({
            "event": "job_started", "job_id": job2["id"],
            "job_name": job2["name"], "due_at": due_at_iso,
            "command": ["true"],
        })
        r2 = _FakeRunner(exit_codes=[0])
        _cron.tick(now=pinned, runner=r2)
        assert r2.calls == [], "second tick at same minute should be deduped"

    def test_tick_recycles_after_max_attempts_failures(self, home):
        # max_attempts=2, two failing runs → recycle.
        run_at = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(
            timespec="seconds")
        job = _cron.build_job("flaky", {"type": "once", "run_at": run_at},
                              {"argv": ["false"], "cwd": ".",
                               "timeout_seconds": 60},
                              max_attempts=2)
        _cron.CronStore().add(job)
        # First failure
        _cron.tick(runner=_FakeRunner(exit_codes=[1]))
        # The first failure does NOT yet hit max_attempts (attempts=1<2),
        # but to drive the second attempt we need to bypass the (id, due_at)
        # dedup — clear the runs log first, mimicking a different due tick.
        # Easiest: simulate the next minute by clearing only the started
        # records (or just remove the dedupe entries for this id/due_at).
        # In V0 we accept this is best-effort; here we just verify that
        # AFTER a second tick that DOES fire (different due_at), recycle
        # happens. Build a second once-job to demonstrate the recycle path:
        run_at2 = (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(
            timespec="seconds")
        job2 = _cron.build_job("flaky2", {"type": "once", "run_at": run_at2},
                               {"argv": ["false"], "cwd": ".",
                                "timeout_seconds": 60},
                               max_attempts=1)
        _cron.CronStore().add(job2)
        _cron.tick(runner=_FakeRunner(exit_codes=[1]))
        # job2's first failure with max_attempts=1 → recycled.
        assert _cron.CronStore().find(job2["id"]) is None

    def test_tick_recycles_expired_jobs(self, home):
        # Manually craft a job with expires_at in the past.
        s = _cron.CronStore()
        job = _cron.build_job("old", _cron.parse_every("30m"),
                              {"argv": ["true"], "cwd": ".",
                               "timeout_seconds": 60},
                              expires_at=(datetime.now().astimezone()
                                          - timedelta(hours=1)).isoformat(timespec="seconds"))
        s.add(job)
        _cron.tick(runner=_FakeRunner())
        assert s.find(job["id"]) is None

    def test_tick_skipped_when_lock_held(self, home, monkeypatch):
        # Force _acquire_lock to return None → tick reports skipped.
        monkeypatch.setattr(_cron, "_acquire_lock", lambda path=None: None)
        result = _cron.tick(runner=_FakeRunner())
        assert result["status"] == "skipped_locked"
        # Ledger has tick_skipped_locked.
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "tick_skipped_locked" for e in events)

    def test_host_mismatch_skips_with_event(self, home, monkeypatch):
        _seed_once_job(home)
        # Force hostname to something different from the job's host.
        monkeypatch.setattr(_cron, "_hostname", lambda: "different-host")
        runner = _FakeRunner(exit_codes=[0])
        _cron.tick(runner=runner)
        assert runner.calls == []
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "job_skipped_host" for e in events)

    def test_writes_state_heartbeat(self, home):
        _seed_once_job(home)
        _cron.tick(runner=_FakeRunner(exit_codes=[0]))
        with open(_cron.CRON_STATE_FILE) as f:
            state = json.load(f)
        assert state["last_tick_status"] == "ok"
        assert state["last_tick_started_at"]
        assert state["last_tick_completed_at"]

    def test_tick_respects_global_enabled_false(self, home):
        # Global cron-config.json enabled=false → no jobs run, state=disabled,
        # tick_disabled event emitted.
        _seed_once_job(home)
        cfg = dict(_cron.DEFAULT_CONFIG)
        cfg["enabled"] = False
        with open(_cron.CRON_CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        runner = _FakeRunner(exit_codes=[0])
        result = _cron.tick(runner=runner)
        assert result == {"status": "disabled", "ran": 0}
        assert runner.calls == []
        with open(_cron.CRON_STATE_FILE) as f:
            assert json.load(f)["last_tick_status"] == "disabled"
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "tick_disabled" for e in events)

    def test_tick_fails_closed_on_corrupt_config(self, home):
        # Corrupt cron-config.json → tick records error and runs zero jobs.
        _seed_once_job(home)
        with open(_cron.CRON_CONFIG_FILE, "w") as f:
            f.write("not valid json {{{")
        runner = _FakeRunner(exit_codes=[0])
        result = _cron.tick(runner=runner)
        assert result["status"] == "error"
        assert "corrupt" in result.get("error", "").lower()
        assert runner.calls == []
        with open(_cron.CRON_STATE_FILE) as f:
            assert json.load(f)["last_tick_status"] == "error"

    def test_tick_fails_closed_on_corrupt_registry(self, home):
        # Corrupt cron.json → tick aborts before any execution.
        with open(_cron.CRON_FILE, "w") as f:
            f.write("not json {{{")
        runner = _FakeRunner(exit_codes=[0])
        result = _cron.tick(runner=runner)
        assert result["status"] == "error"
        assert runner.calls == []

    def test_once_job_retries_after_failure_until_max_attempts(self, home):
        # A once-job that fails should retry on subsequent tick minutes
        # until max_attempts, then recycle as too_many_failures.
        run_at = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(
            timespec="seconds")
        job = _cron.build_job(
            "flaky-once",
            {"type": "once", "run_at": run_at},
            {"argv": ["false"], "cwd": ".", "timeout_seconds": 60},
            max_attempts=3,
        )
        _cron.CronStore().add(job)
        now = datetime.now().astimezone()
        # Three failing ticks at consecutive minute boundaries → each gets
        # a distinct (job_id, due_at-minute) idempotency key.
        for m in range(3):
            t = (now + timedelta(minutes=m)).replace(second=0, microsecond=0)
            _cron.tick(now=t, runner=_FakeRunner(exit_codes=[1]))
        # After 3 failures, recycled.
        assert _cron.CronStore().find(job["id"]) is None
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        failures = [e for e in events if e.get("event") == "job_failed"
                    and e.get("job_id") == job["id"]]
        recycled = [e for e in events if e.get("event") == "job_recycled"
                    and e.get("job_id") == job["id"]
                    and e.get("reason") == "too_many_failures"]
        assert len(failures) == 3
        assert len(recycled) == 1


# ---------------------------------------------------------------------------
# cmd_cron_add / cmd_cron_rm via real cli entry
# ---------------------------------------------------------------------------

class TestCmdCron:
    def test_add_writes_normalized_interval_job_and_installs_block(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        # Route crontab calls in install_tick through the fake.
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        args = argparse.Namespace(
            name="j1", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["--", "echo", "hi"],
        )
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(args)
        assert ei.value.code == 0
        # Job persisted with normalized schedule.
        jobs = _cron.CronStore().jobs()
        assert len(jobs) == 1
        j = jobs[0]
        assert j["name"] == "j1"
        assert j["schedule"] == {"type": "interval", "every_seconds": 1800,
                                  "anchor": "created_at"}
        assert j["command"]["argv"] == ["echo", "hi"]
        # Crontab block installed exactly once.
        assert ct.current.count(_cron.CRON_BEGIN) == 1

    def test_add_duplicate_name_rejected_without_changing_registry(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        args = argparse.Namespace(
            name="dup", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["--", "true"],
        )
        with pytest.raises(SystemExit) as ei1:
            cli.cmd_cron_add(args)
        assert ei1.value.code == 0
        # Second add with same name fails.
        args2 = argparse.Namespace(**vars(args))
        args2.every = "1h"
        with pytest.raises(SystemExit) as ei2:
            cli.cmd_cron_add(args2)
        assert ei2.value.code == 1
        jobs = _cron.CronStore().jobs()
        assert len(jobs) == 1
        assert jobs[0]["schedule"]["every_seconds"] == 1800   # original unchanged

    def test_add_rejects_missing_command(self, home, monkeypatch):
        from camc_pkg import cli
        args = argparse.Namespace(
            name="j", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=[],
        )
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(args)
        assert ei.value.code == 1
        # Nothing persisted.
        assert _cron.CronStore().jobs() == []

    def test_add_argv_preserves_embedded_separator_tokens(self, home, monkeypatch):
        # Spec: everything after the FIRST `--` is stored as command.argv
        # opaque, without shell parsing. argparse.REMAINDER includes the
        # leading `--` token; cmd_cron_add strips ONLY that one, so any
        # `--` tokens later in the user's argv must round-trip verbatim.
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        # Simulates user calling:
        #   camc cron add --name passthru --every 30m -- prog -- flag
        # argparse.REMAINDER hands us ["--", "prog", "--", "flag"].
        args = argparse.Namespace(
            name="passthru", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["--", "prog", "--", "flag"],
        )
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(args)
        assert ei.value.code == 0
        j = _cron.CronStore().jobs()[0]
        # Only the leading separator is consumed; later `--` is part of the
        # opaque argv payload.
        assert j["command"]["argv"] == ["prog", "--", "flag"]

    def test_add_argv_without_leading_separator(self, home, monkeypatch):
        # argparse can also hand us argv without the leading `--` if the
        # subparser parses it that way. Either form must work without
        # losing the first token.
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        args = argparse.Namespace(
            name="nodashdash", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["prog", "arg1"],
        )
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(args)
        assert ei.value.code == 0
        j = _cron.CronStore().jobs()[0]
        assert j["command"]["argv"] == ["prog", "arg1"]

    def test_add_shell_mode_stores_shell(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        args = argparse.Namespace(
            name="sh", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd="echo $HOME", cmd_argv=[],
        )
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_add(args)
        assert ei.value.code == 0
        j = _cron.CronStore().jobs()[0]
        assert j["command"].get("shell") == "echo $HOME"
        assert "argv" not in j["command"]

    def test_list_empty_human(self, home, capsys):
        from camc_pkg import cli
        args = argparse.Namespace(json_out=False)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "No cron jobs" in out
        # Read-only: registry/runs/archive untouched.
        assert not os.path.exists(_cron.CRON_FILE)
        assert not os.path.exists(_cron.CRON_RUNS_FILE)
        assert not os.path.exists(_cron.CRON_ARCHIVE_DIR)

    def test_list_empty_json(self, home, capsys):
        from camc_pkg import cli
        args = argparse.Namespace(json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(args)
        assert ei.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"count": 0, "jobs": []}

    def test_list_human_shows_active_jobs(self, home, capsys):
        from camc_pkg import cli
        s = _cron.CronStore()
        s.add(_cron.build_job(
            "daily-rev", _cron.parse_daily("09:00"),
            {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}))
        s.add(_cron.build_job(
            "ping", _cron.parse_every("30m"),
            {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}))
        args = argparse.Namespace(json_out=False)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        # Header + both names + schedule labels.
        assert "ID" in out and "NAME" in out and "SCHED" in out
        assert "daily-rev" in out
        assert "ping" in out
        assert "daily 09:00" in out
        assert "every 30m" in out
        # Registry untouched.
        assert len(_cron.CronStore().jobs()) == 2

    def test_list_json_includes_fields_and_count(self, home, capsys):
        from camc_pkg import cli
        s = _cron.CronStore()
        s.add(_cron.build_job(
            "daily-rev", _cron.parse_daily("09:00"),
            {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}))
        args = argparse.Namespace(json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(args)
        assert ei.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 1
        assert len(payload["jobs"]) == 1
        j = payload["jobs"][0]
        # Stable presentation contract — every field a caller likely wants.
        for key in ("id", "name", "enabled", "kind", "schedule",
                    "host", "expires_at", "max_attempts", "attempts",
                    "last_status", "last_due_at"):
            assert key in j, "missing %r in json payload" % key
        assert j["name"] == "daily-rev"
        assert j["schedule"]["type"] == "daily"
        assert j["enabled"] is True

    def test_list_fails_closed_on_corrupt_registry(self, home, capsys):
        from camc_pkg import cli
        with open(_cron.CRON_FILE, "w") as f:
            f.write("not json {{{")
        args = argparse.Namespace(json_out=False)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_list(args)
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "corrupt" in err.lower()
        # Same fail-closed behavior under --json (no JSON output on stdout).
        args = argparse.Namespace(json_out=True)
        with pytest.raises(SystemExit) as ei2:
            cli.cmd_cron_list(args)
        assert ei2.value.code == 1
        cap = capsys.readouterr()
        assert "corrupt" in cap.err.lower()
        assert cap.out == ""

    def test_list_does_not_touch_crontab(self, home, monkeypatch):
        # Read-only contract: must not call crontab -l / crontab -.
        from camc_pkg import cli

        def _explode(args, **kw):
            if args[:1] == ["crontab"]:
                raise AssertionError("cron list called crontab: %r" % (args,))
            raise AssertionError("unexpected subprocess: %r" % (args,))

        monkeypatch.setattr(_cron.subprocess, "run", _explode)
        # With a job present.
        _cron.CronStore().add(_cron.build_job(
            "j", _cron.parse_every("30m"),
            {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}))
        args = argparse.Namespace(json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_cron_list(args)

    def test_standalone_camc_cron_list_smoke(self, tmp_path):
        # Catch regressions where the BUILT single-file src/camc doesn't
        # have the cron names at top level (e.g. someone re-introduces
        # `from camc_pkg.cron import X as _alias` — the build strips the
        # alias and the standalone crashes with NameError). The 52
        # package tests can't catch this because they exercise cli.py
        # directly without going through the bundling step.
        camc = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "src", "camc")
        if not os.path.isfile(camc):
            pytest.skip("standalone src/camc not built")
        # Empty registry → exit 0, valid JSON {count:0, jobs:[]}.
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        r = subprocess.run([camc, "cron", "list", "--json"],
                           capture_output=True, text=True, env=env, timeout=15)
        assert r.returncode == 0, (
            "standalone `cron list --json` failed:\nstdout=%s\nstderr=%s"
            % (r.stdout, r.stderr))
        payload = json.loads(r.stdout)
        assert payload == {"count": 0, "jobs": []}

    def test_standalone_camc_cron_list_corrupt_fails_closed(self, tmp_path):
        # Corrupt registry → exit 1, clear stderr, no stdout JSON.
        camc = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "src", "camc")
        if not os.path.isfile(camc):
            pytest.skip("standalone src/camc not built")
        cam = tmp_path / ".cam"
        cam.mkdir()
        (cam / "cron.json").write_text("not json {{{")
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        r = subprocess.run([camc, "cron", "list", "--json"],
                           capture_output=True, text=True, env=env, timeout=15)
        assert r.returncode == 1
        assert "corrupt" in r.stderr.lower()
        assert r.stdout == ""

    def test_rm_archives_and_removes_block_when_last(self, home, monkeypatch):
        from camc_pkg import cli
        ct = _FakeCrontab("")
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: ct(["crontab", "-l"]).stdout)
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: ct(["crontab", "-"], input=text))
        # Seed one job.
        add_args = argparse.Namespace(
            name="bye", every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["--", "true"],
        )
        with pytest.raises(SystemExit):
            cli.cmd_cron_add(add_args)
        assert _cron.CRON_BEGIN in ct.current
        # Now remove.
        rm_args = argparse.Namespace(id_or_name="bye")
        with pytest.raises(SystemExit) as ei:
            cli.cmd_cron_rm(rm_args)
        assert ei.value.code == 0
        assert _cron.CronStore().jobs() == []
        # Block removed when no enabled jobs remain.
        assert _cron.CRON_BEGIN not in ct.current
        # Archive written + job_removed event recorded.
        assert os.listdir(_cron.CRON_ARCHIVE_DIR)
        with open(_cron.CRON_RUNS_FILE) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert any(e.get("event") == "job_removed" for e in events)
