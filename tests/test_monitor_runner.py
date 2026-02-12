"""Tests for the background monitor runner subprocess."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cam.constants import PID_DIR
from cam.core.monitor_runner import _remove_pid, _write_pid


class TestPidFile:
    def test_write_pid_creates_file(self, tmp_path):
        with patch("cam.core.monitor_runner.PID_DIR", tmp_path):
            pid_path = _write_pid("test-agent-123")

        assert pid_path.exists()
        assert pid_path.read_text() == str(os.getpid())
        assert pid_path.name == "test-agent-123.pid"

    def test_write_pid_creates_directory(self, tmp_path):
        pid_dir = tmp_path / "pids"
        with patch("cam.core.monitor_runner.PID_DIR", pid_dir):
            _write_pid("agent-abc")

        assert pid_dir.exists()
        assert (pid_dir / "agent-abc.pid").exists()

    def test_remove_pid_deletes_file(self, tmp_path):
        pid_file = tmp_path / "agent-xyz.pid"
        pid_file.write_text("12345")

        with patch("cam.core.monitor_runner.PID_DIR", tmp_path):
            _remove_pid("agent-xyz")

        assert not pid_file.exists()

    def test_remove_pid_missing_file_no_error(self, tmp_path):
        with patch("cam.core.monitor_runner.PID_DIR", tmp_path):
            _remove_pid("nonexistent-agent")  # Should not raise

    def test_write_pid_overwrites_stale(self, tmp_path):
        pid_file = tmp_path / "stale-agent.pid"
        pid_file.write_text("99999")

        with patch("cam.core.monitor_runner.PID_DIR", tmp_path):
            _write_pid("stale-agent")

        assert pid_file.read_text() == str(os.getpid())


class TestMonitorRunnerImport:
    def test_main_function_exists(self):
        from cam.core.monitor_runner import main
        assert callable(main)

    def test_run_monitor_function_exists(self):
        from cam.core.monitor_runner import run_monitor
        assert callable(run_monitor)
