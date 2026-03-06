#!/usr/bin/env python3
"""Fake coding CLI for E2E testing CAM's monitor pipeline.

Simulates Claude Code, Cursor, or Codex by printing output that matches
adapter patterns, then reading stdin for auto-confirm responses.

Usage:
    cam run generic "test prompt" \
        --command "python3 scripts/fake_coding_cli.py --tool claude --scenario full"
"""

import argparse
import sys
import time

# ---------------------------------------------------------------------------
# Tool profiles — patterns match the TOML adapter configs exactly
# ---------------------------------------------------------------------------

PROFILES = {
    "claude": {
        "ready": "❯",
        "tool_calls": [
            "● Read(/src/main.py)",
            "● Grep(pattern=\"TODO\", path=\"/src\")",
            "● Edit(/src/main.py)",
            "● Bash(pytest -x)",
        ],
        "trust_dialog": (
            "╭──────────────────────────────────────╮\n"
            "│  Do you trust the files in this       │\n"
            "│  folder?                              │\n"
            "│                                       │\n"
            "│  > Yes, trust this project            │\n"
            "│    No                                 │\n"
            "│                                       │\n"
            "│  Enter to confirm · Esc to cancel     │\n"
            "╰──────────────────────────────────────╯"
        ),
        "permission_prompt": (
            "╭──────────────────────────────────────╮\n"
            "│  Allow Bash(rm -rf /tmp/test)?        │\n"
            "│                                       │\n"
            "│  1. Yes  2. No                        │\n"
            "╰──────────────────────────────────────╯"
        ),
        "working_lines": [
            "I'll analyze the codebase and fix the bug.",
            "",
            "● Read(/src/main.py)",
            "",
            "  Looking at the code, I can see the issue is on line 42.",
            "  The variable `count` is used before initialization.",
            "",
            "● Edit(/src/main.py)",
            "",
            "  Fixed the initialization. Let me run the tests.",
            "",
            "● Bash(pytest -x)",
            "",
            "  All tests passing.",
        ],
        "completion_summary": "✻ Whisked code for 12s",
    },
    "cursor": {
        "ready": "→",
        "tool_calls": [
            "⬢ Read /src/main.py",
            "⬢ Grep TODO /src",
            "⬢ Edit /src/main.py",
            "$ pytest -x in /home/user/project",
        ],
        "trust_dialog": (
            "Trust this workspace?\n"
            "This folder contains files that may run code.\n"
            "\n"
            "[Trust] [Don't Trust]"
        ),
        "permission_prompt": (
            "Run command: pytest -x\n"
            "Run (once) (y) / Run (always) / Deny (n)"
        ),
        "working_lines": [
            "I'll look at the code and fix the issue.",
            "",
            "⬢ Read /src/main.py",
            "",
            "  The bug is on line 42 — uninitialized variable.",
            "",
            "⬢ Edit /src/main.py",
            "",
            "  Applied the fix. Running tests now.",
            "",
            "$ pytest -x in /home/user/project",
            "",
            "  All 5 tests passed.",
        ],
        "completion_summary": "· 6.7% of tokens used",
    },
    "codex": {
        "ready": "›",
        "tool_calls": [
            "Thinking...",
            "Reading /src/main.py...",
            "Editing /src/main.py...",
            "Running pytest -x...",
        ],
        "trust_dialog": (
            "Codex needs access to this workspace.\n"
            "\n"
            "1. Yes, allow Codex to work in this folder\n"
            "2. No, deny access"
        ),
        "permission_prompt": (
            "Apply changes to /src/main.py?\n"
            "Accept [Y/n]"
        ),
        "working_lines": [
            "Thinking...",
            "",
            "Reading /src/main.py...",
            "  Found the issue on line 42.",
            "",
            "Editing /src/main.py...",
            "  Fixed uninitialized variable.",
            "",
            "Running pytest -x...",
            "  All tests passed.",
        ],
        "completion_summary": "Done. 3 files changed.",
    },
}


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------

def emit(text: str, delay: float = 0.05) -> None:
    """Print a line and flush immediately (tmux captures this)."""
    print(text, flush=True)
    time.sleep(delay)


def clear_screen() -> None:
    """Clear the terminal screen (like real Ink TUI redraws).

    After auto-confirm dialogs, the old text must be gone from tmux's
    visible pane so the monitor doesn't re-match the same pattern.
    """
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    time.sleep(0.1)


def show_ready(profile: dict) -> None:
    """Show the ready prompt."""
    emit(profile["ready"])


def show_trust_dialog(profile: dict) -> None:
    """Show trust dialog, block until stdin response."""
    emit(profile["trust_dialog"])
    sys.stdin.readline()  # blocks until auto-confirm sends Enter
    clear_screen()  # Real TUIs redraw after dialog dismiss
    emit("  Trusted.\n")


def show_working(profile: dict, duration: float = 3.0) -> None:
    """Print working output over the given duration."""
    lines = profile["working_lines"]
    if not lines:
        time.sleep(duration)
        return
    pause = duration / len(lines)
    for line in lines:
        emit(line, delay=pause)


def show_permission(profile: dict) -> None:
    """Show permission prompt, block until stdin response."""
    emit(profile["permission_prompt"])
    sys.stdin.readline()  # blocks until auto-confirm sends response
    clear_screen()  # Real TUIs redraw after dialog dismiss
    emit("  Approved.\n")


def show_completion(profile: dict) -> None:
    """Show completion: summary + two ready prompts."""
    if profile.get("completion_summary"):
        emit(profile["completion_summary"])
        emit("")
    # Two prompts = completion for prompt_count strategy
    emit(profile["ready"])
    time.sleep(0.3)
    emit(profile["ready"])


def receive_prompt(profile: dict) -> str:
    """Read the prompt from stdin (sent by cam after readiness)."""
    prompt = sys.stdin.readline().strip()
    emit(f"\n  Received task: {prompt}\n")
    return prompt


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_basic(profile: dict, duration: float) -> None:
    """Ready → receive prompt → work → done."""
    show_ready(profile)
    receive_prompt(profile)
    show_working(profile, duration)
    show_completion(profile)


def scenario_trust(profile: dict, duration: float) -> None:
    """Trust dialog → ready → receive prompt → work → done."""
    show_trust_dialog(profile)
    show_ready(profile)
    receive_prompt(profile)
    show_working(profile, duration)
    show_completion(profile)


def scenario_permission(profile: dict, duration: float) -> None:
    """Ready → receive prompt → work → permission → more work → done."""
    show_ready(profile)
    receive_prompt(profile)
    # First half of work
    lines = profile["working_lines"]
    mid = len(lines) // 2
    half_dur = duration / 2

    profile_first = {**profile, "working_lines": lines[:mid]}
    profile_second = {**profile, "working_lines": lines[mid:]}

    show_working(profile_first, half_dur)
    show_permission(profile)
    show_working(profile_second, half_dur)
    show_completion(profile)


def scenario_full(profile: dict, duration: float) -> None:
    """Trust → ready → prompt → work → permission → work → done."""
    show_trust_dialog(profile)
    show_ready(profile)
    receive_prompt(profile)

    lines = profile["working_lines"]
    mid = len(lines) // 2
    half_dur = duration / 2

    profile_first = {**profile, "working_lines": lines[:mid]}
    profile_second = {**profile, "working_lines": lines[mid:]}

    show_working(profile_first, half_dur)
    show_permission(profile)
    show_working(profile_second, half_dur)
    show_completion(profile)


def scenario_error(profile: dict, duration: float) -> None:
    """Ready → receive prompt → work → error → exit(1)."""
    show_ready(profile)
    receive_prompt(profile)
    show_working(profile, duration)
    emit("")
    emit("Error: fatal: something went wrong")
    emit("FAILED — aborting")
    sys.exit(1)


def scenario_hang(profile: dict, duration: float) -> None:
    """Ready → receive prompt → work → hang forever (test idle timeout)."""
    show_ready(profile)
    receive_prompt(profile)
    show_working(profile, duration)
    emit("\n  Processing complex task...")
    # Hang — no more output, no completion. Monitor should detect idle timeout.
    while True:
        time.sleep(60)


SCENARIOS = {
    "basic": scenario_basic,
    "trust": scenario_trust,
    "permission": scenario_permission,
    "full": scenario_full,
    "error": scenario_error,
    "hang": scenario_hang,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fake coding CLI for testing CAM's monitor pipeline"
    )
    parser.add_argument(
        "--tool",
        choices=list(PROFILES.keys()),
        default="claude",
        help="Tool profile to simulate (default: claude)",
    )
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="basic",
        help="Scenario to run (default: basic)",
    )
    parser.add_argument(
        "--work-duration",
        type=float,
        default=3.0,
        help="Seconds of simulated work output (default: 3)",
    )
    args = parser.parse_args()

    profile = PROFILES[args.tool]
    scenario_fn = SCENARIOS[args.scenario]

    emit(f"[fake_coding_cli] tool={args.tool} scenario={args.scenario}")
    emit("")

    scenario_fn(profile, args.work_duration)

    emit("")
    emit("[fake_coding_cli] scenario complete")


if __name__ == "__main__":
    main()
