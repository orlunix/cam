"""ANSI escape sequence stripping utility."""

from __future__ import annotations

import re

# Matches common ANSI escape sequences:
#   - CSI sequences: ESC [ ... <final byte>
#   - OSC sequences: ESC ] ... BEL
#   - Other ESC sequences: ESC <intermediate byte>
_ANSI_RE = re.compile(
    r"\x1B\[[0-9;?]*[ -/]*[@-~]"   # CSI sequences (colors, cursor, etc.)
    r"|\x1B\][^\x07]*\x07"          # OSC sequences (title, etc.)
    r"|\x1B[@-_]"                    # Two-char ESC sequences
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text.

    Args:
        text: Input text potentially containing ANSI codes.

    Returns:
        Text with all ANSI escape sequences removed.
    """
    return _ANSI_RE.sub("", text)
