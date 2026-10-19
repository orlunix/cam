"""Public `camc rm` baseline contract."""

import tarfile
from argparse import Namespace

from camc_pkg import cli, storage, transport


def _agent(agent_id, name):
    return {
        "id": agent_id,
        "task": {"name": name, "tool": "codex"},
        "tmux_session": "cam-" + agent_id,
        "pid": 1000 + len(agent_id),
        "api": {"proxy_pid": 9000},
    }


def test_rm_resolves_one_exact_target_and_cleans_only_owned_resources(
        invoke_cli, isolated_cam_home, monkeypatch):
    store = storage.AgentStore(str(isolated_cam_home.agents))
    target = _agent("aaa11111", "alpha")
    sibling = _agent("bbb22222", "alphabet-sibling")
    store.save(target)
    store.save(sibling)
    socket_path = isolated_cam_home.sockets / "cam-aaa11111.sock"
    socket_path.write_text("private fake socket")
    calls = []

    monkeypatch.setattr(cli, "_kill_monitor",
                        lambda agent: calls.append(("monitor", agent["id"])))
    monkeypatch.setattr(cli, "_stop_agent_api_proxy",
                        lambda agent, **kwargs: calls.append(("proxy", agent["id"])))
    monkeypatch.setattr(cli, "tmux_kill_session",
                        lambda session: calls.append(("tmux", session)) or True)
    monkeypatch.setattr(cli, "_find_tmux_socket",
                        lambda session: str(socket_path) if session == "cam-aaa11111" else None)
    monkeypatch.setattr(transport, "_tmux_server_pid_for_socket", lambda path: None)
    monkeypatch.setattr(cli, "cmd_archive",
                        lambda args: calls.append(("archive", args.id)))

    code = invoke_cli("rm", "alpha")

    assert code == 0
    assert calls == [
        ("monitor", "aaa11111"),
        ("proxy", "aaa11111"),
        ("tmux", "cam-aaa11111"),
    ]
    assert not socket_path.exists()
    assert [agent["id"] for agent in store.list()] == ["bbb22222"]


def test_rm_archives_only_when_requested(
        invoke_cli, isolated_cam_home, monkeypatch):
    store = storage.AgentStore(str(isolated_cam_home.agents))
    store.save(_agent("aaa11111", "alpha"))
    calls = []
    monkeypatch.setattr(cli, "cmd_archive",
                        lambda args: calls.append(args.id))
    monkeypatch.setattr(cli, "_kill_monitor", lambda agent, **kwargs: None)
    monkeypatch.setattr(cli, "_stop_agent_api_proxy", lambda agent, **kwargs: None)
    monkeypatch.setattr(cli, "tmux_kill_session", lambda session: True)
    monkeypatch.setattr(cli, "_find_tmux_socket", lambda session: None)

    assert invoke_cli("rm", "aaa", "--archive") == 0
    assert calls == ["aaa11111"]


def test_real_archive_repairs_a_dangling_archive_directory_symlink(
        isolated_cam_home):
    store = storage.AgentStore(str(isolated_cam_home.agents))
    agent = _agent("aaa11111", "archive-agent")
    agent["tmux_session"] = ""
    store.save(agent)
    archive_target = isolated_cam_home.home / "scratch" / "archives"
    archive_dir = isolated_cam_home.cam_home / "archives"
    archive_dir.symlink_to(archive_target, target_is_directory=True)
    assert archive_dir.is_symlink()
    assert not archive_dir.exists()
    cli.cmd_archive(Namespace(
        id=agent["id"], output=str(archive_dir), session_id=None))

    assert archive_target.is_dir()
    archives = list(archive_dir.glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(str(archives[0]), "r:gz") as archive:
        assert "agent.json" in archive.getnames()


def test_real_archive_rejects_an_existing_non_directory_output(
        isolated_cam_home):
    store = storage.AgentStore(str(isolated_cam_home.agents))
    agent = _agent("aaa11111", "archive-agent")
    agent["tmux_session"] = ""
    store.save(agent)
    archive_path = isolated_cam_home.cam_home / "archives"
    archive_path.write_text("not a directory")

    import pytest
    with pytest.raises(SystemExit) as exc:
        cli.cmd_archive(Namespace(
            id=agent["id"], output=str(archive_path), session_id=None))

    assert exc.value.code == 1
    assert archive_path.read_text() == "not a directory"


def test_rm_can_remove_the_last_registry_record(
        invoke_cli, isolated_cam_home, monkeypatch):
    store = storage.AgentStore(str(isolated_cam_home.agents))
    store.save(_agent("aaa11111", "only-agent"))
    monkeypatch.setattr(cli, "_kill_monitor", lambda agent, **kwargs: None)
    monkeypatch.setattr(cli, "_stop_agent_api_proxy", lambda agent, **kwargs: None)
    monkeypatch.setattr(cli, "tmux_kill_session", lambda session: True)
    monkeypatch.setattr(cli, "_find_tmux_socket", lambda session: None)

    assert invoke_cli("rm", "only-agent") == 0
    assert store.list() == []


def test_store_refuses_to_replace_a_corrupt_nonempty_registry(
        isolated_cam_home):
    corrupt = b'{"id": "truncated"'
    isolated_cam_home.agents.write_bytes(corrupt)
    store = storage.AgentStore(str(isolated_cam_home.agents))

    assert store.remove("truncated") is False
    assert isolated_cam_home.agents.read_bytes() == corrupt


def test_rm_missing_target_is_clear_nonzero_and_non_mutating(
        invoke_cli, isolated_cam_home, capsys):
    store = storage.AgentStore(str(isolated_cam_home.agents))
    store.save(_agent("bbb22222", "sibling"))

    code = invoke_cli("rm", "missing")

    assert code == 1
    assert "not found" in capsys.readouterr().err
    assert [agent["id"] for agent in store.list()] == ["bbb22222"]
