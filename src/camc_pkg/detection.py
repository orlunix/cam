"""Detection logic: state detection, completion, auto-confirm, readiness."""

import re

from camc_pkg.utils import strip_ansi, clean_for_confirm


# Global active-input-cursor guard (2026-06-11 hot-fix).
#
# Detects an INPUT cursor — a TUI marker that says "the user can type
# right now". Markers (line start, optional indent, then char + space):
#   U+276F ❯  Claude Code TUI
#   U+203A ›  Codex TUI
#   >         generic shell / continuation prompt
#
# Restricted to the LAST 5 non-empty lines of the captured screen.
# If no cursor is on screen, no auto-confirm rule may fire — period.
# This kills the false-fire class where a stray substring (markdown
# body, code fence, embedded screen capture, agent prose) matches a
# TOML rule even though no real interactive prompt is present.
_INPUT_CURSOR_RE = re.compile(r"(?m)^\s*[❯›>]\s")


def has_input_cursor(output):
    """True iff one of the active-input-cursor markers is visible on
    the last 5 non-empty lines of ``output``. See _INPUT_CURSOR_RE."""
    tail_lines = [l for l in output.splitlines() if l.strip()][-5:]
    tail = "\n".join(tail_lines)
    return bool(_INPUT_CURSOR_RE.search(tail))


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


def should_auto_confirm(output, config):
    if config.strip_ansi:
        output = strip_ansi(output)
    # 2026-06-11 hot-fix: global input-cursor guard. If no active
    # input cursor (❯ / › / `> `) is visible in the last 5 non-empty
    # lines of the RAW capture, no TOML [[confirm]] rule may fire —
    # there cannot be a real dialog waiting for input without a
    # cursor on screen. Kills false-fires from markdown / code blocks
    # / embedded screen captures that happen to contain a rule's
    # literal substring.
    #
    # Critically: this check runs against `output`, NOT against
    # `clean_for_confirm(output)`. clean_for_confirm() strips a
    # leading selection cursor sitting before a numbered option (so
    # author-friendly rules like `^1\. Yes` still match) — applying
    # the guard AFTER that strip would defeat itself, because the
    # cursor would already be gone from the cleaned text.
    if not has_input_cursor(output):
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
