"""Isolated stdlib regression tests against real CAM source, not an LLM eval.

Run: CAM_SOURCE=/absolute/cam/src python -m unittest -v test_loop_mechanism
Only the clock, owner records and message transport are test doubles.
All scheduling, persistence, retry and host-lock code is production CAM code.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.environ.get("CAM_SOURCE", ""))
from camc_pkg import cron
from camc_pkg import cron_loop as loops


class Store:
    def __init__(self, rec):
        self.rec = rec

    def get(self, key):
        return self.rec if self.rec and key == self.rec["id"] else None


class LoopMechanismTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        self.rec = {"id": "advisor-test", "status": "running", "state": "idle"}
        self.owners = Store(self.rec)
        self.loop = loops.build_loop("supervise", cron.parse_every("5m"),
                                     "Check goal progress", self.rec)
        self.loop["schedule"]["next_due_at"] = cron._iso(self.now)
        self.store = loops.LoopStore(self.rec["id"], root=str(self.root))
        self.store.add(self.loop)
        self.calls = []

    def dispatch(self, loop):
        self.calls.append(loop["id"])
        return True, "message-test"

    def tick(self, **kwargs):
        return loops.tick_loops(now=kwargs.pop("now", self.now),
                                dispatch=kwargs.pop("dispatch", self.dispatch),
                                root=str(self.root), agent_store=self.owners, **kwargs)

    def saved(self):
        return self.store.find(self.loop["id"])

    def force_due(self):
        envelope = self.store.load()
        envelope["loops"][0]["schedule"]["next_due_at"] = cron._iso(self.now)
        self.store.replace_envelope(envelope)

    def test_01_due_idle_dispatches_and_persists(self):
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.calls, [self.loop["id"]])
        self.assertEqual(self.saved()["state"]["last_msg_id"], "message-test")
        self.assertGreater(cron._parse_iso(self.saved()["schedule"]["next_due_at"]), self.now)

    def test_02_early_tick_does_not_dispatch(self):
        self.assertEqual(self.tick(now=self.now - timedelta(seconds=1)), 0)
        self.assertEqual(self.calls, [])

    def test_03_repeated_same_time_tick_does_not_duplicate(self):
        self.tick()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_04_busy_defers_without_burning_attempts(self):
        self.rec["state"] = "testing"
        before = self.saved()
        self.assertEqual(self.tick(), 0)
        after = self.saved()
        self.assertEqual(after["schedule"], before["schedule"])
        self.assertEqual(after["state"]["attempts"], 0)
        self.assertEqual(self.calls, [])

    def test_05_busy_then_idle_eventually_dispatches(self):
        self.rec["state"] = "editing"
        self.tick()
        self.rec["state"] = "idle"
        self.assertEqual(self.tick(now=self.now + timedelta(minutes=7)), 1)
        self.assertEqual(len(self.calls), 1)

    def test_06_missing_owner_defers(self):
        self.owners.rec = None
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.saved()["state"]["attempts"], 0)

    def test_07_dead_owner_does_not_automatically_restart(self):
        self.rec["status"] = "killed"
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(now=self.now + timedelta(days=1)), 0)
        self.assertEqual(self.rec["status"], "killed")
        self.assertTrue(self.saved()["enabled"])
        self.assertEqual(self.calls, [])

    def test_08_disabled_loop_does_not_dispatch(self):
        item = self.saved()
        item["enabled"] = False
        self.store.replace_one(item)
        self.assertEqual(self.tick(), 0)

    def test_09_repeated_dispatch_failure_disables_loop(self):
        for _ in range(3):
            self.force_due()
            self.tick(dispatch=lambda _: (False, "injected transport failure"))
        item = self.saved()
        self.assertFalse(item["enabled"])
        self.assertEqual(item["state"]["attempts"], 3)
        self.assertEqual(item["state"]["last_status"], "recycled_too_many_failures")

    def test_10_success_resets_failure_budget(self):
        self.tick(dispatch=lambda _: (False, "injected failure"))
        self.force_due()
        self.tick()
        self.assertEqual(self.saved()["state"]["attempts"], 0)
        self.assertTrue(self.saved()["enabled"])

    def test_11_missed_intervals_do_not_flood(self):
        late = self.now + timedelta(hours=24)
        self.assertEqual(self.tick(now=late), 1)
        self.assertEqual(self.tick(now=late), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(cron._parse_iso(self.saved()["schedule"]["next_due_at"]), late)

    def test_12_dispatch_ack_is_not_review_ack(self):
        self.tick()
        state = self.saved()["state"]
        self.assertEqual(state["last_status"], "sent")
        self.assertNotIn("review_completed_at", state)
        self.assertNotIn("goal_verified", state)
        events = [json.loads(x)["event"] for x in
                  (self.root / self.rec["id"] / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["loop_queued", "loop_dispatched"])

    def test_13_dispatch_uses_real_message_command_contract(self):
        recorded = []
        def runner(argv, **kwargs):
            recorded.append(argv)
            return subprocess.CompletedProcess(argv, 0, "MSG_ID=1234abcd\n", "")
        result = loops.dispatch_loop(self.loop, runner=runner, camc_path="/test/camc")
        self.assertEqual(result, (True, "1234abcd"))
        self.assertEqual(recorded[0][:4], ["/test/camc", "msg", "send", "advisor-test"])
        self.assertIn("--no-wait", recorded[0])

    def test_14_transport_timeout_is_a_failure(self):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired("camc", 20)
        ok, error = loops.dispatch_loop(self.loop, runner=runner, camc_path="/test/camc")
        self.assertFalse(ok)
        self.assertIn("timed out", error)

    def test_15_host_scheduler_honors_existing_lock(self):
        path = str(self.root / "tick.lock")
        lock = cron._acquire_lock(path)
        self.assertIsNotNone(lock)
        self.addCleanup(cron._release_lock, lock)
        with patch.object(cron, "CRON_LOCK_FILE", path), patch.object(cron, "_append_runs"):
            self.assertEqual(cron.tick()["status"], "skipped_locked")

    def test_16_paused_task_is_not_a_scheduler_input(self):
        # Characterization: the scheduler checks the owner, not its supervised ToDo.
        # The Advisor/template must implement the separate human-pause contract.
        self.rec["task"] = {"status": "paused", "human_paused": True}
        self.assertEqual(self.tick(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
