"""Minimal hard-launch behavior for tools without a camc adapter."""

from camc_pkg import adapters
from camc_pkg import cli
from camc_pkg import runtime_env
from camc_pkg import transport


def _runtime(path):
    return runtime_env.RuntimeEnv(
        env={"PATH": str(path), "HOME": str(path)},
        source="explicit",
        shell="",
        path=str(path),
    )


def test_unknown_tool_gets_minimal_non_automated_config():
    config = adapters._load_config("my-agent")

    assert config.command == ["my-agent"]
    assert config.config_dir == ".agents"
    assert config.prompt_after_launch is False
    assert config.confirm_rules == []
    assert config.readiness is None


def test_unknown_tool_on_path_skips_version_probe(tmp_path, monkeypatch):
    tool = tmp_path / "my-agent"
    tool.write_text("#!/bin/sh\nexit 99\n")
    tool.chmod(0o755)
    probes = []

    monkeypatch.setattr(
        runtime_env, "resolve_tmux_bin", lambda runtime: ("/bin/tmux", "golden")
    )

    def fake_probe(runtime, argv, timeout=5):
        probes.append(argv)
        return (0, "tmux 3.0")

    monkeypatch.setattr(runtime_env, "run_probe", fake_probe)

    result = runtime_env.check_tool_readiness(_runtime(tmp_path), "my-agent")

    assert not [message for level, message in result["issues"] if level == "error"]
    assert result["resolved"]["tool"] == str(tool)
    assert probes == [["/bin/tmux", "-V"]]


def test_missing_unknown_tool_is_passed_raw_to_tmux(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime_env, "resolve_tmux_bin", lambda runtime: ("/bin/tmux", "golden")
    )
    monkeypatch.setattr(
        runtime_env, "run_probe", lambda runtime, argv, timeout=5: (0, "tmux 3.0")
    )

    result = runtime_env.check_tool_readiness(_runtime(tmp_path), "missing-agent")

    assert not [message for level, message in result["issues"] if level == "error"]
    assert result["resolved"]["tool"] == "missing-agent"
    assert result["resolved"]["tool_resolution"]["source"] == "unresolved"


def test_unknown_tool_run_keeps_monitor_but_disables_automation(tmp_path, monkeypatch):
    captured = {}

    class Store:
        def save(self, record):
            captured["record"] = record

        def update(self, *_args, **_kwargs):
            captured["update"] = (_args, _kwargs)

    class Proc:
        pid = 4321

    class Args:
        tool = "my-agent"
        prompt = "do the thing"
        path = str(tmp_path / "work")
        no_inherit_env = False
        use_env_tool = False
        name = "other-test"
        auto_exit = True
        auto_exit_enable = True
        tag = []
        resume_session = None
        system_prompt = None
        system_file = None
        api = None
        no_default_api = True
        api_token = None
        no_api_proxy = False
        proxy_debug = False

    monkeypatch.setattr(
        runtime_env,
        "build_runtime_env",
        lambda **_kwargs: runtime_env.RuntimeEnv(
            env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            source="explicit", shell="/bin/sh", path="/usr/bin",
        ),
    )
    monkeypatch.setattr(cli, "_load_default_context", lambda: {})
    monkeypatch.setattr(
        cli, "_preflight",
        lambda *_args, **_kwargs: ([], {
            "tmux": "/bin/tmux", "tool": "my-agent",
            "tool_resolution": {"bin": "my-agent", "source": "unresolved"},
        }),
    )
    monkeypatch.setattr(cli, "_gen_agent_id", lambda: "abc12345")
    monkeypatch.setattr(cli, "install_manifest_skills", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "create_tmux_session", lambda *_a, **_kw: True)
    monkeypatch.setattr(transport, "ensure_camc_tmux_config", lambda: "")
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *_a, **_kw: b"tmux 3.0")
    monkeypatch.setattr(
        cli.subprocess, "Popen",
        lambda *args, **kwargs: captured.setdefault("monitor", (args, kwargs)) and Proc(),
    )
    monkeypatch.setattr(
        cli, "tmux_send_input",
        lambda *args, **kwargs: captured.setdefault("send", (args, kwargs)),
    )
    monkeypatch.setattr(
        cli.time, "sleep", lambda seconds: captured.setdefault("sleep", seconds),
    )
    monkeypatch.setattr(cli, "AgentStore", lambda: Store())

    cli.cmd_run(Args())

    record = captured["record"]
    assert record["task"]["tool"] == "others"
    assert record["task"]["requested_tool"] == "my-agent"
    assert record["task"]["auto_confirm"] is False
    assert record["task"]["auto_exit"] is False
    assert record["task"]["auto_exit_enable"] is False
    assert record["state"] == "idle"
    assert captured["update"] == (("abc12345",), {"pid": 4321})
    assert "_monitor" in captured["monitor"][0][0]
    assert captured["sleep"] == 2.0
    assert captured["send"] == (
        ("cam-abc12345", "do the thing"), {"send_enter": True},
    )
