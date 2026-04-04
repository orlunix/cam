#!/usr/bin/env python3
"""Fake Claude Code agent for testing the monitor loop.

Simulates Claude Code's TUI behavior: permission dialogs, state transitions,
idle prompt, completion, and edge cases. Reads stdin for keypresses (e.g. "1"
from auto-confirm, BSpace from probe).

Usage:
    python3 tests/fake_agent.py [scenario]

Scenarios:
    full        - Full lifecycle: startup → work → dialog → work → idle (default)
    confirm     - Permission dialog that waits for "1"
    multi       - Multiple dialogs in sequence
    false_pos   - Agent prose containing confirm keywords (false positive test)
    idle        - Go straight to idle prompt
    busy        - Stay busy forever (never idle)
    stuck       - Dialog that never gets confirmed (test stuck detection)
    scrollback  - Dialog followed by output that keeps old text in scrollback
    random      - Random output with occasional patterns mixed in
    rapid       - Rapid confirm dialogs back-to-back (test cooldown)
    yn          - y/n style confirmation prompts
    mixed       - Mix of real dialogs and false-positive prose

Run in tmux for monitor testing:
    tmux new-session -d -s test-agent "python3 tests/fake_agent.py full"
    python3 -m camc_pkg._monitor <agent_id>
"""

import os
import random
import select
import sys
import time

# --- Terminal helpers ---

def write(text):
    """Write text to stdout (the terminal)."""
    sys.stdout.write(text)
    sys.stdout.flush()

def writeln(text=""):
    write(text + "\n")

def read_input(timeout=0.1):
    """Read available stdin (non-blocking). Returns chars or empty string."""
    if select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return ""

def drain_input():
    """Drain all pending input."""
    while select.select([sys.stdin], [], [], 0.01)[0]:
        sys.stdin.read(1)

def wait_for_key(key="1", timeout=None):
    """Wait for a specific keypress. Returns True if received, False if timeout."""
    start = time.time()
    while True:
        ch = read_input(0.5)
        if ch == key:
            return True
        if ch == "\x7f":  # BSpace
            pass  # ignore backspace while waiting
        if timeout and time.time() - start > timeout:
            return False

def clear_screen():
    write("\033[2J\033[H")


# --- TUI Components ---

SEPARATOR = "\u2500" * 100

def prompt():
    """Show the Claude Code idle prompt."""
    writeln(SEPARATOR)
    writeln("\u276f ")  # ❯
    writeln(SEPARATOR)

def tool_call(tool, args):
    """Show a tool call line."""
    writeln("\u25cf %s(%s)" % (tool, args))

def task_summary(verb, duration):
    """Show completion summary: ✻ Verb for duration."""
    writeln("\u273b %s for %s" % (verb, duration))

def permission_dialog(command, description=""):
    """Show a numbered permission dialog and wait for '1'."""
    writeln(SEPARATOR)
    writeln(" Bash command")
    writeln()
    writeln("   %s" % command)
    if description:
        writeln("   %s" % description)
    writeln()
    writeln(" Permission rule Bash requires confirmation for this command.")
    writeln()
    writeln(" Do you want to proceed?")
    writeln(" \u276f 1. Yes")
    writeln("   2. No")
    writeln()
    writeln(" Esc to cancel \u00b7 Tab to amend \u00b7 ctrl+e to explain")

def allow_dialog():
    """Show an Allow once/always dialog."""
    writeln(SEPARATOR)
    writeln(" Allow once")
    writeln(" Always allow")
    writeln(" Deny")
    writeln()
    writeln(" Esc to cancel")

def yn_dialog(question):
    """Show a y/n confirmation prompt."""
    writeln("%s (y/n)" % question)

def thinking(duration_s=3, label="Thinking"):
    """Simulate thinking with a spinner."""
    symbols = ["\u25d0", "\u25d1", "\u25d2", "\u25d3"]
    start = time.time()
    i = 0
    while time.time() - start < duration_s:
        write("\r%s %s..." % (symbols[i % 4], label))
        sys.stdout.flush()
        # Check for input during thinking (ignore it — agent is busy)
        ch = read_input(0.3)
        i += 1
    writeln()

def agent_output(lines):
    """Write multiple lines of agent output."""
    for line in lines:
        writeln(line)
        time.sleep(0.05)

def random_prose():
    """Generate random agent prose that might contain tricky keywords."""
    prose = [
        "Looking at the code structure, I can see several patterns here.",
        "The function processes the input and returns the result.",
        "Let me analyze the test output to understand what's happening.",
        "This implementation handles edge cases like empty arrays.",
        "The configuration file specifies the following options:",
        "Based on my analysis, the bug is in the error handling logic.",
        "I'll create a helper function to simplify this code.",
        "The database query returns all matching records.",
        "Let me check if there are any related test failures.",
        "The API endpoint accepts POST requests with JSON body.",
        "Refactoring the module to improve readability.",
        "The deployment script needs to be updated for the new version.",
        "Running the linter to check for style violations.",
        "The CI pipeline shows all tests passing on main branch.",
        "I need to update the documentation to reflect these changes.",
    ]
    return random.choice(prose)

def false_positive_prose():
    """Agent prose that contains confirm keywords but is NOT a real dialog."""
    prose = [
        # Contains "1. Yes" but in a table/list context
        "The options are:\n  1. Yes, use the new API\n  2. No, keep the old one\nI recommend option 1. Yes is better because...",
        # Contains "Do you want to proceed" but in agent's explanation
        "The script asks 'Do you want to proceed?' before deleting files.\nThis is a safety check that we should keep.",
        # Contains "Allow once" in a description
        "The permission system shows 'Allow once' or 'Always allow' to the user.\nWe should handle both cases in our tests.",
        # Contains "(y/n)" in code output
        'The prompt shows "Continue? (y/n)" and waits for user input.\nOur handler should parse this correctly.',
        # Contains "1. Allow" in a numbered list
        "Configuration options:\n  1. Allow all connections\n  2. Block unknown hosts\n  3. Ask for each connection",
        # Contains keywords buried in a long output
        "Looking at the test results:\n  - test_confirm: PASSED\n  - test_allow_once: PASSED\n  - test_yn_prompt: PASSED\nAll 1. Yes all tests pass. Do you want to proceed with the merge?",
    ]
    return random.choice(prose)


# --- Scenarios ---

def scenario_full():
    """Full lifecycle: startup → work → dialog → work → idle."""
    writeln("claude v4.2.1")
    writeln()
    time.sleep(1)

    # Ready prompt
    prompt()
    time.sleep(0.5)

    # User sends task (simulated)
    writeln("\u276f fix the bug in parser.py")
    writeln()

    # Planning phase
    thinking(3, "Analyzing")
    tool_call("Read", '"src/parser.py"')
    writeln("  \u23bf  Reading file...")
    time.sleep(1)
    agent_output([
        "  \u23bf  def parse_input(data):",
        "         if not data:",
        "             return None",
        "         ...",
    ])
    tool_call("Grep", '"parse_error", path="src/"')
    time.sleep(1)

    # Permission dialog
    permission_dialog("python3 -m pytest tests/test_parser.py", "Run parser tests")
    wait_for_key("1", timeout=60)
    drain_input()

    # Testing phase — dialog consumed, agent resumes
    clear_screen()
    writeln("\u25cf Bash(python3 -m pytest tests/test_parser.py)")
    writeln("  \u23bf  Running...")
    thinking(4, "Running tests")
    agent_output([
        "  \u23bf  tests/test_parser.py::test_basic PASSED",
        "  \u23bf  tests/test_parser.py::test_edge FAILED",
        "  \u23bf  1 passed, 1 failed",
    ])

    # Editing phase
    tool_call("Edit", '"src/parser.py"')
    time.sleep(2)
    agent_output([
        "  \u23bf  Fixed the edge case handling in parse_input().",
    ])

    # Another dialog
    permission_dialog("python3 -m pytest tests/", "Run all tests")
    wait_for_key("1", timeout=60)
    drain_input()

    clear_screen()
    writeln("\u25cf Bash(python3 -m pytest tests/)")
    thinking(3, "Running tests")
    agent_output([
        "  \u23bf  All 15 tests passed!",
    ])

    # Committing
    tool_call("Bash", '"git add -A && git commit -m \\"Fix parser edge case\\""')
    time.sleep(2)
    agent_output([
        "  \u23bf  [main abc1234] Fix parser edge case",
        "  \u23bf   1 file changed, 3 insertions(+), 1 deletion(-)",
    ])

    # Completion — task summary + double prompt
    writeln()
    task_summary("Saut\u00e9ed", "1m 14s")
    writeln()
    prompt()  # First prompt (echo)
    prompt()  # Second prompt (idle)

    # Stay idle — respond to probes
    while True:
        ch = read_input(1.0)
        if ch == "1":
            # Probe: echo "1" at prompt position
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":  # BSpace
            write("\b \b")
            sys.stdout.flush()


def scenario_confirm():
    """Single permission dialog, wait for confirm."""
    prompt()
    time.sleep(1)

    writeln("\u276f run the tests")
    writeln()

    tool_call("Bash", '"pytest tests/"')
    writeln()
    permission_dialog("pytest tests/", "Run test suite")

    # Wait for "1" to confirm
    wait_for_key("1", timeout=300)
    drain_input()

    # Confirmed — resume work
    clear_screen()
    writeln("\u25cf Bash(pytest tests/)")
    thinking(3, "Running tests")
    agent_output(["  \u23bf  All tests passed!"])
    writeln()
    task_summary("Whisked", "32s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_multi():
    """Multiple dialogs in sequence with work between them."""
    prompt()
    time.sleep(0.5)
    writeln("\u276f debug and fix the issue")
    writeln()

    commands = [
        ("cat /var/log/app.log | tail -50", "Check logs"),
        ("docker ps -a", "List containers"),
        ("docker restart app-server", "Restart server"),
        ("curl localhost:8080/health", "Health check"),
        ("python3 -m pytest tests/ -x", "Run tests"),
    ]

    for cmd, desc in commands:
        tool_call("Bash", '"%s"' % cmd)
        writeln()
        permission_dialog(cmd, desc)
        if not wait_for_key("1", timeout=120):
            writeln("\n[TIMEOUT] Dialog not confirmed after 120s")
            return
        drain_input()

        # Work between dialogs
        clear_screen()
        writeln("\u25cf Bash(%s)" % cmd)
        thinking(random.uniform(1, 3))
        agent_output([
            "  \u23bf  %s" % random_prose(),
        ])
        writeln()

    task_summary("Baked", "2m 5s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_false_pos():
    """Agent prose containing confirm keywords — should NOT trigger auto-confirm."""
    prompt()
    time.sleep(0.5)
    writeln("\u276f explain the permission system")
    writeln()

    thinking(2, "Analyzing")

    # Output prose that contains confirm keywords
    for _ in range(10):
        writeln()
        writeln(false_positive_prose())
        time.sleep(1)

    writeln()
    task_summary("Crunched", "45s")
    prompt()
    prompt()

    # Idle — if monitor sends "1" here it's fine (probe).
    # But if it sent "1" during the prose above, that's a false positive.
    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_idle():
    """Go straight to idle prompt."""
    writeln("claude v4.2.1")
    task_summary("Saut\u00e9ed", "5s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_busy():
    """Stay busy forever — continuous output, never idle."""
    prompt()
    writeln("\u276f run infinite analysis")
    writeln()

    i = 0
    while True:
        i += 1
        tool_call("Read", '"file_%d.py"' % i)
        time.sleep(0.5)
        agent_output([
            "  \u23bf  %s" % random_prose(),
        ])
        if i % 5 == 0:
            thinking(2, "Analyzing batch %d" % i)


def scenario_stuck():
    """Dialog that never gets confirmed — test stuck detection."""
    prompt()
    writeln("\u276f deploy to production")
    writeln()
    tool_call("Bash", '"./deploy.sh --production"')
    writeln()
    permission_dialog("./deploy.sh --production", "Deploy to production")

    # Never consume "1" — the dialog stays forever.
    # Monitor should keep retrying every 5s.
    while True:
        ch = read_input(1.0)
        # Ignore all input — simulate a frozen/unresponsive dialog
        pass


def scenario_scrollback():
    """Dialog confirmed, then small output — old dialog text still in scrollback."""
    prompt()
    writeln("\u276f check the status")
    writeln()

    # Show dialog
    permission_dialog("df -h /home/", "Check disk space")
    wait_for_key("1", timeout=60)
    drain_input()

    # Small output — not enough to push dialog text off screen
    writeln()
    writeln("\u25cf Bash(df -h /home/)")
    writeln("  \u23bf  /dev/sda1  500G  250G  250G  50% /home")
    writeln()

    # The old "Do you want to proceed" text is still visible above
    # in the terminal scrollback. Monitor should NOT re-trigger.

    time.sleep(2)
    task_summary("Whisked", "8s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_random():
    """Random output with occasional patterns — stress test detection."""
    prompt()
    writeln("\u276f analyze the codebase")
    writeln()

    random_lines = [
        "Scanning directory structure...",
        "Found 47 Python files, 12 test files",
        "Checking imports and dependencies",
        "Module graph: 23 nodes, 45 edges",
        "No circular dependencies detected",
        "Coverage report: 78% overall",
        "Hotspot: src/core/engine.py (450 lines, 12 functions)",
        "Technical debt score: 3.2/10 (good)",
        "Documentation coverage: 45%",
        "Type annotation coverage: 62%",
        # These look like tool calls but aren't exactly
        "Reading file contents...",
        "Editing configuration...",
        "Running analysis...",
        "Checking test results...",
        # Near-miss confirm keywords
        "The dialog shows options to the user",
        "Users can choose to proceed or cancel",
        "Option 1 is recommended for most cases",
        "Always allow trusted certificates",
        "Do you want to continue reading? I'll explain more.",
        # Actual tool calls (should trigger state detection)
        "\u25cf Read(\"src/core/engine.py\")",
        "\u25cf Bash(\"wc -l src/**/*.py\")",
        "\u25cf Edit(\"src/config.py\")",
        "\u25cf Grep(\"TODO\", path=\"src/\")",
    ]

    for _ in range(50):
        line = random.choice(random_lines)
        writeln(line)
        time.sleep(random.uniform(0.1, 0.8))

        # Occasionally show a real dialog
        if random.random() < 0.1:
            permission_dialog("ls -la /tmp/", "List files")
            wait_for_key("1", timeout=30)
            drain_input()
            clear_screen()
            writeln("\u25cf Bash(ls -la /tmp/)")
            writeln("  \u23bf  total 48")

    task_summary("Baked", "3m 22s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_rapid():
    """Rapid confirm dialogs — test cooldown behavior."""
    prompt()
    writeln("\u276f fix all linting errors")
    writeln()

    # 10 dialogs with only 0.5s between them
    for i in range(10):
        cmd = "eslint --fix src/file_%d.js" % i
        permission_dialog(cmd, "Fix lint errors")
        wait_for_key("1", timeout=30)
        drain_input()
        # Very brief work before next dialog
        writeln("\u25cf Bash(%s)" % cmd)
        writeln("  \u23bf  Fixed 3 errors")
        time.sleep(0.5)

    task_summary("Crunched", "25s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_yn():
    """y/n confirmation prompts."""
    prompt()
    writeln("\u276f clean up the repository")
    writeln()

    tool_call("Bash", '"rm -rf node_modules/"')
    writeln()
    yn_dialog("Delete node_modules directory?")

    wait_for_key("y", timeout=60)
    drain_input()

    writeln("Deleted.")
    time.sleep(1)

    yn_dialog("Also remove .cache/? [Y/n]")
    wait_for_key("y", timeout=60)
    drain_input()

    writeln("Deleted.")
    writeln()
    task_summary("Whisked", "12s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


def scenario_mixed():
    """Mix of real dialogs and false-positive prose — the hardest test."""
    prompt()
    writeln("\u276f review the permission handling code")
    writeln()

    # Phase 1: Agent talks about permissions (false positive territory)
    thinking(2, "Analyzing")
    agent_output([
        "",
        "Looking at the permission system, I see several patterns:",
        "",
        "The code shows 'Do you want to proceed?' in the UI module.",
        "Users see options like:",
        "  1. Yes - accept the change",
        "  2. No - reject the change",
        "",
        "The 'Allow once' and 'Always allow' buttons are rendered by",
        "the Ink TUI framework. Let me check how they're wired up.",
        "",
    ])
    time.sleep(2)

    # Phase 2: Real tool call needing confirmation
    tool_call("Bash", '"grep -r \\"permission\\" src/"')
    writeln()
    permission_dialog('grep -r "permission" src/', "Search files")
    wait_for_key("1", timeout=60)
    drain_input()

    clear_screen()
    writeln('\u25cf Bash(grep -r "permission" src/)')
    agent_output([
        "  \u23bf  src/ui/dialog.py:  'Do you want to proceed?'",
        "  \u23bf  src/ui/dialog.py:  '1. Yes  2. No'",
        "  \u23bf  src/auth/policy.py: 'Allow once'",
        "  \u23bf  src/auth/policy.py: 'Always allow'",
    ])
    time.sleep(1)

    # Phase 3: More false-positive prose referencing the grep output
    agent_output([
        "",
        "Found the permission dialogs in the codebase. The grep output shows",
        "'Do you want to proceed?' appears in dialog.py. The '1. Yes' option",
        "is the default selection. The 'Allow once' flow differs from",
        "'Always allow' in that it doesn't persist the permission.",
        "",
        "Let me also check the (y/n) prompts in the CLI module.",
        "",
    ])
    time.sleep(2)

    # Phase 4: Another real dialog
    tool_call("Bash", '"python3 tests/test_permissions.py"')
    writeln()
    permission_dialog("python3 tests/test_permissions.py", "Run permission tests")
    wait_for_key("1", timeout=60)
    drain_input()

    clear_screen()
    writeln("\u25cf Bash(python3 tests/test_permissions.py)")
    thinking(2, "Running tests")
    agent_output(["  \u23bf  All 8 permission tests passed!"])
    writeln()
    task_summary("Baked", "1m 45s")
    prompt()
    prompt()

    while True:
        ch = read_input(1.0)
        if ch == "1":
            write("1")
            sys.stdout.flush()
        elif ch == "\x7f":
            write("\b \b")
            sys.stdout.flush()


# --- Main ---

SCENARIOS = {
    "full": scenario_full,
    "confirm": scenario_confirm,
    "multi": scenario_multi,
    "false_pos": scenario_false_pos,
    "idle": scenario_idle,
    "busy": scenario_busy,
    "stuck": scenario_stuck,
    "scrollback": scenario_scrollback,
    "random": scenario_random,
    "rapid": scenario_rapid,
    "yn": scenario_yn,
    "mixed": scenario_mixed,
}

if __name__ == "__main__":
    import tty
    import termios

    scenario = sys.argv[1] if len(sys.argv) > 1 else "full"
    if scenario not in SCENARIOS:
        print("Unknown scenario: %s" % scenario)
        print("Available: %s" % ", ".join(sorted(SCENARIOS.keys())))
        sys.exit(1)

    # Set terminal to raw mode so we can read individual keypresses
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        SCENARIOS[scenario]()
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
