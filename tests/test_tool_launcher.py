"""Focused coverage for ``camc run --tool-launcher``."""

import json
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
_OMITTED_LAUNCHER = object()


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


def _resolved_target_script(command):
    argv = _without_env_prefix(shlex.split(command))
    assert argv[:2] == ["/bin/sh", "-c"]
    return argv[2]


def _adapter(name):
    path = ROOT / "src" / "cam" / "adapters" / "configs" / (name + ".toml")
    config = adapters.AdapterConfig(adapters._parse_toml(path.read_text()))
    # Runtime-home behavior is independently covered and would only add local
    # filesystem setup noise to these launch-argv tests.
    config.runtime = None
    config.isolate_tool_home = False
    return config


@pytest.mark.parametrize("value, expected", [
    ("none", []),
    (" none ", []),
    ("ssh -tt root@192.168.1.100",
     ["ssh", "-tt", "root@192.168.1.100"]),
    ("ssh -tt root@192.168.1.100 {command}",
     ["ssh", "-tt", "root@192.168.1.100", "{command}"]),
    ("qsub -q o_cpu_4G -m rel8x -app r89docker --qsla autorun_sla "
     "-app affinity -Is",
     ["qsub", "-q", "o_cpu_4G", "-m", "rel8x", "-app", "r89docker",
      "--qsla", "autorun_sla", "-app", "affinity", "-Is"]),
])
def test_launcher_is_parsed_with_shlex(value, expected):
    assert tool_launcher.parse_tool_launcher(value) == expected


@pytest.mark.parametrize("value", [
    "", "ssh {command} {command}", "ssh x{command}", "ssh '{command",
])
def test_launcher_rejects_bad_prefix_or_legacy_placeholder(value):
    with pytest.raises(ValueError, match="--tool-launcher"):
        tool_launcher.parse_tool_launcher(value)


@pytest.mark.parametrize("value, expected", [
    ("auto", ("auto", "")),
    ("qsub-4g", None),
    ("ssh-auto", None),
    ("ssh-auto:1796123900", None),
    ("ssh host {command}", None),
    ("ssh-auto:not-a-job", None),
])
def test_named_launcher_parser(value, expected):
    assert tool_launcher.named_tool_launcher(value) == expected


@pytest.mark.parametrize("value", [
    "qsub-4g", "ssh-auto", "ssh-auto:1796123900",
])
def test_removed_named_launchers_direct_users_to_auto(value):
    with pytest.raises(ValueError, match="no longer a preset; use auto"):
        tool_launcher.parse_tool_launcher(value)


def test_default_tool_launcher_is_environment_aware(monkeypatch):
    seen = []

    def resolve(env, names):
        seen.append((env, names))
        return "/fake/nvprojectname" if env.get("NV_SITE") == "1" else ""

    monkeypatch.setattr(tool_launcher, "_launcher_binary", resolve)

    assert tool_launcher.default_tool_launcher({"NV_SITE": "1"}) == "auto"
    assert tool_launcher.default_tool_launcher({"PATH": "/usr/bin"}) == "none"
    assert all(names == tool_launcher._NVPROJECTNAME_BINARIES
               for _env, names in seen)


@pytest.mark.parametrize("tool", ["codex", "claude", "cursor", "pi", "custom-tool"])
@pytest.mark.parametrize("launcher", [
    None, "auto", "none",
    "ssh host",
])
def test_run_launcher_defaults_for_every_tool_and_preserves_overrides(
        tool, launcher, monkeypatch):
    argv = ["camc", "run", "-t", tool, "-n", "launcher-test"]
    if launcher is not None:
        argv += ["--tool-launcher", launcher]
    calls = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "_ensure_logs_on_scratch", lambda: None)
    monkeypatch.setattr(cli, "cmd_run", lambda args: calls.append(args))
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")

    cli.main()

    assert len(calls) == 1
    assert calls[0].tool_launcher == ("auto" if launcher is None else launcher)


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


def _current_autorun_job(job_id="123", user="tester",
                         service_class="autorun_sla", status="RUN",
                         interactive=True, host="host-a"):
    mode = ("Interactive pseudo-terminal shell mode" if interactive else
            "Batch job")
    return (
        "Job <%s>, User <%s>, Project <mobile>, Service Class <%s>, "
        "Status <%s>, Queue <o_priority>, %s\n"
        "Job Description <QJT_QSLA=autorun_sla "
        "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE="
        "interactive_asic_16G_48H>\nStarted on <%s>;\n" %
        (job_id, user, service_class, status, mode, host))


def _mock_current_job_query(monkeypatch, output, rc=0):
    calls = []
    monkeypatch.setattr(
        tool_launcher, "_launcher_binary",
        lambda _env, _names: "/fake/bjobs")

    def process(argv, env=None, **kwargs):
        calls.append((argv, env, kwargs))
        return rc, output, ""

    monkeypatch.setattr(tool_launcher, "_launcher_process", process)
    return calls


@pytest.mark.parametrize("dispatch", ["Started on", "Dispatched to"])
def test_detect_current_autorun_allocation_verifies_lsf_record(monkeypatch, dispatch):
    output = _current_autorun_job().replace("Started on", dispatch)
    calls = _mock_current_job_query(monkeypatch, output)

    result = tool_launcher.detect_current_autorun_allocation(
        {"LSB_JOBID": "123", "USER": "tester"},
        local_host="host-a.example.com", current_user="tester")

    assert result == {
        "job_id": "123",
        "service_class": "autorun_sla",
        "host": "host-a",
        "project": "mobile",
        "queue": "o_priority",
        "virtual_queue": "interactive_asic_16G_48H",
    }
    assert calls[0][0] == ["/fake/bjobs", "-l", "-UF", "123"]


@pytest.mark.parametrize("output,rc", [
    (_current_autorun_job(status="PEND"), 0),
    (_current_autorun_job(service_class="normal"), 0),
    (_current_autorun_job(user="other"), 0),
    (_current_autorun_job(interactive=False), 0),
    (_current_autorun_job(host="other-host"), 0),
    (_current_autorun_job(), 1),
])
def test_detect_current_autorun_allocation_fails_closed(
        monkeypatch, output, rc):
    _mock_current_job_query(monkeypatch, output, rc=rc)

    result = tool_launcher.detect_current_autorun_allocation(
        {"LSB_JOBID": "123", "USER": "tester"},
        local_host="host-a", current_user="tester")

    assert result is None


def test_detect_current_autorun_allocation_skips_without_job_id(monkeypatch):
    def unexpected_query(*_args, **_kwargs):
        pytest.fail("bjobs must not run without an LSB_JOBID hint")

    monkeypatch.setattr(tool_launcher, "_launcher_process", unexpected_query)
    assert tool_launcher.detect_current_autorun_allocation(
        {"USER": "tester"}, local_host="host-a",
        current_user="tester") is None


def _fake_launcher_binary(_env, names):
    known = {"bjobs", "battach", "nvprojectname", "qsub"}
    for name in names:
        base = os.path.basename(name)
        if base in known:
            return "/fake/" + base
    return ""


def _run_fake_battach_probe(argv, env, cwd):
    assert argv[1] == "-L"
    result = subprocess.run(
        ["/bin/sh", argv[2]], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    return result.returncode, result.stdout, result.stderr


def _install_fake_target_tool(tmp_path, name="codex"):
    tool_dir = tmp_path / "remote-bin"
    tool_dir.mkdir(exist_ok=True)
    tool = tool_dir / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return tool_dir, tool


def test_local_target_tool_path_hint_uses_absolute_tool_dir(tmp_path):
    tool_dir, tool = _install_fake_target_tool(tmp_path)
    resolved = tool_launcher.resolve_local_target_tool_path(
        "codex", "codex", {"PATH": "/missing"}, tool_dir=str(tool_dir))
    assert resolved == str(tool)


def test_battach_probe_checks_non_codex_version_and_prefers_local_path(
        tmp_path):
    preferred_dir = tmp_path / "preferred"
    preferred_dir.mkdir()
    preferred = preferred_dir / "claude"
    preferred.write_text(
        "#!/bin/sh\n[ \"$1\" = --version ] && exit 0\nexit 42\n")
    preferred.chmod(0o755)
    fallback_dir, _fallback = _install_fake_target_tool(tmp_path, "claude")
    script = tool_launcher._target_tool_probe_script(
        "claude", str(tmp_path), selected_tool="claude",
        preferred_tool_path=str(preferred), version_args=["--version"])
    probe = tmp_path / "probe"
    probe.write_text("#!/bin/sh\n" + script)
    probe.chmod(0o700)

    result = subprocess.run(
        [str(probe)], cwd=str(tmp_path),
        env={"PATH": str(fallback_dir)}, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0
    assert "__CAMC_TARGET_TOOL__%s" % preferred in result.stdout


def test_resolved_tool_command_executes_preferred_absolute_path(tmp_path):
    log = tmp_path / "tool.log"
    preferred_dir = tmp_path / "preferred"
    preferred_dir.mkdir()
    preferred = preferred_dir / "codex"
    preferred.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then echo version >> %s; exit 0; fi\n"
        "echo run:$* >> %s\n" % (shlex.quote(str(log)), shlex.quote(str(log))))
    preferred.chmod(0o755)
    command = tool_launcher.build_resolved_tool_command(
        ["codex", "--yolo", "two words"], "codex", str(tmp_path),
        preferred_tool_path=str(preferred), version_args=["--version"])

    result = subprocess.run(
        command, cwd=str(tmp_path), env={"PATH": "/missing"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "version", "run:--yolo two words"]


def test_resolved_tool_command_uses_target_path_when_local_hint_fails(
        tmp_path):
    preferred_dir = tmp_path / "preferred"
    fallback_dir = tmp_path / "fallback"
    preferred_dir.mkdir()
    fallback_dir.mkdir()
    preferred = preferred_dir / "pi"
    preferred.write_text("#!/bin/sh\nexit 126\n")
    preferred.chmod(0o755)
    fallback_log = tmp_path / "fallback.log"
    fallback = fallback_dir / "pi"
    fallback.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then exit 0; fi\n"
        "echo target-path >> %s\n" % shlex.quote(str(fallback_log)))
    fallback.chmod(0o755)
    command = tool_launcher.build_resolved_tool_command(
        ["pi", "--approve"], "pi", str(tmp_path),
        preferred_tool_path=str(preferred), version_args=["--version"])

    result = subprocess.run(
        command, cwd=str(tmp_path), env={"PATH": str(fallback_dir)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    assert result.returncode == 0, result.stderr
    assert fallback_log.read_text().splitlines() == ["target-path"]


def test_autorun_parser_ranks_then_deduplicates_hosts():
    candidates = tool_launcher._parse_autorun_jobs(
        _autorun_jobs_output(), "codex", current_project="mobile")

    assert [item["job_id"] for item in candidates] == [
        "1729124669", "1761147411", "1796124140"]
    assert candidates[-1]["job_name"] == "dvnet_autorun"


@pytest.mark.parametrize("dispatch", ["Started on", "Dispatched to"])
def test_long_metadata_verifies_owner_and_exec_host(dispatch):
    candidate = {"job_id": "1796123900", "host": "host-ai"}
    output = _long_job(candidate).replace("Started on", dispatch)
    assert tool_launcher._verify_autorun_job_long(
        output, candidate, expected_user="tester")
    assert not tool_launcher._verify_autorun_job_long(
        output.replace("host-ai", "changed-host"), candidate,
        expected_user="tester")


def test_dispatched_allocation_is_reused_before_qsub(monkeypatch, tmp_path):
    candidate = {"job_id": "1729124669", "host": "pdx3-sim-e14-027"}
    tool_dir, tool = _install_fake_target_tool(tmp_path, "claude")
    calls = [];progress = []
    def process(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 0, "mobile\n", ""
        if "-noheader" in argv:
            return 0, "1729124669|RUN|autorun_sla|Y|pdx3-sim-e14-027|mobile|tcsh|tcsh|\n", ""
        if argv[:3] == ["/fake/bjobs", "-l", "-UF"]:
            return 0, _long_job(candidate).replace("Started on", "Wed Sep  2 04:09:14: Dispatched to"), ""
        if argv[0] == "/fake/battach":
            return _run_fake_battach_probe(argv, {"PATH":str(tool_dir)}, kwargs.get("cwd"))
        pytest.fail("must reuse the verified allocation, without qsub: %r" % argv)
    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", process)
    argv, metadata, _ = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "claude", str(tmp_path), {"USER":"tester", "PATH":str(tool_dir)}, progress=progress.append)
    assert argv == ["/fake/battach", "-L", "{command}", "1729124669"]
    assert metadata["_target_tool_path"] == str(tool)
    assert not any("/fake/qsub" in call for call in calls)


def test_allocation_host_uses_last_dispatch_and_ignores_inline_description(monkeypatch):
    candidate = {"job_id":"123", "host":"host-a"}
    output = _current_autorun_job() + "Mon Oct 19 19:00:00: Dispatched to <host-b>;\n"
    assert not tool_launcher._verify_autorun_job_long(output, candidate, expected_user="tester")
    _mock_current_job_query(monkeypatch, output)
    assert tool_launcher.detect_current_autorun_allocation(
        {"LSB_JOBID":"123"}, local_host="host-a", current_user="tester") is None
    output = _current_autorun_job().replace("\nStarted on <host-a>;", " Started on <host-a>;")
    _mock_current_job_query(monkeypatch, output)
    assert tool_launcher.detect_current_autorun_allocation(
        {"LSB_JOBID":"123"}, local_host="host-a", current_user="tester") is None
    assert not tool_launcher._verify_autorun_job_long(
        _long_job(candidate, user="other"), candidate,
        expected_user="tester")


def test_auto_skips_changed_and_unreachable_hosts(monkeypatch, tmp_path):
    calls = []
    tool_dir, remote_tool = _install_fake_target_tool(tmp_path)

    def fake_process(argv, **kwargs):
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
        if argv[0] == "/fake/battach":
            if argv[-1] == "1761147411":
                return 255, "", "connection refused"
            return _run_fake_battach_probe(
                argv, {"PATH": str(tool_dir)}, kwargs.get("cwd"))
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path),
        {"USER": "tester", "PATH": str(tool_dir)})

    assert launcher == ["/fake/battach", "-L", "{command}", "1796124140"]
    assert metadata["job_id"] == "1796124140"
    assert metadata["transport"] == "battach"
    assert metadata["_target_tool_path"] == str(remote_tool)
    assert metadata["fallback"] is True
    assert any("metadata/EXEC_HOST changed" in item for item in notices)
    assert any("connection refused" in item for item in notices)
    assert not any("/fake/qsub" in call for call in calls)
    probes = [call for call in calls if call[0] == "/fake/battach"]
    assert len(probes) == 2
    assert all(call[1] == "-L" for call in probes)
    assert all(not os.path.exists(call[2]) for call in probes)


def test_auto_skips_codex_runtime_failure_and_uses_next_candidate(
        monkeypatch, tmp_path):
    calls = []
    bad_root = tmp_path / "bad"
    good_root = tmp_path / "good"
    bad_root.mkdir()
    good_root.mkdir()
    bad_dir, bad_tool = _install_fake_target_tool(bad_root)
    good_dir, good_tool = _install_fake_target_tool(good_root)
    bad_tool.write_text("#!/bin/sh\nexit 126\n")

    def fake_process(argv, **kwargs):
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
        if argv[0] == "/fake/battach":
            target_dir = bad_dir if argv[-1] == "1761147411" else good_dir
            rc, stdout, stderr = _run_fake_battach_probe(
                argv, {"PATH": str(target_dir)}, kwargs.get("cwd"))
            # battach may merge the attached process's stderr into its stdout.
            if argv[-1] == "1761147411":
                return rc, stdout + stderr, ""
            return rc, stdout, stderr
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path),
        {"USER": "tester", "PATH": str(good_dir)})

    assert launcher == ["/fake/battach", "-L", "{command}", "1796124140"]
    assert metadata["transport"] == "battach"
    assert metadata["job_id"] == "1796124140"
    assert metadata["_target_tool_path"] == str(good_tool)
    assert any("could not start in the LSF job environment" in item
               for item in notices)
    assert not any(call[0] == "/fake/qsub" for call in calls)
    assert len([call for call in calls if call[0] == "/fake/battach"]) == 2


def test_auto_uses_qsub_only_after_all_battach_candidates_fail(
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
        if argv[0] == "/fake/battach":
            return 255, "", "unreachable"
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path),
        {"USER": "tester", "PATH": "/fake"})

    assert launcher == [
        "/fake/nvprojectname", "run", "mobile", "/fake/qsub",
        "-q", "o_pri_interactive_cpu_16G", "-m", "rel8x", "-app", "r89docker",
        "--qsla", "autorun_sla", "-app", "affinity", "-Is", "{command}"]
    assert metadata == {
        "preset": "auto", "transport": "qsub", "project": "mobile",
        "fallback": True, "_auto_plan": []}
    assert notices[-1].startswith("auto: submitting a new qsub allocation")
    assert len([call for call in calls if call[0] == "/fake/battach"]) == 3
    # Resolver only constructs the final launcher; it never submits qsub.
    assert not any(call[0] == "/fake/qsub" for call in calls)


def test_auto_qsub_fallback_saves_default_project_when_mapping_is_empty(
        monkeypatch, tmp_path):
    calls = []
    resolves = [0]
    empty_project_error = (
        "nvprojectname: ERROR: NV::Project::Name::"
        "UnregisteredProjectNameException: project_name: ''")

    def fake_process(argv, env, cwd, timeout):
        calls.append((list(argv), cwd))
        if "-noheader" in argv:
            return 0, "", ""
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            resolves[0] += 1
            if resolves[0] <= 2:
                return 1, "", empty_project_error
            return 0, "gpu_gr102g_hw_mmplex_riscv\n", ""
        if argv[:2] == ["/fake/nvprojectname", "save"]:
            return 0, "saved\n", ""
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)
    launcher, metadata, _notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path), {"PATH": "/fake"})

    save_calls = [call for call in calls
                  if call[0][:2] == ["/fake/nvprojectname", "save"]]
    assert save_calls == [(
        ["/fake/nvprojectname", "save", "./",
         "gpu_gr102g_hw_mmplex_riscv"], str(tmp_path))]
    assert resolves[0] == 3
    assert launcher[0:3] == [
        "/fake/nvprojectname", "run", "gpu_gr102g_hw_mmplex_riscv"]
    assert metadata["transport"] == "qsub"
    assert metadata["preset"] == "auto"


def test_auto_fails_when_qsub_project_is_unavailable(
        monkeypatch, tmp_path):
    calls = []

    def fake_process(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 1, "", "project is not registered"
        if "-noheader" in argv:
            return 0, "", ""
        raise AssertionError("unexpected subprocess: %r" % argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", _fake_launcher_binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", fake_process)

    with pytest.raises(ValueError, match="could not prepare an LSF launcher"):
        tool_launcher.resolve_named_tool_launcher(
            ("auto", ""), "codex", str(tmp_path),
            {"USER": "tester", "PATH": "/fake"})
    assert len([call for call in calls
                if call[:2] == ["/fake/nvprojectname", "resolve_validate"]]) == 2


@pytest.mark.parametrize("ssh_works", [True, False])
def test_auto_checks_all_battach_jobs_before_independent_ssh(
        monkeypatch, tmp_path, ssh_works):
    calls = []
    progress = []
    tool_dir, remote_tool = _install_fake_target_tool(tmp_path)

    def binary(env, names):
        if any(os.path.basename(name) == "ssh" for name in names):
            return "/fake/ssh"
        return _fake_launcher_binary(env, names)

    def process(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["/fake/nvprojectname", "resolve_validate"]:
            return 0, "mobile\n", ""
        if "-noheader" in argv:
            return 0, _autorun_jobs_output(), ""
        if argv[:3] == ["/fake/bjobs", "-l", "-UF"]:
            candidate = next(c for c in tool_launcher._parse_autorun_jobs(
                _autorun_jobs_output(), "codex") if c["job_id"] == argv[-1])
            return 0, _long_job(candidate), ""
        if argv[0] == "/fake/battach":
            return 255, "", "LSF attachment unavailable"
        if argv[0] == "/fake/ssh":
            assert "BatchMode=yes" in argv
            assert "/fake/battach" not in argv[-1]
            if not ssh_works:
                return 255, "", "Permission denied (publickey)"
            result = subprocess.run(
                ["/bin/sh", "-c", argv[-1]],
                env={"PATH": str(tool_dir)}, cwd=kwargs["cwd"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True)
            return result.returncode, result.stdout, result.stderr
        raise AssertionError(argv)

    monkeypatch.setattr(tool_launcher, "_launcher_binary", binary)
    monkeypatch.setattr(tool_launcher, "_launcher_process", process)
    launcher, metadata, notices = tool_launcher.resolve_named_tool_launcher(
        ("auto", ""), "codex", str(tmp_path),
        {"USER": "tester", "PATH": str(tool_dir)}, progress=progress.append)
    transports = [os.path.basename(call[0]) for call in calls
                  if call[0] in ("/fake/battach", "/fake/ssh")]
    assert transports[:3] == ["battach"] * 3
    assert transports[3:] == ["ssh"] * (1 if ssh_works else 3)
    assert progress[0].startswith("battach:")
    assert any(line.startswith("ssh: probing") for line in progress)
    assert not any("publickey" in line for line in progress)
    assert (any(line.startswith("qsub:") for line in progress)) is not ssh_works
    if ssh_works:
        assert launcher[0] == "/fake/ssh"
        assert metadata["transport"] == "ssh"
        assert metadata["_target_tool_path"] == str(remote_tool)
    else:
        assert metadata["transport"] == "qsub"
        assert any("publickey" in notice for notice in notices)


def _executable(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("winning_transport", ["battach", "ssh", "qsub", "none"])
def test_auto_runtime_fallback_order_and_failure_never_starts_local(
        tmp_path, winning_transport):
    events = tmp_path / "events"
    tool_events = tmp_path / "tool-events"
    tool = _executable(tmp_path, "codex", (
        'if [ "$1" = --version ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> %s\nexit 0' %
        ("%s", shlex.quote(str(tool_events)))))
    stages = []
    for transport in ("battach", "ssh", "qsub"):
        body = "printf '%s\\n' %s >> %s\n" % (
            "%s", shlex.quote(transport), shlex.quote(str(events)))
        if transport == winning_transport:
            body += ('exec "$2"' if transport == "battach" else
                     'for _arg do :; done\nexec /bin/sh -c "$_arg"')
        else:
            body += "exit 255"
        binary = _executable(tmp_path, transport, body)
        argv = ([binary, "-L", "{command}", "123"]
                if transport == "battach" else [binary, "{command}"])
        stages.append({"transport": transport, "argv": argv})
    status = str(tmp_path / "exit")
    metadata = {"transport": "battach", "_auto_plan": stages[1:]}
    command, directory = tool_launcher.build_auto_launcher_command(
        stages[0]["argv"], metadata, ["env", tool, "--yolo", "hello"],
        str(tmp_path), "codex",
        preferred_tool_path=tool, exit_status_path=status)
    assert os.stat(command[0]).st_mode & 0o777 == 0o700
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == (125 if winning_transport == "none" else 0), result.stderr
    expected = ["battach", "ssh", "qsub"]
    if winning_transport != "none":
        expected = expected[:expected.index(winning_transport) + 1]
    assert events.read_text().splitlines() == expected
    for stage in expected:
        assert "[launcher] %s: starting" % stage in result.stdout
        if stage != winning_transport:
            assert "[launcher] %s: failed (exit 255); trying next" % stage in result.stdout
    if winning_transport == "none":
        assert not tool_events.exists()
        assert not (tmp_path / "exit.launcher").exists()
        assert "local fallback disabled" in result.stderr
    else:
        assert tool_events.read_text().splitlines() == ["--yolo hello"]
        assert (tmp_path / "exit.launcher").read_text().strip() == winning_transport
    assert (tmp_path / "exit").read_text().strip() == str(result.returncode)
    assert not os.path.exists(directory)


def test_auto_does_not_retry_after_agent_starts_and_exits_with_error(tmp_path):
    tool = _executable(tmp_path, "codex", (
        'if [ "$1" = --version ]; then exit 0; fi\nexit 7'))
    attach = _executable(tmp_path, "battach", 'exec "$2"')
    forbidden = _executable(tmp_path, "forbidden", "exit 99")
    command, _directory = tool_launcher.build_auto_launcher_command(
        [attach, "-L", "{command}", "123"],
        {"transport": "battach", "_auto_plan": [
            {"transport": "ssh", "argv": [forbidden, "{command}"]}]},
        [tool, "--yolo"], str(tmp_path), "codex",
        preferred_tool_path=tool)
    assert subprocess.run(command).returncode == 7


def test_auto_never_executes_local_after_remote_failure(tmp_path):
    events = tmp_path / "local-started"
    local = _executable(tmp_path, "codex", "touch %s" % shlex.quote(str(events)))
    status = tmp_path / "exit"
    command, directory = tool_launcher.build_auto_launcher_command(
        ["/bin/false", "-L", "{command}", "123"],
        {"transport": "battach", "_auto_plan": []}, [local, "--yolo"],
        str(tmp_path), "codex", preferred_tool_path=local,
        exit_status_path=str(status))
    assert subprocess.run(command).returncode == 125
    assert status.read_text().strip() == "125"
    assert not events.exists()
    assert not os.path.exists(directory)


def test_auto_runtime_qsub_preparation_failure_never_runs_local(tmp_path):
    project_events = tmp_path / "project-events"
    project = _executable(tmp_path, "nvprojectname", (
        "printf '%s\\n' \"$*\" >> %s\n"
        "echo 'UnregisteredProjectNameException: project_name: '\"''\" >&2\n"
        "exit 1" % ("%s", shlex.quote(str(project_events)))))
    unreachable = _executable(tmp_path, "battach", "exit 255")
    never_qsub = _executable(tmp_path, "qsub", "exit 99")
    flags = tmp_path / "flags"
    tool = _executable(tmp_path, "codex", (
        'if [ "$1" = --version ]; then exit 0; fi\n'
        "printf '%s\\n' \"$*\" > %s" % ("%s", shlex.quote(str(flags)))))
    command, _directory = tool_launcher.build_auto_launcher_command(
        [unreachable, "-L", "{command}", "123"],
        {"transport": "battach", "_auto_plan": [
            {"transport": "qsub", "nvproject": project,
             "qsub": never_qsub, "project": ""}]},
        [tool, "--yolo"], str(tmp_path), "codex",
        preferred_tool_path=tool)
    assert subprocess.run(command).returncode == 125
    assert not flags.exists()
    assert project_events.read_text().splitlines() == [
        "resolve_validate", "save ./ gpu_gr102g_hw_mmplex_riscv"]


def test_auto_runtime_defers_project_save_when_battach_succeeds(tmp_path):
    tool = _executable(tmp_path, "codex", "exit 0")
    attach = _executable(tmp_path, "battach", 'exec "$2"')
    log = tmp_path / "unexpected-project-operation"
    project = _executable(tmp_path, "nvprojectname", ": > %s\nexit 99" %
                          shlex.quote(str(log)))
    command, _directory = tool_launcher.build_auto_launcher_command(
        [attach, "-L", "{command}", "123"],
        {"transport": "battach", "_auto_plan": [
            {"transport": "qsub", "nvproject": project,
             "qsub": project, "project": ""}]},
        [tool, "--yolo"], str(tmp_path), "codex",
        preferred_tool_path=tool)
    assert subprocess.run(command).returncode == 0
    assert not log.exists()


def test_auto_runtime_qsub_uses_interactive_16g_with_autorun(tmp_path):
    arguments = tmp_path / "qsub-arguments"
    tool_events = tmp_path / "tool-events"
    tool = _executable(tmp_path, "codex", (
        'if [ "$1" = --version ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" > %s' %
        ("%s", shlex.quote(str(tool_events)))))
    project = _executable(tmp_path, "nvprojectname", (
        '[ "$1" = run ] && [ "$2" = mobile ] || exit 98\n'
        'shift 2\nexec "$@"'))
    qsub = _executable(tmp_path, "qsub", (
        'printf "%s\\n" "$@" > %s\n'
        'for _arg do :; done\nexec /bin/sh -c "$_arg"' %
        ("%s", shlex.quote(str(arguments)))))
    status = str(tmp_path / "exit")
    command, directory = tool_launcher.build_auto_launcher_command(
        ["/bin/false", "-L", "{command}", "123"],
        {"transport": "battach", "_auto_plan": [
            {"transport": "qsub", "nvproject": project,
             "qsub": qsub, "project": "mobile"}]},
        [tool, "--yolo"], str(tmp_path), "codex",
        preferred_tool_path=tool, exit_status_path=status)
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0, result.stderr
    assert arguments.read_text().splitlines()[:-1] == [
        "-q", "o_pri_interactive_cpu_16G", "-m", "rel8x", "-app", "r89docker",
        "--qsla", "autorun_sla", "-app", "affinity", "-Is"]
    assert tool_events.read_text().strip() == "--yolo"
    assert (tmp_path / "exit.launcher").read_text().strip() == "qsub"
    assert not os.path.exists(directory)


def test_remote_launcher_rejects_api_profile():
    with pytest.raises(ValueError, match="--no-default-api"):
        tool_launcher.validate_tool_launcher_api("auto", "ihub-direct")
    tool_launcher.validate_tool_launcher_api("auto", None)


def test_local_launcher_supports_api_profiles():
    tool_launcher.validate_tool_launcher_api("none", "ihub-direct")
    assert tool_launcher.parse_tool_launcher("none") == []


def test_launcher_target_environment_carries_home_paths_and_terminal_only():
    assert tool_launcher.launcher_target_environment(
        {"PATH": "/shared/tool/bin:/usr/bin", "CODEX_HOME": "/shared/codex",
         "TERM": "screen-256color", "COLORTERM": "truecolor", "NO_COLOR": "1",
         "LD_LIBRARY_PATH": "/submit-only/lib", "SECRET_TOKEN": "never-transfer"},
        {"environment": "CODEX_HOME"}) == {
            "CODEX_HOME": "/shared/codex",
            "TERM": "screen-256color", "COLORTERM": "truecolor",
            "PATH": "/shared/tool/bin:/usr/bin:/home/nv/bin:/home/utils/bin:/usr/local/lsf/bin",
        }


def test_launcher_carries_explicit_tool_env_without_ambient_secrets(tmp_path):
    runtime = {"PATH": "/usr/bin:/bin", "CAMC_E2E_AGENT": "test-agent",
               "DISABLE_AUTOUPDATER": "1", "UNREQUESTED_SECRET": "not-forwarded",
               "LD_LIBRARY_PATH": "/submit/lib"}
    environment = tool_launcher.launcher_target_environment(
        runtime, explicit_keys=["CAMC_E2E_AGENT", "DISABLE_AUTOUPDATER"])
    command = tool_launcher.build_launcher_command(
        ["{command}"], [sys.executable, "-c", "import os,json;print(json.dumps({k:os.environ.get(k) for k in ['CAMC_E2E_AGENT','DISABLE_AUTOUPDATER','UNREQUESTED_SECRET','LD_LIBRARY_PATH']}))"],
        target_env=environment)[0]
    output = subprocess.check_output(["/bin/sh", "-c", command], env={"PATH": "/usr/bin:/bin"})
    assert json.loads(output.decode()) == {
        "CAMC_E2E_AGENT": "test-agent", "DISABLE_AUTOUPDATER": "1",
        "UNREQUESTED_SECRET": None, "LD_LIBRARY_PATH": None}


def test_target_path_preserves_job_and_appends_visible_submit_paths(tmp_path):
    job = tmp_path / "job-bin"
    shared = tmp_path / "shared bin"
    job.mkdir()
    shared.mkdir()
    _executable(job, "same-tool", "echo job-tool")
    _executable(shared, "same-tool", "echo submit-tool")
    _executable(shared, "new-tool", "echo appended-tool")
    environment = {"PATH": ":".join([str(shared), str(shared),
                                      str(tmp_path / "absent"), "."])}
    command = tool_launcher.build_launcher_command(
        ["{command}"], ["/bin/sh", "-c",
                        'same-tool; new-tool; printf "%s\\n" "$PATH" "$LD_LIBRARY_PATH" "$LSB_JOBID"'],
        target_env=environment)[0]
    result = subprocess.run(["/bin/sh", "-c", command],
                            env={"PATH": str(job) + ":/usr/bin:/bin",
                                 "LD_LIBRARY_PATH": "/job/lib", "LSB_JOBID": "123"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    assert result.returncode == 0, result.stderr
    output = result.stdout.splitlines()
    assert output[:2] == ["job-tool", "appended-tool"]
    assert output[2] == str(job) + ":/usr/bin:/bin:" + str(shared)
    assert output[3:] == ["/job/lib", "123"]


def test_target_environment_drops_transient_and_relative_submit_paths():
    environment = tool_launcher.launcher_target_environment({
        "PATH": ":.:relative:/home/u/.codex/tmp/arg0/old:/shared/bin:/shared/bin"})
    assert environment["PATH"] == "/shared/bin:/home/nv/bin:/home/utils/bin:/usr/local/lsf/bin"


def test_probe_and_launch_use_the_same_supplemental_path(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    executable = _executable(tools, "codex", "echo fake-version")
    environment = tool_launcher.launcher_target_environment({"PATH": str(tools)})
    probe = tool_launcher._target_tool_probe_script(
        "codex", str(tmp_path), target_path=environment["PATH"])
    result = subprocess.run(["/bin/sh", "-c", probe], env={"PATH": "/usr/bin:/bin"},
                            stdout=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0
    assert tool_launcher._target_tool_from_probe(result.stdout) == executable
    resolved = tool_launcher.build_resolved_tool_command(["codex"], "codex", str(tmp_path))
    launch = tool_launcher.build_launcher_command(["{command}"], resolved,
                                                 target_env=environment)[0]
    result = subprocess.run(["/bin/sh", "-c", launch], env={"PATH": "/usr/bin:/bin"},
                            stdout=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "fake-version"


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


def test_complete_quoted_command_appends_to_custom_launcher_prefix():
    prefix = ["qsub", "-q", "o_cpu_4G", "--qsla", "autorun_sla", "-Is"]
    tool = ["/tools/codex/bin/codex", "--yolo", "two words"]

    launch = tool_launcher.build_launcher_command(prefix, tool)

    assert launch[:-1] == prefix
    assert shlex.split(launch[-1]) == tool


def test_launcher_bootstraps_target_dirs_then_execs_without_eval():
    launch = tool_launcher.build_launcher_command(
        ["ssh", "host"], ["codex", "two words"],
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


def test_battach_runs_directly_and_private_script_cleans_and_records_exit(
        tmp_path):
    workdir = tmp_path / "project with spaces"
    workdir.mkdir()
    output = tmp_path / "target-output"
    remote_tool = tmp_path / "remote codex"
    remote_tool.write_text(
        "#!/bin/sh\nprintf '%%s\\n' \"$PWD\" \"$1\" "
        "\"$CAMC_TEST_VALUE\" > %s\nexit 7\n" % shlex.quote(str(output)))
    remote_tool.chmod(0o755)
    status = tmp_path / "tool-exit.status"

    command, private_dir = tool_launcher.build_battach_launcher_command(
        ["/fake/battach", "-L", "{command}", "123"],
        [str(remote_tool), "two words"], str(workdir),
        target_env={"CAMC_TEST_VALUE": "env value"},
        exit_status_path=str(status))
    script_path = command[command.index("-L") + 1]
    assert command == ["/fake/battach", "-L", script_path, "123"]
    script = Path(script_path)
    assert script.exists()
    assert script.stat().st_mode & 0o777 == 0o700
    assert script.parent == Path(private_dir)
    assert "/bin/rm -f -- \"$0\"" in script.read_text()
    assert "cd %s" % shlex.quote(str(workdir)) in script.read_text()
    assert "env 'CAMC_TEST_VALUE=env value' " in script.read_text()
    assert str(remote_tool) in script.read_text()

    result = subprocess.run([script_path], env=os.environ.copy())
    assert result.returncode == 7
    assert not script.exists()
    assert not Path(private_dir).exists()
    assert status.read_text().strip() == "7"
    assert output.read_text().splitlines() == [
        str(workdir), "two words", "env value"]


def test_replace_launcher_target_binary_preserves_env_prefix():
    command = ["env", "CLAUDE_FLAG=1", "claude", "--dangerously-skip-permissions"]
    assert tool_launcher.replace_launcher_target_binary(
        command, "/lsf/bin/claude") == [
            "env", "CLAUDE_FLAG=1", "/lsf/bin/claude",
            "--dangerously-skip-permissions"]


def _capture_run(tmp_path, monkeypatch, tool, launcher, runtime_spec=None,
                 via_cli=False, api=None, reboot=False, launch_id=None,
                 extra_tool_args=None, extra_tool_env=None):
    captured = {}
    config = _adapter(tool)
    if runtime_spec is not None:
        config.runtime = runtime_spec
        config.isolate_tool_home = True

    from camc_pkg.storage import AgentStore
    store = AgentStore(str(tmp_path / "agents.json"))

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
        system_prompt = None
        system_file = None
        api = None
        no_default_api = True
        api_token = None

    Args._reboot = reboot
    Args._launch_id = launch_id
    Args.tool_args = extra_tool_args or []
    Args.tool_env = extra_tool_env or []
    Args.tool = tool
    Args.api = api
    if launcher is not _OMITTED_LAUNCHER:
        Args.tool_launcher = launcher

    monkeypatch.setattr(cli, "_load_config", lambda _tool: config)
    monkeypatch.setattr(cli, "_load_default_context", lambda: {})
    monkeypatch.setattr(
        runtime_env, "build_runtime_env",
        lambda **_kwargs: runtime_env.RuntimeEnv(
            env={"PATH": "/launcher/bin", "HOME": str(tmp_path),
                 "LSB_JOBID": "",
                 "SECRET_TOKEN": "must-not-enter-manifest"},
            source="explicit", shell="/bin/sh", path="/launcher/bin"))

    def fake_preflight(selected, binary, workdir, **kwargs):
        captured["preflight"] = (selected, binary, kwargs)
        resolved_tool = (binary if kwargs.get("tool_launcher") else
                         "/launcher/bin/" + os.path.basename(binary))
        return [], {
            "tmux": "/bin/tmux",
            "launcher": "/launcher/bin/fake-launcher",
            "launcher_resolution": {
                "bin": "/launcher/bin/fake-launcher", "source": "env"},
            "tool": resolved_tool,
            "tool_resolution": {
                "bin": resolved_tool, "source": "target-environment",
                "warnings": []},
        }

    monkeypatch.setattr(cli, "_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_gen_agent_id", lambda: "abc12345")
    monkeypatch.setattr(cli, "install_manifest_skills", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "PIDS_DIR", str(tmp_path / "pids"))
    monkeypatch.setattr(cli, "create_tmux_session",
                        lambda *_a, **_kw: pytest.fail("preparation created another tmux"))
    monkeypatch.setattr(transport, "ensure_camc_tmux_config", lambda: "")
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *_a, **_kw: b"tmux 3.4")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: Proc())
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli, "AgentStore", lambda: store)
    monkeypatch.setattr(cli, "_spawn_codex_session_binder", lambda *_a: None)
    if cli.resolve_local_target_tool_path is tool_launcher.resolve_local_target_tool_path:
        monkeypatch.setattr(cli, "resolve_local_target_tool_path",
                            lambda *_args, **_kwargs: "")

    def prepare(args):
        store.save({"id": "abc12345", "status": "alive",
                    "launch_id": getattr(args, "_launch_id", None)})
        launch = {}
        cli._cmd_run_prepared(args, "abc12345", launch)
        captured["record"] = store.get("abc12345")
        captured["launch"] = ("cam-abc12345", launch["command"],
                              launch["workdir"], {"env": launch["env"]})

    if via_cli:
        # These tests exercise the existing preparation policies. The
        # pane-worker boundary is covered by test_launch_worker.py.
        monkeypatch.setattr(cli, "cmd_run", prepare)
        argv = ["camc", "run", "-t", tool, "-n", Args.name,
                "-p", Args.path, "--no-default-api", Args.prompt]
        if launcher is not None and launcher is not _OMITTED_LAUNCHER:
            argv += ["--tool-launcher", launcher]
        if api:
            argv += ["--api", api]
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(cli, "_ensure_logs_on_scratch", lambda: None)
        cli.main()
    else:
        prepare(Args())
    return captured


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_remote_launch_clears_allocation_color_suppression(tmp_path, monkeypatch, tool):
    captured_kwargs = {}
    original = tool_launcher.build_launcher_command

    def capture(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return original(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(cli, "build_launcher_command", capture)
        captured = _capture_run(tmp_path, patched, tool, "ssh target {command}",
                                extra_tool_env=["NO_COLOR=1", "NODE_DISABLE_COLORS=1"])
    assert "NO_COLOR" not in captured["launch"][3]["env"]
    assert "NODE_DISABLE_COLORS" not in captured["launch"][3]["env"]
    assert "NO_COLOR" in captured_kwargs["target_unset"]
    assert "NODE_DISABLE_COLORS" in captured_kwargs["target_unset"]
    environment = tool_launcher.launcher_target_environment({
        "TERM": "screen-256color", "COLORTERM": "truecolor"})
    command = original(
        ["{command}"], [sys.executable, "-c",
            "import os,json; print(json.dumps({k:os.environ.get(k) for k in "
            "['TERM','COLORTERM','NO_COLOR','NODE_DISABLE_COLORS','LSB_JOBID']}))"],
        target_env=environment, target_unset=captured_kwargs["target_unset"])[0]
    output = subprocess.check_output(["/bin/sh", "-c", command], env={
        "PATH": "/usr/bin:/bin", "TERM": "dumb", "NO_COLOR": "1",
        "NODE_DISABLE_COLORS": "1", "LSB_JOBID": "123"})
    assert json.loads(output.decode()) == {
        "TERM": "screen-256color", "COLORTERM": "truecolor",
        "NO_COLOR": None, "NODE_DISABLE_COLORS": None, "LSB_JOBID": "123"}


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_local_launch_clears_color_suppression_after_tool_env(tmp_path, monkeypatch, tool):
    captured = _capture_run(tmp_path, monkeypatch, tool, "none",
                            extra_tool_env=["NO_COLOR=1", "NODE_DISABLE_COLORS=1"])
    environment = captured["launch"][3]["env"]
    assert "NO_COLOR" not in environment
    assert "NODE_DISABLE_COLORS" not in environment


@pytest.mark.parametrize("tool", ["codex", "claude", "cursor", "pi"])
@pytest.mark.parametrize("transport_name", ["ssh", "qsub"])
def test_default_run_launches_via_auto_and_records_chosen_transport(
        tmp_path, monkeypatch, tool, transport_name):
    calls = []

    def resolve(preset, selected, workdir, env, **kwargs):
        calls.append((preset, selected))
        return (["fake-launcher", "{command}"],
                {"preset": "auto", "transport": transport_name}, [])

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", resolve)
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")
    captured = _capture_run(tmp_path, monkeypatch, tool, None, via_cli=True)

    assert calls == [(("auto", ""), tool)]
    assert captured["preflight"][2]["tool_launcher"] == [
        "fake-launcher", "{command}"]
    assert captured["launch"][1][0] == "/launcher/bin/fake-launcher"
    metadata = captured["record"]["runtime"]["launcher"]
    assert metadata["preset"] == "auto"
    assert metadata["transport"] == transport_name
    assert captured["record"]["task"]["auto_confirm"] is False


@pytest.mark.parametrize("launcher", [None, _OMITTED_LAUNCHER])
def test_direct_run_omitting_launcher_uses_auto(tmp_path, monkeypatch, launcher):
    calls = []

    def resolve(preset, *_args, **_kwargs):
        calls.append(preset)
        return (["fake-launcher", "{command}"], {"preset": "auto"}, [])

    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", resolve)
    _capture_run(tmp_path, monkeypatch, "codex", launcher)

    assert calls == [("auto", "")]


def test_explicit_local_run_never_discovers_remote_jobs(tmp_path, monkeypatch):
    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("local launch must not discover remote jobs")

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", unexpected_discovery)
    captured = _capture_run(tmp_path, monkeypatch, "codex", "none", via_cli=True)

    assert "tool_launcher" not in captured["preflight"][2]
    assert captured["launch"][1][0] == "/launcher/bin/codex"
    assert captured["launch"][1].count("--yolo") == 1
    assert "launcher" not in captured["record"]["runtime"]
    assert captured["record"]["task"]["auto_confirm"] is True


@pytest.mark.parametrize("reboot", [False, True])
def test_auto_rejects_resolved_local_fallback(tmp_path, monkeypatch, reboot):
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", lambda *_a, **_kw: (
        [], {"preset": "auto", "transport": "local", "fallback": True}, []))

    with pytest.raises(SystemExit) as exc:
        _capture_run(tmp_path, monkeypatch, "codex", "auto", reboot=reboot)
    assert exc.value.code == 2


def test_cmd_run_auto_chain_uses_private_runner_without_shell_status_wrapper(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", lambda *_a, **_kw: (
        ["qsub", "-Is", "{command}"],
        {"preset": "auto", "transport": "qsub", "_auto_plan": []}, []))
    captured = _capture_run(tmp_path, monkeypatch, "codex", "auto")
    command = captured["launch"][1]
    assert len(command) == 1
    assert os.path.isfile(command[0])
    metadata = captured["record"]["runtime"]["launcher"]
    assert metadata["fallback_order"] == [
        "direct", "battach", "ssh", "qsub"]
    assert "_auto_plan" not in metadata
    assert command[0] not in json.dumps(captured["record"])
    assert captured["record"]["task"]["auto_confirm"] is False
    assert captured["record"]["task"]["prompt_in_command"] is True
    target = Path(command[0]).parent / "target-0"
    assert "launcher prompt" in target.read_text()


@pytest.mark.parametrize("tool", ["claude", "codex", "cursor"])
def test_native_initial_prompt_is_submitted_once_in_argv(tmp_path, monkeypatch, tool):
    captured = _capture_run(tmp_path, monkeypatch, tool, "none")
    command = captured["launch"][1]
    assert command.count("launcher prompt") == 1
    assert captured["record"]["task"]["prompt_in_command"] is True


@pytest.mark.parametrize("tool,flag", [
    ("claude", "--dangerously-skip-permissions"), ("codex", "--yolo"),
    ("cursor", "--force"), ("pi", "--approve"),
])
@pytest.mark.parametrize("launcher", ["none", "qsub -Is"])
@pytest.mark.parametrize("reboot", [False, True])
@pytest.mark.parametrize("explicit_flag", [False, True])
def test_managed_native_autorun_is_the_same_for_run_and_reboot(
        tmp_path, monkeypatch, tool, flag, launcher, reboot, explicit_flag):
    captured = _capture_run(tmp_path, monkeypatch, tool, launcher, reboot=reboot,
                            extra_tool_args=[flag] if explicit_flag else [])
    command = captured["launch"][1]
    if launcher == "none":
        assert command.count(flag) == 1
    else:
        script = _resolved_target_script(command[-1])
        assert script.count(flag) == 1
    assert "--permission-mode" not in repr(command)
    assert "--allowed-tools" not in repr(command)
    assert "--ask-for-approval" not in repr(command)


@pytest.mark.parametrize("receipt", ["qsub", "custom"])
def test_queue_wait_does_not_consume_tool_boot_timeout(tmp_path, monkeypatch, receipt):
    from camc_pkg import monitor
    from camc_pkg.monitor_features import MonitorRuntime
    status = tmp_path / "launch-status"
    config = types.SimpleNamespace(startup_wait=20)
    runtime = MonitorRuntime("test", config, now=0)
    runtime.boot_deadline = 20
    record = {"state": "running", "phase": "initializing", "task": {"prompt": "hello"},
              "runtime": {"launcher": {"launch_status_file": str(status)}}}
    store = types.SimpleNamespace(get=lambda _id: record)
    now = [200]
    monkeypatch.setattr(monitor.time, "time", lambda: now[0])
    monitor._refresh_boot_runtime(runtime, store, "test", None)
    assert runtime.boot_deadline == 220
    assert not runtime.feature_state.get("launcher_started")
    status.write_text(receipt + "\n")
    now[0] = 250
    monitor._refresh_boot_runtime(runtime, store, "test", None)
    assert runtime.feature_state["launcher_started"] is True
    assert runtime.boot_deadline == 270
    now[0] = 260
    monitor._refresh_boot_runtime(runtime, store, "test", None)
    assert runtime.boot_deadline == 270


def test_auto_receipts_are_isolated_per_launch(tmp_path, monkeypatch):
    from camc_pkg import monitor
    from camc_pkg.monitor_features import MonitorRuntime
    monkeypatch.setattr(cli, "detect_current_autorun_allocation", lambda _env: None)
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", lambda *_a, **_kw: (
        ["qsub", "-Is", "{command}"],
        {"preset": "auto", "transport": "qsub", "_auto_plan": []}, []))
    captured = _capture_run(tmp_path, monkeypatch, "claude", "auto",
                            launch_id="new-generation")
    record = captured["record"]
    status = Path(record["runtime"]["custom_launch"]["exit_status_path"])
    assert status.name == "abc12345.new-generation.tool-exit"
    started = Path(record["runtime"]["launcher"]["launch_status_file"])
    assert str(started) == str(status) + ".launcher"
    runner = Path(captured["launch"][1][0])
    assert str(status) in runner.read_text()
    assert str(started) in (runner.parent / "target-0").read_text()
    status.parent.mkdir(parents=True, exist_ok=True)
    for filename in ("abc12345.tool-exit", "abc12345.old-generation.tool-exit"):
        (status.parent / filename).write_text("7\n")
        (status.parent / (filename + ".launcher")).write_text("qsub\n")
    store = types.SimpleNamespace(get=lambda _id: record,
                                  update=lambda _id, **kw: record.update(kw))
    events = []
    assert not monitor._consume_custom_launch_exit(store, record["id"],
                                                   lambda *event: events.append(event))
    assert record["status"] == "alive" and not events
    config = types.SimpleNamespace(startup_wait=20)
    runtime = MonitorRuntime(record["id"], config, now=0)
    monitor._refresh_boot_runtime(runtime, store, record["id"], None)
    assert not runtime.feature_state.get("launcher_started")
    started.write_text("qsub\n")
    monitor._refresh_boot_runtime(runtime, store, record["id"], None)
    assert runtime.feature_state["launcher_started"] is True
    status.write_text("7\n")
    assert monitor._consume_custom_launch_exit(store, record["id"],
                                               lambda *event: events.append(event))
    assert record["state"] == "failed"
    assert events == [("tool_exit", {"code": 7, "failed": True})]


def test_monitor_does_not_reinject_initial_prompt_passed_in_tool_argv():
    from camc_pkg import monitor
    from camc_pkg.monitor_features import MonitorRuntime
    runtime = MonitorRuntime("test", types.SimpleNamespace(startup_wait=20))
    record = {"state": "running", "phase": "initializing", "task": {
        "prompt": "hello", "prompt_in_command": True}}
    store = types.SimpleNamespace(get=lambda _id: record)
    monitor._refresh_boot_runtime(runtime, store, "test", None)
    assert runtime.boot_prompt == ""


def test_default_remote_launcher_with_api_fails_before_remote_discovery(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")
    monkeypatch.setattr(
        "camc_pkg.api_store.resolve_run_api_name",
        lambda *_args, **_kwargs: ("ihub-direct", "explicit"))

    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("API incompatibility must be checked before discovery")

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", unexpected_discovery)
    with pytest.raises(SystemExit) as exc:
        _capture_run(tmp_path, monkeypatch, "codex", None,
                     via_cli=True, api="ihub-direct")

    assert exc.value.code == 2
    assert "--tool-launcher none" in capsys.readouterr().err


def test_default_run_outside_nv_uses_local_launcher(tmp_path, monkeypatch):
    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("non-NV default must not discover remote jobs")

    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "none")
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", unexpected_discovery)
    captured = _capture_run(tmp_path, monkeypatch, "codex", None, via_cli=True)

    assert "tool_launcher" not in captured["preflight"][2]
    assert captured["launch"][1][0] == "/launcher/bin/codex"
    assert "launcher" not in captured["record"]["runtime"]
    assert captured["record"]["task"]["auto_confirm"] is True


def test_explicit_auto_overrides_non_nv_default(tmp_path, monkeypatch):
    calls = []

    def resolve(preset, selected, *_args, **_kwargs):
        calls.append((preset, selected))
        return (["fake-launcher", "{command}"], {"preset": "auto"}, [])

    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "none")
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", resolve)
    captured = _capture_run(tmp_path, monkeypatch, "codex", "auto", via_cli=True)

    assert calls == [(('auto', ''), "codex")]
    assert captured["launch"][1][0] == "/launcher/bin/fake-launcher"


def test_current_autorun_allocation_runs_local_with_adapter_autorun_args(
        tmp_path, monkeypatch):
    allocation = {
        "job_id": "123", "service_class": "autorun_sla",
        "host": "host-a", "project": "mobile",
        "queue": "o_priority",
        "virtual_queue": "interactive_asic_16G_48H",
    }
    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation",
        lambda _env: dict(allocation))
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")

    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("current autorun allocation must not invoke a launcher")

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", unexpected_discovery)
    captured = _capture_run(
        tmp_path, monkeypatch, "codex", None, via_cli=True)
    command = captured["launch"][1]

    assert command[0] == "/launcher/bin/codex"
    assert command.count("--yolo") == 1
    assert "tool_launcher" not in captured["preflight"][2]
    assert captured["record"]["task"]["auto_confirm"] is False
    assert "launcher" not in captured["record"]["runtime"]
    assert captured["record"]["runtime"]["launch_mode"] == (
        "current_autorun_allocation")
    assert captured["record"]["runtime"]["allocation"] == allocation


@pytest.mark.parametrize("tool,expected_arg", [
    ("claude", "--dangerously-skip-permissions"),
    ("pi", "--approve"),
])
def test_current_autorun_allocation_uses_tool_specific_autorun_args(
        tmp_path, monkeypatch, tool, expected_arg):
    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation",
        lambda _env: {"job_id": "123", "service_class": "autorun_sla",
                      "host": "host-a"})
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")

    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("current autorun allocation must not invoke a launcher")

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", unexpected_discovery)
    captured = _capture_run(tmp_path, monkeypatch, tool, None, via_cli=True)
    command = captured["launch"][1]

    if tool == "claude":
        assert command[0] == "env"
    else:
        assert command[0] == "/launcher/bin/" + tool
    assert expected_arg in command
    if tool == "claude":
        assert "--permission-mode" not in command
    assert captured["record"]["task"]["auto_confirm"] is False


def test_unverified_autorun_job_uses_the_auto_launcher(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation", lambda _env: None)
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")

    def resolve(preset, selected, *_args, **_kwargs):
        calls.append((preset, selected))
        return (["fake-launcher", "{command}"],
                {"preset": "auto", "transport": "qsub"}, [])

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", resolve)
    captured = _capture_run(
        tmp_path, monkeypatch, "codex", None, via_cli=True)

    assert calls == [(("auto", ""), "codex")]
    assert captured["launch"][1][0] == "/launcher/bin/fake-launcher"
    assert captured["record"]["runtime"]["launcher"]["transport"] == "qsub"
    assert "allocation" not in captured["record"]["runtime"]


def test_explicit_non_auto_launcher_keeps_precedence(
        tmp_path, monkeypatch):
    def unexpected_allocation_check(*_args, **_kwargs):
        pytest.fail("explicit launcher override must be preserved")

    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation",
        unexpected_allocation_check)
    custom_launcher = "/launcher/bin/fake-launcher"
    captured = _capture_run(
        tmp_path, monkeypatch, "codex", custom_launcher, via_cli=True)

    assert captured["launch"][1][0] == "/launcher/bin/fake-launcher"


def test_current_autorun_allocation_allows_local_api_profile(
        tmp_path, monkeypatch):
    allocation = {
        "job_id": "123", "service_class": "autorun_sla",
        "host": "host-a", "project": "mobile", "queue": "o_priority",
        "virtual_queue": "interactive_asic_16G_48H",
    }
    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation",
        lambda _env: dict(allocation))
    monkeypatch.setattr(cli, "default_tool_launcher", lambda: "auto")
    monkeypatch.setattr(
        "camc_pkg.api_store.resolve_run_api_name",
        lambda *_args, **_kwargs: ("ihub-direct", "explicit"))
    monkeypatch.setattr(
        "camc_pkg.api_resolver.resolve_run_plan",
        lambda *_args, **_kwargs: {
            "name": "ihub-direct", "provider": "test", "mode": "direct",
            "model": "test-model", "env": {}, "proxy_required": False,
        })
    monkeypatch.setattr(
        "camc_pkg.api_token.resolve_token",
        lambda *_args, **_kwargs: ("token", "test"))

    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("local API run must not invoke a remote launcher")

    monkeypatch.setattr(cli, "resolve_named_tool_launcher", unexpected_discovery)
    captured = _capture_run(
        tmp_path, monkeypatch, "codex", None, via_cli=True,
        api="ihub-direct")

    assert captured["launch"][1][0] == "/launcher/bin/codex"
    assert captured["record"]["runtime"]["launch_mode"] == (
        "current_autorun_allocation")
    assert captured["record"]["api"]["name"] == "ihub-direct"


def test_codex_launcher_adds_yolo_and_keeps_remote_tool_name(tmp_path, monkeypatch):
    secret = "SECRET_TOKEN=manifest-must-not-store-this"
    captured = _capture_run(
        tmp_path, monkeypatch, "codex",
        "qsub --credential %s -q o_cpu_4G -Is" % secret)

    launch = captured["launch"][1]
    assert launch[:-1] == [
        "/launcher/bin/fake-launcher", "--credential", secret,
        "-q", "o_cpu_4G", "-Is"]
    target_script = _resolved_target_script(launch[-1])
    assert "command -v codex" in target_script
    assert 'exec ' in target_script and '"$_camc_tool"' in target_script
    assert "--yolo" in target_script
    assert "/launcher/bin/codex" not in target_script
    assert captured["record"]["runtime"]["custom_launch"]["exit_status_path"].endswith("abc12345.tool-exit")
    manifest = captured["record"]["runtime"]
    assert manifest["launcher"] == {
        "executable": "/launcher/bin/fake-launcher",
        "target_tool": "codex",
        "argv_count": 6,
        "command_index": 6,
        "launch_status_file": manifest["custom_launch"]["exit_status_path"] + ".launcher",
    }
    assert secret in manifest["custom_launch"]["tool_launcher"]
    assert "must-not-enter-manifest" not in repr(manifest)
    assert captured["record"]["task"]["auto_confirm"] is False


@pytest.mark.parametrize("tool,target_binary", [
    ("codex", "codex"),
    ("claude", "claude"),
    ("cursor", "cursor-agent"),
    ("pi", "pi"),
])
def test_named_qsub_resolves_every_tool_before_exec(
        tmp_path, monkeypatch, tool, target_binary):
    preferred = str(tmp_path / "absolute tool dir" / target_binary)
    monkeypatch.setattr(
        cli, "resolve_local_target_tool_path",
        lambda *_args, **_kwargs: preferred)
    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation", lambda _env: None)
    monkeypatch.setattr(
        cli, "resolve_named_tool_launcher",
        lambda *_args, **_kwargs: (
            ["fake-launcher", "-Is", "{command}"],
            {"preset": "auto", "transport": "qsub"}, []))

    captured = _capture_run(tmp_path, monkeypatch, tool, "auto")
    target_script = _resolved_target_script(captured["launch"][1][-1])

    assert "command -v %s" % target_binary in target_script
    assert shlex.quote(preferred) in target_script
    assert "--version" in target_script
    assert 'exec ' in target_script and '"$_camc_tool"' in target_script


def test_qsub_keeps_env_absolute_tool_and_options_in_order(
        tmp_path, monkeypatch):
    preferred = str(tmp_path / "absolute tool dir" / "claude")
    monkeypatch.setattr(
        cli, "resolve_local_target_tool_path",
        lambda *_args, **_kwargs: preferred)
    monkeypatch.setattr(
        cli, "detect_current_autorun_allocation", lambda _env: None)
    monkeypatch.setattr(
        cli, "resolve_named_tool_launcher",
        lambda *_args, **_kwargs: (
            ["fake-launcher", "-Is", "{command}"],
            {"preset": "auto", "transport": "qsub"}, []))

    captured = _capture_run(tmp_path, monkeypatch, "claude", "auto")
    target_script = _resolved_target_script(captured["launch"][1][-1])
    exec_line = next(line for line in target_script.splitlines()
                     if line.startswith("exec "))

    assert shlex.quote(preferred) in target_script
    assert exec_line.startswith(
        "exec /usr/bin/env CLAUDE_CODE_DISABLE_MOUSE=1 "
        "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 \"$_camc_tool\" ")
    assert "--allowed-tools" not in exec_line
    assert "--dangerously-skip-permissions" in exec_line


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
    target_script = _resolved_target_script(captured["launch"][1][-1])
    assert "command -v codex" in target_script
    assert 'exec ' in target_script and '"$_camc_tool"' in target_script
    assert "--yolo" in target_script
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
        "launch_status_file": captured["record"]["runtime"]["custom_launch"]["exit_status_path"] + ".launcher",
    }
    assert captured["record"]["task"]["auto_confirm"] is False


def test_cmd_run_uses_resolved_battach_tool_and_drops_private_metadata(
        tmp_path, monkeypatch):
    remote_tool = "/home/prgn_share/tools/codex/bin/codex"
    monkeypatch.setattr(
        cli, "resolve_named_tool_launcher",
        lambda *_args, **_kwargs: (
            ["/fake/battach", "-L", "{command}", "1796124140"],
            {"preset": "auto", "transport": "battach",
             "job_id": "1796124140", "host": "host-a",
             "_target_tool_path": remote_tool}, []))

    captured = _capture_run(
        tmp_path, monkeypatch, "codex", "auto", via_cli=True)
    command = captured["launch"][1]
    assert command[0] == "/launcher/bin/fake-launcher"
    script_path = command[command.index("-L") + 1]
    assert command == ["/launcher/bin/fake-launcher", "-L", script_path,
                       "1796124140"]
    script_text = Path(script_path).read_text()
    assert remote_tool in script_text
    assert "--yolo" in script_text
    assert captured["record"]["runtime"]["custom_launch"][
        "exit_status_path"].endswith("abc12345.tool-exit")
    launcher_record = captured["record"]["runtime"]["launcher"]
    assert launcher_record["transport"] == "battach"
    assert launcher_record["job_id"] == "1796124140"
    assert "_target_tool_path" not in launcher_record
    assert remote_tool not in repr(launcher_record)
    shutil.rmtree(os.path.dirname(script_path), ignore_errors=True)


def test_pi_launcher_approves_project_resources(tmp_path, monkeypatch):
    captured = _capture_run(
        tmp_path, monkeypatch, "pi",
        "qsub -q o_cpu_4G -Is")

    target_script = _resolved_target_script(captured["launch"][1][-1])
    assert "command -v pi" in target_script
    assert 'exec "$_camc_tool" --approve' in target_script
    assert captured["record"]["task"]["tool"] == "pi"
    assert captured["record"]["state"] == "running"
    assert captured["record"]["phase"] == "initializing"


def test_claude_launcher_uses_skip_permissions_without_auto_mode(tmp_path, monkeypatch):
    captured = _capture_run(
        tmp_path, monkeypatch, "claude",
        "ssh -tt root@192.168.1.100")

    target_script = _resolved_target_script(captured["launch"][1][-1])
    assert "command -v claude" in target_script
    assert "--allowed-tools" not in target_script
    assert "--dangerously-skip-permissions" in target_script
    assert "--permission-mode" not in target_script
    assert "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1" in target_script


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
        tmp_path, monkeypatch, "codex", "ssh target",
        runtime_spec=spec)

    command = captured["launch"][1][-1]
    assert "mkdir -p -- %s" % shlex.quote(str(local)) in command
    assert "chmod 700 -- %s" % shlex.quote(str(local)) in command
    overlay = shared / "abc12345"
    assert " CODEX_HOME=%s " % overlay in command
    assert "PATH=$_camc_path; export PATH" in command
    assert local.is_dir()  # submit host is bootstrapped too
    assert (overlay / "tmp" / "arg0").is_dir()
    assert not (shared / "tmp").exists()


def test_adapter_base_commands_keep_only_workspace_settings(tmp_path):
    claude = _adapter("claude")
    normal = cli._build_command(claude, "prompt", str(tmp_path))
    assert "--allowed-tools" not in normal
    assert "--permission-mode" not in normal
    assert "--dangerously-skip-permissions" not in normal

    codex = _adapter("codex")
    assert codex.command == ["codex"]
    assert codex.trust_workspace is True
    codex_normal = cli._build_command(codex, "prompt", str(tmp_path))
    assert codex_normal[0:2] == ["codex", "-c"]
    assert "projects={" in codex_normal[2]
    assert 'trust_level="trusted"' in codex_normal[2]
    assert "features.goals=true" not in codex_normal
    assert "--yolo" not in codex_normal
    assert codex.launcher_autorun_args == ["--yolo"]
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
        tool_launcher=["fake-launcher"])

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
            tool_launcher.build_launcher_command(
                [str(launcher)], ["remote-tool", "two words"]),
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


def test_auto_failure_progress_is_brief_and_secret_free(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tool_launcher, '_launcher_binary', lambda *_args: '')
    def unavailable(*args, **kwargs):
        raise ValueError('project setup failed SECRET_TOKEN')
    monkeypatch.setattr(tool_launcher, '_qsub_launcher', unavailable)
    with pytest.raises(ValueError, match="could not prepare an LSF launcher") as exc:
        tool_launcher.resolve_named_tool_launcher(
            ('auto', ''), 'codex', str(tmp_path), {}, progress=cli._launcher_progress)
    output = capsys.readouterr().out
    assert '[launcher] battach: bjobs unavailable' in output
    assert '[launcher] ssh: no usable target; trying qsub' in output
    assert '[launcher] qsub: project setup failed; launch failed' in output
    assert 'SECRET_TOKEN' not in output + str(exc.value)
    assert all(len(line) < 120 for line in output.splitlines())


def test_run_reports_self_check_before_remote_progress(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'detect_current_autorun_allocation', lambda env: None)
    def resolve(*args, **kwargs):
        kwargs['progress']('battach: checking reusable autorun jobs')
        kwargs['progress']('qsub: prepared; submission will run in tmux')
        return ['qsub', '-Is', '{command}'], {'preset': 'auto', 'transport': 'qsub'}, []
    monkeypatch.setattr(cli, 'resolve_named_tool_launcher', resolve)
    _capture_run(tmp_path, monkeypatch, 'codex', 'auto')
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line.startswith('[launcher]')]
    assert lines[0] == '[launcher] self: checking current autorun allocation'
    assert 'trying battach' in lines[1]
    assert lines[2] == '[launcher] battach: checking reusable autorun jobs'
    assert lines[3] == '[launcher] qsub: prepared; submission will run in tmux'


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_reboot_auto_uses_autorun_without_resume_or_local_fallback(tmp_path, monkeypatch, tool):
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", lambda *_a, **_kw: (
        ["qsub", "-Is", "{command}"],
        {"preset": "auto", "transport": "qsub", "_auto_plan": []}, []))
    captured = _capture_run(tmp_path, monkeypatch, tool, "auto", reboot=True)
    command = captured["launch"][1]
    target = (Path(command[0]).parent / "target-0").read_text()
    flag = "--yolo" if tool == "codex" else "--dangerously-skip-permissions"
    assert target.count(flag) == 1
    assert "resume" not in target
    assert captured["record"]["runtime"]["launcher"]["fallback_order"] == [
        "direct", "battach", "ssh", "qsub"]
    assert "local fallback disabled" in Path(command[0]).read_text()


def test_reboot_auto_rejects_resolved_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "resolve_named_tool_launcher", lambda *_a, **_kw: (
        [], {"preset": "auto", "transport": "local"}, []))
    with pytest.raises(SystemExit):
        _capture_run(tmp_path, monkeypatch, "codex", "auto", reboot=True)


def test_prepared_record_retains_user_launch_settings(tmp_path, monkeypatch):
    captured = _capture_run(tmp_path, monkeypatch, "claude", "qsub -Is",
                            extra_tool_env=["TOKEN=a b=c", "PATH=/extra:$PATH"],
                            extra_tool_args=['--model "model name"'])
    saved = captured["record"]["runtime"]["custom_launch"]
    assert saved["tool_env"] == ["TOKEN=a b=c", "PATH=/extra:$PATH"]
    assert saved["tool_args"] == ["--model", "model name"]
    assert saved["tool_launcher"] == "qsub -Is"
    assert saved["use_env_tool"] is False
    assert captured["launch"][3]["env"]["TOKEN"] == "a b=c"
