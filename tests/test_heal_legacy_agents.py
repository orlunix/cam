"""Focused coverage for explicit legacy-agent recovery in ``heal --agents``."""

import json
from types import SimpleNamespace

from camc_pkg import cli
from camc_pkg.storage import AgentStore


def test_migrate_legacy_agent_renames_live_session_and_preserves_current_record(
        tmp_path, monkeypatch):
    """A live legacy record becomes one current record without touching peers."""
    path = tmp_path / "agents.json"
    current = {
        "id": "current01",
        "task": {"name": "keep", "tool": "codex", "prompt": "unchanged"},
        "context_path": "/work/current",
        "transport_type": "local",
        "status": "running", "state": "idle",
        "tmux_session": "cam-current01", "hostname": "host",
    }
    legacy = {
        "id": "old-id", "name": "old-agent", "tool": "claude",
        "prompt": "continue", "path": "/work/legacy", "session": "legacy-pane",
        "monitor_pid": 4321, "hostname": "host", "status": "running",
    }
    path.write_text(json.dumps([current, legacy]))
    store = AgentStore(str(path))
    renamed = []
    monkeypatch.setattr(cli, "tmux_session_exists", lambda session: session == "legacy-pane")
    monkeypatch.setattr(cli, "tmux_rename_session",
                        lambda old, new: renamed.append((old, new)) or True)
    monkeypatch.setattr(cli, "uuid4", lambda: SimpleNamespace(hex="a1b2c3d4feed"))

    migrated, skipped, backup = cli._migrate_legacy_agents(store)

    assert (migrated, skipped) == (1, 0)
    assert renamed == [("legacy-pane", "cam-a1b2c3d4-m")]
    assert backup and backup.exists()
    assert json.loads(backup.read_text()) == [current, legacy]
    records = store.list()
    assert records[0] == current
    assert len(records) == 2
    restored = records[1]
    assert restored["id"] == "a1b2c3d4"
    assert restored["tmux_session"] == "cam-a1b2c3d4-m"
    assert restored["context_path"] == "/work/legacy"
    assert restored["pid"] == 4321
    assert restored["task"]["name"] == "old-agent"
    assert restored["task"]["tool"] == "claude"
    assert "session" not in restored and "path" not in restored


def test_migrate_legacy_agent_leaves_dead_session_untouched(tmp_path, monkeypatch):
    path = tmp_path / "agents.json"
    legacy = {"id": "old", "session": "gone", "path": "/work/old"}
    path.write_text(json.dumps([legacy]))
    store = AgentStore(str(path))
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _session: False)

    migrated, skipped, backup = cli._migrate_legacy_agents(store)

    assert (migrated, skipped, backup) == (0, 1, None)
    assert store.list() == [legacy]


def test_heal_agents_only_runs_legacy_migration(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_migrate_legacy_agents",
                        lambda _store: (calls.append("migrate") or (0, 0, None)))
    monkeypatch.setattr(cli, "_do_heal",
                        lambda: (_ for _ in ()).throw(AssertionError("general heal ran")))

    cli.cmd_heal(SimpleNamespace(agents=True, upgrade=False))

    assert calls == ["migrate"]
