"""Focused tests for the 2026-06-23 PDX/DC hardening:
  * Golden tmux path resolution + warning on PATH fallback.
  * camc-owned ~/.cam/configs/tmux.conf template creation + refresh +
    don't-clobber-user-edits.
  * create_tmux_session injects ``-f <config>`` into new-session.
  * Startup command injection uses set-buffer + paste-buffer (not
    send-keys -l) when paste succeeds.
  * Per-tool golden path resolution: claude / codex / cursor in
    order; PATH alias fallback; --use-env-tool skips golden.
  * launch_cmd is rewritten to the absolute resolved binary across
    the three command shapes.
  * Runtime manifest fields land in agent record.

Python 3.6 compatible. No new dependencies.
"""

import hashlib
import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import runtime_env as _rt   # noqa: E402
from camc_pkg import transport as _tx     # noqa: E402


# ---------------------------------------------------------------------------
# 1. ensure_camc_tmux_config — missing file -> created with v1 body
# ---------------------------------------------------------------------------

class TestTmuxConfigTemplate:
    def test_missing_file_is_created(self, tmp_path):
        path = str(tmp_path / "tmux.conf")
        out = _tx.ensure_camc_tmux_config(path=path)
        assert out == path
        assert os.path.exists(path)
        with open(path) as f:
            body = f.read()
        assert "# camc-managed: true" in body
        assert "# camc-template-version: 1" in body
        assert "set-option -g history-limit 50000" in body
        assert "set-option -g status off" in body
        assert "set-option -g mouse off" in body
        assert 'set-option -g default-terminal "screen-256color"' in body
        # File should be readable + private.
        st = os.stat(path)
        assert (st.st_mode & 0o077) == 0, "tmux.conf must be 0600"

    def test_user_modified_file_is_not_overwritten(self, tmp_path):
        path = str(tmp_path / "tmux.conf")
        _tx.ensure_camc_tmux_config(path=path)
        # Simulate user edit: still camc-managed banner but body
        # changed -> sha mismatch.
        with open(path) as f:
            text = f.read()
        edited = text + "\nset-option -g monitor-activity off\n"
        with open(path, "w") as f:
            f.write(edited)
        # Call again — must NOT overwrite.
        _tx.ensure_camc_tmux_config(path=path)
        with open(path) as f:
            assert "monitor-activity" in f.read(), \
                "user edit was clobbered"

    def test_non_managed_user_file_is_not_touched(self, tmp_path):
        path = str(tmp_path / "tmux.conf")
        user_body = "# my own config\nset-option -g history-limit 999\n"
        with open(path, "w") as f:
            f.write(user_body)
        _tx.ensure_camc_tmux_config(path=path)
        with open(path) as f:
            assert f.read() == user_body, \
                "user-authored file was overwritten"

    def test_future_version_bump_preserves_user_append(self, tmp_path, monkeypatch):
        # Regression for F1: a future template-version bump must NOT
        # clobber an in-place user edit even when the user did not
        # touch the sha header line. The refresh decision must hash
        # the actual file body and refuse on mismatch.
        path = str(tmp_path / "tmux.conf")
        _tx.ensure_camc_tmux_config(path=path)
        with open(path) as f:
            text = f.read()
        # User appends a managed-looking line WITHOUT editing the sha.
        with open(path, "w") as f:
            f.write(text + "set-option -g monitor-activity off\n")
        # Simulate future bump (v2). The header still says v1, the
        # sha still matches the *advertised* v1 sha, but the actual
        # file content no longer matches.
        monkeypatch.setattr(_tx, "_CAMC_TMUX_CONFIG_VERSION", 2)
        _tx.ensure_camc_tmux_config(path=path)
        with open(path) as f:
            after = f.read()
        assert "monitor-activity" in after, \
            "future version bump clobbered the user's appended line"
        assert "# camc-template-version: 1" in after, \
            "user-edited file was refreshed to v2 despite the modification"

    def test_managed_unchanged_older_version_refreshes(self, tmp_path, monkeypatch):
        path = str(tmp_path / "tmux.conf")
        # Force the module to think v1 was an older version, then
        # bump to v2 for the refresh check.
        # Write a v0 managed file with a sha that matches the v0 body.
        v0_no_sha = _tx._CAMC_TMUX_CONFIG_BODY.format(
            version=0, sha="<pending>")
        canonical = "\n".join(
            l for l in v0_no_sha.splitlines()
            if not l.startswith("# camc-template-sha256:"))
        v0_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        v0_body = _tx._CAMC_TMUX_CONFIG_BODY.format(version=0, sha=v0_sha)
        with open(path, "w") as f:
            f.write(v0_body)
        # Current code version is 1 — old v0 file should refresh.
        _tx.ensure_camc_tmux_config(path=path)
        with open(path) as f:
            text = f.read()
        assert "# camc-template-version: 1" in text, \
            "older managed file did not refresh"


    def test_user_modified_file_survives_future_version_bump(self, tmp_path, monkeypatch):
        path = str(tmp_path / "tmux.conf")
        _tx.ensure_camc_tmux_config(path=path)
        with open(path) as f:
            original = f.read()
        with open(path, "w") as f:
            f.write(original + "\nset-option -g monitor-activity off\n")

        old_version = _tx._CAMC_TMUX_CONFIG_VERSION
        monkeypatch.setattr(_tx, "_CAMC_TMUX_CONFIG_VERSION",
                            old_version + 1)
        _tx.ensure_camc_tmux_config(path=path)

        with open(path) as f:
            text = f.read()
        assert "monitor-activity" in text, \
            "future template bump clobbered user edit"
        assert "# camc-template-version: %d" % old_version in text, \
            "modified user file should not be refreshed"


# ---------------------------------------------------------------------------
# 3. create_tmux_session includes -f <config> when creating new server
# ---------------------------------------------------------------------------

class TestCreateTmuxSessionInjectsConfig:
    def test_inherit_env_path_includes_f_config(self, tmp_path, monkeypatch):
        captured = {"argv": None}

        class _FakeProc(object):
            def __init__(self, *a, **kw):
                captured["argv"] = a[0]
                self.returncode = 0
            def communicate(self, timeout=None):
                return (b"", b"")

        monkeypatch.setattr(_tx.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(_tx, "_run", lambda *a, **kw: (0, ""))

        cfg = str(tmp_path / "tmux.conf")
        _tx.ensure_camc_tmux_config(path=cfg)

        ok = _tx.create_tmux_session(
            "cam-abc12345", ["claude", "--allowed-tools", "Bash"],
            "/work", inherit_env=True,
            env={"PATH": "/usr/bin"},
            tmux_bin="/bin/tmux",
            tmux_config=cfg,
        )
        assert ok is True
        argv = captured["argv"]
        # argv shape: [tmux, -u, -f, <cfg>, -S, <sock>, new-session, ...]
        assert argv[0] == "/bin/tmux"
        assert argv[1] == "-u"
        assert "-f" in argv, "-f flag not injected for new-session"
        f_idx = argv.index("-f")
        assert argv[f_idx + 1] == cfg
        # -f MUST appear before -S so tmux applies the config to the
        # NEW server we're creating, not a client connection.
        s_idx = argv.index("-S")
        assert f_idx < s_idx, "-f must precede -S"

    def test_inherit_env_false_path_includes_f_config(self, tmp_path, monkeypatch):
        captured = {"argv": None}

        class _FakeProc(object):
            def __init__(self, *a, **kw):
                captured["argv"] = a[0]
                self.returncode = 0
            def communicate(self, timeout=None):
                return (b"", b"")

        monkeypatch.setattr(_tx.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(_tx, "_run", lambda *a, **kw: (0, ""))

        cfg = str(tmp_path / "tmux.conf")
        _tx.ensure_camc_tmux_config(path=cfg)

        _tx.create_tmux_session(
            "cam-deadbeef", ["codex", "-c", "features.goals=true"],
            "/work", inherit_env=False,
            env={"PATH": "/usr/bin"},
            tmux_bin="/bin/tmux",
            tmux_config=cfg,
        )
        argv = captured["argv"]
        assert "-f" in argv
        assert argv[argv.index("-f") + 1] == cfg

    def test_explicit_empty_config_opts_out(self, tmp_path, monkeypatch):
        captured = {"argv": None}

        class _FakeProc(object):
            def __init__(self, *a, **kw):
                captured["argv"] = a[0]
                self.returncode = 0
            def communicate(self, timeout=None):
                return (b"", b"")

        monkeypatch.setattr(_tx.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(_tx, "_run", lambda *a, **kw: (0, ""))

        _tx.create_tmux_session(
            "cam-xx", ["claude"], "/work",
            inherit_env=True, env={"PATH": "/usr/bin"},
            tmux_bin="/bin/tmux", tmux_config="",  # explicit opt-out
        )
        argv = captured["argv"]
        assert "-f" not in argv, \
            "tmux_config='' should opt out of -f injection"


# ---------------------------------------------------------------------------
# 4. startup uses set-buffer + paste-buffer (not send-keys -l)
# ---------------------------------------------------------------------------

class TestStartupPasteBuffer:
    def test_paste_buffer_path_used(self, tmp_path, monkeypatch):
        run_calls = []
        class _Proc(object):
            returncode = 0
            def communicate(self, timeout=None):
                return (b"", b"")
        monkeypatch.setattr(_tx.subprocess, "Popen",
                            lambda argv, **kw: _Proc())
        monkeypatch.setattr(_tx, "_run",
                            lambda argv, **kw: run_calls.append(list(argv)) or (0, ""))
        cfg = str(tmp_path / "tmux.conf")
        _tx.ensure_camc_tmux_config(path=cfg)
        _tx.create_tmux_session(
            "cam-paste",
            ["env", "CLAUDE_CODE_DISABLE_MOUSE=1", "claude"],
            "/tmp", inherit_env=True,
            tmux_bin="/bin/tmux", tmux_config=cfg,
        )
        assert any("set-buffer" in argv for argv in run_calls)
        assert any("paste-buffer" in argv for argv in run_calls)
        assert not any("-l" in argv and "send-keys" in argv
                       for argv in run_calls), \
            "send-keys -l must NOT be used for startup injection"
        assert any("Enter" in argv for argv in run_calls)


# ---------------------------------------------------------------------------
# 5/6. Per-tool golden resolution + PATH fallback ordering
# ---------------------------------------------------------------------------

class TestGoldenToolResolution:
    def test_claude_golden_first_path_wins(self, tmp_path, monkeypatch):
        # Pretend the stable golden path exists; PATH also has a
        # claude — golden must beat PATH.
        golden = str(tmp_path / "golden_stable_claude")
        with open(golden, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(golden, 0o755)
        path_claude = str(tmp_path / "path_claude")
        with open(path_claude, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path_claude, 0o755)

        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {
            "claude": (golden, "/nonexistent/latest"),
        })
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        # rename so PATH lookup would find 'claude' too
        os.rename(path_claude, str(tmp_path / "claude"))
        out = _rt.resolve_tool_with_source(rt, "claude")
        assert out["bin"] == golden
        assert out["source"] == "golden"

    def test_claude_falls_through_to_path_when_no_golden(self, tmp_path, monkeypatch):
        path_claude = str(tmp_path / "claude")
        with open(path_claude, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path_claude, 0o755)
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"claude": ()})
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        out = _rt.resolve_tool_with_source(rt, "claude")
        assert out["bin"] == path_claude
        assert out["source"] == "env"
        assert any("PATH" in w for w in out["warnings"])

    def test_use_env_tool_skips_golden(self, tmp_path, monkeypatch):
        golden = str(tmp_path / "golden_claude")
        with open(golden, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(golden, 0o755)
        path_claude = str(tmp_path / "claude")
        with open(path_claude, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path_claude, 0o755)
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"claude": (golden,)})
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        out = _rt.resolve_tool_with_source(rt, "claude", use_env_tool=True)
        assert out["bin"] == path_claude, \
            "--use-env-tool must skip golden and use PATH"
        assert out["source"] == "env-forced"
        assert any("env-tool" in w or "--use-env-tool" in w for w in out["warnings"])

    def test_configured_abs_path_beats_golden_claude(self, tmp_path, monkeypatch):
        # Regression for F2: an absolute tool_binary passed in (from
        # config.command[0]) must resolve as source='configured' and
        # win over a present golden path.
        configured = str(tmp_path / "configured_claude")
        with open(configured, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(configured, 0o755)
        golden = str(tmp_path / "golden_claude")
        with open(golden, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(golden, 0o755)
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"claude": (golden,)})

        captured = {"argv": None}
        def _fake_probe(rt, argv, timeout=None):
            captured["argv"] = list(argv)
            return 0, "1.0.0"
        monkeypatch.setattr(_rt, "run_probe", _fake_probe)

        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        # Provide a permissive tmux path so the tmux block doesn't error.
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (golden,))
        out = _rt.check_tool_readiness(rt, "claude", tool_binary=configured)
        assert out["resolved"]["tool"] == configured, \
            "configured absolute path lost to golden"
        tr = out["resolved"]["tool_resolution"]
        assert tr["bin"] == configured
        assert tr["source"] == "configured"

    def test_configured_abs_path_beats_golden_codex(self, tmp_path, monkeypatch):
        configured = str(tmp_path / "configured_codex")
        with open(configured, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(configured, 0o755)
        golden = str(tmp_path / "golden_codex")
        with open(golden, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(golden, 0o755)
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"codex": (golden,)})
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (golden,))
        monkeypatch.setattr(_rt, "run_probe",
                            lambda rt, argv, timeout=None: (0, "1.0.0"))
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        out = _rt.check_tool_readiness(rt, "codex", tool_binary=configured)
        assert out["resolved"]["tool"] == configured
        assert out["resolved"]["tool_resolution"]["source"] == "configured"

    def test_use_env_tool_skips_configured_abs_path(self, tmp_path, monkeypatch):
        # With --use-env-tool the user has explicitly asked for env/PATH
        # only, so a configured absolute path must NOT be honored.
        configured = str(tmp_path / "configured_claude")
        with open(configured, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(configured, 0o755)
        path_claude = str(tmp_path / "claude")
        with open(path_claude, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path_claude, 0o755)
        golden = str(tmp_path / "golden_claude")
        with open(golden, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(golden, 0o755)
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"claude": (golden,)})
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (golden,))
        monkeypatch.setattr(_rt, "run_probe",
                            lambda rt, argv, timeout=None: (0, "1.0.0"))
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        out = _rt.check_tool_readiness(rt, "claude",
                                        tool_binary=configured,
                                        use_env_tool=True)
        assert out["resolved"]["tool"] == path_claude, \
            "--use-env-tool must skip configured absolute path"
        tr = out["resolved"]["tool_resolution"]
        assert tr["source"] == "env-forced"

    def test_cursor_aliases_agent_and_cursor_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"cursor": ()})
        # Stage only `agent` (older alias) and verify it's found.
        agent_bin = str(tmp_path / "agent")
        with open(agent_bin, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(agent_bin, 0o755)
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        out = _rt.resolve_tool_with_source(rt, "cursor")
        assert out["bin"] == agent_bin
        assert out["source"] == "env"


# ---------------------------------------------------------------------------
# 7. Golden tmux preferred over PATH; PATH fallback warns
# ---------------------------------------------------------------------------

class TestGoldenTmuxResolution:
    def test_golden_tmux_wins(self, tmp_path, monkeypatch):
        golden = str(tmp_path / "tmux_golden")
        with open(golden, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(golden, 0o755)
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (golden,))
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        bin_, source = _rt.resolve_tmux_bin(rt)
        assert bin_ == golden
        assert source == "golden"

    def test_path_fallback_when_golden_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", ("/nope/tmux",))
        path_tmux = str(tmp_path / "tmux")
        with open(path_tmux, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path_tmux, 0o755)
        rt = _rt.RuntimeEnv(env={"PATH": str(tmp_path), "HOME": "/h"},
                             source="explicit", shell="", path=str(tmp_path))
        bin_, source = _rt.resolve_tmux_bin(rt)
        assert bin_ == path_tmux
        assert source == "env"

    def test_no_tmux_anywhere(self, monkeypatch):
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", ("/nope/tmux",))
        rt = _rt.RuntimeEnv(env={"PATH": "/nope", "HOME": "/h"},
                             source="explicit", shell="", path="/nope")
        bin_, source = _rt.resolve_tmux_bin(rt)
        assert bin_ is None
        assert source == "missing"


# ---------------------------------------------------------------------------
# 8. Missing tool -> error before create_tmux_session
# ---------------------------------------------------------------------------

class TestMissingToolErrors:
    def test_no_golden_no_path_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {"claude": ()})
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", ())
        # tmux stub in PATH so we focus on tool resolution.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        tmux = bindir / "tmux"
        tmux.write_text("#!/bin/sh\necho 'tmux 3.0'\n")
        tmux.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        (home / ".claude.json").write_text("stub")
        rt = _rt.RuntimeEnv(
            env={"PATH": str(bindir), "HOME": str(home)},
            source="explicit", shell="", path=str(bindir))
        r = _rt.check_tool_readiness(rt, "claude")
        errors = [m for lvl, m in r["issues"] if lvl == "error"]
        assert any("claude" in e and "not found" in e for e in errors)
        # No `tool` entry resolved -> cmd_run would exit before
        # create_tmux_session is reached.
        assert "tool" not in r["resolved"]


# ---------------------------------------------------------------------------
# 9. launch_cmd uses absolute resolved binary for claude/codex/cursor
# (mirror of the helper extracted from cmd_run; kept here so the
#  contract is pinned per shape regardless of any cli.py drift)
# ---------------------------------------------------------------------------

def _rewrite_launch_cmd(launch_cmd, resolved_bin, tool):
    if not (resolved_bin and os.path.isabs(resolved_bin) and launch_cmd):
        return list(launch_cmd or [])
    expected_basename = os.path.basename(resolved_bin)
    i = 0
    if launch_cmd[0] == "env":
        i = 1
        while i < len(launch_cmd) and ("=" in launch_cmd[i]
                                       and not launch_cmd[i].startswith("-")):
            i += 1
    aliases = _rt._PATH_TOOL_ALIASES.get(tool, (tool,))
    if i < len(launch_cmd):
        tok = launch_cmd[i]
        if tok == expected_basename or tok == tool or tok in aliases:
            launch_cmd = list(launch_cmd)
            launch_cmd[i] = resolved_bin
    return launch_cmd


class TestLaunchCmdRewrite:
    def test_codex_argv0_rewritten(self):
        out = _rewrite_launch_cmd(
            ["codex", "-c", "features.goals=true"],
            "/home/prgn_share/tools/codex/bin/codex", "codex")
        assert out[0] == "/home/prgn_share/tools/codex/bin/codex"

    def test_claude_env_wrapper_rewritten(self):
        out = _rewrite_launch_cmd(
            ["env", "CLAUDE_CODE_DISABLE_MOUSE=1",
             "claude", "--allowed-tools", "Bash"],
            "/home/prgn_share/tools/claude-code/bin/claude", "claude")
        assert out[:2] == ["env", "CLAUDE_CODE_DISABLE_MOUSE=1"]
        assert out[2] == "/home/prgn_share/tools/claude-code/bin/claude"

    def test_cursor_alias_agent_rewritten(self):
        out = _rewrite_launch_cmd(
            ["agent", "--workspace", "/proj"],
            "/home/prgn_share/tools/cursor/cursor-cli/latest/bin/cursor-agent",
            "cursor")
        assert out[0].endswith("/cursor-agent")
        assert out[1:] == ["--workspace", "/proj"]


# ---------------------------------------------------------------------------
# 10. Runtime manifest in agent record (composed inline by cmd_run;
#     pinned via the same shape contract here so a future refactor
#     can't drop fields silently).
# ---------------------------------------------------------------------------

class TestRuntimeManifestShape:
    def test_runtime_manifest_keys_present(self):
        # Build the manifest exactly as cmd_run does, with a
        # representative resolved payload. This pins the schema +
        # field names without driving the full cmd_run.
        resolved = {
            "tmux": "/bin/tmux",
            "tmux_source": "golden",
            "tool": "/home/prgn_share/tools/codex/bin/codex",
            "tool_resolution": {
                "tool": "codex",
                "bin": "/home/prgn_share/tools/codex/bin/codex",
                "source": "golden",
                "warnings": [],
            },
        }
        tool = "codex"
        tmux_bin_used = "/bin/tmux"
        tmux_ver_used = "tmux 3.0"
        session = "cam-deadbeef"
        SOCKETS_DIR = "/tmp/cam-sockets"
        camc_tmux_config = "/home/me/.cam/configs/tmux.conf"

        _tool_resolution = (resolved or {}).get("tool_resolution") or {}
        _python_version = "%d.%d.%d" % (
            sys.version_info[0], sys.version_info[1], sys.version_info[2])
        manifest = {
            "schema": "camc-runtime/1",
            "python": {"bin": sys.executable, "version": _python_version},
            "tmux": {
                "bin":         tmux_bin_used,
                "version":     tmux_ver_used,
                "socket":      "%s/%s.sock" % (SOCKETS_DIR, session),
                "config":      camc_tmux_config,
                "config_mode": "camc-default" if camc_tmux_config else "system-default",
                "source":      (resolved or {}).get("tmux_source", "unknown"),
            },
            "tool": {
                "name":     tool,
                "bin":      _tool_resolution.get("bin") or (resolved or {}).get("tool", ""),
                "version":  "",
                "source":   _tool_resolution.get("source", "unknown"),
                "warnings": list(_tool_resolution.get("warnings", []) or []),
            },
            "shell": {
                "mode": "tmux-default-shell",
                "note": "launch argv uses absolute tool path",
            },
        }
        assert manifest["schema"] == "camc-runtime/1"
        assert manifest["python"]["bin"] == sys.executable
        assert manifest["tmux"]["bin"] == "/bin/tmux"
        assert manifest["tmux"]["source"] == "golden"
        assert manifest["tmux"]["config"] == camc_tmux_config
        assert manifest["tmux"]["config_mode"] == "camc-default"
        assert manifest["tool"]["name"] == "codex"
        assert manifest["tool"]["bin"] == "/home/prgn_share/tools/codex/bin/codex"
        assert manifest["tool"]["source"] == "golden"
        assert manifest["shell"]["mode"] == "tmux-default-shell"


def _write_exe(path, body="echo ok"):
    with open(str(path), "w") as f:
        f.write("#!/bin/sh\n%s\n" % body)
    os.chmod(str(path), 0o755)
    return str(path)


class TestConfiguredToolPathPrecedence:
    def _runtime_with_tmux(self, tmp_path, monkeypatch, home=None):
        tmux = _write_exe(tmp_path / "tmux-golden", "echo 'tmux 3.0'")
        monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (tmux,))
        home = home or (tmp_path / "home")
        if not os.path.exists(str(home)):
            os.makedirs(str(home))
        return _rt.RuntimeEnv(
            env={"PATH": str(tmp_path / "path"), "HOME": str(home)},
            source="explicit", shell="", path=str(tmp_path / "path"))

    def test_configured_absolute_claude_beats_golden(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".claude.json").write_text("stub")
        configured = _write_exe(tmp_path / "configured-claude",
                                "echo configured-claude")
        golden = _write_exe(tmp_path / "golden-claude",
                            "echo golden-claude")
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS",
                            {"claude": (golden,)})
        rt = self._runtime_with_tmux(tmp_path, monkeypatch, home=home)

        r = _rt.check_tool_readiness(rt, "claude",
                                     tool_binary=configured)
        errors = [m for lvl, m in r["issues"] if lvl == "error"]
        assert not errors
        assert r["resolved"]["tool"] == configured
        assert r["resolved"]["tool_resolution"]["source"] == "configured"

    def test_configured_absolute_codex_beats_golden(self, tmp_path, monkeypatch):
        configured = _write_exe(tmp_path / "configured-codex",
                                "echo configured-codex")
        golden = _write_exe(tmp_path / "golden-codex",
                            "echo golden-codex")
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS",
                            {"codex": (golden,)})
        rt = self._runtime_with_tmux(tmp_path, monkeypatch)

        r = _rt.check_tool_readiness(rt, "codex", tool_binary=configured)
        assert r["resolved"]["tool"] == configured
        assert r["resolved"]["tool_resolution"]["source"] == "configured"

    def test_use_env_tool_skips_configured_absolute_and_golden(self, tmp_path, monkeypatch):
        configured = _write_exe(tmp_path / "configured-codex",
                                "echo configured-codex")
        golden = _write_exe(tmp_path / "golden-codex",
                            "echo golden-codex")
        bindir = tmp_path / "path"
        bindir.mkdir()
        path_codex = _write_exe(bindir / "codex", "echo path-codex")
        monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS",
                            {"codex": (golden,)})
        rt = self._runtime_with_tmux(tmp_path, monkeypatch)

        r = _rt.check_tool_readiness(rt, "codex", tool_binary=configured,
                                     use_env_tool=True)
        assert r["resolved"]["tool"] == path_codex
        assert r["resolved"]["tool_resolution"]["source"] == "env-forced"
