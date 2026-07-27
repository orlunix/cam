"""Focused display contracts for ``camc list`` identity rendering."""

import sys

import pytest

from camc_pkg import cli


def test_list_has_no_compact_layout_option(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["camc", "list", "--compact"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "unrecognized arguments: --compact" in capsys.readouterr().err
