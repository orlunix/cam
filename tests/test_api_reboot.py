from types import SimpleNamespace

import pytest
import json

from camc_pkg import cli, runtime_env


class FakeStore(object):
    def __init__(self, agent):
        self.agent = agent

    def get(self, _agent_id):
        return self.agent

    def save(self, agent):
        self.agent = agent

    def update(self, _agent_id, **fields):
        self.agent.update(fields)


@pytest.mark.parametrize("saved,explicit,default,expected", [
    (None, None, "auto", "auto"),
    (None, None, "none", "none"),
    ("auto", None, "none", "auto"),
    ("auto", "none", "auto", "none"),
    ("none", "auto", "none", "auto"),
])
def test_reboot_launcher_default_and_override(monkeypatch, saved, explicit, default, expected):
    agent = {"runtime": {}}
    if saved is not None:
        agent["runtime"]["launcher_mode"] = saved
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: default)
    monkeypatch.setattr(runtime_env, "build_runtime_env",
                        lambda: SimpleNamespace(env={"PATH": "/usr/bin"}))
    monkeypatch.setattr(cli, "_load_config", lambda _tool:
                        SimpleNamespace(command=["codex"], readiness={}))
    monkeypatch.setattr(cli, "detect_current_autorun_allocation", lambda _env: None)
    monkeypatch.setattr(cli, "resolve_local_target_tool_path", lambda *_a, **_kw: "/abs/codex")
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", lambda *_a, **_kw:
                        (["battach", "-L", "{command}", "123"],
                         {"transport": "battach", "_auto_plan": []}, []))
    plan = cli._prepare_reboot_launcher(agent, "codex", "/tmp", explicit)
    assert (plan["mode"] if plan else "none") == expected


def test_reboot_auto_rejects_local_fallback_before_stopping(monkeypatch):
    agent = {"id": "deadbeef", "session_id": "real-session",
             "task": {"tool": "claude"}, "runtime": {}}
    monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore(agent))
    monkeypatch.setattr(cli, "_prepare_reboot_launcher", lambda *_a:
                        (_ for _ in ()).throw(ValueError("no remote launcher")))
    monkeypatch.setattr(cli, "graceful_exit", lambda *_a, **_kw:
                        pytest.fail("must preserve running agent on preflight rejection"))
    with pytest.raises(SystemExit) as error:
        cli.cmd_migrate(SimpleNamespace(id="deadbeef", tool_launcher="auto"))
    assert error.value.code == 2


def test_reboot_auto_preserves_resume_and_adds_autorun_once(monkeypatch, tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abc"
    agent = {"id": "deadbeef", "session_id": session_id,
             "session_binding": "bound", "tmux_session": "cam-deadbeef",
             "context_path": str(tmp_path),
             "task": {"tool": "codex", "auto_confirm": True}}
    config = SimpleNamespace(runtime=None, launcher_autorun_args=["--yolo"])
    monkeypatch.setattr(cli, "_prepare_reboot_launcher", lambda *_a:
                        {"config": config, "runtime": SimpleNamespace(env={}),
                         "prefix": ["/bin/false", "-L", "{command}", "123"],
                         "metadata": {"transport": "battach", "_auto_plan": [],
                                      "_target_tool_path": "/abs/codex"},
                         "allocation": None, "preferred": "/abs/codex",
                         "binary": "codex", "version_args": ["--version"], "mode": "auto"})
    updated, sent, _stopped, _started = _run_reboot(
        monkeypatch, tmp_path, agent, config, launcher="auto")
    command = __import__("shlex").split(sent[0][1])[-1]
    assert command.endswith("/run")
    target = __import__("pathlib").Path(command).parent / "target-0"
    body = target.read_text()
    assert "resume " + session_id in body
    assert body.count("--yolo") == 1
    assert updated["session_id"] == session_id
    assert updated["runtime"]["launcher_mode"] == "auto"
    assert "_auto_plan" not in updated["runtime"]["launcher"]
    assert "_target_tool_path" not in updated["runtime"]["launcher"]
    assert updated["task"]["auto_confirm"] is False
    assert updated["task"]["prompt_in_command"] is True


@pytest.mark.parametrize("valid_directory", [True, False])
def test_remote_codex_recovery_requires_exact_status_and_transcript(monkeypatch, tmp_path, valid_directory):
    session_id = "12345678-1234-1234-1234-123456789abc"
    home = tmp_path / "codex"
    directory = home / "sessions" / "2026" / "10" / "11"
    directory.mkdir(parents=True)
    path = directory / ("rollout-2026-10-11T12-00-00-%s.jsonl" % session_id)
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "session_id": session_id, "timestamp": "2026-10-11T12:00:00Z",
        "cwd": str(tmp_path) if valid_directory else "/different"}}) + "\n")
    agent = {"id": "deadbeef", "state": "idle", "tmux_session": "cam-deadbeef",
             "context_path": str(tmp_path), "started_at": "2026-10-11T11:00:00Z",
             "runtime": {"launcher": {}, "storage": {"shared_home": str(home)}}}
    receipt = "/status\nDirectory: %s\nSession: %s\n› Ask Codex to do anything" % (tmp_path, session_id)
    screens = iter(["› Ask Codex to do anything", receipt])
    monkeypatch.setattr(cli, "capture_tmux", lambda _s: next(screens, receipt))
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    sent = []
    monkeypatch.setattr(cli, "tmux_send_input", lambda _s, text, **_kw: sent.append(text))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    found = cli._recover_remote_codex_session(agent, FakeStore(agent))
    assert found == (session_id if valid_directory else None)
    assert sent == ["/status"]


@pytest.mark.parametrize("state,prompt", [("working", "›"), ("idle", "› unsent user task")])
def test_remote_codex_recovery_preserves_busy_or_pending_input(monkeypatch, state, prompt):
    agent = {"id": "deadbeef", "state": state, "tmux_session": "cam-deadbeef",
             "runtime": {"launcher": {}}}
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    monkeypatch.setattr(cli, "capture_tmux", lambda _s: prompt)
    monkeypatch.setattr(cli, "tmux_send_input", lambda *_a, **_kw: pytest.fail("must not type"))
    assert cli._recover_remote_codex_session(agent, FakeStore(agent)) is None


def _run_reboot(monkeypatch, tmp_path, agent, config=None, launcher="none"):
    store = FakeStore(agent)
    sent = []
    stopped = []
    started = []

    class Process(object):
        pid = 456

    class ProxyProcess(object):
        pid = 789

    monkeypatch.setattr(cli, "AgentStore", lambda: store)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _session: True)
    monkeypatch.setattr(cli, "graceful_exit",
                        lambda session, **kwargs: stopped.append(
                            (session, kwargs)) or True)
    monkeypatch.setattr(cli, "_kill_monitor", lambda _agent: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(cli, "_load_config", lambda _tool: config or object())
    monkeypatch.setattr(cli, "_build_command",
                        lambda _config, _prompt, _workdir: ["codex"])
    monkeypatch.setattr(cli, "tmux_send_input",
                        lambda session, text, send_enter=False:
                        sent.append((session, text, send_enter)))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: Process())
    monkeypatch.setattr(cli, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "PIDS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_CAMC_SCRIPT", "/tmp/camc")
    monkeypatch.setattr(
        cli, "_stop_agent_api_proxy",
        lambda item, cleanup_home=True: stopped.append(
            ("proxy", item["id"], cleanup_home)))
    monkeypatch.setattr(cli, "_start_api_proxy",
                        lambda plan, owner, port=0:
                        (started.append((plan, owner, port)) or
                         (ProxyProcess(), port)))
    cli.cmd_migrate(SimpleNamespace(id=agent["id"], tool_launcher=launcher))
    return store.agent, sent, stopped, started


def test_codex_api_reboot_preserves_bound_session_model_and_proxy_port(
        monkeypatch, tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abc"
    model = "models/moonshotai/dep-kimi-k3"
    runtime_home = tmp_path / ".codex" / "deadbeef"
    agent = {
        "id": "deadbeef", "session_id": session_id,
        "session_binding": "bound", "tmux_session": "cam-deadbeef-m",
        "context_path": "/tmp/work", "status": "running",
        "task": {"tool": "codex", "name": "api-codex", "prompt": ""},
        "api": {"name": "kimi-k3", "provider": "inference-hub",
                "model": model, "proxy_pid": 111, "proxy_port": 32123},
    }
    plan = {"name": "kimi-k3", "provider": "inference-hub",
            "model": model, "base_url_suffix": "/v1"}
    monkeypatch.setattr("camc_pkg.api_resolver.resolve_run_plan",
                        lambda tool, name: dict(plan, tool=tool))
    configured = []
    storage = {
        "environment": "CODEX_HOME", "shared_home": str(tmp_path / ".codex"),
        "bootstrap_dirs": [str(tmp_path / "local" / "arg0")],
        "overlay": {"path": str(runtime_home),
                    "root": str(runtime_home.parent),
                    "owner_id": "deadbeef"},
    }
    monkeypatch.setattr(
        runtime_env, "build_runtime_env",
        lambda: SimpleNamespace(env={"PATH": "/clean/bin"}))
    monkeypatch.setattr(
        runtime_env, "configure_agent_runtime_home",
        lambda owner, adapter, runtime, api_plan=None, api_base_url="",
        isolate_tool_home=None:
        configured.append((owner, api_plan, api_base_url)) or storage)
    config = SimpleNamespace(runtime={"clear_environment": ["OLD_CODEX"]})

    updated, sent, stopped, started = _run_reboot(
        monkeypatch, tmp_path, agent, config=config)

    assert stopped[0] == ("cam-deadbeef-m", {"tool": "codex"})
    assert ("proxy", "deadbeef", False) in stopped
    assert started[0][1:] == ("deadbeef", 32123)
    assert updated["session_id"] == session_id
    assert updated["api"]["proxy_pid"] == 789
    assert updated["api"]["proxy_port"] == 32123
    assert "resume" in sent[0][1]
    assert session_id in sent[0][1]
    assert 'model=%s' % __import__("json").dumps(model) in sent[0][1]
    assert ('model_providers.camc-ihub.base_url=' +
            __import__("json").dumps("http://127.0.0.1:32123/v1")) in sent[0][1]
    assert "CODEX_HOME=%s" % runtime_home in sent[0][1]
    assert "PATH=/clean/bin" in sent[0][1]
    assert configured[0][0] == "deadbeef"
    assert configured[0][2] == "http://127.0.0.1:32123/v1"
    assert updated["runtime"]["storage"] == storage


def test_codex_login_reboot_does_not_start_or_stop_proxy(monkeypatch, tmp_path):
    session_id = "87654321-4321-4321-4321-cba987654321"
    agent = {
        "id": "feedface", "session_id": session_id,
        "session_binding": "bound", "tmux_session": "cam-feedface-m",
        "context_path": "/tmp/work", "status": "running",
        "task": {"tool": "codex", "name": "login-codex", "prompt": ""},
        "runtime": {"storage": {"isolate_tool_home": False}},
    }

    shared = tmp_path / ".codex"
    storage = {"environment": "CODEX_HOME", "shared_home": str(shared)}
    monkeypatch.setattr(
        runtime_env, "build_runtime_env",
        lambda: SimpleNamespace(env={"PATH": "/clean/bin"}))
    configured = []
    monkeypatch.setattr(
        runtime_env, "configure_agent_runtime_home",
        lambda *_a, **kw: configured.append(kw) or storage)
    config = SimpleNamespace(runtime={"clear_environment": []})
    updated, sent, stopped, started = _run_reboot(
        monkeypatch, tmp_path, agent, config=config)

    assert stopped == [("cam-feedface-m", {"tool": "codex"})]
    assert started == []
    assert updated["session_id"] == session_id
    assert "resume" in sent[0][1]
    assert "model=" not in sent[0][1]
    assert "CODEX_HOME=%s" % shared in sent[0][1]
    assert configured[0]["isolate_tool_home"] is False


def test_codex_api_reboot_migrates_retired_model_to_current_profile(
        monkeypatch, tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abc"
    agent = {
        "id": "deadbeef", "session_id": session_id,
        "session_binding": "bound", "tmux_session": "cam-deadbeef-m",
        "context_path": "/tmp/work", "status": "running",
        "task": {"tool": "codex", "name": "api-codex", "prompt": ""},
        "api": {"name": "kimi-k3", "provider": "inference-hub",
                "model": "models/moonshotai/dep-kimi-k3-max-preview",
                "proxy_pid": 111, "proxy_port": 32123},
    }
    plan = {
        "name": "kimi-k3", "provider": "inference-hub",
        "model": "models/moonshotai/dep-kimi-k3",
        "client_model": "models/moonshotai/dep-kimi-k3",
        "base_url_suffix": "/v1",
    }
    configured = []
    monkeypatch.setattr("camc_pkg.api_resolver.resolve_run_plan",
                        lambda tool, name: dict(plan, tool=tool))

    runtime_home = tmp_path / ".codex" / "deadbeef"
    storage = {
        "environment": "CODEX_HOME", "shared_home": str(tmp_path / ".codex"),
        "overlay": {"path": str(runtime_home),
                    "root": str(runtime_home.parent),
                    "owner_id": "deadbeef"},
    }
    monkeypatch.setattr(
        runtime_env, "build_runtime_env",
        lambda: SimpleNamespace(env={"PATH": "/clean/bin"}))
    monkeypatch.setattr(
        runtime_env, "configure_agent_runtime_home",
        lambda owner, adapter, runtime, api_plan=None, api_base_url="",
        isolate_tool_home=None:
        configured.append((owner, api_plan, api_base_url)) or storage)
    config = SimpleNamespace(runtime={"clear_environment": []})
    updated, _sent, _stopped, started = _run_reboot(
        monkeypatch, tmp_path, agent, config=config)

    assert started[0][0]["model"] == "models/moonshotai/dep-kimi-k3"
    assert updated["api"]["model"] == "models/moonshotai/dep-kimi-k3"
    assert configured[0][1]["model"] == "models/moonshotai/dep-kimi-k3"
    assert configured[0][2] == "http://127.0.0.1:32123/v1"
