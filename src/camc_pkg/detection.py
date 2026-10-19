"""Detection logic: auto-confirm and input readiness."""

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


def _cursor_flag_supported(config):
    """Return whether this adapter can use tmux's native cursor flag."""
    return bool(getattr(config, "cursor_flag_support", True))


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


def should_auto_confirm(output, config, last_response="", prev_output="",
                        recent_lines=None, cursor_flag=_CURSOR_FLAG_UNSET):
    if (_cursor_flag_supported(config)
            and cursor_flag is not _CURSOR_FLAG_UNSET and cursor_flag != 0):
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


def is_ready_for_input(output, config, stable_for=None,
                       cursor_flag=_CURSOR_FLAG_UNSET):
    if (_cursor_flag_supported(config)
            and cursor_flag is not _CURSOR_FLAG_UNSET and cursor_flag != 1):
        return False
    if not config.ready_pattern:
        return True
    clean = strip_ansi(output) if config.strip_ansi else output
    # A visible selection menu is not an input-ready prompt, even when an
    # older prompt line remains in scrollback or the selection uses the same
    # cursor glyph. Keep the menu text for classification; do not strip it.
    # Confirmation uses the configured 128-line search window; only the ready
    # cursor itself is restricted to the final four non-empty lines. With a
    # supported native cursor, flag=1 proves the current UI is an input box;
    # a confirm match can then be stale scrollback. Unknown/menu flags remain
    # fail-closed, while adapters without cursor support retain the text guard.
    menu_active = (not _cursor_flag_supported(config)
                   or cursor_flag is _CURSOR_FLAG_UNSET
                   or cursor_flag in (None, 0, False))
    if menu_active:
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


def should_confirm_active_boot_cursor(output, boot_config, tool_config):
    """Match a boot action on the current bottom-most cursor line only."""
    cfg = boot_config or tool_config
    if cfg is None:
        return None, tool_config
    clean = strip_ansi(output) if cfg.strip_ansi else output
    cursor_line = _find_cursor_line(
        _active_ui_lines(clean, cfg, recent_lines=_READY_UI_LINES))
    if cursor_line is None:
        return None, tool_config
    return should_confirm_initializing(
        cursor_line, boot_config, tool_config, recent_lines=1)


def is_ready_for_boot(output, boot_config, tool_config, stable_for=None,
                      cursor_flag=_CURSOR_FLAG_UNSET):
    """Ready if either boot or tool ready_pattern matches."""
    # Some Codex builds report cursor_flag=1 while the workspace-trust menu
    # is still active.  The final four non-empty lines distinguish that live
    # menu from old trust text left in scrollback after the real input prompt
    # appears.  A live boot menu always wins over readiness.
    active_confirm, _active_cfg = should_confirm_active_boot_cursor(
        output, boot_config, tool_config)
    if active_confirm:
        return False
    # A boot confirmation has priority over both ready patterns. Without
    # this cross-config guard, a trust prompt known only to boot.toml can be
    # mistaken for normal readiness by the tool config's cursor rule.
    # cursor_flag=0 identifies an active selection menu. Once the tool's
    # current input cursor is visible (flag=1), matching boot text may simply
    # be stale scrollback and must not block the ready prompt below.
    menu_active = (cursor_flag is _CURSOR_FLAG_UNSET
                   or cursor_flag in (None, 0, False))
    if (menu_active
            and ((boot_config and should_boot_confirm(output, boot_config))
                 or (tool_config and should_boot_confirm(output, tool_config)))):
        return False
    if (boot_config
            and is_ready_for_input(output, boot_config, stable_for=stable_for,
                                   cursor_flag=cursor_flag)):
        return True
    if (tool_config
            and is_ready_for_input(output, tool_config, stable_for=stable_for,
                                  cursor_flag=cursor_flag)):
        return True
    return False
