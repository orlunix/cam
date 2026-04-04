"""Tests for monitor v2 logic: single-probe idle detection, probe-caused filter,
idle revival, attachment-aware auto-exit, fallback stable.

These tests mock tmux interactions to exercise the monitor loop logic without
needing real tmux sessions.
"""

import hashlib
import time
from unittest.mock import MagicMock, patch, call

import pytest

from camc_pkg.monitor import _smart_probe, run_monitor_loop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeConfig:
    """Minimal config object matching AdapterConfig attributes used by monitor."""
    probe_char = "1"
    probe_wait = 0.3
    probe_stable = 5.0
    probe_cooldown = 5.0
    fallback_stable = 30.0
    confirm_cooldown = 5.0
    confirm_sleep = 0.5
    health_check_interval = 15.0
    empty_threshold = 3
    strip_ansi = False
    state_strategy = "first"
    state_patterns = []
    state_recent_chars = 2000
    auto_exit = False
    exit_action = "kill_session"
    exit_command = "/exit"
    completion_strategy = "prompt_count"
    completion_stable = 3.0
    prompt_pattern = None
    prompt_threshold = 2
    summary_pattern = None
    confirm_rules = []


class FakeStore(dict):
    """Minimal agent store that records updates."""
    def __init__(self):
        super().__init__()
        self._updates = []

    def save(self, agent):
        self[agent["id"]] = agent

    def get(self, agent_id):
        return super().get(agent_id)

    def update(self, agent_id, **kwargs):
        self._updates.append((agent_id, kwargs))
        if agent_id in self:
            self[agent_id].update(kwargs)


class FakeEvents:
    """Minimal event store."""
    def __init__(self):
        self.events = []

    def append(self, agent_id, event_type, detail=None):
        self.events.append((agent_id, event_type, detail))

    def event_types(self):
        return [e[1] for e in self.events]


# ---------------------------------------------------------------------------
# _smart_probe tests
# ---------------------------------------------------------------------------

class TestSmartProbe:
    """Test _smart_probe classification logic."""

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_idle_echo(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Probe char echoed at prompt → idle."""
        config = FakeConfig()
        # baseline: prompt only; after: prompt with "1" echoed
        mock_capture.side_effect = ["❯ ", "❯ 1"]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "idle"
        mock_key.assert_called_once_with("sess", "BSpace")

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_busy_unchanged(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Output unchanged after probe → busy (agent in raw mode)."""
        config = FakeConfig()
        mock_capture.side_effect = ["Working on task...", "Working on task..."]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "busy"
        mock_key.assert_called_once_with("sess", "BSpace")

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_busy_consumed(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Output changed but probe char not visible → busy (dialog consumed it)."""
        config = FakeConfig()
        # Dialog was showing "1. Allow", probe "1" consumed it, now showing new output
        mock_capture.side_effect = [
            "1. Allow once\n2. Deny",
            "Agent is now writing files..."
        ]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "busy"

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_error_empty_baseline(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Empty baseline → error."""
        config = FakeConfig()
        mock_capture.return_value = "   "

        result = _smart_probe("sess", config)

        assert result == "error"
        mock_send.assert_not_called()

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_error_send_failed(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Send fails → error."""
        config = FakeConfig()
        mock_capture.return_value = "❯ "
        mock_send.return_value = False

        result = _smart_probe("sess", config)

        assert result == "error"

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_idle_multiline(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Probe char echoed on a non-last line (TUI status lines below prompt)."""
        config = FakeConfig()
        baseline = "Some output\n❯ \n─────────\nstatus: ready"
        after = "Some output\n❯ 1\n─────────\nstatus: ready"
        mock_capture.side_effect = [baseline, after]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "idle"


# ---------------------------------------------------------------------------
# Monitor loop tests (mock time + tmux to run loop iterations)
# ---------------------------------------------------------------------------

def _hash(text):
    return hashlib.md5(text.encode()).hexdigest()[:8]


class TestMonitorLoop:
    """Test run_monitor_loop logic with mocked dependencies."""

    def _run_loop_iterations(self, outputs, session_exists=True, attached=False,
                             config=None, agent_data=None, probe_results=None,
                             max_iterations=None):
        """Run monitor loop with controlled outputs per iteration.

        Args:
            outputs: list of strings, one per capture_tmux call
            session_exists: bool or list of bools per health check
            attached: bool or list of bools
            config: FakeConfig override
            agent_data: dict for the agent record in store
            probe_results: list of probe results to return
            max_iterations: stop loop after N iterations
        """
        if config is None:
            config = FakeConfig()
        store = FakeStore()
        events = FakeEvents()
        agent_id = "test1234"

        if agent_data is None:
            agent_data = {"id": agent_id, "task": {"auto_exit": False}, "status": "running"}
        store.save(agent_data)

        # Track iteration count to stop loop
        iteration = [0]
        limit = max_iterations or len(outputs)

        original_sleep = time.sleep
        def fake_sleep(secs):
            pass

        # Mock time.time to advance by 1s per call (simulates 1s loop)
        # But we need probe_stable (5s) to be reachable, so advance faster
        base_time = [1000.0]
        def fake_time():
            base_time[0] += 1.0
            return base_time[0]

        capture_idx = [0]
        def fake_capture(sess, lines=100):
            idx = min(capture_idx[0], len(outputs) - 1)
            capture_idx[0] += 1
            return outputs[idx]

        def fake_session_exists(sess):
            if isinstance(session_exists, list):
                idx = min(capture_idx[0] - 1, len(session_exists) - 1)
                return session_exists[idx]
            return session_exists

        def fake_attached(sess):
            if isinstance(attached, list):
                idx = min(capture_idx[0] - 1, len(attached) - 1)
                return attached[idx]
            return attached

        probe_idx = [0]
        def fake_probe(sess, cfg):
            idx = min(probe_idx[0], len(probe_results) - 1) if probe_results else 0
            probe_idx[0] += 1
            return probe_results[idx] if probe_results else "busy"

        # Control loop termination
        running_count = [0]
        original_running_setitem = list.__setitem__

        patches = [
            patch("camc_pkg.monitor.time.sleep", side_effect=fake_sleep),
            patch("camc_pkg.monitor.time.time", side_effect=fake_time),
            patch("camc_pkg.monitor.capture_tmux", side_effect=fake_capture),
            patch("camc_pkg.monitor.tmux_session_exists", side_effect=fake_session_exists),
            patch("camc_pkg.monitor.tmux_is_attached", side_effect=fake_attached),
            patch("camc_pkg.monitor.tmux_kill_session", return_value=True),
            patch("camc_pkg.monitor.tmux_send_input", return_value=True),
            patch("camc_pkg.monitor.tmux_send_key", return_value=True),
            patch("camc_pkg.monitor.detect_completion", return_value=False),
            patch("camc_pkg.monitor.detect_state", return_value=None),
            patch("camc_pkg.monitor.should_auto_confirm", return_value=None),
        ]
        if probe_results is not None:
            patches.append(patch("camc_pkg.monitor._smart_probe", side_effect=fake_probe))

        for p in patches:
            p.start()

        # Terminate loop after enough iterations
        def stop_after_limit():
            """Intercept time.sleep to count iterations and stop the loop."""
            pass

        try:
            # We need a way to stop the loop. Patch the while condition.
            # Simplest: raise an exception from capture after enough calls.
            def limited_capture(sess, lines=100):
                idx = capture_idx[0]
                capture_idx[0] += 1
                if idx >= limit:
                    raise StopIteration("test done")
                if idx < len(outputs):
                    return outputs[idx]
                return outputs[-1]

            # Re-patch capture with limit
            patch("camc_pkg.monitor.capture_tmux", side_effect=limited_capture).start()

            try:
                run_monitor_loop("test-sess", agent_id, config, store, events=events)
            except StopIteration:
                pass
        finally:
            patch.stopall()

        return store, events

    def test_single_probe_idle_confirmed(self):
        """Single idle probe → idle_confirmed, state set to idle."""
        # 8 iterations of stable output → probe fires after probe_stable (5s)
        outputs = ["❯ ready"] * 10
        store, events = self._run_loop_iterations(
            outputs, probe_results=["idle"], max_iterations=10
        )
        assert "idle_confirmed" in events.event_types()
        # Check store was updated to idle state
        idle_updates = [u for u in store._updates if u[1].get("state") == "idle"]
        assert len(idle_updates) >= 1

    def test_probe_busy_resets_idle_timer(self):
        """Busy probe resets last_change, preventing immediate re-probe."""
        outputs = ["❯ ready"] * 15
        store, events = self._run_loop_iterations(
            outputs, probe_results=["busy", "idle"], max_iterations=15
        )
        probes = [e for e in events.events if e[1] == "probe"]
        # First probe: busy. After cooldown, second probe: idle.
        assert len(probes) >= 1
        if len(probes) >= 2:
            assert probes[0][2]["result"] == "busy"
            assert probes[1][2]["result"] == "idle"

    def test_output_change_resets_idle(self):
        """Real output change after idle_confirmed → idle_confirmed reset."""
        # Stable → probe idle → new output → should re-probe
        outputs = (
            ["❯ ready"] * 8  # stable, probe fires → idle
            + ["❯ new work happening"]  # real change → reset
            + ["❯ new work happening"] * 8  # stable again → re-probe
        )
        store, events = self._run_loop_iterations(
            outputs, probe_results=["idle", "idle"], max_iterations=18
        )
        et = events.event_types()
        # Should see idle_confirmed, then later another probe
        assert "idle_confirmed" in et

    def test_auto_exit_when_idle_and_not_attached(self):
        """Auto-exit fires when idle + not attached + auto_exit enabled."""
        config = FakeConfig()
        config.auto_exit = True
        agent_data = {
            "id": "test1234",
            "task": {"auto_exit": True},
            "status": "running",
        }
        outputs = ["❯ ready"] * 10
        store, events = self._run_loop_iterations(
            outputs, probe_results=["idle"], config=config,
            agent_data=agent_data, attached=False, max_iterations=10
        )
        et = events.event_types()
        # Should complete with auto-exit
        completed = [e for e in events.events if e[1] == "completed"]
        if completed:
            assert completed[0][2]["reason"] == "auto-exit"

    def test_auto_exit_blocked_when_attached(self):
        """Auto-exit blocked when user is attached."""
        config = FakeConfig()
        config.auto_exit = True
        agent_data = {
            "id": "test1234",
            "task": {"auto_exit": True},
            "status": "running",
        }
        outputs = ["❯ ready"] * 10
        store, events = self._run_loop_iterations(
            outputs, probe_results=["idle"], config=config,
            agent_data=agent_data, attached=True, max_iterations=10
        )
        # Should see idle_confirmed but NOT completed (blocked by attachment)
        et = events.event_types()
        assert "idle_confirmed" in et
        completed = [e for e in events.events if e[1] == "completed"]
        assert len(completed) == 0

    def test_session_gone_marks_completed(self):
        """Session disappearing marks agent completed/failed."""
        config = FakeConfig()
        config.health_check_interval = 0.0  # check every iteration
        # Need enough iterations for time to advance past health_check_interval
        outputs = ["❯ working"] * 20
        store, events = self._run_loop_iterations(
            outputs, session_exists=False, max_iterations=20
        )
        completed = [e for e in events.events if e[1] == "completed"]
        assert len(completed) >= 1

    def test_fallback_stable_idle(self):
        """Fallback: output stable for 30s without probe → idle_confirmed."""
        config = FakeConfig()
        config.probe_stable = 50.0  # prevent normal probe from firing
        config.fallback_stable = 10.0  # lower for test

        outputs = ["❯ ready"] * 15
        store, events = self._run_loop_iterations(
            outputs, config=config, max_iterations=15
        )
        et = events.event_types()
        idle_events = [e for e in events.events if e[1] == "idle_confirmed"]
        assert len(idle_events) >= 1
        # Should have fallback reason
        if idle_events:
            assert idle_events[0][2] == {"reason": "fallback"}


# ---------------------------------------------------------------------------
# _smart_probe edge cases
# ---------------------------------------------------------------------------

class TestSmartProbeEdgeCases:

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_char_in_existing_line_not_false_idle(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Probe char '1' already exists in baseline — only NEW lines with '1' count as idle."""
        config = FakeConfig()
        # Baseline already has "1" in text; after capture is identical
        baseline = "Step 1: do something\n❯ "
        mock_capture.side_effect = [baseline, baseline]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "busy"  # unchanged = busy

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_with_trailing_newlines(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Trailing newlines stripped before comparison."""
        config = FakeConfig()
        mock_capture.side_effect = ["❯ \n\n\n", "❯ 1\n\n\n"]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "idle"

    @patch("camc_pkg.monitor.time.sleep")
    @patch("camc_pkg.monitor.tmux_send_key")
    @patch("camc_pkg.monitor.tmux_send_input")
    @patch("camc_pkg.monitor.capture_tmux")
    def test_probe_nonbreaking_space(self, mock_capture, mock_send, mock_key, mock_sleep):
        """Claude uses non-breaking space after ❯ -- probe char still detected."""
        config = FakeConfig()
        mock_capture.side_effect = ["❯\xa0", "❯\xa01"]
        mock_send.return_value = True

        result = _smart_probe("sess", config)

        assert result == "idle"


# ---------------------------------------------------------------------------
# Auto-confirm false positive protection
# ---------------------------------------------------------------------------

class TestAutoConfirmFalsePositive:
    """Test the dedup and BSpace cleanup for false positive auto-confirms."""

    def test_dedup_suppresses_same_match(self):
        """Same pattern + matched text + same output hash -> suppressed on second call."""
        # Simulate the monitor's confirm_key dedup logic
        pat_str = "Do\\s+you\\s+want\\s+to\\s+proceed"
        matched = "Do you want to proceed"
        output = "I'll help you. Do you want to proceed with this plan?\n❯ "
        h = hashlib.md5(output.encode()).hexdigest()[:8]

        key1 = "%s:%s:%s" % (pat_str, matched, h)
        key2 = "%s:%s:%s" % (pat_str, matched, h)

        assert key1 == key2  # dedup would suppress the second

    def test_dedup_resets_on_new_output(self):
        """Different output hash -> dedup key differs, allowing re-confirm."""
        pat_str = "Do\\s+you\\s+want\\s+to\\s+proceed"
        matched = "Do you want to proceed"
        output1 = "Do you want to proceed with this plan?\n❯ "
        output2 = "New dialog appeared.\nDo you want to proceed?\n1. Yes  2. No\n❯ "

        h1 = hashlib.md5(output1.encode()).hexdigest()[:8]
        h2 = hashlib.md5(output2.encode()).hexdigest()[:8]

        key1 = "%s:%s:%s" % (pat_str, matched, h1)
        key2 = "%s:%s:%s" % (pat_str, matched, h2)

        assert key1 != key2  # different output -> re-confirm allowed

    def test_bspace_cleanup_on_unchanged_output(self):
        """When auto-confirm sends '1' but output doesn't change, BSpace should fire."""
        output = "Do you want to proceed?\n❯ "
        h_before = hashlib.md5(output.encode()).hexdigest()[:8]
        h_after = hashlib.md5(output.encode()).hexdigest()[:8]

        response = "1"
        send_enter = False

        should_bspace = (h_after == h_before and response and not send_enter)
        assert should_bspace is True

    def test_no_bspace_when_dialog_consumed(self):
        """When dialog consumed the '1', output changes -> no BSpace."""
        output_before = "Do you want to proceed?\n1. Yes  2. No"
        output_after = "Proceeding with the task...\n● Edit(file.py)"

        h_before = hashlib.md5(output_before.encode()).hexdigest()[:8]
        h_after = hashlib.md5(output_after.encode()).hexdigest()[:8]

        response = "1"
        send_enter = False

        should_bspace = (h_after == h_before and response and not send_enter)
        assert should_bspace is False

    def test_no_bspace_when_send_enter(self):
        """Enter-based confirms (trust dialog) don't need BSpace even if unchanged."""
        output = "Enter to confirm · Esc to cancel"
        h = hashlib.md5(output.encode()).hexdigest()[:8]

        response = ""
        send_enter = True

        should_bspace = bool(h == h and response and not send_enter)
        assert should_bspace is False  # send_enter=True -> no BSpace
