# CAMC Explicit Tool Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Add a highest-precedence --tool-dir override that selects the requested tool binary from one explicit absolute directory while preserving all existing fallback behavior.

**Architecture:** runtime_env.py owns direct-directory candidate resolution and provenance. cli.py validates the option, threads it through env diagnostics and run preflight, rewrites launch argv using the resolved absolute binary, and records the requested directory in the runtime manifest only when supplied.

**Tech Stack:** Python 3.6-compatible standard library, argparse, pytest.

## Global Constraints

- -t/--tool continues to select claude, codex, cursor, or another configured adapter.
- --tool-dir accepts a direct executable directory only; there is no recursion and no implicit nested bin lookup.
- The value expands ~, normalizes, and must then be absolute.
- A usable --tool-dir wins over configured executables, golden paths, and runtime PATH.
- --tool-dir also wins with --use-env-tool; an unusable override then falls back to PATH only in that mode.
- An unusable absolute directory warns and continues through the applicable existing fallback flow.
- Omitting --tool-dir preserves existing resolution and output behavior.
- Do not deploy, push, tag, or commit unless the user separately authorizes it.

---

### Task 1: Resolve the selected tool directly inside --tool-dir

**Files:**
- Modify: src/camc_pkg/runtime_env.py
- Modify: tests/test_camc_hardening_pdx.py

**Interfaces:**
- Consumes: selected_tool, tool_dir, configured binary name, and _PATH_TOOL_ALIASES.
- Produces: resolve_tool_dir(tool_dir, selected_tool, configured_binary=None) -> dict with tool, bin, source, warnings, and tool_dir; check_tool_readiness gains tool_dir=None.

- [ ] **Step 1: Write highest-precedence tests for Claude and Codex**

~~~python
@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_tool_dir_beats_configured_golden_and_path(
        self, tmp_path, monkeypatch, tool):
    direct_dir = tmp_path / "direct"
    direct_dir.mkdir()
    direct = _write_exe(direct_dir / tool, "echo direct")
    configured = _write_exe(tmp_path / ("configured-" + tool),
                            "echo configured")
    golden = _write_exe(tmp_path / ("golden-" + tool), "echo golden")
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    _write_exe(path_dir / tool, "echo path")
    tmux = _write_exe(tmp_path / "tmux", "echo 'tmux 3.0'")

    monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS", {tool: (golden,)})
    monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (tmux,))
    monkeypatch.setattr(_rt, "run_probe",
                        lambda runtime, argv, timeout=None: (0, "ok"))
    runtime = _rt.RuntimeEnv(
        env={"PATH": str(path_dir), "HOME": str(tmp_path)},
        source="explicit", shell="", path=str(path_dir))

    result = _rt.check_tool_readiness(
        runtime, tool, tool_binary=configured, tool_dir=str(direct_dir))

    resolution = result["resolved"]["tool_resolution"]
    assert result["resolved"]["tool"] == direct
    assert resolution["source"] == "tool-dir"
    assert resolution["tool_dir"] == str(direct_dir)
~~~

- [ ] **Step 2: Run the precedence tests and verify RED**

Run: python3 -m pytest tests/test_camc_hardening_pdx.py -k tool_dir_beats -q

Expected: FAIL because check_tool_readiness has no tool_dir parameter.

- [ ] **Step 3: Implement deterministic direct-directory candidate resolution**

~~~python
def resolve_tool_dir(tool_dir, selected_tool, configured_binary=None):
    result = {
        "tool": selected_tool,
        "bin": None,
        "source": "missing",
        "warnings": [],
        "tool_dir": "",
    }
    if not tool_dir:
        return result

    expanded = os.path.normpath(os.path.expanduser(tool_dir))
    result["tool_dir"] = expanded
    if not os.path.isabs(expanded):
        result["warnings"].append(
            "--tool-dir must be absolute: %s" % tool_dir)
        return result
    if not os.path.isdir(expanded):
        result["warnings"].append(
            "--tool-dir is not a directory; falling back: %s" % expanded)
        return result

    candidates = []
    if configured_binary and configured_binary not in ("env", "/usr/bin/env"):
        candidates.append(os.path.basename(configured_binary))
    candidates.extend(_PATH_TOOL_ALIASES.get(
        selected_tool, (selected_tool,)))

    seen = set()
    non_executable = []
    for basename in candidates:
        if not basename or basename in seen:
            continue
        seen.add(basename)
        candidate = os.path.join(expanded, basename)
        if _is_executable_file(candidate):
            result.update({
                "bin": candidate,
                "source": "tool-dir",
                "warnings": [],
            })
            return result
        if os.path.exists(candidate):
            non_executable.append(candidate)

    if non_executable:
        result["warnings"].append(
            "--tool-dir matches are not executable; falling back: %s"
            % ", ".join(non_executable))
    else:
        result["warnings"].append(
            "tool %r not found directly in --tool-dir %s; aliases tried: %s"
            % (selected_tool, expanded, ", ".join(sorted(seen))))
    return result
~~~

Call this after bin_name is derived and before configured/golden/PATH resolution. On success, skip every lower source. On failure, preserve its warnings in both issues and the final tool_resolution while continuing.

- [ ] **Step 4: Write Cursor alias and non-recursion tests**

~~~python
@pytest.mark.parametrize("binary_name", ["cursor-agent", "agent"])
def test_tool_dir_accepts_cursor_aliases(tmp_path, binary_name):
    direct_dir = tmp_path / "direct"
    direct_dir.mkdir()
    binary = _write_exe(direct_dir / binary_name)
    result = _rt.resolve_tool_dir(
        str(direct_dir), "cursor", configured_binary="cursor-agent")
    assert result["bin"] == binary
    assert result["source"] == "tool-dir"

def test_tool_dir_does_not_search_nested_bin(tmp_path):
    direct_dir = tmp_path / "direct"
    nested = direct_dir / "bin"
    nested.mkdir(parents=True)
    _write_exe(nested / "codex")
    result = _rt.resolve_tool_dir(str(direct_dir), "codex", "codex")
    assert result["bin"] is None
    assert "not found directly" in result["warnings"][0]
~~~

- [ ] **Step 5: Write unusable-override fallback tests**

~~~python
def runtime_with_tmux(tmp_path, monkeypatch, path=None):
    tmux = _write_exe(tmp_path / "tmux", "echo 'tmux 3.0'")
    monkeypatch.setattr(_rt, "_GOLDEN_TMUX_PATHS", (tmux,))
    monkeypatch.setattr(_rt, "run_probe",
                        lambda runtime, argv, timeout=None: (0, "ok"))
    path_dir = pathlib.Path(path) if path else tmp_path / "empty-path"
    path_dir.mkdir(exist_ok=True)
    runtime = _rt.RuntimeEnv(
        env={"PATH": str(path_dir), "HOME": str(tmp_path)},
        source="explicit", shell="", path=str(path_dir))
    return runtime, tmux

def test_missing_tool_dir_falls_back_to_configured(tmp_path, monkeypatch):
    configured = _write_exe(tmp_path / "configured-codex")
    runtime, tmux = runtime_with_tmux(tmp_path, monkeypatch)
    result = _rt.check_tool_readiness(
        runtime, "codex", tool_binary=configured,
        tool_dir=str(tmp_path / "missing"))
    resolution = result["resolved"]["tool_resolution"]
    assert result["resolved"]["tool"] == configured
    assert resolution["source"] == "configured"
    assert resolution["tool_dir"] == str(tmp_path / "missing")
    assert any("--tool-dir" in item for item in resolution["warnings"])

def test_missing_tool_dir_with_use_env_falls_back_to_path_only(
        tmp_path, monkeypatch):
    configured = _write_exe(tmp_path / "configured-codex")
    golden = _write_exe(tmp_path / "golden-codex")
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    path_binary = _write_exe(path_dir / "codex")
    runtime, tmux = runtime_with_tmux(
        tmp_path, monkeypatch, path=str(path_dir))
    monkeypatch.setattr(_rt, "_GOLDEN_TOOL_PATHS",
                        {"codex": (golden,)})

    result = _rt.check_tool_readiness(
        runtime, "codex", tool_binary=configured,
        tool_dir=str(tmp_path / "missing"), use_env_tool=True)

    assert result["resolved"]["tool"] == path_binary
    assert result["resolved"]["tool_resolution"]["source"] == "env-forced"
    assert any("--tool-dir" in warning for warning
               in result["resolved"]["tool_resolution"]["warnings"])
~~~

Add these explicit invalid-directory cases:

~~~python
def test_tool_dir_regular_file_warns_and_falls_back(tmp_path):
    regular = tmp_path / "not-a-directory"
    regular.write_text("x")
    result = _rt.resolve_tool_dir(str(regular), "codex", "codex")
    assert result["bin"] is None
    assert "not a directory" in result["warnings"][0]

def test_tool_dir_non_executable_match_warns_and_falls_back(tmp_path):
    direct = tmp_path / "direct"
    direct.mkdir()
    candidate = direct / "codex"
    candidate.write_text("#!/bin/sh\n")
    candidate.chmod(0o644)
    result = _rt.resolve_tool_dir(str(direct), "codex", "codex")
    assert result["bin"] is None
    assert "not executable" in result["warnings"][0]
~~~

- [ ] **Step 6: Run resolver tests and verify GREEN**

Run: python3 -m pytest tests/test_camc_hardening_pdx.py -k "tool_dir or ToolPathPrecedence" -q

Expected: PASS.

- [ ] **Step 7: Review checkpoint**

Run: python3 -m py_compile src/camc_pkg/runtime_env.py && git diff --check

Expected: successful compilation and no whitespace errors.

---

### Task 2: Validate and thread --tool-dir through CLI diagnostics and run preflight

**Files:**
- Modify: src/camc_pkg/cli.py
- Modify: tests/test_runtime_env.py
- Test: tests/test_camc_hardening_pdx.py

**Interfaces:**
- Consumes: argparse values from camc run and camc env check.
- Produces: _absolute_tool_dir(value) -> normalized absolute string; _preflight gains tool_dir=None; cmd_env and cmd_run pass tool_dir into check_tool_readiness.

- [ ] **Step 1: Write CLI help and relative-path validation tests**

~~~python
def package_env():
    env = dict(os.environ)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = SRC + (os.pathsep + current if current else "")
    return env

@pytest.mark.parametrize("command", [
    ["run", "--help"],
    ["env", "check", "--help"],
])
def test_tool_dir_is_documented(command):
    result = subprocess.run(
        [sys.executable, "-m", "camc_pkg"] + command,
        cwd=ROOT, env=package_env(), text=True, capture_output=True)
    assert result.returncode == 0
    assert "--tool-dir" in result.stdout

@pytest.mark.parametrize("command", [
    ["run", "--tool-dir", "relative", "prompt"],
    ["env", "check", "--tool-dir", "relative"],
])
def test_relative_tool_dir_is_cli_usage_error(command):
    result = subprocess.run(
        [sys.executable, "-m", "camc_pkg"] + command,
        cwd=ROOT, env=package_env(), text=True, capture_output=True)
    assert result.returncode == 2
    assert "--tool-dir must be an absolute path" in result.stderr
~~~

- [ ] **Step 2: Run CLI validation tests and verify RED**

Run: python3 -m pytest tests/test_runtime_env.py -k "tool_dir_is_documented or relative_tool_dir" -q

Expected: FAIL because the option is not registered.

- [ ] **Step 3: Add the argparse type and both options**

~~~python
def _absolute_tool_dir(value):
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise argparse.ArgumentTypeError(
            "--tool-dir must be an absolute path")
    return os.path.normpath(expanded)
~~~

Register this on both parsers:

~~~python
env_check.add_argument(
    "--tool-dir", dest="tool_dir", type=_absolute_tool_dir,
    default=None, metavar="ABSOLUTE_DIR",
    help="Look for the selected tool directly in this absolute directory first")
r.add_argument(
    "--tool-dir", dest="tool_dir", type=_absolute_tool_dir,
    default=None, metavar="ABSOLUTE_DIR",
    help="Look for the selected tool directly in this absolute directory first")
~~~

- [ ] **Step 4: Write plumbing tests**

~~~python
def test_cmd_env_passes_tool_dir_to_readiness(monkeypatch, capsys, tmp_path):
    captured = {}

    def fake_runtime(**kwargs):
        return re_mod.RuntimeEnv(
            env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            source="explicit", shell="/bin/sh", path="/usr/bin")

    def fake_check(runtime, selected_tool, tool_binary=None,
                   readiness=None, use_env_tool=False, tool_dir=None):
        captured["tool_dir"] = tool_dir
        return {
            "issues": [],
            "resolved": {
                "tmux": "/bin/tmux",
                "tool": str(tmp_path / "codex"),
                "tool_resolution": {
                    "tool": "codex", "bin": str(tmp_path / "codex"),
                    "source": "tool-dir", "warnings": [],
                    "tool_dir": str(tmp_path),
                },
            },
            "readiness_source": "adapter",
        }

    monkeypatch.setattr(re_mod, "check_tool_readiness", fake_check)
    monkeypatch.setattr(re_mod, "build_runtime_env", fake_runtime)

    class Args(object):
        tool = "codex"
        tool_dir = str(tmp_path)
        json = True
        env_action = "check"

    camc_cli.cmd_env(Args())
    assert captured["tool_dir"] == str(tmp_path)
    body = json.loads(capsys.readouterr().out)
    assert body["resolved"]["tool_resolution"]["source"] == "tool-dir"

def test_cmd_run_passes_tool_dir_to_preflight(
        monkeypatch, tmp_path):
    captured = {}

    def fake_preflight(tool, tool_binary, workdir, env_setup=None,
                       runtime=None, adapter_readiness=None,
                       use_env_tool=False, tool_dir=None):
        captured["tool_dir"] = tool_dir
        return [("error", "stop")], {}

    monkeypatch.setattr(camc_cli, "_preflight", fake_preflight)

    class Args(object):
        tool = "codex"
        tool_dir = str(tmp_path)
        prompt = ""
        path = str(tmp_path / "work")
        no_inherit_env = True
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
        camc_cli.cmd_run(Args())
    assert captured["tool_dir"] == str(tmp_path)
~~~

- [ ] **Step 5: Thread the option through existing signatures**

~~~python
def _preflight(tool, tool_binary, workdir, env_setup=None, runtime=None,
               adapter_readiness=None, use_env_tool=False, tool_dir=None):
    if runtime is None:
        runtime = build_runtime_env(env_setup=env_setup)
    readiness = check_tool_readiness(
        runtime, tool, tool_binary,
        readiness=adapter_readiness,
        use_env_tool=use_env_tool,
        tool_dir=tool_dir)
    # Preserve the existing workdir and writable-directory checks.
    return issues, readiness.get("resolved", {})

tool_dir = getattr(args, "tool_dir", None)
~~~

cmd_env passes tool_dir to check_tool_readiness. cmd_run passes tool_dir to _preflight. Every existing fake _preflight and fake check_tool_readiness in tests/test_runtime_env.py adds the same final tool_dir=None keyword so unchanged tests keep their current behavior.

- [ ] **Step 6: Run CLI and runtime tests and verify GREEN**

Run: python3 -m pytest tests/test_runtime_env.py tests/test_camc_hardening_pdx.py -q

Expected: PASS.

- [ ] **Step 7: Review checkpoint**

Run: PYTHONPATH=src python3 -m camc_pkg run --help && PYTHONPATH=src python3 -m camc_pkg env check --help

Expected: both help pages show --tool-dir ABSOLUTE_DIR.

---

### Task 3: Prove launch argv and runtime-manifest consistency

**Files:**
- Modify: src/camc_pkg/cli.py
- Modify: tests/test_runtime_env.py
- Modify: tests/test_camc_hardening_pdx.py

**Interfaces:**
- Consumes: resolved["tool"] and resolved["tool_resolution"].
- Produces: launch argv with the exact direct binary and conditional runtime.tool.requested_dir provenance.

- [ ] **Step 1: Extend the full cmd_run fixture**

Change the existing cmd_run launch test's fake preflight result to:

~~~python
return [], {
    "tmux": "/bin/tmux",
    "tmux_source": "golden",
    "tool": "/opt/codex-direct/codex",
    "tool_resolution": {
        "tool": "codex",
        "bin": "/opt/codex-direct/codex",
        "source": "tool-dir",
        "warnings": [],
        "tool_dir": "/opt/codex-direct",
    },
}
~~~

Assert the captured create_tmux_session argv starts with /opt/codex-direct/codex and the saved agent record contains:

~~~python
assert record["runtime"]["tool"]["bin"] == "/opt/codex-direct/codex"
assert record["runtime"]["tool"]["source"] == "tool-dir"
assert record["runtime"]["tool"]["requested_dir"] == "/opt/codex-direct"
~~~

- [ ] **Step 2: Run the full cmd_run test and verify RED**

Run: python3 -m pytest tests/test_runtime_env.py -k "tool_dir and cmd_run" -q

Expected: FAIL because requested_dir is not recorded or tool_dir is not threaded.

- [ ] **Step 3: Record requested_dir only when specified**

~~~python
runtime_tool = {
    "name": tool,
    "bin": _tool_resolution.get("bin")
           or (resolved or {}).get("tool", ""),
    "version": "",
    "source": _tool_resolution.get("source", "unknown"),
    "warnings": list(_tool_resolution.get("warnings", []) or []),
}
if _tool_resolution.get("tool_dir"):
    runtime_tool["requested_dir"] = _tool_resolution["tool_dir"]
agent_rec["runtime"]["tool"] = runtime_tool
~~~

Keep the existing absolute launch-command rewrite unchanged; its alias-aware matching already accepts claude, codex, cursor-agent, and agent and will replace the executable slot with resolved["tool"].

- [ ] **Step 4: Pin backward-compatible manifest shape when omitted**

~~~python
def test_runtime_manifest_omits_requested_dir_without_tool_dir():
    resolution = {
        "tool": "codex", "bin": "/bin/codex",
        "source": "golden", "warnings": [],
    }
    tool_manifest = {
        "name": "codex",
        "bin": resolution["bin"],
        "version": "",
        "source": resolution["source"],
        "warnings": list(resolution["warnings"]),
    }
    if resolution.get("tool_dir"):
        tool_manifest["requested_dir"] = resolution["tool_dir"]
    assert "requested_dir" not in tool_manifest
~~~

- [ ] **Step 5: Run launch and manifest tests and verify GREEN**

Run: python3 -m pytest tests/test_runtime_env.py tests/test_camc_hardening_pdx.py tests/test_cmd_run_wrapper_default.py -q

Expected: PASS.

- [ ] **Step 6: Review checkpoint**

Run: git diff --check

Expected: no whitespace errors.

---

### Task 4: Build the standalone CAMC and verify both package and bundle

**Files:**
- Generated: dist/camc
- Generated: dist/camc_pkg/skills/**
- Generated: src/camc
- Modify by builder only: dist/BUILD_LOG.md

**Interfaces:**
- Consumes: completed tool resolver plus the companion-skills builder.
- Produces: package and standalone CLI parity for --tool-dir.

- [ ] **Step 1: Run focused tool tests**

Run: python3 -m pytest tests/test_runtime_env.py tests/test_camc_hardening_pdx.py tests/test_cmd_run_wrapper_default.py -q

Expected: PASS.

- [ ] **Step 2: Run the full test suite**

Run: python3 -m pytest -q

Expected: PASS.

- [ ] **Step 3: Build and verify**

Run: python3 build_camc.py --verify

Expected: package and generated run --help output match and every builder verification passes.

- [ ] **Step 4: Synchronize the tracked standalone file**

Run: cp dist/camc src/camc && cmp dist/camc src/camc

Expected: cmp exits 0.

- [ ] **Step 5: Verify standalone help and validation**

Run: dist/camc run --help

Expected: output contains --tool-dir ABSOLUTE_DIR.

Run: dist/camc env check --tool-dir relative

Expected: exit code 2 with --tool-dir must be an absolute path.

- [ ] **Step 6: Verify direct resolution without launching an agent**

Create executable stubs named tmux and codex under isolated absolute directories, point HOME at an isolated directory, then run:

~~~bash
HOME=/tmp/camc-tool-dir-home \
PATH=/tmp/camc-tool-dir-tmux:/usr/bin:/bin \
dist/camc env check -t codex \
  --tool-dir /tmp/camc-tool-dir-codex --json
~~~

Expected: resolved.tool is /tmp/camc-tool-dir-codex/codex, tool_resolution.source is tool-dir, and tool_resolution.tool_dir is /tmp/camc-tool-dir-codex.

- [ ] **Step 7: Final static checks**

Run: python3 -m py_compile src/camc_pkg/runtime_env.py src/camc_pkg/cli.py dist/camc && git diff --check

Expected: successful compilation and no whitespace errors.

- [ ] **Step 8: Report without installation or commit**

Summarize precedence, fallback behavior, CLI proof, focused/full tests, and generated artifact status. Do not install to ~/.cam, deploy, tag, commit, or push.

