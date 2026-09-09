"""Public `camc run` baseline contract."""

import json

import pytest

from camc_pkg import cli, storage, utils


@pytest.mark.parametrize(
    "tool_args, expected_tool, expected_binary, binary_index",
    [
        ([], "codex", "/fake/bin/codex", 0),
        (["--tool", "claude"], "claude", "/fake/bin/claude", 3),
        (["-t", "cursor"], "cursor", "/fake/bin/cursor-agent", 0),
    ],
)
def test_run_selects_tool_and_creates_one_session_and_monitor(
        invoke_cli, run_spy, isolated_cam_home,
        tool_args, expected_tool, expected_binary, binary_index):
    code = invoke_cli(
        "run", "baseline prompt", "--name", "baseline",
        "--path", str(isolated_cam_home.work), "--no-default-api",
        *tool_args
    )

    assert code == 0
    assert len(run_spy.sessions) == 1
    assert len(run_spy.monitors) == 1
    session, command, workdir, kwargs = run_spy.sessions[0]
    assert session == "cam-a1b2c3d4"
    assert command[binary_index] == expected_binary
    assert workdir == str(isolated_cam_home.work)
    assert kwargs["inherit_env"] is True
    if expected_tool == "codex":
        assert [
            "-c",
            utils._workspace_trust_config(str(isolated_cam_home.work)),
        ] == command[3:5]
    config, original_command = run_spy.configs[0]
    assert config.command == original_command

    records = storage.AgentStore(str(isolated_cam_home.agents)).list()
    assert len(records) == 1
    assert records[0]["task"]["tool"] == expected_tool
    assert records[0]["task"]["prompt"] == "baseline prompt"
    assert records[0]["pid"] == 43210
    runtime = records[0]["runtime"]
    assert runtime["schema"] == "camc-runtime/1"
    assert runtime["python"]["bin"]
    assert runtime["tmux"] == {
        "bin": "/fake/bin/tmux", "version": "tmux 3.4",
        "socket": str(isolated_cam_home.sockets / "cam-a1b2c3d4.sock"),
        "config": "", "config_mode": "system-default", "source": "baseline",
    }
    assert runtime["tool"]["name"] == expected_tool
    assert runtime["tool"]["bin"] == expected_binary
    assert runtime["tool"]["source"] == "baseline"
    assert runtime["shell"]["mode"] == "tmux-default-shell"


def test_adapter_declared_workspace_trust_targets_git_root_and_escapes_path(
        tmp_path):
    from camc_pkg.adapters import AdapterConfig

    repo = tmp_path / 'workspace with "quotes"'
    workdir = repo / "nested" / "node"
    workdir.mkdir(parents=True)
    (repo / ".git").mkdir()
    config = AdapterConfig({"launch": {
        "command": ["codex"], "trust_workspace": True,
    }})

    assert cli._build_command(config, "", str(workdir)) == [
        "codex", "-c",
        'projects={%s={trust_level="trusted"},%s={trust_level="trusted"}}' % (
            json.dumps(str(repo.resolve())),
            json.dumps(str(workdir.resolve())),
        ),
    ]


def test_run_preflight_error_precedes_session_monitor_and_record(
        invoke_cli, run_spy, isolated_cam_home, monkeypatch):
    def rejected(*args, **kwargs):
        run_spy.events.append(("preflight-rejected",))
        return [("error", "tool is unavailable")], {}

    monkeypatch.setattr(cli, "_preflight", rejected)
    code = invoke_cli(
        "run", "do not launch", "--path", str(isolated_cam_home.work),
        "--no-default-api",
    )

    assert code == 1
    assert run_spy.events == [("preflight-rejected",)]
    assert run_spy.sessions == []
    assert run_spy.monitors == []
    assert storage.AgentStore(str(isolated_cam_home.agents)).list() == []


def test_run_launch_failure_leaves_no_record_or_monitor(
        invoke_cli, run_spy, isolated_cam_home):
    run_spy.launch_ok = False

    code = invoke_cli(
        "run", "failed launch", "--path", str(isolated_cam_home.work),
        "--no-default-api",
    )

    assert code == 1
    assert len(run_spy.sessions) == 1
    assert run_spy.monitors == []
    assert storage.AgentStore(str(isolated_cam_home.agents)).list() == []
    assert run_spy.stopped_proxies == [None]


def test_run_binds_selected_api_metadata_into_record(
        invoke_cli, run_spy, isolated_cam_home, monkeypatch):
    plan = {
        "name": "baseline-api", "provider": "stub", "model": "model-v1",
        "mode": "proxy", "route": None, "local_base_url": "http://stub.invalid",
        "client_model": "model-v1", "proxy_required": True,
        "base_url_suffix": "/v1", "auth_key": "stub_key",
        "env_names": ["STUB_TOKEN"], "token_file": None, "token_key": None,
        "env": {"_API_USE_RESOLVED_TOKEN": "1"},
    }
    monkeypatch.setattr("camc_pkg.api_store.resolve_run_api_name",
                        lambda *args, **kwargs: ("baseline-api", "cli"))
    monkeypatch.setattr("camc_pkg.api_resolver.resolve_run_plan",
                        lambda *args, **kwargs: dict(plan))
    monkeypatch.setattr("camc_pkg.api_token.resolve_token",
                        lambda *args, **kwargs: ("top-secret", "cli"))
    monkeypatch.setattr("camc_pkg.api_resolver.apply_resolved_token",
                        lambda tool, env, token: {"STUB_TOKEN": token})

    class Proxy(object):
        pid = 777

    def start_proxy(selected, owner):
        selected.update(proxy_port=1777, proxy_pid=777,
                        proxy_base_url="http://127.0.0.1:1777")
        return Proxy(), 1777

    monkeypatch.setattr(cli, "_start_api_proxy", start_proxy)
    monkeypatch.setattr("camc_pkg.api_resolver.ensure_codex_api_config_dir",
                        lambda *args, **kwargs: str(isolated_cam_home.home / ".codex-api"))
    code = invoke_cli(
        "run", "api prompt", "--api", "baseline-api",
        "--api-token", "top-secret", "--path", str(isolated_cam_home.work),
    )

    assert code == 0
    record = storage.AgentStore(str(isolated_cam_home.agents)).list()[0]
    assert record["api"] == {
        "name": "baseline-api", "provider": "stub", "model": "model-v1",
        "mode": "proxy", "route": None, "base_url": "http://stub.invalid",
        "client_model": "model-v1", "proxy_port": 1777, "proxy_pid": 777,
    }
    assert "top-secret" not in repr(record)
