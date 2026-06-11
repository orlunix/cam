"""Centralized runtime environment + tool readiness (F-08).

Goal: give camc a single source of truth for the *effective* environment
agent commands will see, and the same set of helpers that ``camc run``
uses to check whether the selected tool is actually launchable in
that environment BEFORE we create a tmux session, mint an agent id,
or persist a record. Detection and launch share the same env so
"works in preflight, fails in tmux" can't happen for PATH-related
reasons.

The effective environment is:

    1. The user's login shell env (zsh / bash / sh — whatever ``$SHELL``
       points at, falling back to ``/bin/sh``). We invoke the login
       shell once with ``-l -c`` and dump ``os.environ`` from a
       ``sys.executable`` subprocess. ``sys.executable`` is the same
       python that is already running camc, so the dump does NOT
       depend on a ``python3`` being on ``$PATH``.
    2. If a per-context ``env_setup`` string is configured, we source
       it inside the same shell invocation BEFORE the dump so its
       PATH / env mutations are captured.
    3. Cleanup removes ``TMUX``, ``TMUX_PANE``, ``CLAUDECODE`` from
       the captured env so tmux + Claude don't decide we are nested.

Auth checks are conservative: file-existence + readable, NEVER
content inspection. The spec is "auth file exists and is readable",
never "auth verified".

Public API:

    class RuntimeEnv                           # plain dict-backed record
    load_login_shell_env(shell, env_setup, timeout) -> (env, warnings)
    build_runtime_env(shell, env_setup) -> RuntimeEnv
    resolve_tool(runtime, name) -> str | None
    run_probe(runtime, argv, timeout) -> (rc, stdout_str)
    check_tool_readiness(runtime, tool, tool_binary) -> dict

Python 3.6+, stdlib only. No dataclasses, no f-strings.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys


# ---------------------------------------------------------------------------
# RuntimeEnv container
# ---------------------------------------------------------------------------

class RuntimeEnv(object):
    """Plain record describing the effective environment.

    Fields:
      env       — dict[str, str]; the cleaned environment dictionary
                  callers pass to subprocess.* / tmux as `env=`.
      source    — 'login_shell' | 'current_env_fallback' | 'explicit'.
                  Tells the caller (and `camc env check`) WHERE the
                  env came from so a debug session can tell whether
                  the login-shell capture actually ran.
      shell     — path to the login shell that was queried (or '' on
                  fallback).
      path      — convenience: env['PATH'] at construction time.
      warnings  — list[str]; non-fatal notes (login shell timed out,
                  env had no PATH, env_setup syntax skipped, etc).
    """

    def __init__(self, env=None, source="explicit", shell="",
                 path="", warnings=None):
        self.env = dict(env or {})
        self.source = source
        self.shell = shell
        self.path = path or self.env.get("PATH", "")
        self.warnings = list(warnings or [])


# ---------------------------------------------------------------------------
# Login-shell env dump
# ---------------------------------------------------------------------------

# Code snippet executed by `sys.executable -c <code>` inside the user's
# login shell. Writes a NUL-separated KEY=VALUE stream on stdout. NUL
# is safe because legitimate env var names cannot contain '\0' and
# values cannot either (POSIX execve requirement). We use bytes I/O
# so we don't risk a locale-driven encode failure on weird env values.
_DUMP_CODE = (
    "import os,sys\n"
    "sys.stdout.buffer.write(b'\\0'.join("
    "  (k+'='+v).encode('utf-8','replace')"
    "  for k,v in os.environ.items()))\n"
)


def load_login_shell_env(shell=None, env_setup=None, timeout=5):
    """Run the user's login shell to capture its env, optionally after
    applying an env_setup string. Returns (env_dict, warnings).

    On any failure (shell missing, capture timeout, non-zero exit,
    empty stdout, no PATH in result) we fall back to
    ``os.environ.copy()`` and append a warning describing why. The
    caller is expected to surface this warning to the user but NOT
    block: a host whose login shell can't be captured is still
    runnable, just less verifiable.

    `shell`: path to the shell binary. Defaults to ``$SHELL`` then
    ``/bin/sh``.
    `env_setup`: optional string of shell commands to source/exec
    before the dump (e.g. ``"source /etc/profile.d/foo.sh"``). Joined
    with ``&&`` before the dump command.
    `timeout`: seconds to wait for the capture. Conservative default
    of 5 — the dump itself is fast; rc-file work might be slow on
    NFS-heavy hosts.
    """
    warnings = []
    shell = shell or os.environ.get("SHELL") or "/bin/sh"
    capture_shell = shell
    if env_setup and os.path.exists("/bin/bash"):
        # Machine env_setup strings are authored as POSIX/bash snippets
        # (`export ...`, `source ~/.bashrc`, `unset ...`). Some PDX/DC
        # accounts use csh/tcsh as $SHELL, where `-l -c` and bash rc
        # snippets are not portable, so run explicit setup through bash.
        capture_shell = "/bin/bash"
    if not capture_shell or not os.path.exists(capture_shell):
        warnings.append("login shell %r not found; using current env" % capture_shell)
        return os.environ.copy(), warnings

    py = sys.executable
    dump = "%s -c %s" % (shlex.quote(py), shlex.quote(_DUMP_CODE))
    if env_setup:
        inner = env_setup + " && exec " + dump
    else:
        inner = "exec " + dump
    base = os.path.basename(capture_shell)
    if base in ("bash", "zsh", "ksh"):
        argv = [capture_shell, "-l", "-c", inner]
    else:
        argv = [capture_shell, "-c", inner]
    try:
        result = subprocess.run(
            argv, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        warnings.append("login shell env capture failed (%s); using current env" % e)
        return os.environ.copy(), warnings

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        warnings.append(
            "login shell %s exited %d during env capture (%s); using current env"
            % (capture_shell, result.returncode, stderr[:200] or "no stderr"))
        return os.environ.copy(), warnings

    out = result.stdout or b""
    env = {}
    for token in out.split(b"\x00"):
        if not token:
            continue
        eq = token.find(b"=")
        if eq < 0:
            continue
        k = token[:eq].decode("utf-8", "replace")
        v = token[eq + 1:].decode("utf-8", "replace")
        env[k] = v

    if not env.get("PATH"):
        warnings.append("login shell env had no PATH; using current env")
        return os.environ.copy(), warnings
    return env, warnings


# ---------------------------------------------------------------------------
# build_runtime_env: login env + cleanup
# ---------------------------------------------------------------------------

# Variables that camc + Claude treat as "we are inside an existing tmux/
# Claude session" markers — must be removed from the env we hand to a
# fresh tmux session, or the tools will detect themselves and refuse
# to run / nest weirdly. Identical to the set transport.create_tmux_session
# clears today; centralized here so they are checked the same way.
_NEST_CLEAR_KEYS = ("TMUX", "TMUX_PANE", "CLAUDECODE")


def build_runtime_env(shell=None, env_setup=None):
    """Returns a RuntimeEnv: login shell env + env_setup applied +
    nest-marker cleanup. Used by both ``_preflight`` (to decide if a
    tool is reachable) and ``cmd_run`` (to actually launch tmux with
    that exact env)."""
    env, warnings = load_login_shell_env(shell=shell, env_setup=env_setup)
    source = "login_shell" if not warnings else (
        "current_env_fallback" if any("using current env" in w for w in warnings)
        else "login_shell"
    )
    for k in _NEST_CLEAR_KEYS:
        env.pop(k, None)
    return RuntimeEnv(
        env=env, source=source,
        shell=(shell or os.environ.get("SHELL") or "/bin/sh"),
        path=env.get("PATH", ""),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# resolve_tool / run_probe
# ---------------------------------------------------------------------------

def resolve_tool(runtime, name):
    """Locate `name` in the runtime's PATH (NOT os.environ['PATH']).
    Returns the absolute path or None."""
    if not runtime or not name:
        return None
    return shutil.which(name, path=runtime.env.get("PATH"))


def _expand_runtime_path(runtime, path):
    """Expand leading ``~`` or ``~/<rest>`` using ``runtime.env['HOME']``
    when available, NOT ``os.environ['HOME']``. This is the F-08
    invariant: every check the launch will later honor must consult
    the same effective env — login shell or env_setup can legitimately
    rewrite HOME (NVIDIA containers do this), and checking
    ``~/.claude.json`` against the wrong HOME would either falsely
    block or falsely pass."""
    if not path:
        return path
    if runtime is not None and (path == "~" or path.startswith("~/")):
        home = runtime.env.get("HOME")
        if home:
            return home if path == "~" else os.path.join(home, path[2:])
    return os.path.expanduser(path)


def run_probe(runtime, argv, timeout=5):
    """Run `argv` with `env=runtime.env`, capture stdout (decoded UTF-8),
    return (returncode, stdout_text). On any error returns (-1, "")."""
    if not argv:
        return -1, ""
    try:
        result = subprocess.run(
            argv, env=runtime.env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return -1, ""
    out = (result.stdout or b"").decode("utf-8", "replace")
    return result.returncode, out


# ---------------------------------------------------------------------------
# Tool spec data
# ---------------------------------------------------------------------------

# Conservative readiness specs. Keep the set small and well-justified;
# fancier rules (token validity, license checks) belong in a future
# slice, not here. `auth_files` are HARD requirements (file existence
# + readable, no content inspection). `auth_files_any` + `env_keys`
# are soft alternatives: any one match passes; complete miss is a WARN
# but does not block (per task: codex auth path is uncertain).
_TOOL_SPECS = {
    "claude": {
        "binary": "claude",
        "version_args": ["--version"],
        "auth_files": ["~/.claude.json"],
        "install_hint": "npm install -g @anthropic-ai/claude-code",
    },
    "codex": {
        "binary": "codex",
        "version_args": ["--version"],
        "auth_files_any": ["~/.codex/auth.json", "~/.codex/config.toml"],
        "env_keys": ["OPENAI_API_KEY"],
        "install_hint": "npm install -g @openai/codex",
    },
    "cursor": {
        "binary": "cursor",
        "version_args": ["--version"],
        "install_hint": "ensure 'cursor' is in PATH",
    },
}


# ---------------------------------------------------------------------------
# check_tool_readiness — the meat
# ---------------------------------------------------------------------------

def _spec_from_readiness(readiness):
    """Translate an AdapterConfig.readiness dict (TOML-sourced) into
    the internal spec shape check_tool_readiness consumes. Keeps the
    legacy _TOOL_SPECS shape so the rest of the function doesn't need
    a parallel code path."""
    spec = {}
    if readiness.get("binary"):
        spec["binary"] = readiness["binary"]
    if readiness.get("version_args"):
        spec["version_args"] = list(readiness["version_args"])
    if readiness.get("install_hint"):
        spec["install_hint"] = readiness["install_hint"]
    if readiness.get("required_files"):
        spec["auth_files"] = [e["path"] for e in readiness["required_files"] if e.get("path")]
    if readiness.get("optional_files"):
        spec["auth_files_any"] = [e["path"] for e in readiness["optional_files"] if e.get("path")]
    if readiness.get("optional_env"):
        spec["env_keys"] = [e["name"] for e in readiness["optional_env"] if e.get("name")]
    # version_required is informational — current implementation always
    # treats version_args failures as blocking (F2 fix). Carry it so a
    # future advisory mode can opt out.
    spec["version_required"] = bool(readiness.get("version_required", True))
    return spec


def check_tool_readiness(runtime, selected_tool, tool_binary=None, readiness=None):
    """Return a dict shaped:

        {
            "issues":          [(level, message), ...],  # level: 'error' | 'warn'
            "resolved":        {"tmux": "/abs/path", "tool": "/abs/path"},
            "readiness_source": "adapter" | "fallback",
        }

    Levels:
      - 'error' should block ``camc run`` before any tmux / store
        mutation.
      - 'warn'  is surfaced to the user but not blocking.

    `readiness`: adapter-owned readiness dict (from
    ``AdapterConfig.readiness``). When provided, takes precedence over
    the hardcoded ``_TOOL_SPECS`` fallback so each tool owns its
    auth/version rules in its own TOML rather than in Python. When
    ``readiness`` is None or empty, falls back to ``_TOOL_SPECS`` so
    callers that don't yet plumb the adapter config through still get
    sensible defaults for the three known tools.

    Always promotes the runtime's own env-capture warnings to user-
    visible WARN-level issues so a host with a broken login shell
    still gets one clear message about it.
    """
    issues = [("warn", w) for w in runtime.warnings]
    resolved = {}

    if isinstance(readiness, dict) and readiness:
        spec = _spec_from_readiness(readiness)
        readiness_source = "adapter"
    else:
        spec = _TOOL_SPECS.get(selected_tool, {})
        readiness_source = "fallback"

    # ---- tmux (required, always checked from runtime env) ----
    tmux_path = resolve_tool(runtime, "tmux")
    if not tmux_path:
        issues.append(("error",
            "tmux not found in effective PATH. Install: apt install tmux"))
    else:
        resolved["tmux"] = tmux_path
        rc, out = run_probe(runtime, [tmux_path, "-V"], timeout=3)
        if rc != 0:
            # F2: any non-zero rc (including -1 timeout/OSError) means
            # we cannot confirm tmux is usable. Block — silently passing
            # would let a broken tmux land us in a half-created session.
            issues.append(("error",
                "tmux probe %r failed (rc=%d); cannot verify >= 2.4"
                % ([tmux_path, "-V"], rc)))
        else:
            m = re.search(r"(\d+)\.(\d+)", out or "")
            if not m:
                issues.append(("error",
                    "tmux version not parseable from %r; cannot verify >= 2.4"
                    % (out or "").strip()))
            else:
                ver = (int(m.group(1)), int(m.group(2)))
                if ver < (2, 4):
                    issues.append(("error",
                        "tmux %s too old; need >= 2.4" % out.strip()))

    # ---- selected tool binary ----
    # When the adapter TOML wraps the launch command with the POSIX
    # `env KEY=VAL` prefix (claude.toml does this to inject
    # CLAUDE_CODE_DISABLE_MOUSE=1), `config.command[0]` is "env", not
    # the agent binary. In that case fall through to the spec's known
    # binary so we resolve `claude` / `codex` / `cursor` rather than
    # /usr/bin/env (which exists on every host and would falsely pass).
    # With adapter-owned readiness the spec.binary IS the canonical
    # answer, so this fallback handles both readiness sources uniformly.
    bin_name = tool_binary
    if bin_name in (None, "", "env", "/usr/bin/env"):
        bin_name = spec.get("binary") or selected_tool
    tool_path = resolve_tool(runtime, bin_name)
    if not tool_path:
        hint = spec.get("install_hint", "ensure '%s' is in effective PATH" % bin_name)
        issues.append(("error",
            "'%s' not found in effective PATH. Install: %s" % (bin_name, hint)))
    else:
        resolved["tool"] = tool_path
        version_args = spec.get("version_args", ["--version"])
        rc, _out = run_probe(runtime, [tool_path] + version_args, timeout=5)
        # F2: any non-zero rc — including -1 (timeout/OSError) — must
        # block. The previous "warn for rc != 0 and rc != -1" branch
        # let a broken-but-present binary silently pass readiness, then
        # surface as a cryptic tmux failure at launch. None of the
        # current ToolSpecs mark version-probing as advisory; if a
        # future spec needs that, add an explicit advisory flag.
        if rc != 0:
            issues.append(("error",
                "%s sanity probe %r failed (rc=%d); binary at %s exists but cannot run"
                % (bin_name, [bin_name] + version_args, rc, tool_path)))

    # ---- auth / config file (selected tool only) ----
    # F1: expand ~ via runtime.env['HOME'], not os.environ['HOME'].
    # Otherwise a host where login-shell HOME differs from process
    # HOME (NVIDIA containers, sudo -u workflows, etc.) would check
    # the wrong ~/.claude.json file.
    for af in spec.get("auth_files", []):
        path = _expand_runtime_path(runtime, af)
        if not os.path.exists(path):
            issues.append(("error",
                "%s auth file missing: %s (auth file existence is required; "
                "contents are NOT verified)" % (selected_tool, path)))
        elif not os.access(path, os.R_OK):
            issues.append(("error",
                "%s auth file not readable: %s" % (selected_tool, path)))

    soft_paths = spec.get("auth_files_any") or []
    env_keys = spec.get("env_keys") or []
    if soft_paths or env_keys:
        any_ok = False
        for af in soft_paths:
            p = _expand_runtime_path(runtime, af)
            if os.path.exists(p) and os.access(p, os.R_OK):
                any_ok = True
                break
        for k in env_keys:
            if runtime.env.get(k):
                any_ok = True
                break
        if not any_ok:
            checked = []
            if soft_paths:
                checked.append("files {%s}" % ", ".join(soft_paths))
            if env_keys:
                checked.append("env {%s}" % ", ".join(env_keys))
            issues.append(("warn",
                "%s auth not found (checked %s); proceeding because auth path is uncertain"
                % (selected_tool, " or ".join(checked))))

    return {
        "issues": issues,
        "resolved": resolved,
        "readiness_source": readiness_source,
    }
