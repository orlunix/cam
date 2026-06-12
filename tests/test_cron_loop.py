"""Focused tests for `camc cron --loop` (CAM-DESK-CRON-010).

Covers:
  * LoopStore round-trip (add / find / remove / archive).
  * build_loop schema (matches docs/camc-agent-loop-spec.md).
  * Parser/validation: required flags, mutual exclusions, owner resolve.
  * `cron list --loop --json` envelope shape Desktop parses
    (loopParsed.loops).
  * tick_loops() dispatches due loops via injected dispatch fn AND
    advances next_due_at AND records run events — without breaking
    normal cron jobs.
  * cron tick services BOTH normal cron jobs and due loops.
"""

import argparse
import json
import os
from datetime import timedelta

import pytest

from camc_pkg import cron as _cron
from camc_pkg import cron_loop as _loop
from camc_pkg import cli as _cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def home(monkeypatch, tmp_path):
    """Redirect every cron/loop disk path into tmp_path."""
    cam = tmp_path / ".cam"
    cron_dir = cam / "cron"
    loops_dir = cam / "loops"
    cron_dir.mkdir(parents=True)
    loops_dir.mkdir(parents=True)
    # cron paths
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
    # loop paths
    monkeypatch.setattr(_loop, "LOOPS_DIR", str(loops_dir))
    return tmp_path


def _owner_rec(agent_id="f1a1a661", name="cam-dev"):
    return {
        "id": agent_id,
        "task": {"name": name, "tool": "claude"},
        "tmux_session": "cam-" + agent_id,
        "status": "running",
        "state": "idle",
    }


class _FakeStore(object):
    def __init__(self, recs):
        self._by_key = {}
        for r in recs:
            self._by_key[r["id"]] = r
            name = r.get("task", {}).get("name") or r.get("name", "")
            if name:
                self._by_key[name] = r

    def get(self, k):
        return self._by_key.get(k)


# ---------------------------------------------------------------------------
# resolve_owner
# ---------------------------------------------------------------------------

class TestResolveOwner:
    def test_resolves_by_id(self):
        store = _FakeStore([_owner_rec()])
        rec = _loop.resolve_owner("f1a1a661", _store=store)
        assert rec["id"] == "f1a1a661"

    def test_resolves_by_name(self):
        store = _FakeStore([_owner_rec()])
        rec = _loop.resolve_owner("cam-dev", _store=store)
        assert rec["id"] == "f1a1a661"

    def test_missing_raises(self):
        with pytest.raises(_loop.OwnerNotFound):
            _loop.resolve_owner("nope", _store=_FakeStore([]))

    def test_empty_arg_raises(self):
        with pytest.raises(_loop.OwnerNotFound):
            _loop.resolve_owner("", _store=_FakeStore([]))


# ---------------------------------------------------------------------------
# build_loop schema
# ---------------------------------------------------------------------------

class TestBuildLoop:
    def test_schema_fields_present(self, home):
        loop = _loop.build_loop(
            "daily-status", _cron.parse_daily("09:00"),
            "/status\nSummarize blockers.", _owner_rec(),
        )
        assert loop["executor"] == "monitor"
        assert loop["owner"]["agent_id"] == "f1a1a661"
        assert loop["owner"]["agent_name"] == "cam-dev"
        assert loop["action"]["type"] == "prompt"
        assert loop["action"]["delivery"]["method"] == "camc-msg-send"
        assert loop["action"]["delivery"]["to"] == "f1a1a661"
        assert loop["action"]["delivery"]["no_wait"] is True
        assert loop["state"]["attempts"] == 0
        assert loop["state"]["last_msg_id"] is None
        assert loop["schedule"]["next_due_at"]  # populated by initial calc
        assert loop["policy"]["max_attempts"] == 3

    def test_max_attempts_override(self, home):
        loop = _loop.build_loop(
            "x", _cron.parse_every("30m"), "ping",
            _owner_rec(), max_attempts=7,
        )
        assert loop["policy"]["max_attempts"] == 7


# ---------------------------------------------------------------------------
# LoopStore CRUD
# ---------------------------------------------------------------------------

class TestLoopStore:
    def test_add_and_find(self, home):
        store = _loop.LoopStore("owner1", owner_name="o", tmux_session="cam-owner1")
        loop = _loop.build_loop(
            "hello", _cron.parse_every("30m"), "say hi",
            {"id": "owner1", "task": {"name": "o"}, "tmux_session": "cam-owner1"})
        store.add(loop)
        env = store.load()
        assert env["schema"] == _loop.LOOP_SCHEMA_NAME
        assert env["agent_id"] == "owner1"
        assert len(env["loops"]) == 1
        assert store.find("hello")["id"] == loop["id"]
        assert store.find(loop["id"])["name"] == "hello"

    def test_duplicate_name_rejected(self, home):
        store = _loop.LoopStore("ownerD")
        owner = {"id": "ownerD", "task": {"name": "ownerD"}}
        store.add(_loop.build_loop("same", _cron.parse_every("1h"), "x", owner))
        with pytest.raises(_loop.DuplicateLoopName):
            store.add(_loop.build_loop("same", _cron.parse_every("1h"), "y", owner))

    def test_remove_archives(self, home):
        store = _loop.LoopStore("ownerR")
        owner = {"id": "ownerR", "task": {"name": "ownerR"}}
        loop = _loop.build_loop("kill-me", _cron.parse_every("1h"), "p", owner)
        store.add(loop)
        removed = store.remove("kill-me")
        assert removed["id"] == loop["id"]
        assert store.load()["loops"] == []
        archive_path = store.archive(removed, "test")
        assert archive_path and os.path.exists(archive_path)

    def test_ambiguous_prefix_raises(self, home):
        store = _loop.LoopStore("ownerA")
        owner = {"id": "ownerA", "task": {"name": "ownerA"}}
        for i in (1, 2):
            L = _loop.build_loop("n%d" % i, _cron.parse_every("1h"), "p", owner)
            L["id"] = "abc1111%d" % i
            store.replace_envelope({
                "schema": _loop.LOOP_SCHEMA_NAME, "version": 1,
                "agent_id": "ownerA", "agent_name": "", "tmux_session": "",
                "updated_at": "x",
                "loops": store.load().get("loops", []) + [L],
            })
        with pytest.raises(_loop.AmbiguousLoopKey):
            store.find("abc")

    def test_corrupt_file_refused(self, home, monkeypatch):
        store = _loop.LoopStore("ownerC")
        owner_dir = os.path.join(_loop.LOOPS_DIR, "ownerC")
        os.makedirs(owner_dir, exist_ok=True)
        with open(os.path.join(owner_dir, _loop.LOOP_FILENAME), "w") as f:
            f.write("not json {{{")
        with pytest.raises(_loop.CorruptLoopFile):
            store.load()


# ---------------------------------------------------------------------------
# tick_loops dispatch
# ---------------------------------------------------------------------------

class TestTickLoops:
    def _seed_due(self, owner_id, home, name="due-now"):
        store = _loop.LoopStore(owner_id)
        loop = _loop.build_loop(
            name, _cron.parse_every("30m"), "ping",
            {"id": owner_id, "task": {"name": owner_id}})
        # Force due (push next_due_at into the past).
        loop["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=5))
        store.add(loop)
        return loop

    def test_due_loop_dispatched_and_advanced(self, home):
        loop = self._seed_due("ownerT", home)
        calls = []
        def fake_dispatch(L):
            calls.append(L["id"])
            return True, "msg12345"
        idle = _FakeStore([{"id": "ownerT", "status": "running",
                            "state": "idle"}])
        n = _loop.tick_loops(dispatch=fake_dispatch, agent_store=idle)
        assert n == 1
        assert calls == [loop["id"]]
        # next_due_at advanced past now.
        reloaded = _loop.LoopStore("ownerT").find(loop["id"])
        assert reloaded["state"]["last_msg_id"] == "msg12345"
        assert reloaded["state"]["last_status"] == "sent"
        nda = _cron._parse_iso(reloaded["schedule"]["next_due_at"])
        assert nda > _cron._now_local()
        # Runs log captured both events.
        runs_path = os.path.join(_loop.LOOPS_DIR, "ownerT", "runs.jsonl")
        with open(runs_path) as f:
            events = [json.loads(l) for l in f if l.strip()]
        evnames = [e["event"] for e in events]
        assert "loop_queued" in evnames
        assert "loop_dispatched" in evnames

    def test_failed_dispatch_recycles_after_max(self, home):
        owner = "ownerF"
        loop = self._seed_due(owner, home, name="failer")
        # Set max_attempts low so we hit the recycle threshold fast.
        store = _loop.LoopStore(owner)
        env = store.load()
        env["loops"][0]["policy"]["max_attempts"] = 2
        store.replace_envelope(env)
        # Re-set due each cycle so the loop is due both times.
        def cycle(t_offset):
            env = store.load()
            env["loops"][0]["schedule"]["next_due_at"] = _cron._iso(
                _cron._now_local() - timedelta(seconds=10))
            store.replace_envelope(env)
        def bad_dispatch(L):
            return False, "rpc error"
        idle = _FakeStore([{"id": owner, "status": "running",
                            "state": "idle"}])
        cycle(0); _loop.tick_loops(dispatch=bad_dispatch, agent_store=idle)
        cycle(1); _loop.tick_loops(dispatch=bad_dispatch, agent_store=idle)
        final = _loop.LoopStore(owner).find("failer")
        assert final["enabled"] is False  # recycled
        assert "recycled" in (final["state"]["last_status"] or "")

    def test_owner_busy_defers_dispatch(self, home):
        """F1: a due loop must NOT fire when the owner agent is not idle.
        Defer should emit a loop_deferred event and leave both
        next_due_at and attempts untouched."""
        loop = self._seed_due("ownerB", home, name="defer-me")
        nda_before = loop["schedule"]["next_due_at"]
        attempts_before = loop["state"]["attempts"]
        busy_rec = {"id": "ownerB", "status": "running", "state": "editing"}
        store = _FakeStore([busy_rec])
        calls = []
        def fake_dispatch(L):
            calls.append(L["id"])
            return True, "should_not_be_called"
        n = _loop.tick_loops(dispatch=fake_dispatch, agent_store=store)
        assert n == 0, "dispatch must not fire while owner is busy"
        assert calls == []
        reloaded = _loop.LoopStore("ownerB").find(loop["id"])
        # next_due_at preserved
        assert reloaded["schedule"]["next_due_at"] == nda_before
        # attempts preserved
        assert reloaded["state"]["attempts"] == attempts_before
        # loop_deferred event recorded
        runs_path = os.path.join(_loop.LOOPS_DIR, "ownerB", "runs.jsonl")
        with open(runs_path) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e["event"] == "loop_deferred"]
        assert len(deferred) == 1
        assert "owner_state_editing" in deferred[0]["reason"]

    def test_owner_missing_defers_dispatch(self, home):
        """F1: a due loop with no owner record must defer (not crash,
        not dispatch, not advance schedule)."""
        loop = self._seed_due("ownerM", home, name="ghost-owner")
        nda_before = loop["schedule"]["next_due_at"]
        store = _FakeStore([])  # no records at all
        calls = []
        def fake_dispatch(L):
            calls.append(L["id"])
            return True, "x"
        n = _loop.tick_loops(dispatch=fake_dispatch, agent_store=store)
        assert n == 0
        assert calls == []
        reloaded = _loop.LoopStore("ownerM").find(loop["id"])
        assert reloaded["schedule"]["next_due_at"] == nda_before
        runs_path = os.path.join(_loop.LOOPS_DIR, "ownerM", "runs.jsonl")
        with open(runs_path) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e["event"] == "loop_deferred"]
        assert len(deferred) == 1
        assert deferred[0]["reason"] == "owner_not_found"

    def test_undue_loop_not_dispatched(self, home):
        store = _loop.LoopStore("ownerU")
        loop = _loop.build_loop(
            "future", _cron.parse_every("30m"), "ping",
            {"id": "ownerU", "task": {"name": "u"}})
        loop["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() + timedelta(hours=1))
        store.add(loop)
        calls = []
        n = _loop.tick_loops(dispatch=lambda L: (calls.append(L), (True, "x"))[1])
        assert n == 0
        assert calls == []


# ---------------------------------------------------------------------------
# cron.tick() services both normal jobs AND loops
# ---------------------------------------------------------------------------

class TestCronTickServicesLoops:
    def test_normal_cron_and_loops_both_serviced(self, home, monkeypatch):
        # Seed a normal cron job (due now).
        from camc_pkg.cron import CronJobStore, build_job
        store = CronJobStore()
        store.add(build_job("normal-due", _cron.parse_every("30m"),
                            {"argv": ["true"], "cwd": ".", "timeout_seconds": 60}))
        # Force normal-job due
        j = store.list()[0] if hasattr(store, "list") else store.jobs()[0]
        j["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=1))
        store.save(j)
        # Seed a due loop.
        loop_store = _loop.LoopStore("ownerJ")
        loop = _loop.build_loop("loop-due", _cron.parse_every("30m"), "ping",
                                {"id": "ownerJ", "task": {"name": "j"}})
        loop["schedule"]["next_due_at"] = _cron._iso(
            _cron._now_local() - timedelta(minutes=1))
        loop_store.add(loop)
        # Inject dispatchers + idle-owner store so the loop-side guard passes.
        spawned_jobs = []
        loop_calls = []
        monkeypatch.setattr(_loop, "dispatch_loop",
                            lambda L: (loop_calls.append(L["id"]), (True, "msgX"))[1])
        idle = _FakeStore([{"id": "ownerJ", "status": "running",
                            "state": "idle"}])
        # Patch the default AgentStore the loop tick consults when no
        # agent_store kwarg is plumbed through cron.tick().
        import camc_pkg.storage as _storage
        monkeypatch.setattr(_storage, "AgentStore", lambda *a, **kw: idle)
        result = _cron.tick(spawn=lambda rid: spawned_jobs.append(rid))
        assert result["status"] == "ok"
        assert result["queued"] == 1, "normal cron job must queue"
        assert result["loops_dispatched"] == 1, "loop must dispatch"
        assert spawned_jobs, "host-cron worker spawn happened"
        assert loop_calls == [loop["id"]]


# ---------------------------------------------------------------------------
# CLI smoke — add/list/rm via cmd_cron_add/_list/_rm using a fake AgentStore
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_owner(monkeypatch):
    """Patch AgentStore so cmd_cron_add --loop can resolve --owner."""
    from camc_pkg import storage as _storage
    class _S:
        def __init__(self, *a, **kw): pass
        def get(self, k):
            if k in ("cam-dev", "f1a1a661"):
                return _owner_rec()
            return None
    monkeypatch.setattr(_storage, "AgentStore", _S)
    return _S


class TestCliLoop:
    def _add_args(self, **overrides):
        d = dict(
            name="loopA", loop=True, owner="cam-dev",
            prompt="hello world", prompt_file=None,
            every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=[],
        )
        d.update(overrides)
        return argparse.Namespace(**d)

    def test_add_loop_persists(self, home, cli_owner, monkeypatch, capsys):
        # Stub crontab so the install_tick call at end is a no-op success.
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: "")
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: None)
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(self._add_args())
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "added agent loop loopA" in out
        # File present at expected path.
        path = os.path.join(_loop.LOOPS_DIR, "f1a1a661", "agent.loop.json")
        assert os.path.exists(path)
        with open(path) as f:
            env = json.load(f)
        assert env["schema"] == _loop.LOOP_SCHEMA_NAME
        assert env["loops"][0]["name"] == "loopA"
        assert env["loops"][0]["action"]["text"] == "hello world"

    def test_add_loop_exits_nonzero_when_tick_install_fails(
            self, home, cli_owner, monkeypatch, capsys):
        """Regression for cam-review-d123 verify finding:
        loop add must exit 1 when install_tick raises so Desktop /
        CI cannot mistake a saved-but-uninstalled loop for a live
        one. The loop file itself stays on disk (parallel to host
        `cron add` semantics)."""
        # Make crontab unavailable -> install_tick raises.
        def _fail(*a, **kw):
            raise _cron.CrontabUnavailable("crontab intentionally unavailable")
        monkeypatch.setattr(_cron, "install_tick", _fail)
        monkeypatch.setattr(_cli, "install_tick", _fail)
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(self._add_args(name="failtick"))
        assert ei.value.code == 1
        captured = capsys.readouterr()
        assert "ERROR: failed to install system cron tick" in captured.err
        # Loop is still on disk so the user can recover via heal.
        path = os.path.join(_loop.LOOPS_DIR, "f1a1a661", "agent.loop.json")
        assert os.path.exists(path)
        with open(path) as f:
            env = json.load(f)
        assert any(L["name"] == "failtick" for L in env["loops"])

    def test_add_loop_rejects_argv(self, home, cli_owner, capsys):
        args = self._add_args(cmd_argv=["--", "echo", "hi"])
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 1
        assert "not allowed in loop mode" in capsys.readouterr().err

    def test_add_loop_rejects_shell(self, home, cli_owner, capsys):
        args = self._add_args(shell_cmd="echo hi")
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 1
        assert "not allowed in loop mode" in capsys.readouterr().err

    def test_add_loop_requires_prompt(self, home, cli_owner, capsys):
        args = self._add_args(prompt=None)
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 1
        assert "--prompt" in capsys.readouterr().err

    def test_add_loop_requires_owner(self, home, cli_owner, capsys):
        args = self._add_args(owner=None)
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 1
        assert "--owner" in capsys.readouterr().err

    def test_add_loop_unknown_owner(self, home, cli_owner, capsys):
        args = self._add_args(owner="ghost")
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 1
        assert "no agent matching" in capsys.readouterr().err

    def test_normal_cron_rejects_loop_flags(self, home, capsys):
        args = self._add_args(loop=False, owner="cam-dev",
                              prompt="ping", cmd_argv=["--", "true"])
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 1
        assert "require --loop" in capsys.readouterr().err

    def test_list_loop_json(self, home, cli_owner, monkeypatch, capsys):
        # Seed one loop directly through the LoopStore.
        store = _loop.LoopStore("f1a1a661", owner_name="cam-dev",
                                tmux_session="cam-f1a1a661")
        store.add(_loop.build_loop(
            "lst", _cron.parse_every("1h"), "ping prompt",
            _owner_rec()))
        args = argparse.Namespace(loop=True, owner="cam-dev", json_out=True)
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_list(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["agent_id"] == "f1a1a661"
        assert payload["count"] == 1
        L = payload["loops"][0]
        # F2 contract: top-level stable fields Desktop reads.
        required_keys = {
            "id", "name", "owner", "enabled", "schedule",
            "prompt", "next_due_at", "last_status", "last_due_at",
            "last_run_id",
        }
        assert required_keys.issubset(set(L.keys())), \
            "missing top-level keys: %s" % (required_keys - set(L.keys()))
        assert L["name"] == "lst"
        assert L["prompt"] == "ping prompt"
        assert L["next_due_at"]  # comes from schedule.next_due_at
        # Loop hasn't fired yet — these MUST be present as null.
        assert L["last_status"] is None
        assert L["last_due_at"] is None
        assert L["last_run_id"] is None
        # Nested blocks retained for forward-compat.
        assert L["action"]["text"] == "ping prompt"
        assert L["state"]["attempts"] == 0

    def test_list_loop_empty_json_owner_unknown(self, home, cli_owner, capsys):
        args = argparse.Namespace(loop=True, owner="ghost", json_out=True)
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_list(args)
        assert ei.value.code == 1
        # JSON envelope on stdout so Desktop can parse "no loops".
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 0
        assert payload["loops"] == []
        assert "error" in payload

    def test_rm_loop(self, home, cli_owner, capsys):
        store = _loop.LoopStore("f1a1a661")
        store.add(_loop.build_loop(
            "rmme", _cron.parse_every("30m"), "p", _owner_rec()))
        args = argparse.Namespace(loop=True, owner="cam-dev",
                                  id_or_name="rmme")
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_rm(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "removed agent loop rmme" in out
        # Loop is gone from the active file but archive exists.
        assert _loop.LoopStore("f1a1a661").find("rmme") is None
        archive_dir = os.path.join(_loop.LOOPS_DIR, "f1a1a661", "archive")
        assert os.listdir(archive_dir)


# ---------------------------------------------------------------------------
# Sanity: existing host cron paths still intact (no loop interference)
# ---------------------------------------------------------------------------

class TestHostCronIntact:
    def test_normal_cron_add_still_works(self, home, monkeypatch, capsys):
        monkeypatch.setattr(_cron, "_read_user_crontab",
                            lambda runner=None: "")
        monkeypatch.setattr(_cron, "_write_user_crontab",
                            lambda text, runner=None: None)
        args = argparse.Namespace(
            name="normal", loop=False, owner=None,
            prompt=None, prompt_file=None,
            every="30m", daily=None, at_time=None, in_dur=None,
            ttl_days=None, expires_at=None, no_expire=False,
            max_attempts=None, timeout=None, cwd=None, host=None,
            shell_cmd=None, cmd_argv=["--", "true"],
        )
        with pytest.raises(SystemExit) as ei:
            _cli.cmd_cron_add(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "added cron job normal" in out
