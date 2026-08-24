from types import SimpleNamespace

from camc_pkg import cli


class FakeStore(object):
    def __init__(self, agent):
        self.agent = agent

    def get(self, _agent_id):
        return self.agent

    def save(self, agent):
        self.agent = agent

    def update(self, _agent_id, **fields):
        self.agent.update(fields)


def _run_reboot(monkeypatch, tmp_path, agent):
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
    monkeypatch.setattr(cli, "_load_config", lambda _tool: object())
    monkeypatch.setattr(cli, "_build_command",
                        lambda _config, _prompt, _workdir: ["codex"])
    monkeypatch.setattr(cli, "tmux_send_input",
                        lambda session, text, send_enter=False:
                        sent.append((session, text, send_enter)))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: Process())
    monkeypatch.setattr(cli, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_CAMC_SCRIPT", "/tmp/camc")
    monkeypatch.setattr(cli, "_stop_agent_api_proxy",
                        lambda item: stopped.append(("proxy", item["id"])))
    monkeypatch.setattr(cli, "_start_api_proxy",
                        lambda plan, owner, port=0:
                        (started.append((plan, owner, port)) or
                         (ProxyProcess(), port)))
    cli.cmd_migrate(SimpleNamespace(id=agent["id"]))
    return store.agent, sent, stopped, started


def test_codex_api_reboot_preserves_bound_session_model_and_proxy_port(
        monkeypatch, tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abc"
    model = "nvidia/moonshotai/eccn-kimi-k3"
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
    monkeypatch.setattr(
        "camc_pkg.api_resolver.ensure_codex_api_config_dir",
        lambda *_args, **_kwargs: "/tmp/.codex-api",
    )

    updated, sent, stopped, started = _run_reboot(
        monkeypatch, tmp_path, agent)

    assert stopped[0] == ("cam-deadbeef-m", {"tool": "codex"})
    assert ("proxy", "deadbeef") in stopped
    assert started[0][1:] == ("deadbeef", 32123)
    assert updated["session_id"] == session_id
    assert updated["api"]["proxy_pid"] == 789
    assert updated["api"]["proxy_port"] == 32123
    assert "resume" in sent[0][1]
    assert session_id in sent[0][1]
    assert 'model=%s' % __import__("json").dumps(model) in sent[0][1]
    assert ('model_providers.camc-ihub.base_url=' +
            __import__("json").dumps("http://127.0.0.1:32123/v1")) in sent[0][1]


def test_codex_login_reboot_does_not_start_or_stop_proxy(monkeypatch, tmp_path):
    session_id = "87654321-4321-4321-4321-cba987654321"
    agent = {
        "id": "feedface", "session_id": session_id,
        "session_binding": "bound", "tmux_session": "cam-feedface-m",
        "context_path": "/tmp/work", "status": "running",
        "task": {"tool": "codex", "name": "login-codex", "prompt": ""},
    }

    updated, sent, stopped, started = _run_reboot(
        monkeypatch, tmp_path, agent)

    assert stopped == [("cam-feedface-m", {"tool": "codex"})]
    assert started == []
    assert updated["session_id"] == session_id
    assert "resume" in sent[0][1]
    assert "model=" not in sent[0][1]


def test_codex_api_reboot_migrates_retired_model_to_current_profile(
        monkeypatch, tmp_path):
    session_id = "12345678-1234-1234-1234-123456789abc"
    agent = {
        "id": "deadbeef", "session_id": session_id,
        "session_binding": "bound", "tmux_session": "cam-deadbeef-m",
        "context_path": "/tmp/work", "status": "running",
        "task": {"tool": "codex", "name": "api-codex", "prompt": ""},
        "api": {"name": "kimi-k3", "provider": "inference-hub",
                "model": "nvidia/moonshotai/eccn-kimi-k3-max-preview",
                "proxy_pid": 111, "proxy_port": 32123},
    }
    plan = {
        "name": "kimi-k3", "provider": "inference-hub",
        "model": "nvidia/moonshotai/eccn-kimi-k3",
        "client_model": "nvidia/moonshotai/eccn-kimi-k3",
        "base_url_suffix": "/v1",
    }
    configured = []
    monkeypatch.setattr(
        "camc_pkg.api_resolver.ensure_codex_api_config_dir",
        lambda base_url, name, require_endpoint="responses", model_id=None:
        configured.append((base_url, name, require_endpoint, model_id)) or
        "/tmp/.codex-api",
    )
    monkeypatch.setattr("camc_pkg.api_resolver.resolve_run_plan",
                        lambda tool, name: dict(plan, tool=tool))

    updated, _sent, _stopped, started = _run_reboot(
        monkeypatch, tmp_path, agent)

    assert started[0][0]["model"] == "nvidia/moonshotai/eccn-kimi-k3"
    assert updated["api"]["model"] == "nvidia/moonshotai/eccn-kimi-k3"
    assert configured == [(
        "http://127.0.0.1:32123/v1", "kimi-k3", "responses",
        "nvidia/moonshotai/eccn-kimi-k3",
    )]
