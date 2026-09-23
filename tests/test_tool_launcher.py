"""Focused coverage for ``camc run --tool-launcher``."""

import os
import shlex
import shutil
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

from camc_pkg import adapters, cli, runtime_env, tool_launcher, transport


ROOT = Path(__file__).resolve().parents[1]


def _without_env_prefix(argv):
    if not argv or argv[0] != "env":
        return argv
    index = 1
    while index < len(argv):
        if argv[index] == "-u" and index + 1 < len(argv):
            index += 2
        elif "=" in argv[index] and not argv[index].startswith("-"):
            index += 1
        else:
            break
    return argv[index:]


def _adapter(name):
    path = ROOT / "src" / "cam" / "adapters" / "configs" / (name + ".toml")
    config = adapters.AdapterConfig(adapters._parse_toml(path.read_text()))
    # Runtime-home behavior is independently covered and would only add local
    # filesystem setup noise to these launch-argv tests.
    config.runtime = None
    config.isolate_tool_home = False
    return config


@pytest.mark.parametrize("value, expected", [
    ("ssh -tt root@192.168.1.100 {command}",
     ["ssh", "-tt", "root@192.168.1.100", "{command}"]),
    ("qsub -q o_cpu_4G -m rel8x -app r89docker --qsla autorun_sla "
     "-app affinity -Is {command}",
     ["qsub", "-q", "o_cpu_4G", "-m", "rel8x", "-app", "r89docker",
      "--qsla", "autorun_sla", "-app", "affinity", "-Is", "{command}"]),
])
def test_launcher_is_parsed_with_shlex(value, expected):
    assert tool_launcher.parse_tool_launcher(value) == expected


@pytest.mark.parametrize("value", [
    "", "ssh host", "ssh {command} {command}", "ssh x{command}",
    "ssh '{command",
])
def test_launcher_rejects_bad_placeholder_or_shlex(value):
    with pytest.raises(ValueError, match="--tool-launcher"):
        tool_launcher.parse_tool_launcher(value)


@pytest.mark.parametrize("value, expected", [
    ("auto", ("auto", "")),
    ("qsub-4g", ("qsub-4g", "")),
    ("ssh-auto", ("ssh-auto", "")),
    ("ssh-auto:1796123900", ("ssh-auto", "1796123900")),
    ("ssh host {command}", None),
    ("ssh-auto:not-a-job", None),
])
def test_named_launcher_parser(value, expected):
    assert tool_launcher.named_tool_launcher(value) == expected


def _autorun_jobs_output():
    return "\n".join([
        "1729124669|RUN|autorun_sla|Y|host-project|mobile|tcsh|tcsh|"
        "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE=o_pri_interactive_cpu_16G",
        "1761147411|RUN|autorun_sla|Y|host-regress|mobile|tcsh|tcsh|"
        "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE=o_regress_cpu_64G_33D",
        "1796123900|RUN|autorun_sla|Y|host-ai|other|dvnet_autorun|tcsh|"
        "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE=interactive_asic_256G_33D "
        "QJT_AI_AGENT_TYPE=codex",
        # Newer duplicate host wins before the host is deduplicated.
        "1796124140|RUN|autorun_sla|Y|host-ai|other|dvnet_autorun|tcsh|"
        "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE=interactive_asic_256G_33D "
        "QJT_AI_AGENT_TYPE=codex",
        "1796125000|PEND|autorun_sla|Y|-|mobile|tcsh|tcsh|",
        "1796126000|RUN|other_sla|Y|unsafe|mobile|tcsh|tcsh|",
    ])


def _long_job(candidate, user="tester"):
    return (
        "Job <%s>, User <%s>, Service Class <autorun_sla>, Status <RUN>, "
        "Interactive pseudo-terminal shell mode\nStarted on <%s>;\n" %
        (candidate["job_id"], user, candidate["host"]))


def _fake_launcher_binary(_env, names):
    known = {"bjobs", "ssh", "nvprojectname", "qsub"}
    for name in names:
        base = os.path.basename(name)
        if base in known:
            return "/fake/" + base
    return ""


def test_autorun_parser_ranks_then_deduplicates_hosts():
    candidates = tool_launcher._parse_autorun_jobs(
        _autorun_jobs_output(), "codex", current_project="mobile")

    assert [item["job_id"] for item in candidates] == [
        "1729124669", "1761147411", "1796124140"]
    assert candidates[-1]["job_name"] == "dvnet_autorun"
    assert tool_launcher._parse_autorun_jobs(
        _autorun_jobs_output(), "codex",
        explicit_job_id="1796123900")[0]["job_id"] == "1796123900"


def test_long_metadata_verifies_owner_and_exec_host():
    candidate = {"job_id": "1796123900", "host": "host-ai"}
    assert tool_launcher._verify_autorun_job_long(
        _long_job(candidate), candidate, expected_user="tester")
    assert not tool_launcher._verify_autorun_job_long(
        _long_job(candidate).replace("host-ai", "changed-host"), candidate,
        expected_user="tester")
    assert not tool_launcher._verify_autorun_job_long(
        _long_job(candidate, user="other"), candidate,
        expected_user="tester")


def test_auto_skips_changed_and_unreachable_hosts(monkeypatch, tmp_path):
    calls = []

    def fake_process(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 0, "mobile\n", ""
        if "-noheader" in argv:
            return 0, _autorun_jobs_output(), ""
        if argv[:3] == ["/fake/bjobs", "-l", "-UF"]:
            job_id = argv[-1]
            candidate = next(item for item in tool_launcher._parse_autorun_jobs(
                _autorun_jobs_output(), "codex")
                             if item["job_id"] == job_id)
            if job_id == "1729124669":
                return 0, _long_job(candidate).replace(
                    "host-project", "changed-host"), ""
            return 0, _long_job(candidate), ""
        if argv[0] == "/fake/ssh":
            return ((255, "", "connection refused")
                    if argv[-2] == "host-regress" else (0, "", ""))
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path),
        {"USER": "tester", "PATH": "/fake"})

    assert launcher == ["/fake/ssh", "-tt", "host-ai", "{command}"]
    assert metadata["job_id"] == "1796124140"
    assert metadata["fallback"] is True
    assert any("metadata/EXEC_HOST changed" in item for item in notices)
    assert any("connection refused" in item for item in notices)
    assert not any("/fake/qsub" in call for call in calls)


def test_explicit_ssh_auto_job_is_user_scoped(monkeypatch, tmp_path):
    calls = []

    def fake_process(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 0, "mobile\n", ""
        if "-noheader" in argv:
            return 0, _autorun_jobs_output(), ""
        if argv[:3] == ["/fake/bjobs", "-l", "-UF"]:
            candidate = tool_launcher._parse_autorun_jobs(
                _autorun_jobs_output(), "codex",
                explicit_job_id="1796123900")[0]
            return 0, _long_job(candidate), ""
        if argv[0] == "/fake/ssh":
            return 0, "", ""
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, _notices = tool_launcher.resolve_named_tool_launcher(
        ("ssh-auto", "1796123900"), "codex", str(tmp_path),
        {"USER": "tester", "PATH": "/fake"})

    discovery = next(call for call in calls if "-noheader" in call)
    assert discovery[-3:] == ["-u", "tester", "1796123900"]
    assert launcher == ["/fake/ssh", "-tt", "host-ai", "{command}"]
    assert metadata["job_id"] == "1796123900"


def test_explicit_ssh_auto_rejects_host_without_target_tool(
        monkeypatch, tmp_path):
    def fake_process(argv, **_kwargs):
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 0, "mobile\n", ""
        if "-noheader" in argv:
            return 0, _autorun_jobs_output(), ""
        if argv[:3] == ["/fake/bjobs", "-l", "-UF"]:
            candidate = tool_launcher._parse_autorun_jobs(
                _autorun_jobs_output(), "codex",
                explicit_job_id="1796123900")[0]
            return 0, _long_job(candidate), ""
        if argv[0] == "/fake/ssh":
            assert "command -v codex" in argv[-1]
            return 127, "", "__CAMC_TARGET_TOOL_MISSING__\n"
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)

    with pytest.raises(ValueError, match="reachable but target tool codex"):
        tool_launcher.resolve_named_tool_launcher(
            ("ssh-auto", "1796123900"), "codex", str(tmp_path),
            {"USER": "tester", "PATH": "/fake"}, target_tool="codex")


def test_auto_uses_qsub_only_after_all_ssh_candidates_fail(
        monkeypatch, tmp_path):
    calls = []

    def fake_process(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 0, "mobile\n", ""
        if "-noheader" in argv:
            return 0, _autorun_jobs_output(), ""
        if argv[:3] == ["/fake/bjobs", "-l", "-UF"]:
            job_id = argv[-1]
            candidate = next(item for item in tool_launcher._parse_autorun_jobs(
                _autorun_jobs_output(), "codex")
                             if item["job_id"] == job_id)
            return 0, _long_job(candidate), ""
        if argv[0] == "/fake/ssh":
            return 255, "", "unreachable"
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path),
        {"USER": "tester", "PATH": "/fake"})

    assert launcher == [
        "/fake/nvprojectname", "run", "mobile", "/fake/qsub",
        "-q", "o_cpu_4G", "-m", "rel8x", "-app", "r89docker",
        "--qsla", "autorun_sla", "-app", "affinity", "-Is", "{command}"]
    assert metadata == {
        "preset": "auto", "transport": "qsub", "project": "mobile",
        "fallback": True}
    assert notices[-1].startswith("auto: submitting a new qsub allocation")
    assert len([call for call in calls if call[0] == "/fake/ssh"]) == 3
    # Resolver only constructs the final launcher; it never submits qsub.
    assert not any(call[0] == "/fake/qsub" for call in calls)


def test_qsub_launcher_fails_closed_when_project_cannot_resolve(
        monkeypatch, tmp_path):
    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(
        tool_launcher, "_launcher_process",
        lambda _argv, **_kwargs: (1, "", "project is not registered"))

    with pytest.raises(ValueError, match="project resolution failed"):
        tool_launcher.resolve_named_tool_launcher(
            ("qsub-4g", ""), "codex", str(tmp_path), {"PATH": "/fake"})


def test_remote_launcher_rejects_api_profile():
    with pytest.raises(ValueError, match="--no-default-api"):
        tool_launcher.validate_tool_launcher_api("auto", "ihub-direct")
    tool_launcher.validate_tool_launcher_api("auto", None)


def test_launcher_target_environment_carries_only_tool_home():
    assert tool_launcher.launcher_target_environment(
        {"PATH": "/shared/tool/bin:/usr/bin", "CODEX_HOME": "/shared/codex"},
        {"environment": "CODEX_HOME"}) == {
            "CODEX_HOME": "/shared/codex",
        }


def test_update_cannot_enable_auto_confirm_for_launcher_agent(monkeypatch):
    agent = {
        "id": "launcher-agent",
        "task": {"auto_confirm": False, "tags": []},
        "runtime": {"launcher": {"preset": "auto"}},
    }
    saved = []

    class Store(object):
        def get(self, _agent_id):
            return agent

        def save(self, record):
            saved.append(record)

    args = types.SimpleNamespace(
        id="launcher-agent", name=None, auto_confirm=True, tag=[], untag=[])
    monkeypatch.setattr(cli, "AgentStore", Store)

    cli.cmd_update(args)

    assert saved[0]["task"]["auto_confirm"] is False


def test_complete_quoted_command_replaces_one_launcher_argv():
    tool = ["codex", "--yolo", "two words", "quote'safe", "$(no-eval)"]
    launch = tool_launcher.build_launcher_command(
        ["ssh", "-tt", "root@example", "{command}"], tool)

    assert launch[:3] == ["ssh", "-tt", "root@example"]
    assert len(launch) == 4
    assert shlex.split(launch[3]) == tool
    assert launch[3] == " ".join(shlex.quote(item) for item in tool)


def test_launcher_bootstraps_target_dirs_then_execs_without_eval():
    launch = tool_launcher.build_launcher_command(
        ["ssh", "host", "{command}"], ["codex", "two words"],
        bootstrap_dirs=["/tmp/user/codex arg0", "/tmp/user/sqlite"],
        target_env={"CODEX_HOME": "/shared/home with space/.codex"},
        target_unset=["CODEX_SQLITE_HOME"])

    command = launch[-1]
    assert command.startswith("umask 077 && mkdir -p -- ")
    assert "chmod 700 --" in command
    assert "&& exec env -u CODEX_SQLITE_HOME " in command
    assert shlex.quote("CODEX_HOME=/shared/home with space/.codex") in command
    assert command.endswith(" codex 'two words'")
    assert "eval" not in command


def _capture_run(tmp_path, monkeypatch, tool, launcher, runtime_spec=None):
    captured = {}
    config = _adapter(tool)
    if runtime_spec is not None:
        config.runtime = runtime_spec
        config.isolate_tool_home = True

    class Store(object):
        def save(self, record):
            captured["record"] = record

        def update(self, *_args, **_kwargs):
            pass

    class Proc(object):
        pid = 1234

    class Args(object):
        prompt = "launcher prompt"
        path = str(tmp_path / "work")
        name = "launcher-test"
        auto_exit = False
        auto_exit_enable = False
        tag = []
        resume_session = None
        no_inherit_env = False
        use_env_tool = False
        tool_dir = None
        tool_env = []
        tool_args = []
        tool_launcher = launcher
        system_prompt = None
        system_file = None
        api = None
        no_default_api = True
        api_token = None

    Args.tool = tool

    monkeypatch.setattr(cli, "_load_config", lambda _tool: config)
    monkeypatch.setattr(cli, "_load_default_context", lambda: {})
    monkeypatch.setattr(
        runtime_env, "build_runtime_env",
        lambda **_kwargs: runtime_env.RuntimeEnv(
            env={"PATH": "/launcher/bin", "HOME": str(tmp_path),
                 "SECRET_TOKEN": "must-not-enter-manifest"},
            source="explicit", shell="/bin/sh", path="/launcher/bin"))

    def fake_preflight(selected, binary, workdir, **kwargs):
        captured["preflight"] = (selected, binary, kwargs)
        return [], {
            "tmux": "/bin/tmux",
            "launcher": "/launcher/bin/fake-launcher",
            "launcher_resolution": {
                "bin": "/launcher/bin/fake-launcher", "source": "env"},
            "tool": binary,
            "tool_resolution": {
                "bin": binary, "source": "target-environment",
                "warnings": []},
        }

    monkeypatch.setattr(cli, "_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_gen_agent_id", lambda: "abc12345")
    monkeypatch.setattr(cli, "install_manifest_skills", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "PIDS_DIR", str(tmp_path / "pids"))
    monkeypatch.setattr(
        cli, "create_tmux_session",
        lambda session, command, workdir, **kwargs:
        captured.setdefault("launch", (session, list(command), workdir, kwargs))
        is not None)
    monkeypatch.setattr(transport, "ensure_camc_tmux_config", lambda: "")
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *_a, **_kw: b"tmux 3.4")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: Proc())
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli, "AgentStore", lambda: Store())
    monkeypatch.setattr(cli, "_spawn_codex_session_binder", lambda *_a: None)

    cli.cmd_run(Args())
    return captured


def test_codex_launcher_adds_yolo_and_keeps_remote_tool_name(tmp_path, monkeypatch):
    secret = "SECRET_TOKEN=manifest-must-not-store-this"
    captured = _capture_run(
        tmp_path, monkeypatch, "codex",
        "qsub --credential %s -q o_cpu_4G -Is {command}" % secret)

    launch = captured["launch"][1]
    assert launch[:-1] == [
        "/launcher/bin/fake-launcher", "--credential", secret,
        "-q", "o_cpu_4G", "-Is"]
    target = _without_env_prefix(shlex.split(launch[-1]))
    assert target[0] == "codex"
    assert target[0] != "/launcher/bin/codex"
    assert "--yolo" in target
    assert captured["launch"][3]["exit_status_path"].endswith("abc12345.tool-exit")
    manifest = captured["record"]["runtime"]
    assert manifest["launcher"] == {
        "executable": "/launcher/bin/fake-launcher",
        "target_tool": "codex",
        "argv_count": 7,
        "command_index": 6,
    }
    assert secret not in repr(manifest)
    assert "SECRET_TOKEN" not in repr(manifest)
    assert captured["record"]["task"]["auto_confirm"] is False


def test_named_launcher_metadata_is_safe_and_observable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "resolve_named_tool_launcher",
        lambda *_args, **_kwargs: (
            ["fake-launcher", "host-a", "{command}"],
            {"preset": "auto", "transport": "ssh", "job_id": "123",
             "host": "host-a", "fallback": True},
            ["auto: using job 123 on host-a"]))

    captured = _capture_run(tmp_path, monkeypatch, "codex", "auto")

    assert captured["launch"][1][0] == "/launcher/bin/fake-launcher"
    target = _without_env_prefix(shlex.split(captured["launch"][1][-1]))
    assert target[0] == "codex"
    assert "--yolo" in target
    assert captured["record"]["runtime"]["launcher"] == {
        "executable": "/launcher/bin/fake-launcher",
        "target_tool": "codex",
        "argv_count": 3,
        "command_index": 2,
        "preset": "auto",
        "transport": "ssh",
        "job_id": "123",
        "host": "host-a",
        "fallback": True,
    }
    assert captured["record"]["task"]["auto_confirm"] is False


def test_pi_launcher_approves_project_resources(tmp_path, monkeypatch):
    captured = _capture_run(
        tmp_path, monkeypatch, "pi",
        "qsub -q o_cpu_4G -Is {command}")

    target = _without_env_prefix(shlex.split(captured["launch"][1][-1]))
    assert target == ["pi", "--approve"]
    assert captured["record"]["task"]["tool"] == "pi"
    assert captured["record"]["state"] == "initializing"


def test_claude_launcher_uses_skip_permissions_without_auto_mode(tmp_path, monkeypatch):
    captured = _capture_run(
        tmp_path, monkeypatch, "claude",
        "ssh -tt root@192.168.1.100 {command}")

    target = shlex.split(captured["launch"][1][-1])
    assert "--dangerously-skip-permissions" in target
    assert "--permission-mode" not in target
    assert "auto" not in target
    assert target[target.index("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1") + 1] == "claude"


def test_cmd_run_launcher_bootstraps_adapter_targets_on_target_host(
        tmp_path, monkeypatch):
    shared = tmp_path / ".codex"
    local = tmp_path / "target-host" / "codex-arg0"
    spec = {
        "environment": "CODEX_HOME", "shared_home": str(shared),
        "local_links": ["tmp/arg0=%s" % local],
        "config_local_dirs": [], "prune_path_links": ["tmp/arg0"],
        "clear_environment": [],
        "overlay": {"root": str(shared),
                    "config_file": "config.toml",
                    "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "config.toml"]},
    }
    captured = _capture_run(
        tmp_path, monkeypatch, "codex", "ssh target {command}",
        runtime_spec=spec)

    command = captured["launch"][1][-1]
    assert "mkdir -p -- %s" % shlex.quote(str(local)) in command
    assert "chmod 700 -- %s" % shlex.quote(str(local)) in command
    overlay = shared / "abc12345"
    assert " CODEX_HOME=%s " % overlay in command
    assert " PATH=" not in command
    assert local.is_dir()  # submit host is bootstrapped too
    assert (overlay / "tmp" / "arg0").is_dir()
    assert not (shared / "tmp").exists()


def test_normal_launch_command_and_conditional_autorun_remain_unchanged(tmp_path):
    claude = _adapter("claude")
    normal = cli._build_command(claude, "prompt", str(tmp_path))
    assert "--permission-mode" in normal
    assert "auto" in normal
    assert "--dangerously-skip-permissions" not in normal

    codex = _adapter("codex")
    assert codex.auto_run["autorun_config"] == "/etc/codex/requirements.toml"
    assert codex.auto_run["autorun_args"] == [
        "--sandbox", "danger-full-access", "--ask-for-approval", "never"]
    assert "--yolo" not in cli._build_command(codex, "prompt", str(tmp_path))


def test_launcher_preflight_checks_launcher_not_target_tool(tmp_path, monkeypatch):
    launcher = tmp_path / "fake-launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    runtime = runtime_env.RuntimeEnv(
        env={"PATH": str(tmp_path), "HOME": str(tmp_path)},
        source="explicit", shell="/bin/sh", path=str(tmp_path))
    monkeypatch.setattr(runtime_env, "resolve_tmux_bin", lambda _rt: ("/bin/tmux", "golden"))
    monkeypatch.setattr(runtime_env, "run_probe", lambda *_a, **_kw: (0, "tmux 3.4"))

    issues, resolved = cli._preflight(
        "codex", "codex", str(tmp_path / "work"), runtime=runtime,
        adapter_readiness={"binary": "/definitely/local/codex"},
        tool_launcher=["fake-launcher", "{command}"])

    assert not [message for level, message in issues if level == "error"]
    assert resolved["launcher"] == str(launcher)
    assert resolved["tool"] == "codex"
    assert resolved["tool_resolution"]["source"] == "target-environment"


@pytest.mark.skipif(not shutil.which("tmux"), reason="requires tmux")
def test_local_fake_launcher_failure_keeps_shell_and_receives_one_command(
        tmp_path, monkeypatch):
    """Integration test without qsub/ssh: a fake launcher gets one command."""
    # Keep this integration isolated from the invoking account's login shell
    # and rc files.  create_tmux_session intentionally consults pwd rather
    # than $SHELL; on hosts whose account shell is csh, a slow/broken .cshrc
    # can terminate the private tmux server before startup-command injection.
    import pwd
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda _uid: types.SimpleNamespace(pw_shell="/bin/sh"))
    log_path = tmp_path / "launcher.argv"
    target = tmp_path / "remote-tool"
    target.write_text("#!/bin/sh\nexit 7\n")
    target.chmod(0o755)
    launcher = tmp_path / "fake-launcher"
    launcher.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$#\" \"$1\" > \"$LAUNCHER_LOG\"\n"
        "exec /bin/sh -c \"$1\"\n")
    launcher.chmod(0o755)
    session = "launcher-test-" + uuid.uuid4().hex[:10]
    status = tmp_path / "tool-exit.status"
    socket_dir = tmp_path / "sockets"
    monkeypatch.setattr(transport, "SOCKETS_DIR", str(socket_dir))
    tmux_config = transport.ensure_camc_tmux_config(
        str(tmp_path / "tmux.conf"))
    env = dict(os.environ)
    env["LAUNCHER_LOG"] = str(log_path)
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    try:
        ok = transport.create_tmux_session(
            session,
            tool_launcher.build_launcher_command([str(launcher), "{command}"],
                                  ["remote-tool", "two words"]),
            str(tmp_path), inherit_env=True, env=env,
            tmux_bin=shutil.which("tmux"), tmux_config=tmux_config,
            exit_status_path=str(status))
        assert ok, "create_tmux_session returned False"
        deadline = time.time() + 10.0
        while time.time() < deadline and not status.exists():
            time.sleep(0.05)
        socket = socket_dir / (session + ".sock")
        has_session = subprocess.run(
            [shutil.which("tmux"), "-u", "-S", str(socket),
             "has-session", "-t", session],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        pane = transport.capture_tmux(session, lines=100)
        assert status.exists(), (
            "launcher status missing; socket=%s socket_exists=%s "
            "has_session_rc=%d has_session_stderr=%r pane=%r "
            "launcher_log_exists=%s" % (
                socket, socket.exists(), has_session.returncode,
                has_session.stderr, pane, log_path.exists()))
        assert status.read_text().strip() == "7"
        lines = log_path.read_text().splitlines()
        assert lines[0] == "1"
        assert shlex.split(lines[1]) == ["remote-tool", "two words"]
        assert transport.tmux_session_exists(session)
    finally:
        transport.tmux_kill_session(session)
