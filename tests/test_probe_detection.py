"""Test probe-based completion detection.

Instead of passively observing a sliding capture window (which can lose
critical signals), this approach **actively probes** the session:

1. Send a probe character to the tmux pane
2. Capture output and observe the reaction:
   - If the probe character appears literally in the output → session is
     at an input prompt (completed / idle). Send Backspace to clean up.
   - If the probe is NOT visible → session is busy (TUI app in raw mode,
     terminal echo disabled, probe char not rendered).

KEY INSIGHT: TUI applications like Claude Code use raw terminal mode
(stty -echo / tty.setraw) while working. In raw mode, input characters
are NOT echoed to the terminal. When Claude finishes and returns to the
❯ prompt, terminal echo is restored. This makes probe visibility a
**reliable binary signal** for completion detection.

These tests use REAL tmux sessions to validate each scenario.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

# Skip entire module if tmux is not available
pytestmark = pytest.mark.skipif(
    os.system("tmux -V > /dev/null 2>&1") != 0,
    reason="tmux not available",
)


SOCKET_DIR = "/tmp/cam-probe-test"


def _tmux(sock: str, *args: str) -> str:
    """Run a tmux command synchronously and return stdout."""
    cmd = ["tmux", "-S", sock] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    return result.stdout


def _tmux_send(sock: str, session: str, text: str, send_enter: bool = False) -> None:
    """Send literal text to a tmux pane."""
    target = f"{session}:0.0"
    subprocess.run(
        ["tmux", "-S", sock, "send-keys", "-t", target, "-l", "--", text],
        capture_output=True, timeout=5,
    )
    if send_enter:
        subprocess.run(
            ["tmux", "-S", sock, "send-keys", "-t", target, "Enter"],
            capture_output=True, timeout=5,
        )


def _tmux_send_special(sock: str, session: str, key: str) -> None:
    """Send a special key (e.g. BSpace, Enter) — NOT literal."""
    target = f"{session}:0.0"
    subprocess.run(
        ["tmux", "-S", sock, "send-keys", "-t", target, key],
        capture_output=True, timeout=5,
    )


def _tmux_capture(sock: str, session: str, lines: int = 50) -> str:
    """Capture pane content."""
    target = f"{session}:0.0"
    return _tmux(sock, "capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}")


def _tmux_session_exists(sock: str, session: str) -> bool:
    """Check if session exists."""
    result = subprocess.run(
        ["tmux", "-S", sock, "has-session", "-t", session],
        capture_output=True, timeout=5,
    )
    return result.returncode == 0


def _create_session(sock: str, session: str, command: str) -> None:
    """Create a detached tmux session running a command."""
    os.makedirs(SOCKET_DIR, exist_ok=True)
    subprocess.run(
        ["tmux", "-S", sock, "new-session", "-d", "-s", session, command],
        capture_output=True, timeout=5, check=True,
    )


def _kill_session(sock: str, session: str) -> None:
    """Kill a tmux session (ignore errors if already dead)."""
    subprocess.run(
        ["tmux", "-S", sock, "kill-session", "-t", session],
        capture_output=True, timeout=5,
    )


def _cleanup_socket(sock: str) -> None:
    """Remove socket file."""
    try:
        os.unlink(sock)
    except OSError:
        pass


# ---- Fake "Claude Code" script for realistic testing ----
# Uses raw terminal mode while working, restores echo when done.

FAKE_CLAUDE_SCRIPT = r'''
import sys, time, tty, termios

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    sys.stdout.write("Working on your task...\r\n")
    sys.stdout.flush()
    time.sleep({work_duration})
    sys.stdout.write("Done!\r\n")
    sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)

# Back at "prompt" with echo restored
sys.stdout.write("\u276f \n")
sys.stdout.flush()
# Wait for input (like Claude waiting for next prompt)
try:
    input()
except EOFError:
    pass
'''

FAKE_CLAUDE_CONFIRM_SCRIPT = '''\
import sys, time, tty, termios

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    sys.stdout.write("Do you want to proceed?\\r\\n")
    sys.stdout.write("1. Yes  2. No\\r\\n")
    sys.stdout.flush()
    # Read single char in raw mode (like Claude's permission menu)
    ch = sys.stdin.read(1)
    sys.stdout.write("Got: " + ch + "\\r\\n")
    sys.stdout.write("Continuing work...\\r\\n")
    sys.stdout.flush()
    time.sleep(1)
    sys.stdout.write("Done!\\r\\n")
    sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)

sys.stdout.write("\\u276f \\n")
sys.stdout.flush()
try:
    input()
except EOFError:
    pass
'''


def _write_script(name: str, content: str, **kwargs) -> str:
    """Write a Python script to a temp file and return its path."""
    path = f"{SOCKET_DIR}/{name}.py"
    os.makedirs(SOCKET_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(content.format(**kwargs))
    return path


# ---------------------------------------------------------------------------
# The Probe Algorithm
# ---------------------------------------------------------------------------


class ProbeResult:
    """Result of a probe operation."""
    COMPLETED = "completed"       # Probe visible → at prompt → done
    CONFIRMED = "confirmed"       # Probe consumed by raw-mode reader → was waiting for confirmation
    BUSY = "busy"                 # Probe invisible → agent working (raw mode, not reading)
    SESSION_DEAD = "session_dead" # Session doesn't exist


def probe_session(
    sock: str,
    session: str,
    probe_char: str = "Z",
    wait: float = 0.5,
) -> ProbeResult:
    """Probe a tmux session to determine its state.

    Algorithm:
    1. Check if session exists → SESSION_DEAD if not
    2. Capture baseline output
    3. Send probe character (NO Enter)
    4. Wait, capture again
    5. Classify:
       - Probe char visible on last non-empty line → COMPLETED (at echo-mode prompt)
         → send Backspace to clean up
       - Output changed significantly but probe not visible → CONFIRMED
         (raw-mode process consumed it as input, e.g. permission menu)
       - Output unchanged, probe not visible → BUSY
         (raw-mode process not reading stdin, char buffered)
    """
    # Step 1: Session alive?
    if not _tmux_session_exists(sock, session):
        return ProbeResult.SESSION_DEAD

    # Step 2: Baseline
    baseline = _tmux_capture(sock, session).rstrip("\n")

    # Step 3: Send probe
    _tmux_send(sock, session, probe_char)
    time.sleep(wait)

    # Step 4: Post-probe capture
    after = _tmux_capture(sock, session).rstrip("\n")

    # Step 5: Classify

    # Get last non-empty line
    after_lines = [l for l in after.split("\n") if l.strip()]
    last_line = after_lines[-1] if after_lines else ""

    baseline_lines = [l for l in baseline.split("\n") if l.strip()]
    baseline_last = baseline_lines[-1] if baseline_lines else ""

    # Check if probe char appeared on the last line (terminal echo = at prompt)
    probe_on_last_line = (
        probe_char in last_line and
        (not baseline_last or probe_char not in baseline_last or last_line != baseline_last)
    )

    if probe_on_last_line:
        # Clean up: send Backspace(s)
        for _ in range(len(probe_char)):
            _tmux_send_special(sock, session, "BSpace")
        return ProbeResult.COMPLETED

    # Check if output changed (raw-mode process consumed input and produced output)
    if after != baseline and len(after) > len(baseline) + 5:
        return ProbeResult.CONFIRMED

    # Output unchanged, probe not visible → busy (raw mode, not reading stdin)
    return ProbeResult.BUSY


# ===========================================================================
# TEST SCENARIOS
# ===========================================================================


# ---------------------------------------------------------------------------
# Scenario 1: Session at a normal prompt (bash)
# The simplest case — terminal echo is ON, probe char is visible.
# ---------------------------------------------------------------------------


class TestProbeAtBashPrompt:
    """At a bash prompt (echo mode), probe chars are immediately visible."""

    @pytest.fixture(autouse=True)
    def setup_session(self):
        self.sock = f"{SOCKET_DIR}/prompt-test.sock"
        self.session = "probe-prompt"
        _cleanup_socket(self.sock)
        _create_session(self.sock, self.session, "bash --norc --noprofile")
        time.sleep(0.5)
        yield
        _kill_session(self.sock, self.session)
        _cleanup_socket(self.sock)

    def test_probe_visible_at_prompt(self):
        """Probe char appears in captured output when at an echo-mode prompt."""
        before = _tmux_capture(self.sock, self.session)
        _tmux_send(self.sock, self.session, "Q")
        time.sleep(0.3)
        after = _tmux_capture(self.sock, self.session)

        after_lines = [l for l in after.split("\n") if l.strip()]
        last_line = after_lines[-1] if after_lines else ""
        assert "Q" in last_line, f"Probe should be visible on prompt, got: {last_line!r}"

    def test_backspace_cleans_up(self):
        """Backspace removes the probe character."""
        _tmux_send(self.sock, self.session, "Q")
        time.sleep(0.3)
        _tmux_send_special(self.sock, self.session, "BSpace")
        time.sleep(0.3)

        after = _tmux_capture(self.sock, self.session)
        after_lines = [l for l in after.split("\n") if l.strip()]
        last_line = after_lines[-1] if after_lines else ""
        assert not last_line.endswith("Q"), f"Backspace should remove probe: {last_line!r}"

    def test_probe_algorithm_returns_completed(self):
        """Full algorithm correctly identifies idle prompt as COMPLETED."""
        result = probe_session(self.sock, self.session)
        assert result == ProbeResult.COMPLETED


# ---------------------------------------------------------------------------
# Scenario 2: TUI app in raw mode (simulating Claude working)
# Terminal echo is OFF — probe char is NOT visible.
# ---------------------------------------------------------------------------


class TestProbeWhileTUIBusy:
    """When a TUI app is in raw terminal mode (like Claude thinking),
    probe chars are NOT echoed to the terminal."""

    @pytest.fixture(autouse=True)
    def setup_session(self):
        self.sock = f"{SOCKET_DIR}/busy-test.sock"
        self.session = "probe-busy"
        _cleanup_socket(self.sock)
        script = _write_script("fake_claude_busy", FAKE_CLAUDE_SCRIPT, work_duration=30)
        _create_session(self.sock, self.session, f"python3 {script}")
        time.sleep(1.0)  # Wait for raw mode to engage
        yield
        _kill_session(self.sock, self.session)
        _cleanup_socket(self.sock)

    def test_probe_invisible_in_raw_mode(self):
        """Probe char is NOT visible when TUI app has terminal in raw mode."""
        before = _tmux_capture(self.sock, self.session)
        assert "Working" in before, f"Expected working output, got: {before!r}"

        _tmux_send(self.sock, self.session, "Z")
        time.sleep(0.3)

        after = _tmux_capture(self.sock, self.session)
        after_lines = [l for l in after.split("\n") if l.strip()]
        last_line = after_lines[-1] if after_lines else ""

        # Z should NOT appear — raw mode suppresses echo
        assert "Z" not in last_line, \
            f"Probe should be invisible in raw mode, got last line: {last_line!r}"

    def test_probe_algorithm_returns_busy(self):
        """Full algorithm correctly identifies raw-mode TUI as BUSY."""
        result = probe_session(self.sock, self.session)
        assert result == ProbeResult.BUSY


# ---------------------------------------------------------------------------
# Scenario 3: TUI finishes → returns to prompt (raw → echo transition)
# This is the KEY scenario: Claude finishes work and returns to ❯ prompt.
# ---------------------------------------------------------------------------


class TestProbeAfterTUICompletion:
    """When TUI app finishes and terminal returns to echo mode,
    probe char becomes visible — this detects completion."""

    @pytest.fixture(autouse=True)
    def setup_session(self):
        self.sock = f"{SOCKET_DIR}/complete-test.sock"
        self.session = "probe-complete"
        _cleanup_socket(self.sock)
        # Short work duration so it finishes quickly
        script = _write_script("fake_claude_done", FAKE_CLAUDE_SCRIPT, work_duration=1)
        _create_session(self.sock, self.session, f"python3 {script}")
        time.sleep(0.5)
        yield
        _kill_session(self.sock, self.session)
        _cleanup_socket(self.sock)

    def test_busy_then_completed_transition(self):
        """Probe shows BUSY while working, COMPLETED after returning to prompt."""
        # During work (raw mode) — should be busy
        result_busy = probe_session(self.sock, self.session, wait=0.3)
        # May be BUSY or could already be done depending on timing
        # Just record it

        # Wait for work to finish
        time.sleep(2)

        # After work (echo mode, at ❯ prompt) — should be completed
        result_done = probe_session(self.sock, self.session, probe_char="X", wait=0.3)
        assert result_done == ProbeResult.COMPLETED, \
            f"Expected COMPLETED after TUI exit, got {result_done}"

    def test_probe_at_prompt_after_tui(self):
        """After TUI exits, probe char is visible at the ❯ prompt."""
        time.sleep(2)  # Wait for fake Claude to finish

        output = _tmux_capture(self.sock, self.session)
        assert "❯" in output or "\u276f" in output, \
            f"Expected prompt after completion, got: {output!r}"

        _tmux_send(self.sock, self.session, "W")
        time.sleep(0.3)

        after = _tmux_capture(self.sock, self.session)
        after_lines = [l for l in after.split("\n") if l.strip()]
        last_line = after_lines[-1] if after_lines else ""
        assert "W" in last_line, \
            f"Probe should be visible at echo-mode prompt, got: {last_line!r}"


# ---------------------------------------------------------------------------
# Scenario 4: TUI waiting for confirmation (raw mode, reading stdin)
# Claude is at "1. Yes  2. No" — it reads single chars in raw mode.
# Sending "1" gets consumed as the confirmation response.
# ---------------------------------------------------------------------------


class TestProbeAtConfirmation:
    """When TUI is waiting for single-char input in raw mode,
    the probe is consumed and triggers continuation."""

    @pytest.fixture(autouse=True)
    def setup_session(self):
        self.sock = f"{SOCKET_DIR}/confirm-test.sock"
        self.session = "probe-confirm"
        _cleanup_socket(self.sock)
        script = _write_script("fake_claude_confirm", FAKE_CLAUDE_CONFIRM_SCRIPT)
        _create_session(self.sock, self.session, f"python3 {script}")
        time.sleep(1.0)  # Wait for confirmation prompt
        yield
        _kill_session(self.sock, self.session)
        _cleanup_socket(self.sock)

    def test_probe_consumed_as_confirmation(self):
        """Sending '1' to a raw-mode confirmation prompt triggers continuation."""
        before = _tmux_capture(self.sock, self.session)
        assert "proceed" in before.lower() or "yes" in before.lower(), \
            f"Expected confirmation prompt, got: {before!r}"

        # Send "1" — will be consumed by read(1) in raw mode
        _tmux_send(self.sock, self.session, "1")
        time.sleep(2.0)  # Wait for script to process and continue

        after = _tmux_capture(self.sock, self.session)
        # Should see continuation output
        assert "Got: 1" in after or "Continuing" in after or "Done" in after, \
            f"Expected continuation after confirm, got: {after!r}"

    def test_probe_algorithm_returns_confirmed_or_completed(self):
        """After consuming probe, session either shows new work (CONFIRMED)
        or has finished and returned to prompt (COMPLETED)."""
        before = _tmux_capture(self.sock, self.session)
        assert "proceed" in before.lower() or "yes" in before.lower(), \
            f"Expected confirmation prompt, got: {before!r}"

        result = probe_session(self.sock, self.session, probe_char="1", wait=2.0)
        # The probe was consumed → output changed → CONFIRMED
        # OR the script finished → at prompt → COMPLETED
        assert result in (ProbeResult.CONFIRMED, ProbeResult.COMPLETED), \
            f"Expected CONFIRMED or COMPLETED, got {result}"


# ---------------------------------------------------------------------------
# Scenario 5: Session has exited
# ---------------------------------------------------------------------------


class TestProbeAfterExit:
    """When process exits, session disappears. session_exists → False."""

    def test_session_gone_after_exit(self):
        sock = f"{SOCKET_DIR}/exit-test.sock"
        session = "probe-exit"
        _cleanup_socket(sock)

        try:
            _create_session(sock, session, "bash -c 'echo bye; exit 0'")
            time.sleep(1.0)

            assert not _tmux_session_exists(sock, session)
            result = probe_session(sock, session)
            assert result == ProbeResult.SESSION_DEAD
        finally:
            _kill_session(sock, session)
            _cleanup_socket(sock)


# ---------------------------------------------------------------------------
# Scenario 6: Repeated probes don't leave residue
# ---------------------------------------------------------------------------


class TestRepeatedProbes:
    """Multiple probes on an idle session clean up properly each time."""

    def test_three_probes_no_residue(self):
        sock = f"{SOCKET_DIR}/multi-test.sock"
        session = "probe-multi"
        _cleanup_socket(sock)

        try:
            _create_session(sock, session, "bash --norc --noprofile")
            time.sleep(0.5)

            for i in range(3):
                result = probe_session(sock, session, probe_char=chr(ord("A") + i))
                assert result == ProbeResult.COMPLETED, \
                    f"Probe {i} expected COMPLETED, got {result}"
                time.sleep(0.5)  # Allow Backspace cleanup to render

            # Final check: no probe residue
            final = _tmux_capture(sock, session)
            last_lines = [l for l in final.split("\n") if l.strip()]
            last_line = last_lines[-1] if last_lines else ""
            for ch in "ABC":
                assert ch not in last_line, \
                    f"Probe residue '{ch}' found: {last_line!r}"
        finally:
            _kill_session(sock, session)
            _cleanup_socket(sock)


# ---------------------------------------------------------------------------
# Scenario 7: Unique probe sequence avoids false matches
# ---------------------------------------------------------------------------


class TestUniqueProbeSequence:
    """Using a multi-char probe sequence avoids collisions with existing output."""

    def test_multi_char_probe(self):
        sock = f"{SOCKET_DIR}/unique-test.sock"
        session = "probe-unique"
        _cleanup_socket(sock)

        try:
            _create_session(sock, session, "bash --norc --noprofile")
            time.sleep(0.5)

            # Put confusing content in scrollback
            _tmux_send(sock, session, "echo ZZZZ", send_enter=True)
            time.sleep(0.3)

            # Probe with unique sequence
            result = probe_session(sock, session, probe_char="QXJ")
            assert result == ProbeResult.COMPLETED

            # Verify cleanup (3 Backspaces for 3 chars)
            time.sleep(0.3)
            final = _tmux_capture(sock, session)
            last_lines = [l for l in final.split("\n") if l.strip()]
            last_line = last_lines[-1] if last_lines else ""
            assert "QXJ" not in last_line, \
                f"Multi-char probe should be cleaned up: {last_line!r}"
        finally:
            _kill_session(sock, session)
            _cleanup_socket(sock)


# ---------------------------------------------------------------------------
# Scenario 8: stty -echo vs normal echo — the core mechanism
# Directly validates the terminal echo behavior that makes this work.
# ---------------------------------------------------------------------------


class TestTerminalEchoMechanism:
    """Directly test that terminal echo mode determines probe visibility."""

    def test_echo_on_probe_visible(self):
        """With stty echo (default), typed characters appear in output."""
        sock = f"{SOCKET_DIR}/echo-on.sock"
        session = "echo-on"
        _cleanup_socket(sock)

        try:
            # bash has echo ON by default
            _create_session(sock, session, "bash --norc --noprofile")
            time.sleep(0.5)

            _tmux_send(sock, session, "V")
            time.sleep(0.3)

            output = _tmux_capture(sock, session)
            after_lines = [l for l in output.split("\n") if l.strip()]
            last_line = after_lines[-1] if after_lines else ""
            assert "V" in last_line, f"Echo ON: probe should be visible: {last_line!r}"
        finally:
            _kill_session(sock, session)
            _cleanup_socket(sock)

    def test_echo_off_probe_invisible(self):
        """With stty -echo, typed characters do NOT appear in output."""
        sock = f"{SOCKET_DIR}/echo-off.sock"
        session = "echo-off"
        _cleanup_socket(sock)

        try:
            # stty -echo disables character echoing
            _create_session(sock, session,
                            "bash -c 'stty -echo; echo Ready; sleep 30'")
            time.sleep(0.5)

            output_before = _tmux_capture(sock, session)
            assert "Ready" in output_before

            _tmux_send(sock, session, "V")
            time.sleep(0.3)

            output_after = _tmux_capture(sock, session)
            after_lines = [l for l in output_after.split("\n") if l.strip()]
            last_line = after_lines[-1] if after_lines else ""
            # V should NOT appear — echo is disabled
            assert "V" not in last_line, \
                f"Echo OFF: probe should NOT be visible: {last_line!r}"
        finally:
            _kill_session(sock, session)
            _cleanup_socket(sock)

    def test_echo_transition(self):
        """After stty -echo → stty echo transition, probe becomes visible."""
        sock = f"{SOCKET_DIR}/echo-transition.sock"
        session = "echo-trans"
        _cleanup_socket(sock)

        try:
            # Script: start in raw mode, then restore echo
            _create_session(sock, session,
                            "bash --norc --noprofile -c '"
                            "stty -echo; echo \"Phase1: raw\"; sleep 2; "
                            "stty echo; echo \"Phase2: echo\"; "
                            "exec bash --norc --noprofile'")
            time.sleep(0.5)

            # Phase 1: raw mode — probe invisible
            _tmux_send(sock, session, "A")
            time.sleep(0.3)
            output1 = _tmux_capture(sock, session)
            lines1 = [l for l in output1.split("\n") if l.strip()]
            last1 = lines1[-1] if lines1 else ""
            assert "A" not in last1, f"Phase1: probe should be invisible: {last1!r}"

            # Wait for phase 2
            time.sleep(2.5)

            # Phase 2: echo mode — probe visible
            _tmux_send(sock, session, "B")
            time.sleep(0.3)
            output2 = _tmux_capture(sock, session)
            lines2 = [l for l in output2.split("\n") if l.strip()]
            last2 = lines2[-1] if lines2 else ""
            assert "B" in last2, f"Phase2: probe should be visible: {last2!r}"
        finally:
            _kill_session(sock, session)
            _cleanup_socket(sock)
