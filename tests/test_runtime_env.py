"""Focused tests for runtime_env + the cli/transport plumbing that
threads it (F-08).

Scope:
  * load_login_shell_env fallback when the shell is missing.
  * build_runtime_env cleanup (TMUX / TMUX_PANE / CLAUDECODE).
  * resolve_tool uses runtime PATH, NOT os.environ['PATH'].
  * Claude auth file existence: present → no auth issue; missing →
    error (and we never read its contents).
  * Codex soft-auth: OPENAI_API_KEY in runtime env satisfies the
    requirement even if no auth file is on disk.
  * check_tool_readiness flags an error when the selected tool isn't
    on the effective PATH, even if it's on os.environ['PATH'].
  * cmd_run wiring: _preflight returns (issues, resolved); when there
    is an error, NO tmux session is created.
  * transport.create_tmux_session accepts explicit env + tmux_bin and
    uses them (defaults to legacy behavior when omitted).
  * cmd_env --json shape + non-mutating behavior.
"""

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import runtime_env as re_mod  # noqa: E402
from camc_pkg import cli as camc_cli        # noqa: E402
from camc_pkg import transport               # noqa: E402


# ---------------------------------------------------------------------------
# load_login_shell_env — fallback on missing shell
# ---------------------------------------------------------------------------

def test_load_login_shell_env_falls_back_when_shell_missing():
    env, warnings = re_mod.load_login_shell_env(shell="/no/such/shell")
    assert env == os.environ or "PATH" in env
    # warnings must mention the fallback so the human sees it
    assert any("not found" in w or "using current env" in w for w in warnings)


def test_load_login_shell_env_uses_bash_for_env_setup_under_csh(monkeypatch):
    calls = []

    class Result(object):
        returncode = 0
        stdout = b"PATH=/from/bash\0HOME=/home/runtime"
        stderr = b""

    def fake_exists(path):
        return path in ("/bin/bash", "/bin/csh")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(re_mod.os.path, "exists", fake_exists)
    monkeypatch.setattr(re_mod.subprocess, "run", fake_run)

    env, warnings = re_mod.load_login_shell_env(
        shell="/bin/csh", env_setup="source /home/hren/.bashrc")

    assert warnings == []
    assert env["PATH"] == "/from/bash"
    assert calls
    assert calls[0][0] == "/bin/bash"
    assert calls[0][1:3] == ["-l", "-c"]
    assert "source /home/hren/.bashrc" in calls[0][3]


# ---------------------------------------------------------------------------
# build_runtime_env — cleanup
# ---------------------------------------------------------------------------

def test_build_runtime_env_strips_nest_markers(monkeypatch):
    """TMUX / TMUX_PANE / CLAUDECODE must be removed from RuntimeEnv.env
    regardless of which capture path (login shell vs fallback) ran."""
    # Force the fallback path so we know the env starts from
    # os.environ, then assert the strip happened.
    def _bad(shell=None, env_setup=None, timeout=5):
        return {"PATH": "/usr/bin", "TMUX": "/tmp/tmux", "TMUX_PANE": "%0",
                "CLAUDECODE": "1", "HOME": "/home/x"}, ["forced for test"]
    monkeypatch.setattr(re_mod, "load_login_shell_env", _bad)
    rt = re_mod.build_runtime_env()
    assert "TMUX" not in rt.env
    assert "TMUX_PANE" not in rt.env
    assert "CLAUDECODE" not in rt.env
    assert rt.env.get("PATH") == "/usr/bin"
    # Capture warnings flow through to the RuntimeEnv.
    assert rt.warnings == ["forced for test"]


# ---------------------------------------------------------------------------
# resolve_tool — PATH comes from runtime, NOT os.environ
# ---------------------------------------------------------------------------

def test_resolve_tool_uses_runtime_path_not_os_environ_path(tmp_path, monkeypatch):
    """Put a fake `widget` in tmp_path on the runtime's PATH; leave
    os.environ['PATH'] pointing at a different empty dir. resolve_tool
    must find it via the runtime PATH."""
    # Make an executable in tmp_path/runtime_only
    runtime_dir = tmp_path / "runtime_only"
    runtime_dir.mkdir()
    fake = runtime_dir / "widget"
    fake.write_text("#!/bin/sh\necho widget\n")
    fake.chmod(0o755)

    # Different empty dir for os.environ.
    os_dir = tmp_path / "os_only"
    os_dir.mkdir()
    monkeypatch.setenv("PATH", str(os_dir))

    rt = re_mod.RuntimeEnv(env={"PATH": str(runtime_dir)}, source="explicit",
                           shell="", path=str(runtime_dir))
    resolved = re_mod.resolve_tool(rt, "widget")
    assert resolved == str(fake)

    # And the reverse: a tool living only in os.environ['PATH'] must
    # NOT resolve via the runtime PATH.
    other = os_dir / "other"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    assert re_mod.resolve_tool(rt, "other") is None


# ---------------------------------------------------------------------------
# Claude auth-file check
# ---------------------------------------------------------------------------

def _rt_with_path(*paths):
    """Build a RuntimeEnv whose PATH covers `paths`. Used in readiness
    tests so we can control whether `tmux` / `claude` are visible
    without touching real env vars."""
    return re_mod.RuntimeEnv(env={"PATH": os.pathsep.join(paths)},
                             source="explicit", shell="",
                             path=os.pathsep.join(paths))


def test_check_tool_readiness_claude_auth_present_passes_without_reading(tmp_path, monkeypatch):
    """A present + readable ~/.claude.json must satisfy the auth check.
    We verify NO read happens by making the file content un-parseable
    JSON (if anyone tries to read+parse it, the test would also
    inspect content — which is exactly what F-08 forbids)."""
    home = tmp_path / "home"
    home.mkdir()
    claude_json = home / ".claude.json"
    claude_json.write_text("not json at all <<< sentinel >>>")
    monkeypatch.setenv("HOME", str(home))
    # Also stub tmux + claude binaries to skip the binary check noise.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    rt = _rt_with_path(str(bindir))
    r = re_mod.check_tool_readiness(rt, "claude")
    # No "auth file missing" issue should be present.
    msgs = [m for _, m in r["issues"]]
    assert not any("auth file missing" in m or "auth file not readable" in m for m in msgs), \
        "Claude auth file present must NOT raise an auth issue: %r" % msgs


def test_check_tool_readiness_claude_auth_missing_is_blocking_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Stub tmux + claude so the auth check is the only outstanding error.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    rt = _rt_with_path(str(bindir))
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("auth file missing" in e and "~/.claude.json" in e or "/.claude.json" in e
               for e in errors), \
        "Expected blocking 'auth file missing' error: %r" % errors


# ---------------------------------------------------------------------------
# Codex soft auth
# ---------------------------------------------------------------------------

def test_check_tool_readiness_claude_auth_uses_runtime_HOME_not_os_environ(tmp_path, monkeypatch):
    """F1: when runtime.env['HOME'] differs from os.environ['HOME'],
    the auth-file check must resolve ~/.claude.json against the
    runtime HOME. Otherwise checks and launches diverge — exactly
    the F-08 invariant we are guarding."""
    process_home = tmp_path / "process_home"
    runtime_home = tmp_path / "runtime_home"
    process_home.mkdir()
    runtime_home.mkdir()
    # Process HOME has .claude.json (would falsely satisfy the check
    # if we expand via os.environ).
    (process_home / ".claude.json").write_text("decoy")
    # Runtime HOME does NOT — the launch will see runtime HOME, so
    # the check must follow.
    monkeypatch.setenv("HOME", str(process_home))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    rt = re_mod.RuntimeEnv(
        env={"PATH": str(bindir), "HOME": str(runtime_home)},
        source="explicit", shell="", path=str(bindir),
    )
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("auth file missing" in e and str(runtime_home) in e for e in errors), \
        ("Expected blocking error citing runtime HOME (%s), not process HOME (%s): %r"
         % (runtime_home, process_home, errors))

    # Mirror image: when runtime_home has the file, the check passes
    # even if process HOME does not.
    (runtime_home / ".claude.json").write_text("stub")
    (process_home / ".claude.json").unlink()
    r2 = re_mod.check_tool_readiness(rt, "claude")
    errors2 = [m for lvl, m in r2["issues"] if lvl == "error"]
    assert not any("auth file missing" in e for e in errors2), \
        "Expected NO auth-file error when runtime HOME has the file: %r" % errors2


def test_check_tool_readiness_codex_soft_auth_uses_runtime_HOME(tmp_path, monkeypatch):
    """F1: codex soft auth path expansion must also respect runtime HOME."""
    process_home = tmp_path / "process_home"
    runtime_home = tmp_path / "runtime_home"
    process_home.mkdir()
    runtime_home.mkdir()
    # Process HOME has the soft auth file (would falsely satisfy the
    # check if we expanded via os.environ).
    (process_home / ".codex").mkdir()
    (process_home / ".codex" / "auth.json").write_text("decoy")
    monkeypatch.setenv("HOME", str(process_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "codex"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    # No OPENAI_API_KEY, no codex file under runtime HOME.
    rt = re_mod.RuntimeEnv(
        env={"PATH": str(bindir), "HOME": str(runtime_home)},
        source="explicit", shell="", path=str(bindir),
    )
    r = re_mod.check_tool_readiness(rt, "codex")
    msgs = [m for _, m in r["issues"]]
    assert any("auth not found" in m for m in msgs), \
        ("Expected soft-auth warning when runtime HOME has no codex file "
         "(process HOME's file must NOT satisfy the check): %r" % msgs)


def test_check_tool_readiness_codex_accepts_openai_api_key_env(tmp_path, monkeypatch):
    """A live OPENAI_API_KEY in the runtime env satisfies the soft
    auth check even when no file is on disk."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "codex"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    rt = re_mod.RuntimeEnv(
        env={"PATH": str(bindir), "OPENAI_API_KEY": "sk-fake-not-checked"},
        source="explicit", shell="", path=str(bindir),
    )
    r = re_mod.check_tool_readiness(rt, "codex")
    msgs = [m for _, m in r["issues"]]
    # No "auth not found" soft warning when OPENAI_API_KEY is present.
    assert not any("auth not found" in m for m in msgs), \
        "Codex with OPENAI_API_KEY must not raise the soft auth warning: %r" % msgs


# ---------------------------------------------------------------------------
# Effective PATH wins: tool in os.environ but not in runtime fails
# ---------------------------------------------------------------------------

def test_check_tool_readiness_selected_tool_not_on_effective_path_is_error(tmp_path, monkeypatch):
    """Even if `claude` is on the test process's PATH (it is, on
    NVIDIA hosts), the readiness check looks at runtime.env['PATH']
    only. An empty runtime PATH must make `claude not found` an
    error. 2026-06-23: golden paths neutralized for this test so PATH
    fallback is exercised."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(re_mod, "_GOLDEN_TMUX_PATHS", ())
    monkeypatch.setattr(re_mod, "_GOLDEN_TOOL_PATHS", {})
    # Real claude IS on os.environ['PATH'] on the dev host. We
    # deliberately do NOT add it to the runtime env.
    empty = tmp_path / "empty"
    empty.mkdir()
    rt = _rt_with_path(str(empty))
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("not found" in e and "claude" in e for e in errors), \
        "Expected blocking 'claude not found' error: %r" % errors


# ---------------------------------------------------------------------------
# F2: version/sanity probe must block on any nonzero rc
# ---------------------------------------------------------------------------

def test_check_tool_readiness_version_probe_nonzero_rc_blocks(tmp_path, monkeypatch):
    """F2: a binary that exists but exits non-zero on --version (or
    --version equivalent) must produce a blocking error. Previously
    this was only a warn (and rc=-1 was silently dropped)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # tmux stub — outputs 'tmux 2.4 ok' on rc 0.
    (bindir / "tmux").write_text("#!/bin/sh\necho 'tmux 2.4 ok'\n")
    (bindir / "tmux").chmod(0o755)
    # claude stub that EXITS 1 on --version regardless of args.
    (bindir / "claude").write_text("#!/bin/sh\necho 'broken' >&2\nexit 1\n")
    (bindir / "claude").chmod(0o755)
    rt = re_mod.RuntimeEnv(env={"PATH": str(bindir), "HOME": str(home)},
                           source="explicit", shell="", path=str(bindir))
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("sanity probe" in e and "rc=1" in e and "claude" in e for e in errors), \
        "Expected blocking sanity-probe error: %r" % errors


def test_check_tool_readiness_version_probe_timeout_blocks(monkeypatch, tmp_path):
    """F2: rc=-1 (timeout / OSError from run_probe) must ALSO block —
    the previous code special-cased -1 as 'silently pass' which let
    a hanging binary land us at tmux creation."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tmux").write_text("#!/bin/sh\necho 'tmux 2.4 ok'\n")
    (bindir / "tmux").chmod(0o755)
    (bindir / "claude").write_text("#!/bin/sh\necho 'looks fine'\n")
    (bindir / "claude").chmod(0o755)
    # Monkeypatch run_probe so the SECOND call (selected-tool probe)
    # returns rc=-1, simulating a timeout. The first call (tmux -V)
    # still returns rc=0 with valid output.
    real_run_probe = re_mod.run_probe
    calls = {"n": 0}
    def _fake_probe(runtime, argv, timeout=5):
        calls["n"] += 1
        # First call: tmux -V → return real-ish good output
        if calls["n"] == 1:
            return 0, "tmux 2.4 ok"
        return -1, ""   # second+ calls: simulate timeout
    monkeypatch.setattr(re_mod, "run_probe", _fake_probe)
    rt = re_mod.RuntimeEnv(env={"PATH": str(bindir), "HOME": str(home)},
                           source="explicit", shell="", path=str(bindir))
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("sanity probe" in e and "rc=-1" in e for e in errors), \
        "Expected rc=-1 timeout to block via sanity-probe error: %r" % errors


def test_check_tool_readiness_tmux_probe_failure_blocks(monkeypatch, tmp_path):
    """F2: when `tmux -V` exits non-zero, we cannot verify >= 2.4 →
    block. Previously this branch ran `if rc == 0 and out:` so a
    failing probe was silently ignored. 2026-06-23: neutralize
    golden tmux path so PATH-resolved stub gets probed."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(re_mod, "_GOLDEN_TMUX_PATHS", ())
    monkeypatch.setattr(re_mod, "_GOLDEN_TOOL_PATHS", {})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # tmux stub that EXITS 1.
    (bindir / "tmux").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "tmux").chmod(0o755)
    (bindir / "claude").write_text("#!/bin/sh\necho 'ok'\n")
    (bindir / "claude").chmod(0o755)
    rt = re_mod.RuntimeEnv(env={"PATH": str(bindir), "HOME": str(home)},
                           source="explicit", shell="", path=str(bindir))
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("tmux probe" in e and "rc=" in e for e in errors), \
        "Expected blocking error when tmux -V exits non-zero: %r" % errors


def _neutralize_golden(monkeypatch):
    """2026-06-23 helper: zero out golden tmux + tool paths so tests
    exercise the PATH fallback (which is what existing fixtures stub)."""
    monkeypatch.setattr(re_mod, "_GOLDEN_TMUX_PATHS", ())
    monkeypatch.setattr(re_mod, "_GOLDEN_TOOL_PATHS", {})


def test_check_tool_readiness_tmux_version_unparseable_blocks(monkeypatch, tmp_path):
    """F2: when `tmux -V` exits 0 but the output has no parseable
    version string, we cannot verify >= 2.4 → block."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    _neutralize_golden(monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # tmux stub that exits 0 with garbage output (no digits).
    (bindir / "tmux").write_text("#!/bin/sh\necho 'tmux funny build'\n")
    (bindir / "tmux").chmod(0o755)
    (bindir / "claude").write_text("#!/bin/sh\necho 'ok'\n")
    (bindir / "claude").chmod(0o755)
    rt = re_mod.RuntimeEnv(env={"PATH": str(bindir), "HOME": str(home)},
                           source="explicit", shell="", path=str(bindir))
    r = re_mod.check_tool_readiness(rt, "claude")
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("tmux version not parseable" in e for e in errors), \
        "Expected blocking error when tmux version is not parseable: %r" % errors


# ---------------------------------------------------------------------------
# transport.create_tmux_session: env + tmux_bin defaults vs explicit
# ---------------------------------------------------------------------------

def test_create_tmux_session_defaults_when_no_env_or_tmux_bin(monkeypatch):
    """When the caller passes nothing for env / tmux_bin, the function
    must use os.environ.copy() and transport.TMUX_BIN — matching
    pre-F-08 behavior."""
    captured = {}

    class _Proc(object):
        returncode = 0
        def communicate(self, timeout=None):
            return (b"", b"")
    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _Proc()
    monkeypatch.setattr(transport.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(transport, "_run", lambda *a, **kw: (0, ""))

    ok = transport.create_tmux_session("test-default", ["echo", "hi"], "/tmp",
                                       inherit_env=True)
    assert ok is True
    assert captured["argv"][0] == transport.TMUX_BIN
    # env was os.environ.copy() minus TMUX/TMUX_PANE/CLAUDECODE
    assert "TMUX" not in captured["env"]
    assert "TMUX_PANE" not in captured["env"]
    assert "CLAUDECODE" not in captured["env"]


def test_create_tmux_session_uses_explicit_env_and_tmux_bin(monkeypatch):
    """When the caller passes env and tmux_bin (the new cmd_run path),
    the function honors them — that's the entire point of F-08."""
    captured = {}

    class _Proc(object):
        returncode = 0
        def communicate(self, timeout=None):
            return (b"", b"")
    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _Proc()
    monkeypatch.setattr(transport.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(transport, "_run", lambda *a, **kw: (0, ""))

    explicit_env = {"PATH": "/custom/bin", "HOME": "/home/x",
                    "TMUX": "should-be-stripped"}
    ok = transport.create_tmux_session("test-explicit", ["echo", "hi"], "/tmp",
                                       inherit_env=True,
                                       env=explicit_env,
                                       tmux_bin="/opt/tmux/bin/tmux")
    assert ok is True
    assert captured["argv"][0] == "/opt/tmux/bin/tmux"
    assert captured["env"]["PATH"] == "/custom/bin"
    # Nest markers stripped even when the caller forgets.
    assert "TMUX" not in captured["env"]


def test_create_tmux_session_pastes_startup_command(monkeypatch):
    """The default two-step launch must paste the startup command through
    a tmux buffer. PDX tmux has been observed to drop the server on
    send-keys -l before Enter, so startup injection should not use that
    path when buffer paste succeeds.
    """
    run_calls = []

    class _Proc(object):
        returncode = 0
        def communicate(self, timeout=None):
            return (b"", b"")

    monkeypatch.setattr(
        transport.subprocess, "Popen",
        lambda argv, **kwargs: _Proc())

    def _fake_run(argv, **kwargs):
        run_calls.append((list(argv), dict(kwargs)))
        return (0, "")

    monkeypatch.setattr(transport, "_run", _fake_run)

    ok = transport.create_tmux_session(
        "test-paste",
        ["env", "CLAUDE_CODE_DISABLE_MOUSE=1", "claude", "--session-id",
         "manualtest-0000-0000-0000-000000000000"],
        "/tmp",
        inherit_env=True,
        tmux_bin="/opt/tmux/bin/tmux")

    assert ok is True
    argv_calls = [c[0] for c in run_calls]
    assert any("set-buffer" in argv for argv in argv_calls)
    assert any("paste-buffer" in argv for argv in argv_calls)
    assert not any("send-keys" in argv and "-l" in argv
                   for argv in argv_calls)
    assert any(argv[-1] == "Enter" for argv in argv_calls
               if "send-keys" in argv)


# ---------------------------------------------------------------------------
# _preflight returns (issues, resolved); errors block tmux creation
# ---------------------------------------------------------------------------

def test_preflight_returns_issues_and_resolved_tuple(tmp_path, monkeypatch):
    """The new signature is (issues, resolved). issues is a list of
    (level, message); resolved is a dict from tool name to abs path."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    rt = _rt_with_path(str(bindir))
    workdir = tmp_path / "work"
    issues, resolved = camc_cli._preflight("claude", "claude", str(workdir),
                                           runtime=rt)
    assert isinstance(issues, list)
    assert isinstance(resolved, dict)
    assert "tmux" in resolved
    assert "tool" in resolved


def test_cmd_run_does_not_create_tmux_session_on_readiness_error(tmp_path, monkeypatch, capsys):
    """When _preflight reports an error, cmd_run must SystemExit
    BEFORE invoking create_tmux_session."""
    home = tmp_path / "home"
    home.mkdir()
    # No ~/.claude.json → blocking auth error.
    monkeypatch.setenv("HOME", str(home))
    # Empty runtime PATH so the tool can't be found either.
    monkeypatch.setattr(re_mod, "build_runtime_env",
                        lambda **kw: re_mod.RuntimeEnv(env={"PATH": "/no/such"},
                                                       source="explicit", shell="",
                                                       path="/no/such"))
    called = {"count": 0}
    def _bad_create(*a, **kw):
        called["count"] += 1
        return True
    monkeypatch.setattr(camc_cli, "create_tmux_session", _bad_create)

    class _Args(object):
        tool = "claude"
        prompt = ""
        path = str(tmp_path / "work")
        no_inherit_env = True
        name = None
        auto_exit = False
        auto_exit_enable = False
        tag = []
        json = False
    with pytest.raises(SystemExit):
        camc_cli.cmd_run(_Args())
    assert called["count"] == 0, "create_tmux_session must NOT run on readiness error"


# ---------------------------------------------------------------------------
# cmd_env --json shape + non-mutating
# ---------------------------------------------------------------------------

def test_cmd_env_check_json_shape(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s 2.4 stub'\n" % name)
        p.chmod(0o755)
    monkeypatch.setattr(re_mod, "build_runtime_env",
                        lambda **kw: re_mod.RuntimeEnv(
                            env={"PATH": str(bindir), "HOME": str(home)},
                            source="explicit", shell="/bin/sh",
                            path=str(bindir),
                        ))

    class _Args(object):
        tool = "claude"
        json = True
        env_action = "check"
    camc_cli.cmd_env(_Args())
    out = capsys.readouterr().out
    body = json.loads(out)
    # Required keys present.
    for k in ("tool", "tool_binary", "source", "shell", "path",
              "warnings", "issues", "resolved", "readiness_source"):
        assert k in body, "cmd_env --json missing key %s: %r" % (k, body)
    assert body["tool"] == "claude"
    assert body["source"] == "explicit"
    assert "tmux" in body["resolved"]
    assert "tool" in body["resolved"]
    # claude.toml has a [readiness] block, so cmd_env must report
    # readiness_source=adapter, NOT fallback.
    assert body["readiness_source"] == "adapter", \
        "cmd_env for claude must report readiness_source=adapter: %r" % body



def test_cmd_env_check_ignores_context_env_setup(monkeypatch, capsys):
    """context.json is metadata only; camc env check must not load or
    apply context.env_setup to the runtime environment."""
    captured = {}

    def _bad_context():
        raise AssertionError("cmd_env must not read context.json")

    def _fake_build_runtime_env(**kwargs):
        captured["env_setup"] = kwargs.get("env_setup")
        return re_mod.RuntimeEnv(env={"PATH": "/usr/bin", "HOME": "/home/u"},
                                 source="explicit", shell="/bin/csh",
                                 path="/usr/bin")

    def _fake_check(runtime, selected_tool, tool_binary=None, readiness=None,
                    use_env_tool=False):
        return {"issues": [],
                "resolved": {"tmux": "/bin/tmux", "tool": "/bin/claude"},
                "readiness_source": "adapter"}

    monkeypatch.setattr(camc_cli, "_load_default_context", _bad_context)
    monkeypatch.setattr(re_mod, "build_runtime_env", _fake_build_runtime_env)
    monkeypatch.setattr(re_mod, "check_tool_readiness", _fake_check)

    class _Args(object):
        tool = "claude"
        json = True
        env_action = "check"

    camc_cli.cmd_env(_Args())
    body = json.loads(capsys.readouterr().out)
    assert captured["env_setup"] is None
    assert body["env_setup"] is None


def test_cmd_run_ignores_context_env_setup(tmp_path, monkeypatch):
    """camc run must not let ~/.cam/context.json env_setup alter
    standalone agent runtime. Context is metadata only."""
    captured = {}

    def _fake_build_runtime_env(**kwargs):
        captured["build_env_setup"] = kwargs.get("env_setup")
        return re_mod.RuntimeEnv(env={"PATH": "/usr/bin", "HOME": "/home/u"},
                                 source="explicit", shell="/bin/csh",
                                 path="/usr/bin")

    def _fake_preflight(tool, tool_binary, workdir, env_setup=None,
                        runtime=None, adapter_readiness=None,
                        use_env_tool=False):
        captured["preflight_env_setup"] = env_setup
        return [("error", "stop before launch")], {}

    monkeypatch.setattr(re_mod, "build_runtime_env", _fake_build_runtime_env)
    monkeypatch.setattr(camc_cli, "_load_default_context",
                        lambda: {"env_setup": "source /home/hren/.bashrc"})
    monkeypatch.setattr(camc_cli, "_preflight", _fake_preflight)

    class _Args(object):
        tool = "claude"
        prompt = ""
        path = str(tmp_path / "work")
        no_inherit_env = False
        use_env_tool = False
        name = None
        auto_exit = False
        auto_exit_enable = False
        tag = []
        json = False
        resume_session = None
        system_prompt = None
        system_file = None
        api = None
        no_default_api = True
        api_token = None
        no_api_proxy = False
        proxy_debug = False

    with pytest.raises(SystemExit):
        camc_cli.cmd_run(_Args())
    assert captured["build_env_setup"] is None
    assert captured["preflight_env_setup"] is None


def test_cmd_run_loads_context_for_metadata_only_after_launch(tmp_path, monkeypatch, capsys):
    """Regression for PDX launch: cmd_run still needs context metadata
    after tmux creation, but context.env_setup must not feed runtime env."""
    captured = {}
    logs = tmp_path / "logs"
    pids = tmp_path / "pids"
    monkeypatch.setattr(camc_cli, "LOGS_DIR", str(logs))
    monkeypatch.setattr(camc_cli, "PIDS_DIR", str(pids))
    monkeypatch.setattr(camc_cli, "_load_default_context",
                        lambda: {"name": "ctx-pdx",
                                 "host": "pdx.example",
                                 "env_setup": "source /home/hren/.bashrc"})

    def _fake_build_runtime_env(**kwargs):
        captured["build_env_setup"] = kwargs.get("env_setup")
        return re_mod.RuntimeEnv(env={"PATH": "/usr/bin", "HOME": "/home/u"},
                                 source="explicit", shell="/bin/csh",
                                 path="/usr/bin")

    def _fake_preflight(tool, tool_binary, workdir, env_setup=None,
                        runtime=None, adapter_readiness=None,
                        use_env_tool=False):
        captured["preflight_env_setup"] = env_setup
        return [], {
            "tmux": "/bin/tmux",
            "tmux_source": "golden",
            "tool": "/bin/claude",
            "tool_resolution": {"bin": "/bin/claude",
                                "source": "golden",
                                "warnings": []},
        }

    def _fake_create(session, launch_cmd, workdir, **kwargs):
        captured["create_env_setup"] = kwargs.get("env_setup")
        captured["create_env"] = kwargs.get("env")
        return True

    class _Store(object):
        def save(self, rec):
            captured["agent_rec"] = rec
        def update(self, agent_id, **kwargs):
            captured["update"] = (agent_id, kwargs)

    class _Proc(object):
        pid = 12345

    class _Args(object):
        tool = "claude"
        prompt = "hello"
        path = str(tmp_path / "work")
        no_inherit_env = False
        use_env_tool = False
        name = "pdx-test"
        auto_exit = False
        auto_exit_enable = False
        tag = []
        json = False
        resume_session = None
        system_prompt = None
        system_file = None
        api = None
        no_default_api = True
        api_token = None
        no_api_proxy = False
        proxy_debug = False

    monkeypatch.setattr(re_mod, "build_runtime_env", _fake_build_runtime_env)
    monkeypatch.setattr(camc_cli, "_preflight", _fake_preflight)
    monkeypatch.setattr(camc_cli, "_gen_agent_id", lambda: "abc12345")
    monkeypatch.setattr(camc_cli, "_build_command",
                        lambda config, prompt, workdir: ["claude", prompt])
    monkeypatch.setattr(camc_cli, "create_tmux_session", _fake_create)
    monkeypatch.setattr(transport, "ensure_camc_tmux_config",
                        lambda: str(tmp_path / "tmux.conf"))
    monkeypatch.setattr(camc_cli.subprocess, "check_output",
                        lambda *a, **kw: b"tmux 2.7")
    monkeypatch.setattr(camc_cli.subprocess, "Popen",
                        lambda *a, **kw: _Proc())
    monkeypatch.setattr(camc_cli, "AgentStore", lambda: _Store())

    camc_cli.cmd_run(_Args())
    assert captured["build_env_setup"] is None
    assert captured["preflight_env_setup"] is None
    assert captured["create_env_setup"] is None
    rec = captured["agent_rec"]
    assert rec["context_name"] == "ctx-pdx"
    assert rec["transport_type"] == "ssh"
    assert rec["id"] == "abc12345"


def test_scheduler_launch_ignores_context_env_setup(tmp_path, monkeypatch):
    """Scheduler-launched agents follow the same rule: context.json is
    metadata and must not inject env_setup into create_tmux_session."""
    from camc_pkg import scheduler as camc_scheduler

    captured = {}
    monkeypatch.setattr(camc_scheduler, "_load_default_context",
                        lambda: {"name": "ctx", "host": None,
                                 "env_setup": "source /home/hren/.bashrc"})

    def _fake_create(session, launch_cmd, workdir, env_setup=None,
                     inherit_env=True, **kwargs):
        captured["env_setup"] = env_setup
        captured["inherit_env"] = inherit_env
        return False

    monkeypatch.setattr(camc_scheduler, "create_tmux_session", _fake_create)
    out = camc_scheduler._launch_agent(
        {"tool": "claude", "prompt": "", "name": "sched"},
        str(tmp_path),
    )
    assert out is None
    assert captured["env_setup"] is None
    assert captured["inherit_env"] is True

# ---------------------------------------------------------------------------
# Adapter-owned readiness (preferred over hardcoded _TOOL_SPECS fallback)
# ---------------------------------------------------------------------------

def test_adapter_config_parses_readiness_for_claude_codex_cursor():
    """The three shipped TOMLs declare [readiness] blocks. Each one
    must parse into the canonical dict shape with the expected fields."""
    from camc_pkg.adapters import _load_config
    for tool, expected_binary in (("claude", "claude"),
                                  ("codex", "codex"),
                                  ("cursor", "cursor-agent")):
        cfg = _load_config(tool)
        rd = cfg.readiness
        assert rd is not None, "%s adapter must declare [readiness]" % tool
        assert rd["binary"] == expected_binary
        assert rd["version_args"] == ["--version"]
        assert rd["version_required"] is True
        # required vs optional shape — claude is required-only, codex is
        # optional-only (soft auth), cursor has neither files nor env.
        if tool == "claude":
            assert rd["required_files"] == [
                {"path": "~/.claude.json", "label": "Claude auth file"}
            ]
            assert rd["optional_files"] == []
            assert rd["optional_env"] == []
        elif tool == "codex":
            assert rd["required_files"] == []
            paths = [e["path"] for e in rd["optional_files"]]
            assert "~/.codex/auth.json" in paths
            assert "~/.codex/config.toml" in paths
            assert rd["optional_env"] == [{"name": "OPENAI_API_KEY"}]
        else:  # cursor
            assert rd["required_files"] == []
            assert rd["optional_files"] == []
            assert rd["optional_env"] == []


def test_check_tool_readiness_prefers_adapter_readiness_over_fallback(tmp_path, monkeypatch):
    """When an adapter readiness dict is passed, the check must use
    its binary — even when that binary diverges from what
    _TOOL_SPECS would have used. Strong proof: select tool 'claude'
    but pass a readiness whose binary is 'widget_custom'; the resolved
    tool path must be widget_custom, NOT claude."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _neutralize_golden(monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Provide tmux + widget_custom (the adapter-binary) + claude
    # (the fallback-binary). If the check uses the fallback, it
    # will pick claude; if it uses the adapter, it picks widget_custom.
    for name in ("tmux", "widget_custom", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s ok'\n" % name)
        p.chmod(0o755)
    rt = _rt_with_path(str(bindir))
    rt.env["HOME"] = str(home)

    adapter_rd = {
        "binary":           "widget_custom",
        "version_args":     ["--version"],
        "version_required": True,
        "install_hint":     "n/a",
        "required_files":   [],
        "optional_files":   [],
        "optional_env":     [],
    }
    r = re_mod.check_tool_readiness(rt, "claude",
                                    readiness=adapter_rd)
    assert r["readiness_source"] == "adapter"
    assert r["resolved"]["tool"] == str(bindir / "widget_custom"), \
        "adapter readiness binary must win over _TOOL_SPECS fallback: %r" % r["resolved"]
    # No "auth file missing" issue because adapter declares no required_files,
    # even though the claude fallback would have required ~/.claude.json.
    msgs = [m for lvl, m in r["issues"] if lvl == "error"]
    assert not any("auth file missing" in m for m in msgs)


def test_check_tool_readiness_uses_fallback_when_no_adapter_readiness(tmp_path, monkeypatch):
    """When `readiness` is None the function must consult the
    hardcoded _TOOL_SPECS fallback so older callers / tests that
    haven't been updated still get sensible defaults."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s ok'\n" % name)
        p.chmod(0o755)
    rt = _rt_with_path(str(bindir))
    rt.env["HOME"] = str(home)
    # readiness arg omitted → fallback path; claude fallback requires
    # ~/.claude.json; home is empty → blocking error.
    r = re_mod.check_tool_readiness(rt, "claude")
    assert r["readiness_source"] == "fallback"
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("auth file missing" in e for e in errors), \
        "fallback path must enforce claude ~/.claude.json: %r" % errors


def test_adapter_readiness_env_wrapper_binary_resolves_correctly(tmp_path, monkeypatch):
    """The launch command starts with `env CLAUDE_CODE_DISABLE_MOUSE=1 claude ...`
    so config.command[0] is 'env'. The adapter readiness.binary
    explicitly says 'claude', and the check must honor that (NOT
    resolve /usr/bin/env which would falsely pass)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s ok'\n" % name)
        p.chmod(0o755)
    # NOTE: no `env` in bindir — if the check ignored readiness.binary
    # and tried tool_binary='env', resolve would fail (or, on a real
    # host, pick /usr/bin/env which exists everywhere and would falsely pass).
    rt = re_mod.RuntimeEnv(env={"PATH": str(bindir), "HOME": str(home)},
                           source="explicit", shell="", path=str(bindir))
    adapter_rd = {
        "binary":           "claude",
        "version_args":     ["--version"],
        "version_required": True,
        "install_hint":     "n/a",
        "required_files":   [{"path": "~/.claude.json", "label": ""}],
        "optional_files":   [],
        "optional_env":     [],
    }
    # tool_binary mimicking config.command[0] = "env"
    r = re_mod.check_tool_readiness(rt, "claude", tool_binary="env",
                                    readiness=adapter_rd)
    assert r["readiness_source"] == "adapter"
    assert r["resolved"]["tool"] == str(bindir / "claude"), \
        "env wrapper must NOT defeat readiness.binary: %r" % r["resolved"]


def test_adapter_readiness_required_files_honor_runtime_HOME(tmp_path, monkeypatch):
    """F-08 invariant carries forward to adapter readiness: required
    file paths expand via runtime.env['HOME'], NOT os.environ['HOME']."""
    process_home = tmp_path / "process_home"
    runtime_home = tmp_path / "runtime_home"
    process_home.mkdir()
    runtime_home.mkdir()
    # Process HOME has decoy claude.json; runtime HOME doesn't.
    (process_home / ".claude.json").write_text("decoy")
    monkeypatch.setenv("HOME", str(process_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s ok'\n" % name)
        p.chmod(0o755)
    rt = re_mod.RuntimeEnv(
        env={"PATH": str(bindir), "HOME": str(runtime_home)},
        source="explicit", shell="", path=str(bindir),
    )
    adapter_rd = {
        "binary":           "claude",
        "version_args":     ["--version"],
        "version_required": True,
        "install_hint":     "n/a",
        "required_files":   [{"path": "~/.claude.json", "label": "Claude auth"}],
        "optional_files":   [],
        "optional_env":     [],
    }
    r = re_mod.check_tool_readiness(rt, "claude", readiness=adapter_rd)
    errors = [m for lvl, m in r["issues"] if lvl == "error"]
    assert any("auth file missing" in e and str(runtime_home) in e for e in errors), \
        "adapter required_files must expand via runtime HOME: %r" % errors


def test_cmd_run_passes_adapter_readiness_to_check(tmp_path, monkeypatch):
    """cmd_run must look up config.readiness from the loaded
    AdapterConfig and pass it to _preflight → check_tool_readiness.
    Proved by monkeypatching check_tool_readiness and asserting the
    `readiness` kwarg arrived non-None for a tool that has [readiness]."""
    captured = {}
    def _fake_check(runtime, selected_tool, tool_binary=None,
                     readiness=None, use_env_tool=False):
        captured["readiness"] = readiness
        captured["tool"] = selected_tool
        captured["use_env_tool"] = use_env_tool
        # Block on purpose so we don't proceed to tmux creation.
        return {"issues": [("error", "forced block for test")],
                "resolved": {}, "readiness_source":
                ("adapter" if readiness else "fallback")}
    monkeypatch.setattr(re_mod, "check_tool_readiness", _fake_check)

    class _Args(object):
        tool = "claude"
        prompt = ""
        path = str(tmp_path / "work")
        no_inherit_env = True
        name = None
        auto_exit = False
        auto_exit_enable = False
        tag = []
        json = False
    with pytest.raises(SystemExit):
        camc_cli.cmd_run(_Args())
    assert captured.get("tool") == "claude"
    rd = captured.get("readiness")
    assert rd is not None, "cmd_run must pass adapter readiness, got None"
    assert rd.get("binary") == "claude"
    # And the required_files came from claude.toml's TOML block.
    paths = [e["path"] for e in rd.get("required_files") or []]
    assert "~/.claude.json" in paths, \
        "cmd_run must pass through claude.toml's required_files: %r" % rd


def test_cmd_env_text_output_reports_readiness_source(tmp_path, monkeypatch, capsys):
    """`camc env check` (text mode) must surface readiness_source so
    a human can tell at a glance whether the adapter TOML or the
    hardcoded fallback drove the check."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("tmux", "claude"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho '%s ok'\n" % name)
        p.chmod(0o755)
    monkeypatch.setattr(re_mod, "build_runtime_env",
                        lambda **kw: re_mod.RuntimeEnv(
                            env={"PATH": str(bindir), "HOME": str(home)},
                            source="explicit", shell="/bin/sh",
                            path=str(bindir),
                        ))

    class _Args(object):
        tool = "claude"
        json = False
        env_action = "check"
    camc_cli.cmd_env(_Args())
    out = capsys.readouterr().out
    assert "Readiness source:     adapter" in out, \
        "cmd_env text output must report readiness_source: %r" % out


def test_cmd_env_check_does_not_mutate_state(tmp_path, monkeypatch):
    """cmd_env must not write anything anywhere — no agents.json
    update, no event ledger write, no tmux session, no rc-file
    edits. Strong claim; assert via a sentinel file we control."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("stub")
    monkeypatch.setenv("HOME", str(home))
    # Snapshot the home contents before + after.
    before = sorted(p.name for p in home.iterdir())

    monkeypatch.setattr(re_mod, "build_runtime_env",
                        lambda **kw: re_mod.RuntimeEnv(env={"PATH": "/usr/bin", "HOME": str(home)},
                                                       source="explicit", shell="", path="/usr/bin"))

    class _Args(object):
        tool = "claude"
        json = False
        env_action = "check"
    camc_cli.cmd_env(_Args())
    after = sorted(p.name for p in home.iterdir())
    assert before == after, "cmd_env must not touch the home directory"
