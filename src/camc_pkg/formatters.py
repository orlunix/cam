"""Output formatters with optional Rich support.

If 'rich' is installed, uses Rich tables and panels for pretty output.
Otherwise, falls back to ANSI escape codes and fixed-width formatting.
Both modes produce equivalent information — just different visual polish.
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# Rich detection
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    _HAS_RICH = True
    _console = Console()
    _err_console = Console(stderr=True)
except ImportError:
    _HAS_RICH = False
    _console = None
    _err_console = None

# ---------------------------------------------------------------------------
# ANSI helpers (for non-rich fallback)
# ---------------------------------------------------------------------------

_use_color = sys.stdout.isatty()

_ANSI = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def _c(text, color):
    """Wrap text in ANSI color if terminal supports it."""
    if not _use_color or not color:
        return str(text)
    return "%s%s%s" % (_ANSI.get(color, ""), text, _ANSI["reset"])


# ---------------------------------------------------------------------------
# Status / state styling
# ---------------------------------------------------------------------------

_STATUS_STYLE = {
    "running": ("green", "▶"),
    "completed": ("green", "✓"),
    "failed": ("red", "✗"),
    "stopped": ("yellow", "■"),
    "killed": ("red", "☠"),
    "pending": ("yellow", "⏳"),
    "starting": ("yellow", "🚀"),
    "timeout": ("red", "⏱"),
    "retrying": ("yellow", "↻"),
}

_STATE_STYLE = {
    "initializing": ("dim", "..."),
    "planning": ("cyan", "🧠"),
    "editing": ("blue", "✏"),
    "testing": ("magenta", "🧪"),
    "committing": ("green", "📦"),
    "idle": ("dim", "💤"),
}


def styled_status(status):
    """Return styled status string."""
    color, icon = _STATUS_STYLE.get(status, ("", ""))
    if _HAS_RICH:
        return Text("%s %s" % (icon, status), style=color) if icon else Text(status)
    return _c("%s %s" % (icon, status) if icon else status, color)


def styled_state(state):
    """Return styled state string."""
    if not state:
        return Text("-", style="dim") if _HAS_RICH else _c("-", "dim")
    color, icon = _STATE_STYLE.get(state, ("", ""))
    if _HAS_RICH:
        return Text("%s %s" % (icon, state), style=color) if icon else Text(state)
    return _c("%s %s" % (icon, state) if icon else state, color)


# ---------------------------------------------------------------------------
# Table output
# ---------------------------------------------------------------------------

def print_table(headers, rows, title=None, col_styles=None, col_widths=None):
    """Print a table with optional Rich or ANSI fallback.

    Args:
        headers: list of column header strings
        rows: list of lists/tuples (one per row)
        title: optional table title
        col_styles: optional dict {col_index: rich_style_string} for Rich mode
        col_widths: optional dict {col_index: min_width} for ANSI fallback
    """
    if not rows:
        return

    col_styles = col_styles or {}
    col_widths = col_widths or {}

    if _HAS_RICH:
        table = Table(title=title, show_lines=False)
        for i, h in enumerate(headers):
            style = col_styles.get(i, None)
            table.add_column(h, style=style, no_wrap=(i == 0))
        for row in rows:
            cells = []
            for cell in row:
                if isinstance(cell, Text):
                    cells.append(cell)
                else:
                    cells.append(str(cell) if cell is not None else "")
            table.add_row(*cells)
        _console.print(table)
    else:
        # Calculate column widths
        ncols = len(headers)
        widths = [max(len(str(h)), col_widths.get(i, 0)) for i, h in enumerate(headers)]
        for row in rows:
            for i in range(min(ncols, len(row))):
                cell = row[i]
                # Strip ANSI for width calc, but keep for display
                plain = _strip_ansi(str(cell)) if cell is not None else ""
                widths[i] = max(widths[i], len(plain))

        # Cap widths to avoid overflow
        widths = [min(w, 40) for w in widths]

        fmt_parts = []
        for i, w in enumerate(widths):
            if i == ncols - 1:
                fmt_parts.append("%s")  # last col unlimited
            else:
                fmt_parts.append("%%-%ds" % w)
        fmt = "  ".join(fmt_parts)

        if title:
            print(_c(title, "bold"))
        print(fmt % tuple(headers))
        print("-" * (sum(widths) + 2 * (ncols - 1)))
        for row in rows:
            cells = []
            for i in range(ncols):
                c = row[i] if i < len(row) else ""
                cells.append(str(c) if c is not None else "")
            print(fmt % tuple(cells))


def _strip_ansi(text):
    """Strip ANSI escape codes for width calculation."""
    import re
    return re.sub(r'\033\[[0-9;]*m', '', text)


# ---------------------------------------------------------------------------
# Panel output
# ---------------------------------------------------------------------------

def print_panel(lines, title=None, border_style="cyan"):
    """Print a bordered panel with optional Rich or ANSI fallback."""
    if _HAS_RICH:
        _console.print(Panel("\n".join(str(l) for l in lines),
                             title=title, border_style=border_style))
    else:
        border = "\u2500"
        if title:
            hdr = "%s%s %s %s%s" % (_ANSI.get(border_style, ""), border * 2,
                                     title, border * 30, _ANSI["reset"])
            print(hdr if _use_color else "%s %s %s" % (border * 2, title, border * 30))
        else:
            print(border * 40)
        for l in lines:
            print("  %s" % l)
        if _use_color and border_style in _ANSI:
            print("%s%s%s" % (_ANSI[border_style], border * 40, _ANSI["reset"]))
        else:
            print(border * 40)


# ---------------------------------------------------------------------------
# Status messages
# ---------------------------------------------------------------------------

def print_success(msg):
    if _HAS_RICH:
        _console.print("[green]✓[/green] %s" % msg)
    else:
        print("%s %s" % (_c("✓", "green"), msg))


def print_error(msg):
    if _HAS_RICH:
        _err_console.print("[red]✗[/red] %s" % msg)
    else:
        print("%s %s" % (_c("✗", "red"), msg), file=sys.stderr)


def print_warning(msg):
    if _HAS_RICH:
        _console.print("[yellow]⚠[/yellow] %s" % msg)
    else:
        print("%s %s" % (_c("⚠", "yellow"), msg))


def print_info(msg):
    if _HAS_RICH:
        _console.print("[cyan]ℹ[/cyan] %s" % msg)
    else:
        print("%s %s" % (_c("ℹ", "cyan"), msg))


# ---------------------------------------------------------------------------
# Key-value detail display
# ---------------------------------------------------------------------------

def print_detail(pairs, title=None, border_style="cyan"):
    """Print key-value pairs, optionally inside a panel.

    Args:
        pairs: list of (key, value) tuples. Value can be str or (str, style).
        title: if provided, wraps in a panel
    """
    lines = []
    max_key_len = max((len(k) for k, v in pairs if v is not None), default=10)
    for key, value in pairs:
        if value is None:
            continue
        if isinstance(value, tuple):
            value, style = value
        else:
            style = None
        padding = " " * (max_key_len - len(key) + 1)
        if _HAS_RICH:
            if style:
                lines.append("[bold]%s:[/bold]%s[%s]%s[/%s]" % (key, padding, style, value, style))
            else:
                lines.append("[bold]%s:[/bold]%s%s" % (key, padding, value))
        else:
            if style and _use_color and style in _ANSI:
                lines.append("%s:%s%s%s%s" % (key, padding, _ANSI[style], value, _ANSI["reset"]))
            else:
                lines.append("%s:%s%s" % (key, padding, value))

    if title:
        print_panel(lines, title=title, border_style=border_style)
    else:
        for l in lines:
            if _HAS_RICH:
                _console.print("  %s" % l)
            else:
                print("  %s" % l)
