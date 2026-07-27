"""Focused rendering contracts for CAMC terminal tables."""

import re
from io import StringIO

import pytest

from camc_pkg import formatters


@pytest.mark.skipif(not formatters._HAS_RICH, reason="Rich renderer unavailable")
def test_table_gives_name_remaining_width_and_truncates_secondary_columns(monkeypatch):
    from rich.console import Console

    rendered = StringIO()
    monkeypatch.setattr(
        formatters, "_console",
        Console(file=rendered, width=42, color_system=None),
    )

    formatters.print_table(
        ["ID", "NAME", "PROMPT"],
        [["1234abcd", "agent name that must remain visible", "this prompt is secondary"]],
        no_truncate_cols={1}, col_max_widths={0: 8, 1: 32, 2: 12},
    )

    compact = re.sub(r"[^a-z0-9]", "", rendered.getvalue().lower())
    assert "1234abcd" in compact
    for token in ("agent", "name", "that", "must", "remain", "visible"):
        assert token in compact
    assert "…" in rendered.getvalue()


def test_ansi_table_ellipsizes_secondary_columns_but_preserves_name(monkeypatch, capsys):
    monkeypatch.setattr(formatters, "_HAS_RICH", False)

    formatters.print_table(
        ["ID", "NAME", "STATE", "PROMPT"],
        [["1234abcd", "short-name", "initializing", "a prompt that is secondary"]],
        no_truncate_cols={1}, col_max_widths={0: 8, 1: 32, 2: 6, 3: 10},
    )

    output = capsys.readouterr().out
    assert "short-name" in output
    assert "initi…" in output
    assert "a prompt …" in output


def test_ansi_table_aligns_prompt_after_colored_emoji_state(monkeypatch, capsys):
    monkeypatch.setattr(formatters, "_HAS_RICH", False)
    monkeypatch.setattr(formatters, "_use_color", True)

    formatters.print_table(
        ["STATE", "PROMPT"],
        [
            [formatters._c("💤 idle", "dim"), "first prompt"],
            [formatters._c("🧪 testing", "magenta"), "second prompt"],
        ],
        col_max_widths={0: 12, 1: 20},
    )

    lines = [formatters._strip_ansi(line) for line in capsys.readouterr().out.splitlines()]
    data_lines = [line for line in lines if "prompt" in line]
    first_prefix = data_lines[0].split("first prompt", 1)[0]
    second_prefix = data_lines[1].split("second prompt", 1)[0]
    assert formatters._display_width(first_prefix) == formatters._display_width(second_prefix)


@pytest.mark.skipif(not formatters._HAS_RICH, reason="Rich renderer unavailable")
def test_rich_table_never_collapses_name_when_secondary_columns_compete(monkeypatch):
    from rich.console import Console

    rendered = StringIO()
    monkeypatch.setattr(formatters, "_console", Console(file=rendered, width=80, color_system=None))
    formatters.print_table(
        ["ID", "NAME", "TAG", "TOOL", "STATUS", "STATE", "PROMPT", "UPDATED"],
        [["1234abcd", "important-agent-name", "tag-with-extra-content", "codex", "running", "initializing", "prompt", "today"]],
        no_truncate_cols={1},
        col_max_widths={0: 8, 1: 32, 2: 20, 3: 10, 4: 12, 5: 10, 6: 24, 7: 12},
    )

    output = rendered.getvalue()
    assert "NAME" in output
    assert "important" in output
