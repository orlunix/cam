"""Focused coverage for CAMC-owned tmux template refresh."""

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from camc_pkg import cli, transport


class TestHealTmux(unittest.TestCase):
    def test_builtin_heal_skills_document_current_modes(self):
        skills_dir = Path(__file__).resolve().parents[1] / "src" / "camc_pkg" / "skills"
        diagnose = (skills_dir / "camc-diagnose" / "SKILL.md").read_text()
        managing = (skills_dir / "managing-camc" / "SKILL.md").read_text()

        for text in (diagnose, managing):
            self.assertIn("heal --monitor", text)
            self.assertIn("heal --restart", text)
            self.assertIn("heal --tmux", text)
            self.assertIn("heal --agents", text)
            self.assertIn("deprecated", text)

    def test_tmux_template_overwrites_and_disables_alternate_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tmux.conf")
            with open(path, "w") as handle:
                handle.write("user-stale-content\n")

            transport.ensure_camc_tmux_config(path=path)

            with open(path) as handle:
                text = handle.read()
        self.assertNotIn("user-stale-content", text)
        self.assertIn("set-window-option -g alternate-screen off", text)

    def test_heal_tmux_only_refreshes_template(self):
        refreshed = []
        class Store(object):
            def list(self):
                return []

        with patch.object(transport, "ensure_camc_tmux_config",
                          lambda: refreshed.append(True) or "/tmp/tmux.conf"), \
             patch.object(cli, "AgentStore", return_value=Store()), \
             patch.object(cli, "_do_heal", side_effect=AssertionError("general heal ran")), \
             patch.object(cli, "_refresh_embedded_skills_after_heal",
                          side_effect=AssertionError("skills refresh ran")), \
             patch.object(cli, "cmd_upgrade", side_effect=AssertionError("upgrade ran")):
            cli.cmd_heal(SimpleNamespace(tmux=True, agents=False, upgrade=False))

        self.assertEqual([True], refreshed)

    def test_heal_tmux_sources_each_local_socket(self):
        host = cli._sock.gethostname()
        agents = [
            {"hostname": host, "tmux_socket": "/tmp/one.sock", "tmux_bin": "/opt/tmux"},
            {"hostname": host, "tmux_socket": "/tmp/one.sock", "tmux_bin": "/opt/tmux"},
            {"hostname": "remote.example", "tmux_socket": "/tmp/remote.sock", "tmux_bin": "/opt/tmux"},
            {"hostname": host},
        ]
        calls = []

        class Store(object):
            def list(self):
                return agents

        class Result(object):
            returncode = 0

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return Result()

        with patch.object(transport, "ensure_camc_tmux_config", return_value="/tmp/tmux.conf"), \
             patch.object(cli, "AgentStore", return_value=Store()), \
             patch.object(cli.subprocess, "run", side_effect=run):
            cli.cmd_heal(SimpleNamespace(tmux=True, agents=False, upgrade=False))

        self.assertEqual([
            ["/opt/tmux", "-S", "/tmp/one.sock", "source-file", "/tmp/tmux.conf"],
            ["/opt/tmux", "-S", "/tmp/one.sock", "source-file", "/tmp/tmux.conf"],
        ], [call[0] for call in calls])

    def test_heal_monitor_flag_routes_to_default_monitor_heal(self):
        received = []
        with patch.object(cli.sys, "argv", ["camc", "heal", "--monitor"]), \
             patch.object(cli, "_ensure_logs_on_scratch"), \
             patch.object(cli, "cmd_heal", side_effect=lambda args: received.append(args)):
            cli.main()

        self.assertEqual(1, len(received))
        self.assertTrue(received[0].monitor)
        self.assertFalse(received[0].tmux)

    def test_restart_stops_one_local_monitor_before_starting_replacement(self):
        host = cli._sock.gethostname()
        agent = {"id": "abcd1234", "status": "running", "hostname": host,
                 "tmux_session": "cam-abcd1234-m"}
        events = []
        alive = {101: True}

        class Store(object):
            def list(self):
                return [agent]

            def update(self, agent_id, **fields):
                events.append(("update", agent_id, fields))

        class Proc(object):
            pid = 202

        def kill(pid, signal):
            events.append(("kill", pid, signal))
            if signal == cli.signal.SIGTERM:
                alive[pid] = False
            elif not alive.get(pid, False):
                raise ProcessLookupError()

        def popen(*args, **kwargs):
            self.assertFalse(alive[101], "replacement started before old monitor exited")
            alive[202] = True
            events.append(("start", args[0]))
            return Proc()

        with patch.object(cli, "AgentStore", return_value=Store()), \
             patch.object(cli, "tmux_session_exists", return_value=True), \
             patch.object(cli, "_find_monitor_pids", return_value=[(101, 4)]), \
             patch.object(cli.os, "kill", side_effect=kill), \
             patch.object(cli.subprocess, "Popen", side_effect=popen), \
             patch("builtins.open", mock_open()), \
             patch.object(cli.time, "sleep"):
            restarted, failed, skipped = cli._restart_local_monitors()

        self.assertEqual((1, 0, 0), (restarted, failed, skipped))
        self.assertEqual(("kill", 101, cli.signal.SIGTERM), events[0])
        self.assertLess(
            events.index(("kill", 101, cli.signal.SIGTERM)),
            next(index for index, event in enumerate(events) if event[0] == "start"))

    def test_heal_restart_runs_forced_restart_then_monitor_heal(self):
        calls = []
        with patch.object(cli, "_restart_local_monitors",
                          side_effect=lambda: calls.append("restart") or (1, 0, 0)), \
             patch.object(cli, "_do_heal", side_effect=lambda: calls.append("heal")), \
             patch.object(cli, "_refresh_embedded_skills_after_heal",
                          side_effect=lambda: calls.append("skills")):
            cli.cmd_heal(SimpleNamespace(restart=True, tmux=False,
                                         agents=False, upgrade=False))

        self.assertEqual(["restart", "heal", "skills"], calls)

    def test_top_level_help_hides_legacy_upgrade(self):
        output = StringIO()
        with patch.object(cli.sys, "argv", ["camc", "--help"]), \
             redirect_stdout(output), \
             self.assertRaises(SystemExit):
            cli.main()

        self.assertNotIn("upgrade", output.getvalue())

    def test_monitor_pid_permission_error_is_treated_as_alive(self):
        with patch.object(cli.os, "kill", side_effect=PermissionError()):
            self.assertTrue(cli._monitor_pid_alive(123))
