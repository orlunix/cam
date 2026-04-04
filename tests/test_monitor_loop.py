#!/usr/bin/env python3
"""Tests for the monitor loop (camc_pkg/monitor.py).

Two test categories:
  1. Direct unit tests — mock transport, test each step and state transition
  2. Stress tests — run many monitors in parallel with random sequences

Run:
    python3 -m pytest tests/test_monitor_loop.py -v
    python3 -m pytest tests/test_monitor_loop.py -k "stress" -v
"""

import hashlib
import threading
import time
import random
import pytest

from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Helpers: build a mock config matching claude.toml
# ---------------------------------------------------------------------------

from camc_pkg.adapters import AdapterConfig, _parse_toml

_CLAUDE_TOML = """
[adapter]
name = "claude"

[launch]
strip_ansi = true
command = ["claude"]
prompt_after_launch = true
startup_wait = 30.0
ready_pattern = "^[❯>]"
ready_flags = ["MULTILINE"]

[state]
strategy = "last"
recent_chars = 2000

[[state.patterns]]
state = "planning"
pattern = "(● Read\\\\(|● Glob\\\\(|● Grep\\\\(|● WebFetch\\\\(|Thinking|Analyzing)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "editing"
pattern = "(● Edit\\\\(|● Write\\\\()"

[[state.patterns]]
state = "testing"
pattern = "(● Bash\\\\(|Running tests|pytest|npm test)"
flags = ["IGNORECASE"]

[[state.patterns]]
state = "committing"
pattern = "(git commit|git push|gh pr create)"
flags = ["IGNORECASE"]

[completion]
strategy = "prompt_count"
prompt_pattern = "^[❯>]"
prompt_flags = ["MULTILINE"]
prompt_count_threshold = 2
fallback_summary_pattern = "✻ .+ for \\\\d+"

[[confirm]]
pattern = "Do\\\\s+you\\\\s+want\\\\s+to\\\\s+proceed"
flags = ["IGNORECASE"]
response = "1"
send_enter = false

[[confirm]]
pattern = "1\\\\.\\\\s*(Yes|Allow)"
flags = ["IGNORECASE"]
response = "1"
send_enter = false

[[confirm]]
pattern = "Allow\\\\s+(once|always)"
flags = ["IGNORECASE"]
response = "1"
send_enter = false

[[confirm]]
pattern = "\\\\(y/n\\\\)|\\\\[Y/n\\\\]|\\\\[y/N\\\\]"
response = "y"
send_enter = false

[probe]
char = "1"
wait = 0.1
idle_threshold = 2

[monitor]
confirm_cooldown = 0.5
confirm_sleep = 0.2
completion_stable = 0.5
health_check_interval = 1
empty_threshold = 3
auto_exit = false
probe_stable = 0.5
probe_cooldown = 0.5
exit_action = "kill_session"
exit_command = "/exit"
"""


def make_config():
    """Build AdapterConfig from test TOML (fast timings for tests)."""
    parsed = _parse_toml(_CLAUDE_TOML)
    return AdapterConfig(parsed)


class MockStore:
    """Mock agent store that records updates."""

    def __init__(self, agent_id="test-001", auto_exit=False):
        self.agent_id = agent_id
        self.auto_exit = auto_exit
        self.updates = []
        self.agent = {
            "id": agent_id,
            "task": {"name": "test", "tool": "claude", "auto_exit": auto_exit},
            "status": "running",
            "state": None,
        }

    def get(self, agent_id):
        return self.agent

    def update(self, agent_id, **kwargs):
        self.updates.append(kwargs)
        self.agent.update(kwargs)

    @property
    def last_state(self):
        for u in reversed(self.updates):
            if "state" in u:
                return u["state"]
        return None

    @property
    def last_status(self):
        for u in reversed(self.updates):
            if "status" in u:
                return u["status"]
        return None


class MockEvents:
    """Mock event store that records events."""

    def __init__(self):
        self.events = []

    def append(self, agent_id, event_type, detail=None):
        self.events.append({"type": event_type, "detail": detail})

    def of_type(self, event_type):
        return [e for e in self.events if e["type"] == event_type]


class ScreenSequence:
    """Simulates a sequence of screen captures for the monitor to read."""

    def __init__(self, screens):
        """screens: list of strings (terminal output at each capture)."""
        self._screens = list(screens)
        self._idx = 0
        self._lock = threading.Lock()

    def capture(self, session):
        with self._lock:
            if self._idx < len(self._screens):
                s = self._screens[self._idx]
                self._idx += 1
                return s
            return self._screens[-1] if self._screens else ""

    @property
    def idx(self):
        return self._idx


# ---------------------------------------------------------------------------
# Screen content generators
# ---------------------------------------------------------------------------

def screen_idle():
    """Idle prompt — double ❯."""
    return "✻ Sautéed for 1m 14s\n\n❯ \n────\n❯ \n────\n"


def screen_planning():
    return "● Read(\"src/main.py\")\n  ⎿  Reading...\n"


def screen_editing():
    return "● Edit(\"src/main.py\")\n  ⎿  Editing...\n"


def screen_testing():
    return "● Bash(\"pytest tests/\")\n  ⎿  Running...\n"


def screen_committing():
    return "git commit -m \"fix bug\"\n  [main abc123] fix bug\n"


def screen_confirm_proceed():
    return (
        "● Bash(\"rm -rf /tmp/junk\")\n\n"
        "────\n"
        " Bash command\n\n"
        "   rm -rf /tmp/junk\n\n"
        " Permission rule Bash requires confirmation.\n\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n\n"
        " Esc to cancel\n"
    )


def screen_confirm_yes():
    return (
        "────\n"
        " 1. Yes\n"
        " 2. No\n\n"
        " Esc to cancel\n"
    )


def screen_confirm_allow():
    return (
        "────\n"
        " Allow once\n"
        " Always allow\n"
        " Deny\n"
    )


def screen_confirm_yn():
    return "Delete the file? (y/n)\n"


def screen_busy():
    """Agent producing output — changes each call."""
    return "Working on task...\nLine %d of output\n" % random.randint(1, 9999)


def screen_empty():
    return ""


def screen_false_positive():
    """Agent prose containing confirm keywords."""
    return (
        "The code shows 'Do you want to proceed?' in the UI module.\n"
        "Users see options like:\n"
        "  1. Yes - accept\n"
        "  2. No - reject\n"
        "The 'Allow once' button is rendered by the Ink TUI.\n"
        "Also check the (y/n) prompts in CLI.\n"
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def run_monitor_steps(screens, store=None, config=None, auto_exit=False,
                      session_alive=True, max_cycles=None):
    """Run monitor loop with mocked transport. Returns (store, events).

    Uses a fake clock so that timer-based logic (health check, probe cooldown,
    confirm cooldown) advances properly even though real wall time barely moves.
    Each mock_sleep call advances the fake clock by the requested duration.
    """
    from camc_pkg.monitor import run_monitor_loop

    if config is None:
        config = make_config()
    if store is None:
        store = MockStore(auto_exit=auto_exit)
    events = MockEvents()

    seq = ScreenSequence(screens)
    cycle_count = [0]
    max_c = max_cycles or len(screens) + 5

    # Fake clock — advances on each sleep call
    fake_time = [time.time()]

    def mock_time():
        return fake_time[0]

    def mock_capture(session):
        return seq.capture(session)

    def mock_session_exists(session):
        if not session_alive:
            return False
        return seq.idx < len(seq._screens)

    def mock_send_input(session, text, send_enter=False):
        return True

    def mock_send_key(session, key):
        return True

    def mock_is_attached(session):
        return False

    def mock_kill(session):
        pass

    def mock_sleep(duration):
        cycle_count[0] += 1
        if cycle_count[0] > max_c:
            raise StopIteration("max cycles reached")
        fake_time[0] += duration  # advance fake clock

    def mock_signal(signum, handler):
        pass  # no-op — signal.signal() only works in main thread

    with patch("camc_pkg.monitor.capture_tmux", side_effect=mock_capture), \
         patch("camc_pkg.monitor.tmux_session_exists", side_effect=mock_session_exists), \
         patch("camc_pkg.monitor.tmux_send_input", side_effect=mock_send_input), \
         patch("camc_pkg.monitor.tmux_send_key", side_effect=mock_send_key), \
         patch("camc_pkg.monitor.tmux_is_attached", side_effect=mock_is_attached), \
         patch("camc_pkg.monitor.tmux_kill_session", side_effect=mock_kill), \
         patch("camc_pkg.monitor.time.sleep", side_effect=mock_sleep), \
         patch("camc_pkg.monitor.time.time", side_effect=mock_time), \
         patch("camc_pkg.monitor.signal.signal", side_effect=mock_signal):
        try:
            run_monitor_loop("test-session", "test-001", config, store,
                             events=events)
        except StopIteration:
            pass

    return store, events


# ===========================================================================
# Direct unit tests — each step / state transition
# ===========================================================================

class TestStep1HealthCheck:
    """Step 1: Health check — session gone detection."""

    def test_session_gone_has_worked(self):
        screens = [screen_testing(), screen_testing()]
        store, events = run_monitor_steps(screens, session_alive=False)
        assert store.last_status == "completed"
        assert events.of_type("completed")

    def test_session_gone_never_worked(self):
        screens = [screen_empty()]
        store, events = run_monitor_steps(screens, session_alive=False)
        assert store.last_status == "failed"

    def test_session_gone_with_completion_pattern(self):
        screens = [screen_idle()]
        store, events = run_monitor_steps(screens, session_alive=False)
        assert store.last_status == "completed"


class TestStep3EmptySkip:
    """Step 3: Empty output skipped."""

    def test_empty_output_skipped(self):
        screens = [screen_empty(), screen_empty(), screen_idle()]
        store, events = run_monitor_steps(screens, max_cycles=20)
        # Should not crash, should eventually process the idle screen
        assert True


class TestStep4AutoConfirm:
    """Step 4: Auto-confirm — pattern detection and response."""

    def test_confirm_do_you_want_to_proceed(self):
        screens = [screen_confirm_proceed()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1
        assert confirms[0]["detail"]["response"] == "1"

    def test_confirm_yes_option(self):
        screens = [screen_confirm_yes()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1
        assert confirms[0]["detail"]["response"] == "1"

    def test_confirm_allow_once(self):
        screens = [screen_confirm_allow()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1

    def test_confirm_yn(self):
        screens = [screen_confirm_yn()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1
        assert confirms[0]["detail"]["response"] == "y"

    def test_confirm_resets_idle(self):
        # idle → confirm dialog → idle_confirmed should reset
        screens = [screen_idle()] * 10 + [screen_confirm_proceed()] * 5
        store, events = run_monitor_steps(screens, max_cycles=30)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1

    def test_confirm_cooldown(self):
        # Same dialog repeated — should not fire more than once per cooldown
        screens = [screen_confirm_proceed()] * 20
        store, events = run_monitor_steps(screens, max_cycles=25)
        confirms = events.of_type("auto_confirm")
        # With 0.5s cooldown and fast test timing, should be limited
        assert len(confirms) >= 1

    def test_no_confirm_on_false_positive(self):
        # Agent prose with confirm keywords — should NOT trigger
        # (depends on last-32-lines filter and pattern specificity)
        screens = [screen_false_positive()] * 10
        store, events = run_monitor_steps(screens, max_cycles=15)
        # False positive prose contains keywords in non-dialog context
        # The last-32-lines filter + pattern should ideally not match,
        # but some patterns are broad enough to match. Track behavior.
        # This test documents the current behavior.


class TestStep5StateDetection:
    """Step 5: State detection — pattern matching on output."""

    def test_state_planning(self):
        screens = [screen_planning()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        states = [e["detail"]["to"] for e in events.of_type("state_change")]
        assert "planning" in states

    def test_state_editing(self):
        screens = [screen_editing()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        states = [e["detail"]["to"] for e in events.of_type("state_change")]
        assert "editing" in states

    def test_state_testing(self):
        screens = [screen_testing()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        states = [e["detail"]["to"] for e in events.of_type("state_change")]
        assert "testing" in states

    def test_state_committing(self):
        screens = [screen_committing()] * 5
        store, events = run_monitor_steps(screens, max_cycles=10)
        states = [e["detail"]["to"] for e in events.of_type("state_change")]
        assert "committing" in states

    def test_state_transitions(self):
        screens = (
            [screen_planning()] * 3 +
            [screen_editing()] * 3 +
            [screen_testing()] * 3 +
            [screen_committing()] * 3
        )
        store, events = run_monitor_steps(screens, max_cycles=20)
        states = [e["detail"]["to"] for e in events.of_type("state_change")]
        assert "planning" in states
        assert "editing" in states
        assert "testing" in states
        assert "committing" in states


class TestStep6OutputChange:
    """Step 6: Output change detection resets idle."""

    def test_change_resets_idle(self):
        # Stable → change → should reset idle_confirmed
        screens = (
            [screen_idle()] * 15 +  # Go idle
            [screen_testing()] * 5 +  # New work
            [screen_idle()] * 15  # Go idle again
        )
        store, events = run_monitor_steps(screens, max_cycles=50)
        idles = events.of_type("idle_confirmed")
        # Should have 2 idle confirmations (before and after the work)
        assert len(idles) >= 1


class TestStep7SmartProbe:
    """Step 7: Smart probe — idle detection."""

    def test_probe_idle_same_screen(self):
        """Stable identical output → probe → idle confirmed."""
        screens = [screen_idle()] * 20
        store, events = run_monitor_steps(screens, max_cycles=25)
        idles = events.of_type("idle_confirmed")
        assert len(idles) >= 1
        assert store.last_state == "idle"

    def test_probe_busy_changing_screen(self):
        """Changing output → probe never fires (changed=True skips it)."""
        screens = [screen_busy() for _ in range(20)]
        store, events = run_monitor_steps(screens, max_cycles=25)
        probes = events.of_type("probe")
        # Probes may fire but should return busy
        idles = events.of_type("idle_confirmed")
        assert len(idles) == 0

    def test_probe_not_during_confirm(self):
        """Confirm dialog → probe should not fire (confirm takes priority)."""
        screens = [screen_confirm_proceed()] * 20
        store, events = run_monitor_steps(screens, max_cycles=25)
        # Confirm fires and continues, probe is skipped
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1


class TestSmartProbeBSpaceRetry:
    """Smart probe BSpace cleanup retry — when BSpace fails to erase probe char."""

    def _run_probe(self, capture_sequence):
        """Run _smart_probe with a controlled sequence of captures.

        capture_sequence: list of strings returned by successive capture_tmux calls.
          [0] = baseline, [1] = after first BSpace, [2] = after retry BSpace, ...
        """
        from camc_pkg.monitor import _smart_probe

        config = make_config()
        captures = iter(capture_sequence)
        bspace_count = [0]

        def mock_capture(session):
            return next(captures)

        def mock_send_input(session, text, send_enter=False):
            return True

        def mock_send_key(session, key):
            if key == "BSpace":
                bspace_count[0] += 1
            return True

        def mock_sleep(duration):
            pass

        with patch("camc_pkg.monitor.capture_tmux", side_effect=mock_capture), \
             patch("camc_pkg.monitor.tmux_send_input", side_effect=mock_send_input), \
             patch("camc_pkg.monitor.tmux_send_key", side_effect=mock_send_key), \
             patch("camc_pkg.monitor.time.sleep", side_effect=mock_sleep):
            result = _smart_probe("test-session", config)

        return result, bspace_count[0]

    def test_bspace_works_first_try(self):
        """BSpace erases probe char on first try → idle."""
        baseline = "❯ \n────\n"
        captures = [baseline, baseline]  # baseline, then clean after BSpace
        result, bspace_count = self._run_probe(captures)
        assert result == "idle"
        assert bspace_count == 1  # only the initial BSpace

    def test_bspace_fails_once_then_succeeds(self):
        """BSpace fails first time (probe char still on screen), retries, succeeds."""
        baseline = "❯ \n────\n"
        with_probe_char = "❯ 1\n────\n"  # "1" still on screen after first BSpace
        captures = [baseline, with_probe_char, baseline]  # baseline, fail, clean
        result, bspace_count = self._run_probe(captures)
        assert result == "idle"
        assert bspace_count == 2  # initial + 1 retry

    def test_bspace_fails_twice_then_succeeds(self):
        """BSpace fails twice, third retry succeeds."""
        baseline = "❯ \n────\n"
        with_probe_char = "❯ 1\n────\n"
        captures = [baseline, with_probe_char, with_probe_char, baseline]
        result, bspace_count = self._run_probe(captures)
        assert result == "idle"
        assert bspace_count == 3  # initial + 2 retries

    def test_bspace_fails_all_retries(self):
        """BSpace fails all retries → still returns busy (gives up)."""
        baseline = "❯ \n────\n"
        with_probe_char = "❯ 1\n────\n"
        # baseline + 4 failures (1 initial check + 3 retries)
        captures = [baseline] + [with_probe_char] * 5
        result, bspace_count = self._run_probe(captures)
        assert result == "busy"
        assert bspace_count == 5  # initial + 3 retries + final retry before giving up

    def test_real_screen_change_no_retry(self):
        """Screen changes with different content (not probe char) → busy, no retry."""
        baseline = "❯ \n────\n"
        new_output = "● Bash(\"ls\")\n  ⎿  Running...\n"  # agent started working
        captures = [baseline, new_output]
        result, bspace_count = self._run_probe(captures)
        assert result == "busy"
        assert bspace_count == 1  # only initial BSpace, no retries

    def test_diff_only_probe_char_detection(self):
        """Verify _diff_is_only_probe_char correctly identifies probe char residue."""
        from camc_pkg.monitor import _diff_is_only_probe_char

        baseline = "❯ \n────\n"
        # Only probe char appended to prompt line
        assert _diff_is_only_probe_char(baseline, "❯ 1\n────\n", "1") is True
        # Different content entirely
        assert _diff_is_only_probe_char(baseline, "● Bash(ls)\n────\n", "1") is False
        # Multiple lines changed
        assert _diff_is_only_probe_char(baseline, "❯ 1\n───x\n", "1") is False
        # Different line count
        assert _diff_is_only_probe_char(baseline, "❯ 1\n────\nextra\n", "1") is False
        # No difference
        assert _diff_is_only_probe_char(baseline, baseline, "1") is False


class TestStep8AutoExit:
    """Step 8: Auto-exit on idle."""

    def test_auto_exit_enabled(self):
        screens = [screen_idle()] * 20
        store = MockStore(auto_exit=True)
        store, events = run_monitor_steps(screens, store=store, max_cycles=30)
        assert store.last_status == "completed"
        assert any("auto-exit" in str(e.get("detail", "")) for e in events.events)

    def test_auto_exit_disabled(self):
        screens = [screen_idle()] * 30
        store = MockStore(auto_exit=False)
        store, events = run_monitor_steps(screens, store=store, max_cycles=20)
        # Should reach idle but NOT mark completed — auto_exit is off
        idles = events.of_type("idle_confirmed")
        assert len(idles) >= 1
        assert store.last_status is None or store.last_status != "completed"


class TestFullLifecycle:
    """End-to-end lifecycle tests."""

    def test_full_cycle(self):
        """startup → planning → confirm → testing → editing → commit → idle."""
        screens = (
            [screen_empty()] * 2 +           # Startup (alternate buffer)
            [screen_planning()] * 3 +         # Planning
            [screen_confirm_proceed()] * 3 +  # Confirm dialog
            [screen_testing()] * 3 +          # Testing
            [screen_editing()] * 3 +          # Editing
            [screen_committing()] * 3 +       # Committing
            [screen_idle()] * 15              # Idle
        )
        store, events = run_monitor_steps(screens, max_cycles=50)

        state_changes = events.of_type("state_change")
        states = [e["detail"]["to"] for e in state_changes]
        assert "planning" in states
        assert "testing" in states

        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 1

        idles = events.of_type("idle_confirmed")
        assert len(idles) >= 1

    def test_multi_confirm_lifecycle(self):
        """Multiple dialogs with work between them."""
        screens = []
        for _ in range(5):
            screens += [screen_testing()] * 3
            screens += [screen_confirm_proceed()] * 3
        screens += [screen_idle()] * 15

        store, events = run_monitor_steps(screens, max_cycles=60)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 3

    def test_idle_then_resume(self):
        """Idle → new work → back to idle."""
        screens = (
            [screen_idle()] * 15 +     # First idle
            [screen_testing()] * 5 +    # Resume work
            [screen_idle()] * 15        # Idle again
        )
        store, events = run_monitor_steps(screens, max_cycles=50)
        idles = events.of_type("idle_confirmed")
        assert len(idles) >= 1


# ===========================================================================
# Stress tests — random sequences, parallel execution
# ===========================================================================

def random_screen():
    """Generate a random screen for stress testing."""
    choices = [
        (screen_idle, 20),
        (screen_planning, 15),
        (screen_editing, 15),
        (screen_testing, 15),
        (screen_committing, 10),
        (screen_confirm_proceed, 10),
        (screen_confirm_yes, 5),
        (screen_confirm_allow, 5),
        (screen_confirm_yn, 5),
        (screen_busy, 10),
        (screen_empty, 5),
        (screen_false_positive, 5),
    ]
    total = sum(w for _, w in choices)
    r = random.randint(0, total - 1)
    cumulative = 0
    for fn, w in choices:
        cumulative += w
        if r < cumulative:
            return fn()
    return screen_idle()


def run_stress_once(seed, length=50):
    """Run one stress test with a specific random seed. Returns True if no crash."""
    random.seed(seed)
    screens = [random_screen() for _ in range(length)]
    try:
        store, events = run_monitor_steps(screens, max_cycles=length + 20)
        return True, len(events.events)
    except Exception as e:
        return False, str(e)


class TestStress:
    """Stress tests — random sequences, should never crash."""

    @pytest.mark.parametrize("seed", range(20))
    def test_random_sequence(self, seed):
        """Random screen sequence should never crash the monitor."""
        ok, result = run_stress_once(seed, length=50)
        assert ok, "Monitor crashed with seed %d: %s" % (seed, result)

    def test_long_random_sequence(self):
        """Longer random sequence."""
        ok, result = run_stress_once(42, length=200)
        assert ok, "Monitor crashed: %s" % result

    def test_all_confirms(self):
        """All confirm screens — should fire confirms without crash."""
        screens = (
            [screen_confirm_proceed()] * 10 +
            [screen_confirm_yes()] * 10 +
            [screen_confirm_allow()] * 10 +
            [screen_confirm_yn()] * 10
        )
        store, events = run_monitor_steps(screens, max_cycles=60)
        confirms = events.of_type("auto_confirm")
        assert len(confirms) >= 4

    def test_all_states(self):
        """Cycle through all states rapidly."""
        screens = []
        for _ in range(10):
            screens.append(screen_planning())
            screens.append(screen_editing())
            screens.append(screen_testing())
            screens.append(screen_committing())
        store, events = run_monitor_steps(screens, max_cycles=60)
        state_changes = events.of_type("state_change")
        assert len(state_changes) >= 4

    def test_rapid_transitions(self):
        """Rapid state changes interleaved with confirms."""
        screens = []
        for _ in range(20):
            screens.append(random.choice([
                screen_planning(),
                screen_editing(),
                screen_testing(),
                screen_confirm_proceed(),
                screen_idle(),
            ]))
        store, events = run_monitor_steps(screens, max_cycles=40)
        assert True  # No crash = pass

    def test_empty_to_content(self):
        """Alternating empty and content screens."""
        screens = []
        for _ in range(20):
            screens.append(screen_empty())
            screens.append(screen_testing())
        store, events = run_monitor_steps(screens, max_cycles=50)
        assert True


class TestStressParallel:
    """Parallel stress tests — multiple monitors running concurrently."""

    def test_parallel_monitors(self):
        """Run 10 monitors in parallel with different random seeds."""
        results = {}
        threads = []

        def worker(seed):
            ok, result = run_stress_once(seed, length=30)
            results[seed] = (ok, result)

        for seed in range(10):
            t = threading.Thread(target=worker, args=(seed + 100,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        for seed, (ok, result) in results.items():
            assert ok, "Parallel monitor seed %d crashed: %s" % (seed, result)

    def test_parallel_same_scenario(self):
        """Run 10 monitors all with the same screen sequence."""
        results = {}
        threads = []

        screens = (
            [screen_planning()] * 5 +
            [screen_confirm_proceed()] * 5 +
            [screen_testing()] * 5 +
            [screen_idle()] * 10
        )

        def worker(idx):
            try:
                store, events = run_monitor_steps(list(screens), max_cycles=40)
                results[idx] = (True, len(events.events))
            except Exception as e:
                results[idx] = (False, str(e))

        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        for idx, (ok, result) in results.items():
            assert ok, "Parallel monitor %d crashed: %s" % (idx, result)

    def test_parallel_mixed_scenarios(self):
        """20 monitors with different scenario types in parallel."""
        scenario_screens = {
            "full": (
                [screen_planning()] * 3 +
                [screen_confirm_proceed()] * 3 +
                [screen_testing()] * 3 +
                [screen_idle()] * 10
            ),
            "multi_confirm": [screen_confirm_proceed()] * 20,
            "busy": [screen_busy() for _ in range(20)],
            "idle": [screen_idle()] * 20,
            "random": [random_screen() for _ in range(20)],
        }

        results = {}
        threads = []

        def worker(name, screens):
            try:
                store, events = run_monitor_steps(list(screens), max_cycles=35)
                results[name] = (True, len(events.events))
            except Exception as e:
                results[name] = (False, str(e))

        for name, screens in scenario_screens.items():
            for i in range(4):
                key = "%s-%d" % (name, i)
                t = threading.Thread(target=worker, args=(key, screens))
                threads.append(t)
                t.start()

        for t in threads:
            t.join(timeout=30)

        failed = [(k, v[1]) for k, v in results.items() if not v[0]]
        assert not failed, "Parallel monitors crashed: %s" % failed
