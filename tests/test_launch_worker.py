"""The launcher preparation boundary must stay inside the existing pane."""

import argparse
import json
import os
import stat
import importlib.util
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from camc_pkg import cli, transport
from camc_pkg.storage import AgentStore


@pytest.fixture
def queued(tmp_path, monkeypatch):
    store = AgentStore(str(tmp_path / "agents.json"))
    monkeypatch.setattr(cli, "AgentStore", lambda: store)
    monkeypatch.setattr(cli, "PIDS_DIR", str(tmp_path / "pids"))
    monkeypatch.setattr(cli, "_gen_agent_id", lambda: "1234abcd")
    monkeypatch.setattr(transport, "ensure_camc_tmux_config", lambda: "")
    monkeypatch.setattr(cli, "_kill_monitor", lambda _a: None)
    monkeypatch.setattr(cli, "_stop_agent_api_proxy", lambda *_a, **_kw: None)
    calls = []
    monkeypatch.setattr(cli, "create_tmux_session",
                        lambda *a, **kw: calls.append((a, kw)) or True)
    args = argparse.Namespace(path=str(tmp_path / "work"), tool="claude",
                              name="async-test", prompt="", tool_launcher="auto",
                              tool_env=["TOKEN=private-test-value"])
    return store, calls, args


def test_queue_creates_pane_without_running_preparation(queued, monkeypatch):
    store, calls, args = queued
    monkeypatch.setattr(cli, "_cmd_run_prepared",
                        lambda *_a: pytest.fail("preparation ran before tmux"))
    cli.cmd_run(args)
    record = store.get("1234abcd")
    assert record["runtime"]["preparing"] is True
    assert record["pid"] is None  # no monitor boot timeout during preparation
    assert calls[0][0][1][-2:] == ["_launch_worker", "1234abcd"]
    assert "private-test-value" not in repr(calls)
    assert record["runtime"]["custom_launch"]["tool_env"] == args.tool_env
    assert stat.S_IMODE(os.stat(store._path).st_mode) == 0o600
    request = cli._launch_request_path("1234abcd")
    assert stat.S_IMODE(os.stat(request).st_mode) == 0o600
    with open(request) as stream:
        assert json.load(stream)["tool_env"] == args.tool_env


def test_none_also_prepares_inside_pane(queued, monkeypatch):
    store, calls, args = queued
    args.tool_launcher = "none"
    prepared = []
    monkeypatch.setattr(cli, "_cmd_run_prepared", prepared.append)
    cli.cmd_run(args)
    assert prepared == []
    assert len(calls) == 1
    assert store.get("1234abcd")["runtime"]["preparing"] is True


@pytest.mark.parametrize("path_kind", ["relative", "absolute", "home"])
def test_queue_resolves_system_file_before_changing_directory(
        queued, monkeypatch, tmp_path, path_kind):
    _, _, args = queued
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("caller prompt")
    args.system_file = {"relative": "prompt.md", "absolute": str(prompt),
                        "home": "~/prompt.md"}[path_kind]
    cli.cmd_run(args)
    values = json.loads(Path(cli._launch_request_path("1234abcd")).read_text())
    monkeypatch.chdir(args.path)
    assert cli.load_prompt_text(None, values["system_file"]) == "caller prompt"


def test_public_cli_queues_local_run(queued, monkeypatch):
    store, calls, args = queued
    monkeypatch.setattr(sys, "argv", ["camc", "run", "-t", "claude", "-n", args.name,
                                    "-p", args.path, "--tool-launcher", "none"])
    monkeypatch.setattr(cli, "_ensure_logs_on_scratch", lambda: None)
    cli.main()
    assert len(calls) == 1
    assert store.get("1234abcd")["runtime"]["preparing"] is True


def test_tmux_failure_removes_pending_request(queued, monkeypatch):
    store, _, args = queued
    monkeypatch.setattr(cli, "create_tmux_session", lambda *_a, **_kw: False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_run(args)
    assert exc.value.code == 1
    record = store.get("1234abcd")
    assert (record["status"], record["state"], record["phase"]) == ("dead", "failed", None)
    assert record["exit_reason"] == "Failed to create tmux session"
    assert not os.path.exists(cli._launch_request_path("1234abcd"))


@pytest.mark.parametrize("exit_code", [0, 7])
def test_worker_runs_foreground_command_in_existing_pane(
        queued, monkeypatch, tmp_path, exit_code):
    store, calls, args = queued
    cli.cmd_run(args)
    marker = tmp_path / "started"

    def prepare(actual, agent_id, launch):
        assert actual.tool_env == args.tool_env
        assert agent_id == "1234abcd"
        assert not os.path.exists(cli._launch_request_path(agent_id))
        launch.update(command=["/bin/sh", "-c", 'printf ready > "$MARKER"; exit %s' % exit_code],
                      workdir=str(tmp_path), env=dict(os.environ, MARKER=str(marker)))
        store.update(agent_id, runtime={})

    monkeypatch.setattr(cli, "_cmd_run_prepared", prepare)
    assert cli._run_launch_worker("1234abcd") == exit_code
    assert marker.read_text() == "ready"
    assert len(calls) == 1
    assert store.get("1234abcd")["state"] == ("stopped" if exit_code == 0 else "failed")


def test_prepare_failure_is_recorded_and_retains_pane(queued, monkeypatch):
    store, calls, args = queued
    cli.cmd_run(args)
    def fail(*_a):
        raise SystemExit(2)
    monkeypatch.setattr(cli, "_cmd_run_prepared", fail)
    assert cli._run_launch_worker("1234abcd") == 2
    assert store.get("1234abcd")["state"] == "failed"
    assert store.get("1234abcd")["runtime"]["preparing"] is False
    assert len(calls) == 1


def test_ctrl_c_during_preparation_cancels_launch(queued, monkeypatch):
    store, _, args = queued
    cli.cmd_run(args)
    def interrupted(*_args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli, "_cmd_run_prepared", interrupted)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("tool spawned"))
    assert cli._run_launch_worker("1234abcd") == 130
    assert store.get("1234abcd")["state"] == "failed"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_repeated_ctrl_c_reaches_tool_without_stopping_worker(tmp_path, exit_code):
    """Real foreground process group; the tool interrupts work but stays alive."""
    tool = tmp_path / "tool.py"
    tool.write_text('''import signal, sys, time
from pathlib import Path
root = Path(__file__).parent
count = 0
def interrupted(signum, frame):
    global count
    count += 1
    (root / ('interrupt-%s' % count)).touch()
signal.signal(signal.SIGINT, interrupted)
(root / 'ready').touch()
while not (root / 'finish').exists():
    time.sleep(.02)
sys.exit(int(sys.argv[1]))
''')
    worker = tmp_path / "worker.py"
    worker.write_text('''import json, os, signal, sys
from pathlib import Path
from camc_pkg import cli
from camc_pkg.storage import AgentStore
root = Path(__file__).parent
store = AgentStore(str(root / 'agents.json'))
cli.AgentStore = lambda: store
cli.PIDS_DIR = str(root)
cli._kill_monitor = lambda _a: (root / 'monitor-stopped').touch()
cli._stop_agent_api_proxy = lambda *_a, **_kw: (root / 'proxy-stopped').touch()
store.save({'id': '1234abcd', 'launch_id': 'test', 'status': "alive", 'runtime': {}})
request = root / '1234abcd.launch.json'
request.write_text(json.dumps({'_launch_id': 'test'}))
request.chmod(0o600)
def prepare(args, agent_id, launch):
    launch.update(command=[sys.executable, str(root / 'tool.py'), sys.argv[1]],
                  workdir=str(root), env=dict(os.environ))
cli._cmd_run_prepared = prepare
previous = signal.getsignal(signal.SIGINT)
code = cli._run_launch_worker('1234abcd')
assert signal.getsignal(signal.SIGINT) == previous
sys.exit(code)
''')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.Popen([sys.executable, str(worker), str(exit_code)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    def wait_for(name):
        deadline = time.monotonic() + 8
        while not (tmp_path / name).exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / name).exists(), "missing tool event: %s" % name
    try:
        wait_for("ready")
        for index in (1, 2):
            os.killpg(proc.pid, signal.SIGINT)
            wait_for("interrupt-%s" % index)
            time.sleep(.1)
            assert proc.poll() is None
            assert not (tmp_path / "proxy-stopped").exists()
            assert not (tmp_path / "monitor-stopped").exists()
            assert json.loads((tmp_path / "agents.json").read_text())[0]["status"] == "alive"
        (tmp_path / "finish").touch()
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == exit_code, (stdout, stderr)
        record = json.loads((tmp_path / "agents.json").read_text())[0]
        assert record["state"] == ("stopped" if exit_code == 0 else "failed")
        assert record["exit_reason"].endswith("code %s" % exit_code)
        assert (tmp_path / "proxy-stopped").exists()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=5)


def test_stop_preparation_cannot_launch_tool(queued, monkeypatch):
    store, _, args = queued
    cli.cmd_run(args)
    panes = []
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    monkeypatch.setattr(cli, "tmux_stop_agent_pane", panes.append)
    cli.cmd_stop(argparse.Namespace(id="1234abcd"))
    assert panes == ["cam-1234abcd"]
    assert store.get("1234abcd")["state"] == "stopped"
    assert store.get("1234abcd")["runtime"]["preparing"] is False
    assert not os.path.exists(cli._launch_request_path("1234abcd"))


@pytest.mark.parametrize("tool", ["claude", "codex", "cursor", "pi"])
def test_stop_resets_pane_for_every_tool(queued, monkeypatch, tool):
    store, _, args = queued
    args.tool = tool
    cli.cmd_run(args)
    store.update("1234abcd", runtime={})
    calls = []
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    def reset(session):
        assert store.get("1234abcd")["status"] == "alive"
        calls.append(session)
    monkeypatch.setattr(cli, "tmux_stop_agent_pane", reset)
    monkeypatch.setattr(cli, "_find_claude_pid", lambda _s: pytest.fail("PID lookup used"))
    cli.cmd_stop(argparse.Namespace(id="1234abcd"))
    assert calls == ["cam-1234abcd"]
    assert store.get("1234abcd")["state"] == "stopped"


def test_stop_failure_does_not_claim_stopped(queued, monkeypatch):
    store, _, args = queued
    cli.cmd_run(args)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    def fail(_session):
        raise RuntimeError("tmux failure")
    monkeypatch.setattr(cli, "tmux_stop_agent_pane", fail)
    monkeypatch.setattr(cli, "_kill_monitor", lambda _a: pytest.fail("monitor killed"))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_stop(argparse.Namespace(id="1234abcd"))
    assert exc.value.code == 1
    assert store.get("1234abcd")["status"] == "alive"
    assert os.path.exists(cli._launch_request_path("1234abcd"))


@pytest.mark.parametrize("reset_rc", [0, 1])
def test_pane_stop_targets_only_pane_zero_with_explicit_shell(monkeypatch, reset_rc):
    calls = []
    monkeypatch.setattr(transport, "_tmux_base", lambda _s: ["tmux", "-S", "/private/socket"])
    def run(argv):
        calls.append(argv)
        if "list-panes" in argv:
            return 0, "0:0 %7\n0:1 %8\n1:0 %9\n"
        return reset_rc, ""
    monkeypatch.setattr(transport, "_run", run)
    if reset_rc:
        with pytest.raises(RuntimeError):
            transport.tmux_stop_agent_pane("cam-test")
    else:
        transport.tmux_stop_agent_pane("cam-test")
    assert calls[1][3:7] == ["respawn-pane", "-k", "-t", "%7"]
    assert calls[1][-1].startswith("exec /")
    assert calls[1][-1].endswith(" -i")


@pytest.mark.parametrize("rc, output", [(0, "0:1 %8\n"), (1, "server error")])
def test_pane_stop_does_not_touch_other_panes_or_ignore_errors(monkeypatch, rc, output):
    monkeypatch.setattr(transport, "_tmux_base", lambda _s: ["tmux"])
    calls = []
    def run(argv):
        calls.append(argv)
        return rc, output
    monkeypatch.setattr(transport, "_run", run)
    if rc:
        with pytest.raises(RuntimeError):
            transport.tmux_stop_agent_pane("cam-test")
    else:
        transport.tmux_stop_agent_pane("cam-test")
    assert len(calls) == 1


def test_stop_during_preparation_does_not_spawn_tool(queued, monkeypatch):
    store, _, args = queued
    cli.cmd_run(args)
    def prepare(_args, agent_id, _launch):
        store.update(agent_id, status="stopped")
    monkeypatch.setattr(cli, "_cmd_run_prepared", prepare)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("tool spawned"))
    cli._run_launch_worker("1234abcd")
    assert store.get("1234abcd")["state"] == "stopped"


def test_worker_rejects_readable_request(queued, monkeypatch):
    store, _, args = queued
    cli.cmd_run(args)
    os.chmod(cli._launch_request_path("1234abcd"), 0o644)
    monkeypatch.setattr(cli, "_cmd_run_prepared", lambda *_a: pytest.fail("unsafe request used"))
    assert cli._run_launch_worker("1234abcd") == 1
    assert store.get("1234abcd")["state"] == "failed"


def test_source_worker_uses_module_entrypoint(queued, monkeypatch):
    _, calls, args = queued
    monkeypatch.setattr(cli, "_CAMC_SCRIPT", "/source/camc_pkg/__main__.py")
    cli.cmd_run(args)
    assert calls[0][0][1][1:3] == ["-m", "camc_pkg"]


def test_heal_does_not_start_monitor_while_preparing(queued, monkeypatch):
    _, _, args = queued
    cli.cmd_run(args)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda *_a: pytest.fail("preparing agent probed"))
    assert cli._restart_local_monitors() == (0, 0, 1)


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("launch_test_builder", str(root / "build_camc.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    path = tmp_path_factory.mktemp("launch-artifact") / "camc"
    # Keep real CLI heal's orphan scan inside this test HOME.
    path.write_text(builder.build().replace(
        'SOCKETS_DIR = "/tmp/cam-sockets"',
        'SOCKETS_DIR = os.path.join(CAM_DIR, "sockets")'))
    path.chmod(0o755)
    return path


@pytest.mark.skipif(not shutil.which("tmux"), reason="requires tmux")
@pytest.mark.parametrize("tool", ["codex", "claude", "cursor", "pi", "my-tool"])
@pytest.mark.parametrize("launcher", ["none", "custom"])
def test_built_worker_starts_tool_in_real_tmux_and_is_removed(
        candidate, tmp_path, tool, launcher):
    """Real tmux and worker, deterministic tools; no credentials or farm."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}")  # preflight checks existence only
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    marker = tmp_path / "argv.json"
    binary = binary_dir / ("cursor-agent" if tool == "cursor" else tool)
    binary.write_text(
        "#!%s\nimport json, os, sys, time\n"
        "if '--version' in sys.argv: print('tool 1.0'); sys.exit(0)\n"
        "with open(os.environ['WORKER_MARKER'], 'w') as f: json.dump(sys.argv, f)\n"
        "with open(os.environ['WORKER_MARKER'] + '.pid', 'w') as f: f.write(str(os.getpid()))\n"
        "print('WORKER_TOOL_READY\\n> ', flush=True)\ntime.sleep(60)\n" % sys.executable)
    binary.chmod(0o755)
    custom = binary_dir / "launcher"
    custom.write_text('#!/bin/sh\nexec /bin/sh -c "$1"\n')
    custom.chmod(0o755)
    env = dict(os.environ, HOME=str(home), WORKER_MARKER=str(marker),
               CAMC_PRELUDE_DISABLE="all")
    for name in ("TMUX", "TMUX_PANE", "CLAUDECODE", "CODEX_HOME", "CODEX_SQLITE_HOME"):
        env.pop(name, None)
    name = "worker-smoke"
    workdir = tmp_path / "work"
    workdir.mkdir()
    (tmp_path / "prompt.md").write_text("CALLER_SYSTEM_PROMPT")
    (workdir / "prompt.md").write_text("WRONG_DIRECTORY_PROMPT")
    prompt_args = [] if tool == "my-tool" else ["--system-file", "prompt.md"]

    def run(*args):
        return subprocess.run([str(candidate)] + list(args), env=env, cwd=str(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=15)

    try:
        result = run("run", "-t", tool, "-n", name, "-p", str(tmp_path / "work"),
                     "--tool-dir", str(binary_dir), "--no-default-api",
                     "--tool-launcher", str(custom) if launcher == "custom" else "none",
                     *prompt_args)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Preparing in tmux" in result.stdout
        deadline = time.time() + 12
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert marker.exists(), run("capture", name, "--lines", "100").stdout
        argv = json.loads(marker.read_text())
        assert argv[0] == str(binary)
        assert "--allowed-tools" not in argv
        if tool == "claude":
            assert argv.count("--dangerously-skip-permissions") == 1
            assert "--permission-mode" not in argv
        elif tool == "codex":
            assert argv.count("--yolo") == 1
        elif tool == "cursor":
            assert argv.count("--force") == 1
        elif tool == "pi":
            assert argv.count("--approve") == 1
        record = json.loads((home / ".cam" / "agents.json").read_text())[0]
        if tool in ("claude", "codex"):
            deadline = time.time() + 15
            while record.get("phase") != "started" and time.time() < deadline:
                time.sleep(.2)
                record = json.loads((home / ".cam" / "agents.json").read_text())[0]
            assert (record["status"], record["phase"]) == ("alive", "started"), record
        if prompt_args:
            assert record["task"]["system_prompt"] == "CALLER_SYSTEM_PROMPT"
        assert not record["runtime"].get("preparing")
        assert not list((home / ".cam" / "pids").glob("*.launch.json"))
        pane = run("capture", name, "--lines", "100")
        assert "WORKER_TOOL_READY" in pane.stdout
        if tool in ("claude", "codex", "my-tool"):
            # A stale terminal label must not hide a tool that is demonstrably live.
            registry = home / ".cam" / "agents.json"
            stale = json.loads(registry.read_text())
            stale[0].update(status="completed", state="idle", completed_at="yesterday",
                            exit_reason="old monitor guessed completion")
            registry.write_text(json.dumps(stale))
            healed_live = run("heal")
            assert healed_live.returncode == 0, healed_live.stdout + healed_live.stderr
            restored = json.loads(registry.read_text())[0]
            assert restored["status"] == "alive" and restored["phase"] == "started"
            assert restored["state"] in ("running", "idle")
            assert restored["exit_reason"] is None and restored["completed_at"] is None
        pid = int(Path(str(marker) + ".pid").read_text())
        tmux_base = [record["tmux_bin"], "-S", record["runtime"]["tmux"]["socket"]]
        other = subprocess.check_output(
            tmux_base + ["split-window", "-d", "-P", "-F", "#{pane_id} #{pane_pid}",
                         "-t", record["tmux_session"] + ":0.0", "/bin/sh"],
            env=env).decode().split()
        old_server_pid = subprocess.check_output(
            tmux_base + ["display-message", "-p", "#{pid}"], env=env).decode().strip()
        marker.unlink()
        rebooted = run("reboot", name)
        assert rebooted.returncode == 0, rebooted.stdout + rebooted.stderr
        deadline = time.time() + 12
        while not marker.exists() and time.time() < deadline:
            time.sleep(.1)
        assert marker.exists(), run("capture", name, "--lines", "100").stdout
        new_argv = json.loads(marker.read_text())
        assert "--resume" not in new_argv and "resume" not in new_argv
        if tool == "claude":
            assert new_argv[new_argv.index("--session-id") + 1] != argv[argv.index("--session-id") + 1]
        new_record = json.loads((home / ".cam" / "agents.json").read_text())[0]
        assert new_record["id"] == record["id"]
        assert new_record["launch_id"] != record["launch_id"]
        old_status = Path(record["runtime"]["custom_launch"]["exit_status_path"])
        new_status = Path(new_record["runtime"]["custom_launch"]["exit_status_path"])
        assert new_status != old_status
        # Simulate a late old remote exit, including a legacy ID-only receipt.
        for status in (old_status, old_status.parent / (record["id"] + ".tool-exit")):
            status.write_text("7\n")
            Path(str(status) + ".launcher").write_text("qsub\n")
        time.sleep(.5)
        assert json.loads((home / ".cam" / "agents.json").read_text())[0]["status"] == "alive"
        new_server_pid = subprocess.check_output(
            tmux_base + ["display-message", "-p", "#{pid}"], env=env).decode().strip()
        assert new_server_pid != old_server_pid
        deadline = time.time() + 3
        while (Path("/proc/%s" % pid).exists() or Path("/proc/%s" % other[1]).exists()) and time.time() < deadline:
            time.sleep(.05)
        assert not Path("/proc/%s" % pid).exists(), "old tool survived reboot"
        assert not Path("/proc/%s" % other[1]).exists(), "old extra pane survived reboot"
        pid = int(Path(str(marker) + ".pid").read_text())
        other = subprocess.check_output(
            tmux_base + ["split-window", "-d", "-P", "-F", "#{pane_id} #{pane_pid}",
                         "-t", record["tmux_session"] + ":0.0", "/bin/sh"],
            env=env).decode().split()
        stopped = run("stop", name)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "Stopped agent" in stopped.stdout
        deadline = time.time() + 3
        while Path("/proc/%s" % pid).exists() and time.time() < deadline:
            time.sleep(.05)
        assert not Path("/proc/%s" % pid).exists(), "tool survived pane reset"
        stopped_record = json.loads((home / ".cam" / "agents.json").read_text())[0]
        assert stopped_record["state"] == "stopped"
        healed = run("heal")
        assert healed.returncode == 0, healed.stdout + healed.stderr
        after_heal = json.loads((home / ".cam" / "agents.json").read_text())[0]
        assert after_heal == stopped_record
        assert Path(record["runtime"]["tmux"]["socket"]).exists()
        other_pid = subprocess.check_output(
            tmux_base + ["display-message", "-p", "-t", other[0], "#{pane_pid}"],
            env=env).decode().strip()
        assert other_pid == other[1] and Path("/proc/%s" % other_pid).exists()
        sent = run("send", name, "--text", "printf 'STOPPED_%s_READY\\n' SHELL")
        assert sent.returncode == 0
        deadline = time.time() + 3
        while time.time() < deadline:
            output = run("capture", name, "--lines", "100").stdout
            if "STOPPED_SHELL_READY" in output:
                break
            time.sleep(.1)
        assert "STOPPED_SHELL_READY" in output
    finally:
        removed = run("rm", name)
        if (home / ".cam" / "agents.json").exists():
            assert json.loads((home / ".cam" / "agents.json").read_text()) == [], removed.stderr


def test_old_worker_exit_cannot_overwrite_reboot(queued, monkeypatch):
    store, _, args = queued
    cli.cmd_run(args)
    def superseded(actual, agent_id, launch):
        store.update(agent_id, launch_id="new-launch", status="running")
        Path(cli._launch_request_path(agent_id)).write_text("new-request")
        raise SystemExit(2)
    monkeypatch.setattr(cli, "_cmd_run_prepared", superseded)
    assert cli._run_launch_worker("1234abcd") == 2
    assert store.get("1234abcd")["status"] == "alive"
    assert Path(cli._launch_request_path("1234abcd")).read_text() == "new-request"
