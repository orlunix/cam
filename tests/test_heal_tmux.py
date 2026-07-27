"""Focused coverage for CAMC-owned tmux template refresh."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from camc_pkg import cli, transport


class TestHealTmux(unittest.TestCase):
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
        with patch.object(transport, "ensure_camc_tmux_config",
                          lambda: refreshed.append(True) or "/tmp/tmux.conf"), \
             patch.object(cli, "_do_heal", side_effect=AssertionError("general heal ran")), \
             patch.object(cli, "_refresh_embedded_skills_after_heal",
                          side_effect=AssertionError("skills refresh ran")), \
             patch.object(cli, "cmd_upgrade", side_effect=AssertionError("upgrade ran")):
            cli.cmd_heal(SimpleNamespace(tmux=True, agents=False, upgrade=False))

        self.assertEqual([True], refreshed)
