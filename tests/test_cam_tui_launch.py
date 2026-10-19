#!/usr/bin/env python3
"""Bare `cam` launches the TUI; `cam tui` explicitly; help fallback off-tty."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from typer.testing import CliRunner

import cam.cli.app as cam_app
import cam.tui as cam_tui


def _patch_tui(monkeypatch, available):
    calls = []
    monkeypatch.setattr(cam_app, "_tui_available", lambda: available)
    monkeypatch.setattr(cam_tui, "tui_main", lambda: calls.append(True) or 0)
    return calls


def test_bare_cam_launches_tui(monkeypatch):
    calls = _patch_tui(monkeypatch, True)
    r = CliRunner().invoke(cam_app.app, [])
    assert r.exit_code == 0 and calls == [True]


def test_bare_cam_falls_back_to_help_off_tty(monkeypatch):
    calls = _patch_tui(monkeypatch, False)
    r = CliRunner().invoke(cam_app.app, [])
    assert r.exit_code == 0
    assert not calls
    assert "Usage" in r.output and "Hint:" in r.output


def test_cam_tui_command_launches_tui(monkeypatch):
    calls = _patch_tui(monkeypatch, True)
    r = CliRunner().invoke(cam_app.app, ["tui"])
    assert r.exit_code == 0 and calls == [True]


def test_cam_tui_errors_off_tty(monkeypatch):
    calls = _patch_tui(monkeypatch, False)
    r = CliRunner().invoke(cam_app.app, ["tui"])
    assert r.exit_code == 1 and not calls


def test_help_still_works_without_starting_tui(monkeypatch):
    calls = _patch_tui(monkeypatch, True)
    r = CliRunner().invoke(cam_app.app, ["--help"])
    assert r.exit_code == 0 and not calls
    assert "tui" in r.output


def test_cam_cli_command_shows_classic_help_without_tui(monkeypatch):
    calls = _patch_tui(monkeypatch, True)
    r = CliRunner().invoke(cam_app.app, ["cli"])
    assert r.exit_code == 0 and not calls
    assert "Usage" in r.output
    assert "tui" in r.output and "run" in r.output
