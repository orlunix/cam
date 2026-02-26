"""Claude Code adapter implementation.

This module provides the ToolAdapter implementation for Claude Code CLI,
using interactive mode with --allowed-tools for reliable auto-confirmation.
"""

from __future__ import annotations

import re

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition
from cam.utils.ansi import strip_ansi


class ClaudeAdapter(ToolAdapter):
    """Adapter for Claude Code CLI tool.

    Runs Claude in interactive mode (not headless -p) with pre-authorized
    tools via --allowed-tools. Permission prompts show a numbered menu
    (1. Yes, 2. Yes don't ask again, 3. No) — the adapter sends "1"
    without Enter to confirm.
    """

    name = "claude"
    display_name = "Claude Code"

    # State detection patterns using Claude Code TUI tool-call markers
    # (● ToolName(...)) for reliable detection.
    _STATE_PATTERNS = {
        AgentState.PLANNING: re.compile(
            r"(● Read\(|● Glob\(|● Grep\(|● WebFetch\(|● WebSearch\(|Thinking|Analyzing)",
            re.IGNORECASE,
        ),
        AgentState.EDITING: re.compile(
            r"(● Edit\(|● Write\(|● NotebookEdit\()",
        ),
        AgentState.TESTING: re.compile(
            r"(● Bash\(|Running tests|pytest|npm test|npm run)",
            re.IGNORECASE,
        ),
        AgentState.COMMITTING: re.compile(
            r"(git commit|git push|gh pr create)",
            re.IGNORECASE,
        ),
    }

    # Auto-confirm patterns for Claude's numbered menu UI.
    # Claude v2.1+ uses Ink select menus for trust/permission dialogs:
    #   "❯ 1. Yes, I trust this folder" + "Enter to confirm"
    # These require pressing Enter (cursor already on the right option).
    # Older/inline prompts still use "1. Yes  2. No" where typing "1" works.
    # Order matters: more specific patterns first.
    _AUTO_CONFIRM_PATTERNS = [
        # Trust folder select menu (v2.1+): cursor already on "Yes", press Enter
        (re.compile(r"Enter to confirm.*Esc to cancel", re.IGNORECASE | re.DOTALL),
         ConfirmAction(response="", send_enter=True)),
        # Direct permission prompt
        (re.compile(r"Do\s+you\s+want\s+to\s+proceed", re.IGNORECASE),
         ConfirmAction(response="1", send_enter=False)),
        # Numbered menu with "1. Yes" or "1. Allow" visible
        (re.compile(r"1\.\s*(Yes|Allow)", re.IGNORECASE),
         ConfirmAction(response="1", send_enter=False)),
        # "Allow once" / "Always allow" select menu (Claude 4.x+)
        (re.compile(r"Allow\s+(once|always)", re.IGNORECASE),
         ConfirmAction(response="", send_enter=True)),
        # Permission prompt ending with (y/n) or [Y/n]
        (re.compile(r"\(y/n\)|\[Y/n\]|\[y/N\]", re.IGNORECASE),
         ConfirmAction(response="y", send_enter=True)),
    ]

    # Claude Code cost/token output pattern
    _COST_PATTERN = re.compile(
        r"Total cost:\s*\$?([\d.]+)",
        re.IGNORECASE,
    )

    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Launch Claude Code in interactive mode with pre-authorized tools.

        Uses --allowed-tools to pre-authorize key tools, reducing permission
        prompts. The prompt is sent separately via stdin after launch
        (see needs_prompt_after_launch).

        Args:
            task: Task containing the prompt to execute
            context: Context with working directory and environment

        Returns:
            Command list for interactive mode execution
        """
        return [
            "claude",
            "--allowed-tools",
            "Bash,Edit,Read,Write,Glob,Grep,WebFetch,TodoWrite,NotebookEdit",
        ]

    def detect_state(self, output: str) -> AgentState | None:
        """Detect agent state from recent output.

        Strips ANSI codes, then finds the last matching pattern in the
        output so the most recent activity wins.

        Args:
            output: Recent TMUX output

        Returns:
            Detected AgentState or None if no patterns match
        """
        recent = output[-2000:] if len(output) > 2000 else output
        clean = strip_ansi(recent)

        last_pos = -1
        last_state: AgentState | None = None
        for state, pattern in self._STATE_PATTERNS.items():
            for m in pattern.finditer(clean):
                if m.start() > last_pos:
                    last_pos = m.start()
                    last_state = state

        return last_state

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Check if output contains a Claude permission prompt.

        Strips ANSI codes and trailing whitespace per line before matching.
        The rstrip handles SSH captures where each line is padded to terminal
        width with spaces, which would otherwise push prompt text outside
        a fixed-size tail window.

        Args:
            output: Recent TMUX output

        Returns:
            ConfirmAction with response/send_enter, or None
        """
        clean = strip_ansi(output)
        # Strip trailing whitespace per line + trailing blank lines,
        # then take last 500 chars for pattern matching.
        clean = "\n".join(line.rstrip() for line in clean.splitlines()).rstrip()
        recent = clean[-500:] if len(clean) > 500 else clean

        for pattern, action in self._AUTO_CONFIRM_PATTERNS:
            if pattern.search(recent):
                return action

        return None

    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect if Claude Code has completed its task.

        Claude's TUI shows '❯ <user prompt>' when the task is sent, then
        displays work output, then returns to '❯' (with placeholder text)
        when done. Two or more lines starting with ❯ means Claude has
        finished the task and is waiting for the next prompt.

        For long outputs (especially over SSH with limited capture windows),
        the first ❯ may scroll past the capture buffer. In that case, a
        single ❯ combined with a "Crunched for" summary line (Claude's
        task-complete marker) is also treated as completion.

        Args:
            output: Recent TMUX output

        Returns:
            AgentStatus.COMPLETED if Claude is back at its input prompt,
            None if still working.
        """
        clean = strip_ansi(output)
        prompt_count = len(self._READY_PATTERN.findall(clean))
        if prompt_count >= 2:
            return AgentStatus.COMPLETED

        # Fallback: single ❯ + task summary = completion
        # This handles long outputs where the first ❯ scrolled past
        # the capture window (common with SSH's 50-line capture).
        # Claude's summary uses rotating verbs: "Crunched for", "Sautéed for",
        # "Whisked for", etc. — match the "✻ <verb> for <time>" pattern.
        if prompt_count == 1 and self._TASK_SUMMARY_PATTERN.search(clean):
            return AgentStatus.COMPLETED

        return None

    # Claude's input prompt: "❯" at the start of a line (between ──── borders).
    # May have placeholder text like "Try "fix lint errors"" or be empty.
    _READY_PATTERN = re.compile(r"^❯", re.MULTILINE)

    # Claude's task summary: "✻ <cooking verb> for <time>" (e.g. "✻ Crunched for 1m 11s")
    # Claude rotates verbs (Crunched, Sautéed, Whisked, etc.) but the
    # "✻ ... for \d+" pattern is stable.
    _TASK_SUMMARY_PATTERN = re.compile(r"✻ .+ for \d+")

    def is_ready_for_input(self, output: str) -> bool:
        """Check if Claude's TUI shows the input prompt (❯).

        The input prompt appears as a line starting with ❯ between
        horizontal rule borders (────). This indicates Claude has
        finished loading and is ready to accept input.
        """
        clean = strip_ansi(output)
        return bool(self._READY_PATTERN.search(clean))

    def get_startup_wait(self) -> float:
        """Maximum seconds to wait for Claude's TUI to become ready."""
        return 30.0

    def needs_prompt_after_launch(self) -> bool:
        """Prompt must be sent via stdin after Claude is ready."""
        return True

    def estimate_cost(self, output: str) -> float | None:
        """Estimate cost from Claude Code output."""
        return None

    def parse_files_changed(self, output: str) -> list[str]:
        """Extract modified file paths from Claude Code output."""
        return []
