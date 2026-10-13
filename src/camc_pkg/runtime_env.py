"""Centralized runtime environment + tool readiness (F-08).

Goal: give camc a single source of truth for the *effective* environment
agent commands will see, and the same set of helpers that ``camc run``
uses to check whether the selected tool is actually launchable in
that environment BEFORE we create a tmux session, mint an agent id,
or persist a record. Detection and launch share the same env so
"works in preflight, fails in tmux" can't happen for PATH-related
reasons.

The effective environment is:

    1. The user's login shell env (zsh / bash / csh / sh — whatever the
       passwd entry names, falling back to ``$SHELL`` then ``/bin/sh``).
       We invoke the login shell once with ``-l -c`` and dump
       ``os.environ`` from a
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

import fnmatch
import json
import os
import pwd
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
# Per-agent runtime homes
# ---------------------------------------------------------------------------

_RUNTIME_HOME_MARKER = ".camc-runtime-home.json"

def _runtime_env_mapping(runtime):
    """Return the effective environment mapping for a runtime value."""
    if isinstance(runtime, RuntimeEnv):
        return runtime.env
    if isinstance(runtime, dict):
        return runtime
    return os.environ


def _expand_runtime_home_path(path, runtime=None):
    """Expand a runtime-home path against the effective HOME."""
    text = str(path or "")
    env = _runtime_env_mapping(runtime)
    home = env.get("HOME")
    if home and (text == "~" or text.startswith("~/")):
        text = home if text == "~" else os.path.join(home, text[2:])
    else:
        text = os.path.expanduser(text)
    return os.path.abspath(text)


def _safe_runtime_owner(owner_id):
    """Validate the owner component used below a managed runtime root."""
    owner = str(owner_id or "")
    if not owner or not re.match(r"^[A-Za-z0-9_-]+$", owner):
        raise ValueError("invalid runtime home owner id")
    return owner


def _link_runtime_home_entry(source, target):
    """Link one shared file/directory without replacing user content."""
    if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
        return "linked"
    if os.path.lexists(target):
        return "preserved"
    try:
        os.symlink(source, target, target_is_directory=os.path.isdir(source))
    except OSError:
        return "error"
    return "linked"


def _read_managed_marker(path, name):
    try:
        with open(os.path.join(path, name), "r") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (IOError, OSError, ValueError):
        return None


def _write_managed_marker(path, name, value):
    marker = os.path.join(path, name)
    tmp = marker + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, marker)


def _write_runtime_config(source, target, owner):
    """Snapshot donor config for a managed per-agent overlay."""
    if not os.path.isfile(source):
        return "missing_source"
    if os.path.lexists(target) and (
            not os.path.isfile(target) or os.path.islink(target)):
        return "error"

    try:
        with open(source, "r") as handle:
            content = handle.read()
    except OSError:
        return "error"

    tmp = target + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "error"
    return "copied"


def ensure_agent_runtime_home(owner_id, spec, runtime=None):
    """Create one isolated tool home from an adapter-declared specification.

    ``spec`` is intentionally generic and is derived from an adapter's
    optional ``[runtime.overlay]`` block. Selected donor assets are linked so
    auth, MCP, skills, sessions, and permission profiles remain shared. The
    config file is a private snapshot so API-only routing changes can never
    rewrite the donor or another agent's login configuration.

    Returns a request-safe manifest suitable for the agent's runtime record,
    or ``None`` when no valid specification is supplied.
    """
    if not isinstance(spec, dict) or not spec.get("environment"):
        return None
    owner = _safe_runtime_owner(owner_id)
    environment = str(spec.get("environment"))
    # The adapter donor is authoritative.  Inherited tool-home variables may
    # point at a parent tool process or another managed agent and must never
    # become the source for a new agent home.
    donor_value = spec.get("donor") or "~"
    donor = _expand_runtime_home_path(donor_value, runtime)
    root = _expand_runtime_home_path(spec.get("root") or
                                     "~/.camc/runtime", runtime)
    target = os.path.join(root, owner)
    if os.path.realpath(target) == os.path.realpath(root):
        raise ValueError("runtime home target may not be its root")

    created_target = False
    try:
        os.makedirs(root, exist_ok=True)
        if os.path.lexists(target) and os.path.islink(target):
            raise ValueError("runtime home target is a symlink: %s" % target)
        expected_marker = {
            "schema": "camc-runtime-home/1", "owner_id": owner,
            "donor": donor, "root": root,
        }
        if os.path.lexists(target):
            if not os.path.isdir(target):
                raise ValueError("runtime home target is not a directory: %s" % target)
            if _read_managed_marker(target, _RUNTIME_HOME_MARKER) != expected_marker:
                raise ValueError("runtime home target is not owned by camc: %s" % target)
        else:
            os.makedirs(target, mode=0o700)
            created_target = True
            _write_managed_marker(
                target, _RUNTIME_HOME_MARKER, expected_marker)
    except OSError as exc:
        raise IOError("cannot create runtime home %s: %s" % (target, exc))

    info = {
        "path": target,
        "root": root,
        "donor": donor,
        "environment": environment,
        "owner_id": owner,
        "config_mode": "snapshot",
        "shared": {},
    }
    try:
        config_name = str(spec.get("config_file") or "")
        if config_name:
            status = _write_runtime_config(
                os.path.join(donor, config_name),
                os.path.join(target, config_name), owner,
            )
            info["shared"][config_name] = status
            if status == "error":
                raise IOError("cannot materialize runtime config %s" % config_name)

        excluded = set(str(name) for name in
                       (spec.get("shared_exclude") or []))
        for name in excluded:
            previous = os.path.join(target, name)
            source = os.path.join(donor, name)
            if (os.path.islink(previous) and
                    os.path.realpath(previous) == os.path.realpath(source)):
                os.unlink(previous)
        names = []
        for name in (spec.get("shared") or []):
            text = str(name or "")
            if (text and text not in names and text != config_name and
                    text not in excluded):
                names.append(text)
        try:
            donor_names = sorted(os.listdir(donor))
        except OSError:
            donor_names = []
        for pattern in (spec.get("shared_globs") or []):
            pattern = str(pattern or "")
            if not pattern:
                continue
            for name in donor_names:
                source = os.path.join(donor, name)
                managed = (os.path.isdir(source) and not os.path.islink(source)
                           and (_read_managed_marker(
                               source, _RUNTIME_HOME_MARKER) or {}).get(
                                   "schema") == "camc-runtime-home/1")
                if (fnmatch.fnmatchcase(name, pattern) and not managed and
                        name not in names and name != config_name and
                        name not in excluded and name != _RUNTIME_HOME_MARKER):
                    names.append(name)
        for name in names:
            source = os.path.join(donor, name)
            target_entry = os.path.join(target, name)
            if not (os.path.isfile(source) or os.path.isdir(source) or
                    os.path.islink(source)):
                info["shared"][name] = "missing_source"
                continue
            status = _link_runtime_home_entry(source, target_entry)
            info["shared"][name] = status
            if status in ("error", "preserved"):
                raise IOError("cannot link shared runtime asset %s" % name)
    except Exception:
        # A failed setup must never leave a half-owned home that a later agent
        # could accidentally reuse.  The donor is never touched.
        try:
            if (created_target and os.path.isdir(target) and
                    not os.path.islink(target)):
                shutil.rmtree(target)
        except OSError:
            pass
        raise
    return info


def _runtime_placeholders(runtime):
    """Return safe values accepted by adapter local-path templates."""
    env = _runtime_env_mapping(runtime)
    home = env.get("HOME") or os.path.expanduser("~")
    user = env.get("USER") or env.get("LOGNAME") or ""
    if not user:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except (KeyError, OSError):
            user = str(os.getuid())
    if not re.match(r"^[A-Za-z0-9_.-]+$", str(user)):
        raise ValueError("unsafe runtime user name")
    return {
        "home": os.path.abspath(home),
        "user": str(user),
        "uid": str(os.getuid()),
    }


def _expand_local_path(value, runtime):
    """Expand a small, non-shell adapter path template."""
    text = str(value or "")
    values = _runtime_placeholders(runtime)
    for key, replacement in values.items():
        text = text.replace("{%s}" % key, replacement)
    if "{" in text or "}" in text:
        raise ValueError("unsupported runtime path placeholder: %s" % text)
    return _expand_runtime_home_path(text, runtime)


def _ensure_private_directory(path):
    """Create a host-local directory before the tool observes its symlink."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        if not os.path.isdir(path) or os.path.islink(path):
            raise IOError("not a directory")
        os.chmod(path, 0o700)
    except OSError as exc:
        raise IOError("cannot bootstrap local runtime directory %s: %s" %
                      (path, exc))


def _ensure_local_link(home, declaration, runtime):
    """Create one shared-home -> host-local link without leaving it dangling."""
    text = str(declaration or "")
    relative, separator, target_value = text.partition("=")
    relative = relative.strip().strip("/")
    if not separator or not relative or relative.startswith("../"):
        raise ValueError("invalid runtime local link: %s" % text)
    link_path = os.path.abspath(os.path.join(home, relative))
    if not link_path.startswith(os.path.abspath(home) + os.sep):
        raise ValueError("runtime local link escapes shared home: %s" % text)
    target = _expand_local_path(target_value.strip(), runtime)

    # Some tools treat a dangling runtime symlink as EEXIST. The ordering is a
    # correctness requirement: make/chmod the local target first, then expose
    # or validate the shared symlink.
    _ensure_private_directory(target)
    try:
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        if os.path.lexists(link_path):
            if not os.path.islink(link_path):
                raise IOError("runtime link path already exists: %s" % link_path)
            if os.path.realpath(link_path) != os.path.realpath(target):
                raise IOError("runtime link points elsewhere: %s" % link_path)
        else:
            os.symlink(target, link_path, target_is_directory=True)
    except OSError as exc:
        raise IOError("cannot install runtime link %s: %s" % (link_path, exc))
    if not os.path.isdir(link_path):
        raise IOError("runtime link is dangling: %s" % link_path)
    return {"link": link_path, "target": target, "mode": "0700"}


def _config_string_value(config_path, key):
    """Read one top-level quoted TOML string without evaluating config."""
    try:
        with open(config_path, "r") as handle:
            content = handle.read()
    except OSError:
        return ""
    match = re.search(
        r"(?m)^\s*%s\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$" %
        re.escape(str(key)), content)
    return match.group(2) if match else ""


def _path_is_within(path, roots):
    absolute = os.path.abspath(path)
    real = os.path.realpath(path)
    for root in roots:
        root_abs = os.path.abspath(root)
        root_real = os.path.realpath(root)
        if (absolute == root_abs or absolute.startswith(root_abs + os.sep) or
                real == root_real or real.startswith(root_real + os.sep)):
            return True
    return False


def ensure_runtime_split(spec, runtime, home=None):
    """Bootstrap adapter-declared host-local paths for one effective home."""
    if not isinstance(spec, dict):
        return None
    shared_home = _expand_runtime_home_path(
        home or spec.get("shared_home") or "~", runtime)
    try:
        os.makedirs(shared_home, exist_ok=True)
    except OSError as exc:
        raise IOError("cannot access shared runtime home %s: %s" %
                      (shared_home, exc))

    links = [_ensure_local_link(shared_home, item, runtime)
             for item in (spec.get("local_links") or [])]
    local_dirs = []
    config_path = os.path.join(shared_home, "config.toml")
    for key in (spec.get("config_local_dirs") or []):
        value = _config_string_value(config_path, key)
        if not value:
            continue
        path = _expand_local_path(value, runtime)
        _ensure_private_directory(path)
        local_dirs.append({"config_key": str(key), "target": path,
                           "mode": "0700"})

    prune_roots = []
    prune_names = set(str(value).strip("/") for value in
                      (spec.get("prune_path_links") or []))
    for link in links:
        relative = os.path.relpath(link["link"], shared_home)
        if relative in prune_names:
            prune_roots.extend([link["link"], link["target"]])
    env = _runtime_env_mapping(runtime)
    old_path = env.get("PATH", "")
    if prune_roots and old_path:
        env["PATH"] = os.pathsep.join(
            entry for entry in old_path.split(os.pathsep)
            if entry and not _path_is_within(entry, prune_roots))

    return {
        "shared_home": shared_home,
        "environment": str(spec.get("environment") or ""),
        "links": links,
        "config_dirs": local_dirs,
        "bootstrap_dirs": sorted(set(
            [item["target"] for item in links] +
            [item["target"] for item in local_dirs])),
    }


def _overlay_home_spec(spec):
    overlay = (spec or {}).get("overlay") or {}
    return {
        "environment": str((spec or {}).get("environment") or ""),
        "donor": str((spec or {}).get("shared_home") or "~"),
        "root": str(overlay.get("root") or "~/.camc/runtime"),
        "config_file": str(overlay.get("config_file") or "config.toml"),
        "api_config_format": str(overlay.get("config_format") or ""),
        "api_wire_api": str(overlay.get("wire_api") or ""),
        "api_catalog_file": str(overlay.get("catalog_file") or ""),
        "shared": list(overlay.get("shared") or []),
        "shared_globs": list(overlay.get("shared_globs") or []),
        "shared_exclude": list(overlay.get("shared_exclude") or []),
    }


def configure_agent_runtime_home(owner_id, adapter, runtime, api_plan=None,
                                 api_base_url="", isolate_tool_home=None,
                                 launcher=False):
    """Apply an adapter's shared-vs-local runtime declaration.

    The adapter declares whether its tool home is isolated by default, while
    ``isolate_tool_home`` can override that boolean for one run. API runs add
    provider/catalog changes only when an isolated home is selected.
    """
    spec = getattr(adapter, "runtime", None)
    if not spec:
        return None
    overlay_info = None
    try:
        overlay = spec.get("overlay") or {}
        adapter_default = bool(getattr(adapter, "isolate_tool_home", False))
        use_overlay = (adapter_default if isolate_tool_home is None else
                       bool(isolate_tool_home))
        if (api_plan and not use_overlay and
                str(overlay.get("config_format") or "")):
            raise ValueError(
                "this adapter's API configuration requires an isolated "
                "tool home")
        overlay_spec = _overlay_home_spec(spec) if use_overlay else None
        if use_overlay:
            overlay_info = ensure_agent_runtime_home(
                owner_id, overlay_spec, runtime)
            overrides = dict(overlay.get("config_boolean_overrides") or {})
            if launcher:
                overrides.update(overlay.get("launcher_config_boolean_overrides") or {})
            if overrides:
                config_path = os.path.join(
                    overlay_info["path"], overlay_spec["config_file"])
                if os.path.islink(config_path):
                    raise ValueError("runtime configuration must be a private snapshot")
                try:
                    with open(config_path) as handle:
                        content = handle.read()
                except FileNotFoundError:
                    content = ""
                first_table = re.search(r"(?m)^\s*\[", content)
                boundary = first_table.start() if first_table else len(content)
                top, tables = content[:boundary], content[boundary:]
                for key, value in overrides.items():
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(key)) or not isinstance(value, bool):
                        raise ValueError("runtime config overrides must be named booleans")
                    assignment = "%s = %s\n" % (key, "true" if value else "false")
                    pattern = r"(?m)^\s*%s\s*=.*(?:\n|$)" % re.escape(key)
                    top = (re.sub(pattern, assignment, top) if re.search(pattern, top)
                           else assignment + top)
                temporary = config_path + ".tmp.%d" % os.getpid()
                with open(temporary, "w") as handle:
                    handle.write(top + tables)
                os.replace(temporary, config_path)
        selected_home = (overlay_info or {}).get("path") or str(
            spec.get("shared_home") or "~")
        info = ensure_runtime_split(spec, runtime, home=selected_home)
        if api_plan and overlay_info:
            from camc_pkg.api_resolver import configure_runtime_home_api
            configure_runtime_home_api(
                overlay_info, overlay_spec, api_plan,
                api_base_url=api_base_url)
    except Exception:
        if overlay_info:
            remove_agent_runtime_home(overlay_info, owner_id=owner_id)
        raise
    if runtime is not None:
        for name in (spec.get("clear_environment") or []):
            runtime.env.pop(str(name), None)
        environment = info.get("environment") or ""
        selected_home = (overlay_info or {}).get("path") or info["shared_home"]
        if environment:
            runtime.env[environment] = selected_home
    info["donor_home"] = _expand_runtime_home_path(
        spec.get("shared_home") or "~", runtime)
    info["isolate_tool_home"] = use_overlay
    if overlay_info:
        info["overlay"] = overlay_info
    return info


def remove_agent_runtime_home(home_info, owner_id=None):
    """Remove one generated runtime home after validating its ownership."""
    if isinstance(home_info, dict):
        path = home_info.get("path") or ""
        root = home_info.get("root") or ""
        recorded_owner = home_info.get("owner_id") or ""
    else:
        path = str(home_info or "")
        root = ""
        recorded_owner = ""
    owner = str(owner_id or recorded_owner or "")
    try:
        safe_owner = _safe_runtime_owner(owner)
    except ValueError:
        return False
    path = os.path.abspath(os.path.expanduser(path)) if path else ""
    root = (os.path.abspath(os.path.expanduser(root)) if root else
            os.path.dirname(path))
    if not path or os.path.basename(path) != safe_owner:
        return False
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(path)
    if real_target == real_root or not real_target.startswith(real_root + os.sep):
        return False
    if not os.path.lexists(path):
        return False
    marker = _read_managed_marker(path, _RUNTIME_HOME_MARKER)
    if not marker or marker.get("schema") != "camc-runtime-home/1":
        return False
    if marker.get("owner_id") != safe_owner:
        return False
    if os.path.abspath(str(marker.get("root") or "")) != root:
        return False
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        return True
    except OSError:
        return False


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


def _default_login_shell():
    """Best guess at the user's login shell.

    Prefer the passwd entry (``getpwuid``) over ``$SHELL``: ``$SHELL``
    reflects whoever launched the current process (e.g. an IDE or a
    tool wrapper like Claude Code's Bash shell may set ``SHELL=zsh``
    even when the account's login shell is csh/bash). The passwd entry
    is what a real login session would use, so it matches the env the
    user actually configures. Fall back to ``$SHELL`` then ``/bin/sh``
    for environments without a passwd entry (containers, nologin
    accounts).
    """
    try:
        pw_shell = pwd.getpwuid(os.getuid()).pw_shell
        if pw_shell and os.path.exists(pw_shell):
            return pw_shell
    except (KeyError, OSError):
        pass
    env_shell = os.environ.get("SHELL")
    if env_shell and os.path.exists(env_shell):
        return env_shell
    return "/bin/sh"


def load_login_shell_env(shell=None, env_setup=None, timeout=5):
    """Run the user's login shell to capture its env, optionally after
    applying an env_setup string. Returns (env_dict, warnings).

    On any failure (shell missing, capture timeout, non-zero exit,
    empty stdout, no PATH in result) we fall back to
    ``os.environ.copy()`` and append a warning describing why. The
    caller is expected to surface this warning to the user but NOT
    block: a host whose login shell can't be captured is still
    runnable, just less verifiable.

    `shell`: path to the shell binary. Defaults to the passwd entry
    (``getpwuid``), then ``$SHELL``, then ``/bin/sh``.
    `env_setup`: optional string of shell commands to source/exec
    before the dump (e.g. ``"source /etc/profile.d/foo.sh"``). Joined
    with ``&&`` before the dump command.
    `timeout`: seconds to wait for the capture. Conservative default
    of 5 — the dump itself is fast; rc-file work might be slow on
    NFS-heavy hosts.
    """
    warnings = []
    shell = shell or _default_login_shell()
    capture_shell = shell
    base = os.path.basename(capture_shell) if capture_shell else ""
    # csh/tcsh can't parse the bash-style `exec <py> -c '...'` dump
    # command we inject below (the quoting trips `Unmatched '''` /
    # `Badly placed ()'s`), and machine env_setup strings are authored
    # as POSIX/bash snippets (`export ...`, `source ~/.bashrc`,
    # `unset ...`). Capture through bash when the login shell is csh
    # family and bash exists; the passwd entry is still what we report
    # as the login shell (see _default_login_shell).
    if base in ("csh", "tcsh") and os.path.exists("/bin/bash"):
        capture_shell = "/bin/bash"
    elif env_setup and os.path.exists("/bin/bash"):
        # Same reasoning for other non-bash shells when an env_setup
        # snippet is present.
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

# A camc agent always has an interactive tmux terminal.  The parent process
# can nevertheless carry NO_COLOR=1 (for example from an automation host or
# an IDE non-interactive shell).  Passing that through suppresses ANSI in
# Codex/Claude/Cursor despite the interactive PTY and makes Desktop appear
# monochrome.  Do not inherit that non-interactive presentation preference.
_INTERACTIVE_CLEAR_KEYS = ("NO_COLOR",)


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
    for k in _INTERACTIVE_CLEAR_KEYS:
        env.pop(k, None)
    # Force SHELL to the resolved login shell. The captured env's SHELL
    # is whatever the parent process exported — e.g. Claude Code's
    # Bash tool sets SHELL=/usr/bin/zsh even when the account's passwd
    # entry is /bin/csh. tmux new-session with no command runs $SHELL
    # from this env, so a stale SHELL launches the wrong shell (on a
    # box with no ~/.zshrc that fires zsh-newuser-install, which aborts
    # and garbles camc's injected launch line -> wedge). Reading
    # /etc/passwd via getpwuid keeps the effective env aligned with the
    # account. Applied to both the captured path and the current-env
    # fallback so the fallback can't reintroduce the wrong SHELL.
    resolved_shell = shell or _default_login_shell()
    if env.get("SHELL") != resolved_shell:
        env["SHELL"] = resolved_shell
    return RuntimeEnv(
        env=env, source=source,
        shell=resolved_shell,
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
    rewrite HOME (ACME containers do this), and checking
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
    "pi": {
        "binary": "pi",
        "version_args": ["--version"],
        "auth_files_any": ["~/.pi/agent/auth.json"],
        "env_keys": ["ACME_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
        "install_hint": "use the approved Pi installation for your environment",
    },
}


# ---------------------------------------------------------------------------
# Golden paths (2026-06-23 PDX hardening). Independent of user PATH /
# shell / tmux config drift so camc agents resolve stable executables
# even when the caller's env is messy.
# ---------------------------------------------------------------------------

# /bin/tmux is the fallback tmux on ACME containers/VMs. New sessions
# prefer the effective runtime PATH so a user's selected tmux binary (for
# example Homebrew tmux on macOS) matches the client they use to attach.
_GOLDEN_TMUX_PATHS = (
    "/bin/tmux",
)

# Per-tool ordered preference list. Absolute paths; first existing +
# executable wins. PATH/env is the fallback after this list is
# exhausted (unless --use-env-tool is set).
_GOLDEN_TOOL_PATHS = {
    "claude": (
        "/home/tools_ai/anthropic-ai/claude/latest/claude",
        "/home/tools_ai/anthropic-ai/claude/stable/claude",
        "/home/prgn_share/tools/claude-code/bin/claude",
    ),
    "codex": (
        "/home/tools_ai/openai/codex/latest/bin/codex",
        "/home/tools_ai/openai/codex/stable/bin/codex",
        "/home/prgn_share/tools/codex/bin/codex",
        "~/.local/bin/codex",
    ),
    "cursor": (
        "/home/tools_ai/cursor/cursor-cli/latest/bin/cursor-agent",
        "/home/prgn_share/tools/cursor/cursor-cli/latest/bin/cursor-agent",
    ),
    "pi": (
        "/home/tools_ai/earendil-works/pi-coding-agent/latest/bin/pi",
        "/home/tools_ai/earendil-works/pi-coding-agent/stable/bin/pi",
    ),
}

# Alias set used for PATH/env fallback resolution. Some tools ship
# under multiple binary names (cursor's CLI binary is `cursor-agent`
# in some packages and `agent` in others) — the alias list lets us
# locate any of them without forcing one specific install.
_PATH_TOOL_ALIASES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor-agent", "agent"),
    "pi": ("pi",),
}


def _is_executable_file(path):
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_tmux_bin(runtime=None):
    """Resolve the tmux binary camc should drive new sessions with.

    Order:
      1) PATH lookup via runtime.env — match the tmux client selected by
         the user's effective launch environment.
      2) /bin/tmux (golden fallback) — preserve container compatibility
         when the effective PATH has no tmux.

    Returns (path_or_None, source_str). source_str is 'golden' or
    'env' or 'missing'. Callers that just want the path can ignore
    source_str.
    """
    if runtime is not None:
        env_path = resolve_tool(runtime, "tmux")
        if env_path:
            return env_path, "env"
    for golden in _GOLDEN_TMUX_PATHS:
        if _is_executable_file(golden):
            return golden, "golden"
    return None, "missing"


def resolve_tool_with_source(runtime, tool, use_env_tool=False, tool_dir=None):
    """Resolve a tool's absolute binary path with source attribution.

    ``tool`` is the user-facing name (claude / codex / cursor). The
    return shape is a dict so callers (cli.cmd_run, the agent-record
    manifest writer) can record the resolution provenance without a
    second round of detection.

    Returns:
        {
          "tool":     "claude" | "codex" | "cursor" | <unknown>,
          "bin":      absolute path or None,
          "source":   "golden" | "env" | "env-forced" | "missing",
          "warnings": [<str>, ...],
        }

    Resolution order (default):
      1) Golden absolute paths from ``_GOLDEN_TOOL_PATHS[tool]``.
      2) PATH/env lookup using ``_PATH_TOOL_ALIASES[tool]``.

    With ``use_env_tool=True``, the golden list is skipped entirely;
    only the alias-based PATH lookup runs, and ``source`` is set to
    ``env-forced`` on success so the agent record can mark the run.

    Configured explicit paths (from adapter readiness.binary or the
    user's external config) are handled in ``check_tool_readiness``
    one level up — this helper is the golden-vs-PATH split only.
    """
    result = {
        "tool":     tool,
        "bin":      None,
        "source":   "missing",
        "warnings": [],
    }
    aliases = _PATH_TOOL_ALIASES.get(tool, (tool,))
    if tool_dir:
        for alias in aliases:
            candidate = os.path.join(tool_dir, alias)
            if _is_executable_file(candidate):
                result["bin"] = candidate
                result["source"] = "tool-dir"
                return result
        result["warnings"].append(
            "tool not found in --tool-dir %s; falling back to configured/PATH resolution"
            % tool_dir)
    if use_env_tool:
        for alias in aliases:
            p = resolve_tool(runtime, alias) if runtime is not None else None
            if p and _is_executable_file(p):
                result["bin"] = p
                result["source"] = "env-forced"
                result["warnings"].append(
                    "tool resolved from PATH (--use-env-tool); golden "
                    "paths skipped: %s" % p)
                return result
        result["warnings"].append(
            "tool %r not found in PATH (--use-env-tool); aliases tried: %s"
            % (tool, ", ".join(aliases)))
        return result
    # Default: golden first.
    for golden in _GOLDEN_TOOL_PATHS.get(tool, ()):
        candidate = _expand_runtime_path(runtime, golden)
        if _is_executable_file(candidate):
            result["bin"] = candidate
            result["source"] = "golden"
            return result
    # Golden exhausted — fall through to PATH/env aliases.
    for alias in aliases:
        p = resolve_tool(runtime, alias) if runtime is not None else None
        if p and _is_executable_file(p):
            result["bin"] = p
            result["source"] = "env"
            result["warnings"].append(
                "tool resolved from PATH (no golden path present): %s" % p)
            return result
    return result


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
    if readiness.get("version_timeout"):
        spec["version_timeout"] = readiness["version_timeout"]
    return spec


def check_tool_readiness(runtime, selected_tool, tool_binary=None,
                          readiness=None, use_env_tool=False, tool_dir=None):
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
    other_tool = not spec

    # ---- tmux (required) ----
    # Prefer the effective runtime PATH so the tmux client used by camc
    # matches the user's selected tmux version; /bin/tmux is fallback only.
    tmux_path, tmux_source = resolve_tmux_bin(runtime)
    if not tmux_path:
        issues.append(("error",
            "tmux not found (no effective PATH match and /bin fallback missing). "
            "Install: apt install tmux"))
    else:
        resolved["tmux"] = tmux_path
        resolved["tmux_source"] = tmux_source
        if tmux_source == "env":
            issues.append(("warn",
                "tmux resolved from effective PATH (%s)"
                % tmux_path))
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

    # 2026-06-23 PDX hardening: prefer configured explicit absolute
    # executables, then golden paths, then PATH. ``use_env_tool=True``
    # is the explicit advanced opt-out: skip configured/golden paths
    # and resolve from PATH aliases only.
    tool_path = None
    tool_resolution = {
        "tool": selected_tool, "bin": None,
        "source": "missing", "warnings": [],
    }
    if tool_dir:
        tool_dir_resolution = resolve_tool_with_source(
            runtime, selected_tool, use_env_tool=use_env_tool,
            tool_dir=tool_dir)
        if tool_dir_resolution.get("source") == "tool-dir":
            tool_path = tool_dir_resolution.get("bin")
            tool_resolution = tool_dir_resolution
        else:
            for warning in tool_dir_resolution.get("warnings", []) or []:
                issues.append(("warn", warning))
    configured_abs = bin_name if os.path.isabs(bin_name) else ""
    if not tool_path and configured_abs and not use_env_tool:
        if _is_executable_file(configured_abs):
            tool_path = configured_abs
            tool_resolution = {
                "tool": selected_tool, "bin": tool_path,
                "source": "configured", "warnings": [],
            }
        else:
            issues.append(("warn",
                "configured tool path is not executable; falling back "
                "to golden/PATH: %s" % configured_abs))

    # Spec-binary override: when the adapter readiness (or fallback
    # spec) declares a custom binary that differs from the selected
    # tool aliases (e.g. 'widget_custom'), that binary is the canonical
    # PATH alias. This path deliberately does NOT override a configured
    # absolute executable above.
    spec_binary = spec.get("binary")
    if not tool_path and spec_binary and spec_binary not in _PATH_TOOL_ALIASES.get(
            selected_tool, (selected_tool,)):
        if os.path.isabs(spec_binary) and not use_env_tool:
            tool_path = spec_binary if _is_executable_file(spec_binary) else None
            tool_resolution = {
                "tool": selected_tool, "bin": tool_path,
                "source": "configured" if tool_path else "missing",
                "warnings": [],
            }
        elif not os.path.isabs(spec_binary):
            tool_path = resolve_tool(runtime, spec_binary)
            tool_resolution = {
                "tool":     selected_tool,
                "bin":      tool_path,
                "source":   "env-forced" if use_env_tool else "env",
                "warnings": (
                    ["tool resolved from PATH using adapter-spec binary "
                     "%r: %s" % (spec_binary, tool_path)] if tool_path else []),
            }
            for w in tool_resolution["warnings"]:
                issues.append(("warn", w))

    if not tool_path:
        tool_resolution = resolve_tool_with_source(
            runtime, selected_tool, use_env_tool=use_env_tool)
        tool_path = tool_resolution.get("bin")
        for w in tool_resolution.get("warnings", []) or []:
            issues.append(("warn", w))
        if not tool_path and not (use_env_tool and os.path.isabs(bin_name)):
            # Last-resort: try the legacy bin_name (may be a custom
            # alias the spec inherited from _TOOL_SPECS). Do not use
            # an absolute configured path here when --use-env-tool was
            # requested; that option means PATH/env only.
            tool_path = resolve_tool(runtime, bin_name)
            if tool_path:
                tool_resolution = {
                    "tool": selected_tool, "bin": tool_path,
                    "source": "env-forced" if use_env_tool else "env",
                    "warnings": [
                        "tool resolved from PATH via legacy bin_name=%s: %s"
                        % (bin_name, tool_path)],
                }
                issues.append(("warn", tool_resolution["warnings"][0]))
    if not tool_path and other_tool:
        tool_path = bin_name
        tool_resolution = {
            "tool": selected_tool, "bin": tool_path,
            "source": "unresolved", "warnings": [
                "tool not found in PATH; passing raw name to tmux: %s" % tool_path],
        }
        issues.append(("warn", tool_resolution["warnings"][0]))
    resolved["tool_resolution"] = tool_resolution
    if not tool_path:
        hint = spec.get("install_hint", "ensure '%s' is in effective PATH" % bin_name)
        issues.append(("error",
            "'%s' not found (golden paths missing AND no PATH match). "
            "Install: %s" % (bin_name, hint)))
    else:
        resolved["tool"] = tool_path
        version_args = spec.get("version_args", ["--version"])
        rc, _out = (0, "") if other_tool else run_probe(
            runtime, [tool_path] + version_args,
            timeout=spec.get("version_timeout", 5))
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
    # HOME (ACME containers, sudo -u workflows, etc.) would check
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
