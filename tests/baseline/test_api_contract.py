"""Public API-profile and `run --api` baseline contract."""

import json

import pytest

from camc_pkg import api_resolver, api_store, cli, storage


def _seed_api_file(enabled=("glm-5.2",), catalog_path=""):
    data = api_store._default_seed()
    data["providers"][api_store.DEFAULT_PROVIDER]["catalog_path"] = catalog_path
    for name, entry in data["apis"].items():
        entry["enabled"] = name in enabled
        entry["enabled_reason"] = "baseline" if entry["enabled"] else "disabled"
    api_store.save_api_models(data)
    return data


def test_api_list_check_and_default_commands_are_local_and_durable(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    _seed_api_file()
    monkeypatch.setenv("INFERENCE_HUB_TOKEN", "check-secret")
    monkeypatch.setattr(api_store, "sync_metadata_in_data",
                        lambda data, provider, cost_map=None: 0)
    monkeypatch.setattr(
        api_store.urllib.request, "urlopen",
        lambda *args, **kwargs: pytest.fail("api check attempted real network"),
    )

    assert invoke_cli("api", "list") == 0
    listed = capsys.readouterr().out
    assert "glm-5.2" in listed
    assert "check-secret" not in listed
    assert invoke_cli("api", "check") == 0
    checked = capsys.readouterr().out
    assert "reachable" in checked
    assert "check-secret" not in checked

    assert invoke_cli("api", "default", "set", "glm", "--tool", "codex") == 0
    capsys.readouterr()
    assert invoke_cli("api", "default", "show", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    codex = next(row for row in rows if row["tool"] == "codex")
    assert codex["api"] == "glm-5.2"
    assert invoke_cli("api", "default", "clear", "--tool", "codex") == 0
    data = api_store.load_api_models()
    assert "codex" not in data.get("defaults", {})


@pytest.mark.parametrize(
    "tool, profile, expected",
    [
        ("codex", "does-not-exist", "unknown API"),
        ("claude", "kimi-k3", "not supported"),
        ("codex", "deepseek-v4-pro", "disabled"),
    ],
)
def test_api_rejects_invalid_incompatible_and_disabled_profiles_before_launch(
        invoke_cli, run_spy, isolated_cam_home, monkeypatch, capsys,
        tool, profile, expected):
    _seed_api_file(enabled=("kimi-k3",))
    monkeypatch.setattr(api_store, "check_provider", lambda data: {})

    code = invoke_cli(
        "run", "blocked", "--tool", tool, "--api", profile,
        "--api-token", "secret", "--path", str(isolated_cam_home.work),
    )

    assert code == 1
    assert expected in capsys.readouterr().err
    assert run_spy.sessions == []
    assert storage.AgentStore(str(isolated_cam_home.agents)).list() == []


def test_api_alias_and_cli_secret_precedence_reach_run_without_secret_output(
        invoke_cli, run_spy, isolated_cam_home, monkeypatch, capsys):
    _seed_api_file()
    monkeypatch.setenv("INFERENCE_HUB_TOKEN", "environment-secret")

    class Proxy(object):
        pid = 777

    def start_proxy(plan, owner):
        plan.update(proxy_port=1777, proxy_pid=777,
                    proxy_base_url="http://127.0.0.1:1777")
        return Proxy(), 1777

    monkeypatch.setattr(cli, "_start_api_proxy", start_proxy)
    monkeypatch.setattr(
        api_resolver, "ensure_codex_api_config_dir",
        lambda *args, **kwargs: str(isolated_cam_home.home / ".codex-api"),
    )

    code = invoke_cli(
        "run", "api baseline", "--tool", "codex", "--api", "glm",
        "--api-token", "cli-secret", "--path", str(isolated_cam_home.work),
    )
    output = capsys.readouterr()

    assert code == 0
    assert "cli-secret" not in output.out + output.err
    assert "environment-secret" not in output.out + output.err
    _session, _command, _workdir, kwargs = run_spy.sessions[0]
    assert kwargs["env"][api_resolver.CODEX_API_ENV_KEY] == "cli-secret"
    record = storage.AgentStore(str(isolated_cam_home.agents)).list()[0]
    assert record["api"]["name"] == "glm-5.2"
    assert "cli-secret" not in repr(record)
    assert "environment-secret" not in repr(record)
