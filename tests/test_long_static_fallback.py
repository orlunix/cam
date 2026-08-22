"""Focused tests for the long, strict screen-static fallback.

These tests intentionally use only the feature pipeline and fake snapshots;
they never send keys to a real tmux session.
"""

import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import monitor_features as mf  # noqa: E402
from camc_pkg import cli  # noqa: E402


class _Cfg(object):
    confirm_cooldown = 5.0
    confirm_sleep = 0.5
    confirm_rules = [(re.compile(r"1\. Yes"), "1", False)]
    confirm_recent_lines = 8
    confirm_stuck_recent_lines = 40
    strip_ansi = False


def _snap(now, h0="same", h1="same", idle0=0.0, idle1=0.0,
          output="1. Yes\n", prompt_visible=False):
    return mf.MonitorSnapshot(
        output=output, hash=h0, prev_hash=h0, changed=False,
        now=now, cycle=1, prompt_visible=prompt_visible,
        screen_busy=False, screen_done=False, bare_prompt=False,
        tail_lines=[line for line in output.splitlines() if line],
        idle_for=idle0, hash0=h0, hash1=h1,
        idle_for_hash1=idle1,
    )


class LongStaticFallbackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            hasattr(mf, "FinalStaticFallbackFeature"),
            "strict long-static fallback feature is not implemented yet",
        )
        self.runtime = mf.MonitorRuntime("agent", _Cfg(), now=0.0)
        self.feature = mf.FinalStaticFallbackFeature()

    def test_requires_both_hashes_stable_for_one_minute(self):
        self.runtime.final_fallback = {
            "kind": "confirm", "text": "1", "send_enter": False,
            "pattern": r"1\. Yes", "recent_lines": 8,
            "baseline_hash0": "same", "baseline_hash1": "same",
            "baseline_at": 0.0, "attempts": 0, "next_at": 0.0,
        }

        self.assertEqual(self.feature.after_confirm(
            _snap(60.0, idle0=60.0, idle1=59.0), self.runtime), [])
        actions = self.feature.after_confirm(
            _snap(61.0, idle0=61.0, idle1=61.0), self.runtime)
        self.assertIn({"kind": "send_key", "key": "Enter"}, actions)

    def test_idle_nudge_sends_enter_with_backoff_and_stops_after_three(self):
        self.runtime.final_fallback = {
            "kind": "confirm", "baseline_hash0": "same",
            "baseline_hash1": "same", "baseline_at": 0.0,
            "pattern": r"1\. Yes", "attempts": 0, "next_at": 60.0,
        }

        for now in (60.0, 180.0, 480.0):
            actions = self.feature.after_confirm(
                _snap(now, idle0=now, idle1=now), self.runtime)
            self.assertIn({"kind": "send_key", "key": "Enter"}, actions)

        self.assertIsNone(self.runtime.final_fallback)

    def test_any_hash_change_is_feedback_and_clears_pending_action(self):
        self.runtime.final_fallback = {
            "kind": "message", "baseline_hash0": "same",
            "baseline_hash1": "same", "baseline_at": 0.0,
            "attempts": 0, "next_at": 0.0,
        }
        actions = self.feature.after_confirm(
            _snap(61.0, h0="changed", h1="same", idle0=61.0, idle1=61.0,
                  output="❯ ", prompt_visible=True), self.runtime)
        self.assertFalse(any(a.get("kind") == "send_key" for a in actions))
        self.assertIsNone(self.runtime.final_fallback)

    def test_no_feedback_escalates_to_five_minutes_then_stops(self):
        self.runtime.final_fallback = {
            "kind": "message", "baseline_hash0": "same",
            "baseline_hash1": "same", "baseline_at": 0.0,
            "attempts": 2, "next_at": 300.0,
        }
        self.assertEqual(self.feature.after_confirm(
            _snap(299.0, idle0=299.0, idle1=299.0,
                  output="❯ ", prompt_visible=True), self.runtime), [])
        actions = self.feature.after_confirm(
            _snap(300.0, idle0=300.0, idle1=300.0,
                  output="❯ ", prompt_visible=True), self.runtime)
        self.assertTrue(any(a.get("kind") == "send_key" and
                            a.get("key") == "Enter" for a in actions))
        self.assertIn({"kind": "send_key", "key": "Enter"}, actions)
        self.assertIsNone(self.runtime.final_fallback)

    def test_confirm_fallback_respects_current_input_cursor(self):
        self.runtime.final_fallback = {
            "kind": "confirm", "text": "1", "send_enter": False,
            "pattern": r"1\. Yes", "recent_lines": 8,
            "baseline_hash0": "same", "baseline_hash1": "same",
            "baseline_at": 0.0, "attempts": 0, "next_at": 0.0,
        }
        actions = self.feature.after_confirm(
            _snap(61.0, idle0=61.0, idle1=61.0,
                  output="1. Yes\n❯ ", prompt_visible=True), self.runtime)
        self.assertFalse(any(a.get("kind") == "send_input" for a in actions))

    def test_message_fallback_is_enter_only(self):
        self.runtime.final_fallback = {
            "kind": "message", "baseline_hash0": "same",
            "baseline_hash1": "same", "baseline_at": 0.0,
            "attempts": 0, "next_at": 0.0,
        }
        actions = self.feature.after_confirm(
            _snap(60.0, idle0=60.0, idle1=60.0,
                  output="❯ [paste 3 lines]", prompt_visible=True),
            self.runtime)
        self.assertEqual(
            [(a.get("kind"), a.get("key")) for a in actions
             if a.get("kind") == "send_key"],
            [("send_key", "Enter")],
        )

    def test_confirm_starts_static_window_after_first_post_action_frame(self):
        self.runtime.final_fallback = {
            "kind": "confirm", "text": "1", "send_enter": False,
            "pattern": r"1\. Yes", "recent_lines": 8,
            "baseline_hash0": None, "baseline_hash1": None,
            "baseline_at": None, "attempts": 0, "next_at": 0.0,
        }
        self.assertEqual(self.feature.after_confirm(
            _snap(60.0, idle0=60.0, idle1=60.0), self.runtime), [])
        self.assertEqual(self.runtime.final_fallback["baseline_at"], 60.0)
        self.assertEqual(self.feature.after_confirm(
            _snap(119.0, idle0=119.0, idle1=119.0), self.runtime), [])
        actions = self.feature.after_confirm(
            _snap(120.0, idle0=120.0, idle1=120.0), self.runtime)
        self.assertIn({"kind": "send_key", "key": "Enter"}, actions)

    def test_confirm_fallback_preserves_cursor_special_key(self):
        self.runtime.config.confirm_rules = [
            (re.compile(r"Auto-run everything \(shift\+tab\)"),
             "BTab", False),
        ]
        self.runtime.final_fallback = {
            "kind": "confirm", "text": "BTab", "send_enter": False,
            "pattern": r"Auto-run everything \(shift\+tab\)",
            "recent_lines": 8, "baseline_hash0": "same",
            "baseline_hash1": "same", "baseline_at": 0.0,
            "attempts": 0, "next_at": 0.0,
        }
        actions = self.feature.after_confirm(
            _snap(61.0, idle0=61.0, idle1=61.0,
                  output="Auto-run everything (shift+tab)\n"), self.runtime)
        self.assertIn({"kind": "send_key", "key": "BTab"}, actions)

    def test_msg_inject_records_owned_pending_message_for_monitor(self):
        class _Store(object):
            def __init__(self):
                self.updates = []

            def update(self, agent_id, **fields):
                self.updates.append((agent_id, fields))

        store = _Store()
        target = {
            "target_id": "target-id", "target_name": "target",
            "target_tmux_session": "cam-target-id",
        }
        with mock.patch.object(cli, "_msg_sender_identity", return_value=None), \
                mock.patch.object(cli, "_msg_target_identity", return_value=target), \
                mock.patch.object(cli, "_msg_ledger_append"), \
                mock.patch.object(cli, "tmux_submit_input", return_value=True), \
                mock.patch.object(cli, "capture_tmux",
                                  return_value="❯ [paste 1 line]"), \
                mock.patch.object(cli, "AgentStore", return_value=store):
            msg_id, ok = cli._msg_inject(
                "cam-target-id", "target", "hello", 5, submit_delay=0.5)

        self.assertTrue(ok)
        self.assertTrue(msg_id)
        self.assertEqual(len(store.updates), 1)
        agent_id, fields = store.updates[0]
        self.assertEqual(agent_id, "target-id")
        self.assertEqual(fields["screen_fallback"]["kind"], "message")
        self.assertEqual(fields["screen_fallback"]["msg_id"], msg_id)
        self.assertTrue(fields["screen_fallback"]["baseline_at"])
        self.assertTrue(fields["screen_fallback"]["baseline_hash0"])
        self.assertTrue(fields["screen_fallback"]["baseline_hash1"])


if __name__ == "__main__":
    unittest.main()
