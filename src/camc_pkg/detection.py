"""Detection logic: state detection, completion, auto-confirm, readiness."""

import re

from camc_pkg.utils import strip_ansi, clean_for_confirm


# The native tmux ``cursor_flag`` is the authoritative input-cursor signal.
# A missing value is intentionally fail-closed in monitor calls.  The
# sentinel preserves the old direct-call API for offline tests/callers that
# do not have a tmux pane to query yet.
_CURSOR_LINE_RE = re.compile(r"^\s*[❯›→>](\s|$)")
_ACTIVE_UI_LINES = 128
_READY_UI_LINES = 4
_READY_STABLE_SECONDS = 10.0
_CURSOR_FLAG_UNSET = object()


def _find_cursor_line(lines):
    """Return the bottom-most cursor line in ``lines``, or None."""
    for line in reversed(lines):
        if _CURSOR_LINE_RE.match(line):
            return line
    return None


def _active_ui_lines(output, config, recent_lines=None):
    """Return the small visible UI region where prompts and menus live."""
    configured = (recent_lines if recent_lines is not None
                  else getattr(config, "confirm_recent_lines", _ACTIVE_UI_LINES))
    try:
        limit = max(1, int(configured))
        if recent_lines is None:
            limit = min(_ACTIVE_UI_LINES, limit)
    except (TypeError, ValueError):
        limit = recent_lines if recent_lines is not None else _ACTIVE_UI_LINES
    return [line for line in output.splitlines() if line.strip()][-limit:]


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


def should_auto_confirm(output, config, last_response="", prev_output="",
                        recent_lines=None, cursor_flag=_CURSOR_FLAG_UNSET):
    if (cursor_flag is not _CURSOR_FLAG_UNSET and cursor_flag != 0):
        return None
    if config.strip_ansi:
        output = strip_ansi(output)
    clean = clean_for_confirm(output)
    # Only check the recent pane tail — real permission dialogs appear near
    # the bottom of the screen. Matching the full output causes
    # false positives when the agent's *response* contains trigger text
    # (e.g. a table mentioning "1. Yes").
    recent = "\n".join(_active_ui_lines(clean, config, recent_lines))
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


def is_ready_for_input(output, config, stable_for=None,
                       cursor_flag=_CURSOR_FLAG_UNSET):
    if (cursor_flag is not _CURSOR_FLAG_UNSET and cursor_flag != 1):
        return False
    if not config.ready_pattern:
        return True
    clean = strip_ansi(output) if config.strip_ansi else output
    # A visible selection menu is not an input-ready prompt, even when an
    # older prompt line remains in scrollback or the selection uses the same
    # cursor glyph. Keep the menu text for classification; do not strip it.
    # Confirmation uses the configured 128-line search window; only the ready
    # cursor itself is restricted to the final four non-empty lines.
    confirm_lines = _active_ui_lines(clean, config)
    confirm_active = "\n".join(confirm_lines)
    for pattern, _response, _send_enter in config.confirm_rules:
        if pattern.search(confirm_active):
            return False
    active_lines = _active_ui_lines(clean, config, recent_lines=_READY_UI_LINES)
    active = "\n".join(active_lines)
    cursor_line = _find_cursor_line(active_lines)
    if cursor_line is not None:
        # A lone cursor can be rendered before Codex finishes its startup
        # trust screen.  Boot callers pass the existing hash-stability age;
        # require a short stable window before treating it as input-ready.
        if (stable_for is not None
                and stable_for < _READY_STABLE_SECONDS):
            return False
        return bool(config.ready_pattern.search(cursor_line))
    return bool(config.ready_pattern.search(active))


def should_boot_confirm(output, config, last_response="", prev_output="",
                        recent_lines=None):
    """Boot-phase confirm rules — no input-cursor guard (onboarding menus)."""
    if config.strip_ansi:
        output = strip_ansi(output)
    clean = clean_for_confirm(output)
    recent = "\n".join(_active_ui_lines(clean, config, recent_lines))
    for pattern, response, send_enter in config.confirm_rules:
        m = pattern.search(recent)
        if m:
            return (response, send_enter, pattern.pattern, m.group())
    return None


def should_confirm_initializing(output, boot_config, tool_config,
                                last_response="", prev_output="", recent_lines=None):
    """During initializing: boot.toml first, then tool.toml [[confirm]]."""
    if boot_config:
        hit = should_boot_confirm(
            output, boot_config, last_response=last_response, prev_output=prev_output,
            recent_lines=recent_lines)
        if hit:
            return hit, boot_config
    if tool_config:
        # The tool's initial menu is not user input. In particular Codex
        # changes its active cursor line when rendering `1. Yes`; the normal
        # runtime input guard would mistake that transition for typing.
        hit = should_boot_confirm(
            output, tool_config, last_response=last_response, prev_output=prev_output,
            recent_lines=recent_lines)
        if hit:
            return hit, tool_config
    return None, tool_config


def is_ready_for_boot(output, boot_config, tool_config, stable_for=None,
                      cursor_flag=_CURSOR_FLAG_UNSET):
    """Ready if either boot or tool ready_pattern matches."""
    if (boot_config
            and is_ready_for_input(output, boot_config, stable_for=stable_for,
                                   cursor_flag=cursor_flag)):
        return True
    if (tool_config
            and is_ready_for_input(output, tool_config, stable_for=stable_for,
                                  cursor_flag=cursor_flag)):
        return True
    return False
