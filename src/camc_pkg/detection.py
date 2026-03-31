"""Detection logic: state detection, completion, auto-confirm, readiness."""

from camc_pkg.utils import strip_ansi, clean_for_confirm


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
    clean = clean_for_confirm(output)
    # Only check the last few non-empty lines — real permission dialogs
    # appear at the bottom of the screen.  Matching the full output causes
    # false positives when the agent's *response* contains trigger text
    # (e.g. a table mentioning "1. Yes").
    lines = [l for l in clean.splitlines() if l.strip()]
    recent = "\n".join(lines[-32:]) if len(lines) > 8 else clean
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
