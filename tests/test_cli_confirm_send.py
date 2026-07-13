import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import cli  # noqa: E402


def test_send_confirm_response_single_char_uses_keypress(monkeypatch):
    calls = []

    def fake_key(session, key):
        calls.append(("key", session, key))
        return True

    def fake_input(session, text, send_enter=True):
        calls.append(("input", session, text, send_enter))
        return True

    monkeypatch.setattr(cli, "tmux_send_key", fake_key)
    monkeypatch.setattr(cli, "tmux_send_input", fake_input)

    assert cli._send_confirm_response("cam-test", "1", True) is True
    assert calls == [("key", "cam-test", "1"), ("key", "cam-test", "Enter")]


def test_send_confirm_response_multichar_uses_input(monkeypatch):
    calls = []

    monkeypatch.setattr(cli, "tmux_send_key", lambda session, key: calls.append(("key", key)) or True)
    monkeypatch.setattr(
        cli, "tmux_send_input",
        lambda session, text, send_enter=True: calls.append(("input", text, send_enter)) or True)

    assert cli._send_confirm_response("cam-test", "yes", True) is True
    assert calls == [("input", "yes", True)]


def test_cmd_send_does_not_acknowledge_failed_delivery(monkeypatch, capsys):
    class FakeStore:
        def get(self, agent_id):
            assert agent_id == "deadbeef"
            return {"tmux_session": "cam-deadbeef", "tool": "claude"}

    monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore())
    monkeypatch.setattr(cli, "tmux_send_input", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_send(SimpleNamespace(
            id="deadbeef", text="hello", file=None, stdin=False,
            no_enter=False,
        ))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Sent." not in captured.out
    assert "Failed to send input" in captured.err
