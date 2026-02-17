"""Terminal emulation utilities using pyte.

Renders raw terminal byte streams (from tmux pipe-pane) into clean text
with full scrollback history preserved through screen clears.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def render_raw_log(raw_path: str | Path, tail: int | None = None) -> str:
    """Render a raw pipe-pane log into clean text using pyte.

    Feeds the raw terminal byte stream through a pyte HistoryScreen to
    produce readable text with all ANSI escapes, cursor movements, and
    screen clears properly handled.

    Args:
        raw_path: Path to the raw pipe-pane log file.
        tail: If set, return only the last N lines.

    Returns:
        Clean text output, or empty string on error.
    """
    raw_path = Path(raw_path)
    if not raw_path.exists():
        return ""

    try:
        raw_data = raw_path.read_text(errors="replace")
    except Exception:
        logger.debug("Failed to read raw log %s", raw_path)
        return ""

    if not raw_data:
        return ""

    try:
        import pyte
    except ImportError:
        logger.warning("pyte not installed, falling back to strip_ansi")
        from cam.utils.ansi import strip_ansi
        return strip_ansi(raw_data)

    screen = pyte.HistoryScreen(220, 50, history=100000)
    screen.set_mode(pyte.modes.LNM)
    stream = pyte.Stream(screen)

    try:
        stream.feed(raw_data)
    except Exception:
        logger.debug("pyte feed error for %s", raw_path)
        return ""

    # Extract scrollback history + current screen
    lines: list[str] = []

    for row in screen.history.top:
        chars = "".join(
            row[col].data for col in sorted(row.keys())
            if hasattr(row[col], "data")
        )
        text = chars.rstrip()
        if text:
            lines.append(text)

    for line in screen.display:
        text = line.rstrip()
        if text:
            lines.append(text)

    if tail and len(lines) > tail:
        lines = lines[-tail:]

    return "\n".join(lines)


def render_raw_data(raw_data: str, tail: int | None = None) -> str:
    """Render raw terminal data string into clean text using pyte.

    Same as render_raw_log but takes a string instead of a file path.
    Useful for rendering data fetched from remote sources.

    Args:
        raw_data: Raw terminal byte stream as string.
        tail: If set, return only the last N lines.

    Returns:
        Clean text output, or empty string on error.
    """
    if not raw_data:
        return ""

    try:
        import pyte
    except ImportError:
        from cam.utils.ansi import strip_ansi
        return strip_ansi(raw_data)

    screen = pyte.HistoryScreen(220, 50, history=100000)
    screen.set_mode(pyte.modes.LNM)
    stream = pyte.Stream(screen)

    try:
        stream.feed(raw_data)
    except Exception:
        return ""

    lines: list[str] = []

    for row in screen.history.top:
        chars = "".join(
            row[col].data for col in sorted(row.keys())
            if hasattr(row[col], "data")
        )
        text = chars.rstrip()
        if text:
            lines.append(text)

    for line in screen.display:
        text = line.rstrip()
        if text:
            lines.append(text)

    if tail and len(lines) > tail:
        lines = lines[-tail:]

    return "\n".join(lines)
