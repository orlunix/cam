from types import SimpleNamespace

from camc_pkg import cli


def test_status_omits_unreliable_alive_probe(monkeypatch):
    agent = {
        "id": "abc12345",
        "task": {"name": "worker", "tool": "codex", "prompt": ""},
        "status": "running",
        "state": "testing",
        "context_path": "/tmp/work",
        "tmux_session": "cam-abc12345",
        "started_at": "2026-08-13T00:00:00Z",
    }

    class Store(object):
        def list(self):
            return [agent]

    captured = {}
    monkeypatch.setattr(cli, "AgentStore", Store)
    monkeypatch.setattr(
        cli, "tmux_session_exists",
        lambda _session: (_ for _ in ()).throw(
            AssertionError("status must not perform an unreliable live probe")))
    monkeypatch.setattr(
        cli, "print_detail",
        lambda pairs, **_kwargs: captured.update(pairs=pairs))

    cli.cmd_status(SimpleNamespace(
        agent_id="abc12345", id=None, hash=None, json=False))

    fields = dict(captured["pairs"])
    assert fields["Status"] == "running"
    assert fields["State"] == "testing"
    assert "Alive" not in fields
