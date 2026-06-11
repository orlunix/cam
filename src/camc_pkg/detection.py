"""Detection logic: state detection, completion, auto-confirm, readiness."""

import re

from camc_pkg.utils import strip_ansi, clean_for_confirm


# Global input-box guard (2026-06-11).
#
# Detects an INPUT cursor sitting in the user's input box, meaning the
# agent is waiting for the human to type — auto-confirm MUST NOT fire
# in that state or it injects keystrokes into the user's input.
#
# Cursor markers (line start, optional indent, then char + space):
#   U+276F ❯  Claude Code TUI
#   U+203A ›  Codex TUI
#   U+2192 →  Cursor Agent TUI
#   >         generic shell / continuation prompt
#
# Detects "input box is currently active" in three ways, ANY of which
# means auto-confirm must skip:
#
# (1) Bare cursor line: ``^\s*[❯›→>]\s*$`` — input box empty, idle.
# (2) Cursor line consists only of repeated last_response chars:
#     ``^\s*[❯›→>]\s+<response>+\s*$``. Catches the spam-in-input case
#     where our own ``1`` or ``a`` keystroke landed in the input box
#     instead of selecting a menu option. NOT triggered by menu lines
#     like ``❯ 1. Yes`` because of the ``.`` after the digit.
# (3) Cursor line content changed vs the previous capture — the
#     input box is being typed into (by user or by codex animating).
#
# Search window: last 8 non-empty lines.
_CURSOR_LINE_RE = re.compile(r"^\s*[❯›→>](\s|$)")
_BARE_CURSOR_RE = re.compile(r"^\s*[❯›→>]\s*$")
_CURSOR_PREFIX_RE = re.compile(r"^\s*[❯›→>]\s+(.*?)\s*$")


def _find_cursor_line(lines):
    """Return the bottom-most cursor line in ``lines``, or None."""
    for line in reversed(lines):
        if _CURSOR_LINE_RE.match(line):
            return line
    return None


def has_input_cursor(output, last_response="", prev_output=""):
    """True iff the input box is visible / active and auto-confirm
    must therefore skip. Three conditions documented above."""
    tail_lines = [l for l in output.splitlines() if l.strip()][-8:]
    cur_line = _find_cursor_line(tail_lines)
    if cur_line is None:
        return False
    # (1) Bare cursor line
    if _BARE_CURSOR_RE.match(cur_line):
        return True
    # (2) Cursor line is only repeated last_response chars
    if last_response:
        m = _CURSOR_PREFIX_RE.match(cur_line)
        if m:
            content = m.group(1)
            if content and all(c == last_response for c in content):
                return True
    # (3) Cursor line changed since the previous capture
    if prev_output:
        prev_tail = [l for l in prev_output.splitlines() if l.strip()][-8:]
        prev_cur = _find_cursor_line(prev_tail)
        if prev_cur is not None and prev_cur != cur_line:
            return True
    return False


def detect_state(output, config):
    recent = output[-config.state_recent_chars:]
    if config.strip_ansi:
        recent = strip_ansi(recent)
    if config.state_strategy == "last":
        last_pos, last_state = -1, None
        for state_name, pattern in config.state_patterns:
            for m in pattern.finditer(recent):
                if m.start() > last_pos:
                    last_pos = m.start()
                    last_state = state_name
        return last_state
    else:
        for state_name, pattern in config.state_patterns:
            if pattern.search(recent):
                return state_name
        return None


def should_auto_confirm(output, config, last_response="", prev_output=""):
    if config.strip_ansi:
        output = strip_ansi(output)
    # Input-box guard. Three conditions in has_input_cursor: bare
    # cursor, cursor + only our last_response chars, or cursor line
    # content changing since the previous capture. Any → skip.
    if has_input_cursor(output, last_response=last_response,
                        prev_output=prev_output):
        return None
    clean = clean_for_confirm(output)
    # Only check the last few non-empty lines — real permission dialogs
    # appear at the bottom of the screen.  Matching the full output causes
    # false positives when the agent's *response* contains trigger text
    # (e.g. a table mentioning "1. Yes").
    lines = [l for l in clean.splitlines() if l.strip()]
    recent = "\n".join(lines[-config.confirm_recent_lines:])
    for pattern, response, send_enter in config.confirm_rules:
        m = pattern.search(recent)
        if m:
            return (response, send_enter, pattern.pattern, m.group())
    return None


def detect_completion(output, config):
    if config.completion_strategy == "process_exit":
        return None
    if config.completion_strategy == "prompt_count":
        return _detect_prompt_count(output, config)
    return _detect_pattern(output, config)


def _detect_pattern(output, config):
    if config.strip_ansi:
        output = strip_ansi(output)
    if config.error_pattern:
        search_text = output if config.error_search_full else output[-config.completion_recent_chars:]
        if config.error_pattern.search(search_text):
            return "failed"
    recent = output[-config.completion_recent_chars:]
    if config.completion_pattern and config.completion_pattern.search(recent):
        return "completed"
    if (config.shell_prompt_pattern
            and config.shell_prompt_pattern.search(recent)
            and len(output) > config.min_output_length):
        return "completed"
    return None


def _detect_prompt_count(output, config):
    if not config.prompt_pattern:
        return None
    clean = strip_ansi(output) if config.strip_ansi else output
    count = len(config.prompt_pattern.findall(clean))
    if count >= config.prompt_count_threshold:
        if config.confirm_rules:
            for cp, _resp, _enter in config.confirm_rules:
                if cp.search(clean):
                    return None
        return "completed"
    if (count == 1
            and config.fallback_summary_pattern
            and config.fallback_summary_pattern.search(clean)):
        return "completed"
    return None


def is_ready_for_input(output, config):
    if not config.ready_pattern:
        return True
    clean = strip_ansi(output) if config.strip_ansi else output
    return bool(config.ready_pattern.search(clean))
