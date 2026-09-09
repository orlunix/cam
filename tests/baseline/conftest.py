"""Hermetic fixtures for CamC's public baseline contracts."""

import os
import sys
from types import SimpleNamespace

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import adapters, api_resolver, api_store, api_token, cli, storage, transport
from camc_pkg.runtime_env import RuntimeEnv


@pytest.fixture(autouse=True)
def isolated_cam_home(tmp_path, monkeypatch):
    """Route every baseline state file away from the user's home and tmux."""
    home = tmp_path / "home"
    cam_home = home / ".cam"
    sockets = tmp_path / "sockets"
    work = tmp_path / "work"
    for path in (home, cam_home, sockets, work):
        path.mkdir()

    paths = SimpleNamespace(
        home=home,
        cam_home=cam_home,
        sockets=sockets,
        work=work,
        agents=cam_home / "agents.json",
        events=cam_home / "events.jsonl",
        messages=cam_home / "messages.jsonl",
        api_models=cam_home / "api-models.json",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CAM_HOME", str(cam_home))

    monkeypatch.setattr(cli, "CAM_DIR", str(cam_home))
    monkeypatch.setattr(cli, "CONFIGS_DIR", str(cam_home / "configs"))
    monkeypatch.setattr(cli, "LOGS_DIR", str(cam_home / "logs"))
    monkeypatch.setattr(cli, "PIDS_DIR", str(cam_home / "pids"))
    monkeypatch.setattr(cli, "SOCKETS_DIR", str(sockets))
    monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(paths.messages))
    monkeypatch.setattr(cli, "_ensure_logs_on_scratch", lambda: None)
    monkeypatch.setattr(cli, "_load_default_context", lambda: {})

    monkeypatch.setattr(storage, "AGENTS_FILE", str(paths.agents))
    monkeypatch.setattr(storage, "EVENTS_FILE", str(paths.events))
    monkeypatch.setattr(cli, "AgentStore", lambda: storage.AgentStore(str(paths.agents)))
    monkeypatch.setattr(cli, "EventStore", lambda: storage.EventStore(str(paths.events)))

    monkeypatch.setattr(adapters, "CONFIGS_DIR", str(cam_home / "configs"))
    monkeypatch.setattr(transport, "CAM_DIR", str(cam_home))
    monkeypatch.setattr(transport, "SOCKETS_DIR", str(sockets))

    monkeypatch.setattr(api_store, "CAM_DIR", str(cam_home))
    monkeypatch.setattr(api_store, "API_MODELS_FILE", str(paths.api_models))
    monkeypatch.setattr(api_store, "TOKEN_ENV_FILE", str(cam_home / "token.env"))
    monkeypatch.setattr(api_token, "TOKEN_ENV_FILE", str(cam_home / "token.env"))
    monkeypatch.setattr(api_token, "LEGACY_TOKEN_ENV", str(cam_home / "inference-hub.env"))
    monkeypatch.setattr(api_token, "MY_TOKENS_YAML", str(home / ".my_tokens.yaml"))
    monkeypatch.setattr(api_resolver, "CODEX_API_CONFIG_DIR", str(home / ".codex-api"))
    monkeypatch.setattr(api_resolver, "CODEX_LOGIN_SKILLS_DIR", str(home / ".codex" / "skills"))

    return paths


@pytest.fixture
def invoke_cli(monkeypatch):
    """Invoke the real argparse/dispatch boundary and return its exit code."""
    def invoke(*argv):
        monkeypatch.setattr(sys, "argv", ["camc"] + list(argv))
        try:
            cli.main()
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    return invoke


@pytest.fixture
def run_spy(isolated_cam_home, monkeypatch):
    """Replace only run's OS process/tmux boundary with deterministic spies."""
    state = SimpleNamespace(
        events=[], sessions=[], monitors=[], stopped_proxies=[],
        configs=[], launch_ok=True, agent_id="a1b2c3d4",
    )

    real_load_config = cli._load_config

    def load_config(tool):
        config = real_load_config(tool)
        state.configs.append((config, list(config.command)))
        return config

    monkeypatch.setattr(
        "camc_pkg.runtime_env.build_runtime_env",
        lambda **kwargs: RuntimeEnv(
            env={"PATH": "/fake/bin", "HOME": str(isolated_cam_home.home)},
            source="baseline", shell="/bin/sh", path="/fake/bin"),
    )

    def preflight(tool, tool_binary, workdir, **kwargs):
        state.events.append(("preflight", tool, workdir))
        binary = {
            "codex": "/fake/bin/codex",
            "claude": "/fake/bin/claude",
            "cursor": "/fake/bin/cursor-agent",
        }.get(tool, "/fake/bin/" + tool)
        return [], {
            "tmux": "/fake/bin/tmux",
            "tmux_source": "baseline",
            "tool": binary,
            "tool_resolution": {
                "bin": binary, "source": "baseline", "warnings": [],
            },
        }

    def create(session, command, workdir, **kwargs):
        state.events.append(("session", session))
        state.sessions.append((session, list(command), workdir, kwargs))
        return state.launch_ok

    class Proc(object):
        pid = 43210

    def popen(*args, **kwargs):
        state.events.append(("monitor", list(args[0])))
        state.monitors.append((args, kwargs))
        return Proc()

    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli, "_load_config", load_config)
    monkeypatch.setattr(cli, "_gen_agent_id", lambda: state.agent_id)
    monkeypatch.setattr(cli, "create_tmux_session", create)
    monkeypatch.setattr(cli, "install_manifest_skills", lambda *args, **kwargs: state.events.append(("skills", args[0])))
    monkeypatch.setattr(transport, "ensure_camc_tmux_config", lambda: "")
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *args, **kwargs: b"tmux 3.4")
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(cli, "_spawn_codex_session_binder", lambda *args: None)
    monkeypatch.setattr(cli, "_stop_api_proxy", lambda proc: state.stopped_proxies.append(proc))
    monkeypatch.setattr(cli, "_now_iso", lambda: "2026-09-05T12:00:00Z")
    return state
