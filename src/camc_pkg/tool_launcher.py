"""Replaceable tool-launcher policy and command construction.

This module owns every launcher-specific decision: raw template parsing,
named presets, LSF discovery/ranking, LSF attachment probing, qsub fallback, target
command quoting, API compatibility, and launcher-record detection.  CLI and
monitor code should contain integration hooks only.
"""

import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile


_NAMED_TOOL_LAUNCHER_RE = re.compile(r"^(auto)$")
_REMOVED_NAMED_TOOL_LAUNCHER_RE = re.compile(
    r"^(?:qsub-4g|ssh-auto(?::[0-9]+)?)$")
_AUTORUN_SHELLS = frozenset(("bash", "csh", "sh", "tcsh", "zsh"))
_TOOL_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NVPROJECTNAME_BINARIES = (
    "/home/nv/utils/nvprojectname/bin/nvprojectname",
    "/home/nv/bin/nvprojectname", "nvprojectname")
_BJOBS_FORMAT = (
    "jobid stat service_class interactive exec_host proj_name "
    "job_name command job_description delimiter='|'")


def parse_tool_launcher(value):
    """Parse a custom launcher prefix and enforce its argv contract."""
    text = str(value or "").strip()
    if _REMOVED_NAMED_TOOL_LAUNCHER_RE.match(text):
        raise ValueError(
            "--tool-launcher %s is no longer a preset; use auto or a "
            "custom launcher prefix" % text)
    if value is None or text == "none":
        return []
    try:
        argv = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValueError("--tool-launcher: %s" % exc)
    if not argv:
        raise ValueError("--tool-launcher must not be empty")
    placeholders = [item for item in argv if item == "{command}"]
    embedded = [item for item in argv
                if "{command}" in item and item != "{command}"]
    if len(placeholders) > 1 or embedded:
        raise ValueError(
            "--tool-launcher may contain at most one standalone "
            "{command} argv element")
    return argv


def named_tool_launcher(value):
    """Return ``(preset, "")`` for a supported named launcher."""
    text = str(value or "").strip()
    if not text or "{command}" in text:
        return None
    match = _NAMED_TOOL_LAUNCHER_RE.match(text)
    if not match:
        return None
    return text, ""


def validate_tool_launcher_api(value, api_plan):
    """Remote launchers cannot reach the submit host's loopback proxy."""
    if value and str(value).strip() != "none" and api_plan:
        raise ValueError(
            "--tool-launcher is not compatible with an API profile yet: "
            "the per-agent proxy listens on this host only; use "
            "--tool-launcher none for local API runs or "
            "--no-default-api for remote launchers")


def record_uses_tool_launcher(record):
    """Return whether an agent record was created with a tool launcher."""
    runtime = record.get("runtime") if isinstance(record, dict) else None
    return isinstance(runtime, dict) and "launcher" in runtime


def launcher_start_pending(record):
    """Wait for a target-side receipt, independently of queue duration."""
    launcher = ((record.get("runtime") or {}).get("launcher") or {})
    path = launcher.get("launch_status_file")
    if not path:
        return False
    try:
        with open(path) as stream:
            started = stream.read(32).strip()
    except (IOError, OSError):
        return True
    return started not in ("battach", "ssh", "qsub", "local", "custom")


def launcher_target_environment(runtime_env, storage_info=None, explicit_keys=None):
    """Send supported context and only caller-explicit extra environment."""
    result = {}
    # An existing LSF allocation retains the environment of its original
    # submitter. Its TERM is not necessarily the terminal attached now.
    for name in ("TERM", "COLORTERM"):
        if (runtime_env or {}).get(name):
            result[name] = str(runtime_env[name])
    paths = []
    for path in str((runtime_env or {}).get("PATH") or "").split(os.pathsep):
        if (os.path.isabs(path) and "/tmp/arg0/" not in path and
                not path.endswith("/tmp/arg0") and path not in paths):
            paths.append(path)
    for path in ("/home/nv/bin", "/home/utils/bin", "/usr/local/lsf/bin"):
        if path not in paths:
            paths.append(path)
    result["PATH"] = os.pathsep.join(paths)
    storage = storage_info if isinstance(storage_info, dict) else {}
    name = str(storage.get("environment") or "")
    if name and _TOOL_ENV_NAME_RE.match(name):
        result[name] = str((runtime_env or {}).get(name) or "")
    # --tool-env is an intentional override, distinct from copying the
    # submit host's ambient credentials/loader environment. PATH continues
    # through the allocation-preserving merge above.
    for name in explicit_keys or []:
        if (name != "PATH" and _TOOL_ENV_NAME_RE.match(name) and
                name in (runtime_env or {})):
            result[name] = str(runtime_env[name])
    return result


def _target_path_lines(extra_path):
    """Preserve allocation PATH; append only target-visible directories."""
    if not extra_path:
        return []
    return [
        "_camc_path=${PATH:-/usr/bin:/bin}",
        "_camc_extra_path=%s" % shlex.quote(str(extra_path)),
        "_camc_previous_ifs=$IFS",
        "_camc_restore_glob=''",
        "case $- in *f*) ;; *) set -f; _camc_restore_glob=1 ;; esac",
        "IFS=:",
        "for _camc_dir in $_camc_extra_path; do",
        "  case \"$_camc_dir\" in /*) ;; *) continue ;; esac",
        "  [ -d \"$_camc_dir\" ] || continue",
        "  case \":$_camc_path:\" in *\":$_camc_dir:\"*) ;; "
        "*) _camc_path=$_camc_path:$_camc_dir ;; esac",
        "done",
        "IFS=$_camc_previous_ifs",
        "if [ -n \"$_camc_restore_glob\" ]; then set +f; fi",
        "PATH=$_camc_path; export PATH",
        "unset _camc_path _camc_extra_path _camc_dir _camc_previous_ifs _camc_restore_glob",
    ]


def _launcher_binary(env, names):
    """Resolve one site command without depending on the caller's bash PATH."""
    for name in names:
        if os.path.isabs(name):
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
            continue
        resolved = shutil.which(name, path=(env or {}).get("PATH"))
        if resolved:
            return resolved
    return ""


def default_tool_launcher(env=None):
    """Use remote autorun by default only where NVIDIA project tools exist.

    ``nvprojectname`` is the site-specific prerequisite for the auto
    launcher's qsub path and serves as a local, subprocess-free NV signal.
    Explicit ``--tool-launcher`` choices are handled by the caller and are
    unaffected by this default.
    """
    effective_env = os.environ if env is None else env
    return ("auto" if _launcher_binary(
        effective_env, _NVPROJECTNAME_BINARIES) else "none")


def _launcher_process(argv, env=None, cwd=None, timeout=10):
    """Run one bounded launcher-discovery command; return ``(rc,out,err)``."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True)
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)


def _job_description_value(description, name):
    match = re.search(
        r"(?:^|\s)%s=([^\s]+)" % re.escape(str(name)),
        str(description or ""))
    return match.group(1).rstrip(">") if match else ""


def _parse_autorun_jobs(output, selected_tool, current_project=""):
    """Parse and rank safe user-owned autorun LSF attachment candidates."""
    candidates = []
    for raw in str(output or "").splitlines():
        fields = raw.rstrip("\n").split("|", 8)
        if len(fields) != 9:
            continue
        (job_id, status, service_class, interactive, host, project,
         job_name, command, description) = [item.strip() for item in fields]
        if (not job_id.isdigit() or status != "RUN" or
                service_class != "autorun_sla" or interactive != "Y" or
                not host or host == "-"):
            continue
        command_name = ""
        try:
            command_argv = shlex.split(command, posix=True)
            if command_argv:
                command_name = os.path.basename(command_argv[0]).lower()
        except ValueError:
            pass
        job_name_base = os.path.basename(job_name).lower()
        generic_shell = (command_name in _AUTORUN_SHELLS and
                         job_name_base in _AUTORUN_SHELLS)
        ai_tool = _job_description_value(description, "QJT_AI_AGENT_TYPE")
        virtual_queue = _job_description_value(
            description, "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE")
        preferred_queue = (
            virtual_queue.startswith("o_pri_interactive_cpu_") or
            ("interactive" in virtual_queue and
             "regress" not in virtual_queue))
        score = (
            int(bool(current_project) and project == current_project),
            int(bool(ai_tool) and ai_tool.lower() == str(selected_tool).lower()),
            int(generic_shell),
            int(preferred_queue),
            int(job_id),
        )
        candidates.append({
            "job_id": job_id,
            "host": host,
            "project": project,
            "job_name": job_name,
            "command": command,
            "ai_tool": ai_tool,
            "virtual_queue": virtual_queue,
            "score": score,
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    result, seen_hosts = [], set()
    for candidate in candidates:
        if candidate["host"] in seen_hosts:
            continue
        seen_hosts.add(candidate["host"])
        result.append(candidate)
    return result


def _verify_autorun_job_long(output, candidate, expected_user=""):
    """Recheck one candidate against unwrapped ``bjobs -l -UF`` metadata."""
    text = str(output or "")
    required = (
        "Job <%s>" % candidate["job_id"],
        "Service Class <autorun_sla>",
        "Status <RUN>",
        "Interactive",
        "Started on <%s>" % candidate["host"],
    )
    if not all(token in text for token in required):
        return False
    user = str(expected_user or "")
    return not user or user.isdigit() or "User <%s>" % user in text


def _long_job_field(output, name):
    name_pattern = r"\s+".join(
        re.escape(part) for part in str(name).split())
    match = re.search(r"\b%s\s*<([^>]+)>" % name_pattern,
                      str(output or ""), re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _current_username(env):
    try:
        import pwd
        name = pwd.getpwuid(os.getuid()).pw_name
        if name:
            return str(name)
    except (ImportError, KeyError, OSError):
        pass
    for key in ("USER", "LOGNAME"):
        name = str((env or {}).get(key) or "").strip()
        if name and not name.isdigit():
            return name
    return ""


def _normalized_lsf_host(value):
    host = str(value or "").strip().strip("<>")
    host = re.sub(r"^\d+\*", "", host)
    host = host.split()[0] if host else ""
    return host.rstrip(".").split(".", 1)[0].lower()


def detect_current_autorun_allocation(env=None, local_host=None,
                                      current_user=None):
    """Return safe metadata when this process is inside its RUN autorun job.

    ``LSB_JOBID`` is only a hint. The job record must still prove ownership,
    RUN state, autorun service class, interactive mode, and execution on this
    host. Failure to query or verify the record deliberately returns None so
    callers can use their ordinary launcher path.
    """
    effective_env = dict(env or {})
    job_id = str(effective_env.get("LSB_JOBID") or "").strip()
    if not job_id.isdigit():
        return None

    bjobs = _launcher_binary(effective_env, (
        "/usr/local/lsf/bin/bjobs", "bjobs"))
    if not bjobs:
        return None
    expected_user = str(current_user or _current_username(effective_env)).strip()
    if not expected_user:
        return None
    expected_host = _normalized_lsf_host(local_host or socket.gethostname())
    if not expected_host:
        return None

    rc, output, _stderr = _launcher_process(
        [bjobs, "-l", "-UF", job_id], env=effective_env, timeout=10)
    if rc:
        return None

    record_job_id = _long_job_field(output, "Job")
    user = _long_job_field(output, "User")
    service_class = _long_job_field(output, "Service Class")
    status = _long_job_field(output, "Status")
    started = re.search(
        r"\bStarted\s+on\s*<([^>]+)>", str(output or ""),
        re.IGNORECASE)
    host = started.group(1).strip() if started else ""
    text = str(output or "")
    description_start = re.search(
        r"\bJob\s+Description\s*<", text, re.IGNORECASE)
    summary = text[:description_start.start()] if description_start else text
    if (record_job_id != job_id or user != expected_user or
            service_class != "autorun_sla" or status != "RUN" or
            not re.search(
                r"\bInteractive\s+pseudo-terminal\s+shell\s+mode\b",
                summary, re.IGNORECASE) or
            _normalized_lsf_host(host) != expected_host):
        return None

    description = str(output or "")
    return {
        "job_id": job_id,
        "service_class": service_class,
        "host": _normalized_lsf_host(host),
        "project": _long_job_field(output, "Project"),
        "queue": _long_job_field(output, "Queue"),
        "virtual_queue": _job_description_value(
            description, "QSUB_JOB_TAG_QSUB_VIRTUAL_QUEUE"),
    }


def _resolve_launcher_project(workdir, env, nvproject_bin):
    rc, stdout, stderr = _launcher_process(
        [nvproject_bin, "resolve_validate"], env=env, cwd=workdir, timeout=10)
    project = next((line.strip() for line in reversed(stdout.splitlines())
                    if line.strip()), "")
    if rc or not re.match(r"^[A-Za-z0-9_.-]+$", project):
        detail = "\n".join(
            part.strip() for part in (stderr, stdout) if part and part.strip())
        detail = detail or "project could not be resolved"
        raise ValueError("auto launcher project resolution failed: %s" % detail)
    return project


def _is_unregistered_empty_project(error):
    """Match nvprojectname's specific error for a missing project mapping."""
    text = str(error or "")
    return (re.search(r"UnregisteredProjectNameException", text,
                      re.IGNORECASE) is not None and
            re.search(r"project_name\s*:\s*['\"]{2}", text,
                      re.IGNORECASE) is not None)


def _resolve_or_save_launcher_project(workdir, env, nvproject_bin):
    try:
        return _resolve_launcher_project(workdir, env, nvproject_bin)
    except ValueError as exc:
        if not _is_unregistered_empty_project(str(exc)):
            raise

    # The save is scoped to the qsub fallback and only runs when
    # resolve_validate confirms that this directory has no project name.
    # Do not overwrite an existing project mapping or mask other failures.
    save_bin = (_launcher_binary(env, ("/home/nv/bin/nvprojectname",)) or
                nvproject_bin)
    rc, stdout, stderr = _launcher_process(
        [save_bin, "save", "./", "gpu_gr102g_hw_mmplex_riscv"],
        env=env, cwd=workdir, timeout=10)
    if rc:
        detail = (stderr or stdout or "exit %d" % rc).strip()
        raise ValueError(
            "qsub launcher nvprojectname save failed: %s" % detail)
    return _resolve_launcher_project(workdir, env, nvproject_bin)


def _qsub_launcher(workdir, env, preset="auto"):
    nvproject = _launcher_binary(env, _NVPROJECTNAME_BINARIES)
    qsub = _launcher_binary(env, ("/home/nv/bin/qsub", "qsub"))
    if not nvproject:
        raise ValueError("%s launcher requires nvprojectname" % preset)
    if not qsub:
        raise ValueError("%s launcher requires qsub" % preset)
    project = _resolve_or_save_launcher_project(workdir, env, nvproject)
    return ([
        nvproject, "run", project, qsub,
        "-q", "o_cpu_4G", "-m", "rel8x",
        "-app", "r89docker", "--qsla", "autorun_sla",
        "-app", "affinity", "-Is", "{command}",
    ], {
        "preset": preset,
        "transport": "qsub",
        "project": project,
        "fallback": preset == "auto",
    }, ["%s: submitting a new qsub allocation for project %s" %
        (preset, project)])


def _private_script(workdir, prefix, body):
    """Write an executable, owner-only script under the shared workdir."""
    root = os.path.abspath(workdir)
    directory = tempfile.mkdtemp(prefix=prefix, dir=root)
    path = os.path.join(directory, "run")
    try:
        with open(path, "w") as script:
            script.write("#!/bin/sh\n")
            script.write(body)
            if not body.endswith("\n"):
                script.write("\n")
        os.chmod(path, 0o700)
        return directory, path
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def resolve_local_target_tool_path(selected_tool, target_tool, env,
                                   tool_dir=None, configured_binary=None,
                                   use_env_tool=False):
    """Best-effort local absolute path to try in a remote launch target.

    A remote allocation may have a different PATH and filesystem view, so
    failure to resolve locally is not fatal. The target-side probe still
    resolves and validates the command before it starts.
    """
    from camc_pkg.runtime_env import (
        _PATH_TOOL_ALIASES, _is_executable_file, resolve_tool,
        resolve_tool_with_source)

    class _RuntimeView(object):
        def __init__(self, runtime_env):
            self.env = runtime_env

    runtime = _RuntimeView(env or {})
    names = []
    for name in (target_tool, configured_binary):
        value = str(name or "").strip()
        if value and value not in names:
            names.append(value)
    for alias in _PATH_TOOL_ALIASES.get(selected_tool, (selected_tool,)):
        if alias and alias not in names:
            names.append(alias)

    if tool_dir:
        for name in names:
            candidate = os.path.join(tool_dir, os.path.basename(name))
            if _is_executable_file(candidate):
                return candidate
        resolved = resolve_tool_with_source(
            runtime, selected_tool, use_env_tool=use_env_tool,
            tool_dir=tool_dir)
        if resolved.get("source") == "tool-dir" and resolved.get("bin"):
            return resolved["bin"]

    if not use_env_tool:
        for name in names:
            if os.path.isabs(name) and _is_executable_file(name):
                return name

    # Adapter-specific binaries can differ from the selected tool's standard
    # aliases, so try those directly before the standard tool lookup.
    for name in names:
        if not name or os.path.isabs(name):
            continue
        path = resolve_tool(runtime, name)
        if path and _is_executable_file(path):
            return path

    resolved = resolve_tool_with_source(
        runtime, selected_tool, use_env_tool=use_env_tool)
    path = resolved.get("bin")
    return path if path and os.path.isabs(path) else ""


def _target_tool_resolution_lines(target_tool, preferred_tool_path="",
                                  version_args=None, env_assignments=None):
    """Return POSIX shell statements that choose a runnable absolute path."""
    quoted_tool = shlex.quote(str(target_tool or ""))
    quoted_preferred = shlex.quote(str(preferred_tool_path or ""))
    version_args = (["--version"] if version_args is None else
                    [str(item) for item in version_args])
    env_values = [str(item) for item in (env_assignments or [])]
    probe_prefix = ["/usr/bin/env"] if env_values else []
    probe_command = (" ".join(
        probe_prefix + [shlex.quote(item) for item in env_values] +
        ['"$_camc_candidate"'] +
        [shlex.quote(item) for item in version_args])
        if version_args else ":")
    return [
        "_camc_tool=''",
        "_camc_unrunnable=0",
        "_camc_check_tool() {",
        "  _camc_candidate=$1",
        "  case \"$_camc_candidate\" in /*) ;; *) "
        "_camc_candidate=\"$PWD/$_camc_candidate\" ;; esac",
        "  [ -f \"$_camc_candidate\" ] && [ -x \"$_camc_candidate\" ] "
        "|| return 1",
        "  if %s >/dev/null 2>&1; then" % probe_command,
        "    _camc_tool=$_camc_candidate",
        "    return 0",
        "  fi",
        "  _camc_unrunnable=1",
        "  return 1",
        "}",
        "if [ -n %s ]; then _camc_check_tool %s || :; fi" %
        (quoted_preferred, quoted_preferred),
        "if [ -z \"$_camc_tool\" ] && [ -n %s ]; then" % quoted_tool,
        "  _camc_candidate=$(command -v %s 2>/dev/null || :)" % quoted_tool,
        "  if [ -n \"$_camc_candidate\" ] && "
        "[ \"$_camc_candidate\" != %s ]; then" % quoted_preferred,
        "    _camc_check_tool \"$_camc_candidate\" || :",
        "  fi",
        "fi",
    ]


def _target_tool_probe_script(target_tool, workdir, selected_tool="",
                              preferred_tool_path="", version_args=None,
                              target_path=""):
    """Resolve and smoke-test a tool inside an LSF job before selecting it."""
    quoted_workdir = shlex.quote(os.path.abspath(workdir))
    lines = ["cd %s || exit 125" % quoted_workdir]
    lines.extend(_target_path_lines(target_path))
    lines.extend(_target_tool_resolution_lines(
        target_tool, preferred_tool_path, version_args))
    lines.extend((
        "if [ -n \"$_camc_tool\" ]; then",
        "  printf '__CAMC_TARGET_TOOL__%s\\n' \"$_camc_tool\"",
        "  exit 0",
        "fi",
        "if [ \"$_camc_unrunnable\" -eq 1 ]; then",
        "  printf '%s\\n' __CAMC_TARGET_TOOL_UNRUNNABLE__ >&2",
        "  exit 126",
        "fi",
        "printf '%s\\n' __CAMC_TARGET_TOOL_MISSING__ >&2",
        "exit 127",
    ))
    return "\n".join(lines) + "\n"


def build_resolved_tool_command(tool_argv, target_tool, workdir,
                                preferred_tool_path="", version_args=None,
                                started_path="", started_transport="",
                                started_status_path=""):
    """Build a target-side resolve/verify/exec command for non-battach paths.

    This is used by qsub and raw launch templates, where a target-side probe
    cannot run until the final allocation/host has started. It resolves the
    preferred local absolute path first, then the target's PATH, validates the
    selected executable with the adapter version command, and execs that same
    absolute path with the original tool arguments.
    """
    command = list(tool_argv or [])
    index = 1 if command and command[0] == "env" else 0
    env_assignments = []
    if index:
        while (index < len(command) and "=" in command[index]
               and not command[index].startswith("-")):
            env_assignments.append(command[index])
            index += 1
    if index >= len(command):
        raise ValueError("tool command does not contain an executable")
    tool_args = command[index + 1:]
    version_args = (["--version"] if version_args is None else
                    [str(item) for item in version_args])
    lines = ["cd %s || exit 125" % shlex.quote(os.path.abspath(workdir))]
    lines.extend(_target_tool_resolution_lines(
        target_tool, preferred_tool_path, version_args, env_assignments))
    lines.extend((
        "if [ -z \"$_camc_tool\" ]; then",
        "  if [ \"$_camc_unrunnable\" -eq 1 ]; then",
        "    echo 'camc: target tool exists but failed its version check' >&2",
        "    exit 126",
        "  fi",
        "  echo 'camc: target tool was not found or is not executable' >&2",
        "  exit 127",
        "fi",
    ))
    exec_prefix = "exec"
    if env_assignments:
        exec_prefix += " /usr/bin/env " + " ".join(
            shlex.quote(item) for item in env_assignments)
    exec_args = [exec_prefix, '"$_camc_tool"'] + [
        shlex.quote(str(item)) for item in tool_args]
    if started_status_path:
        lines.append("printf '%%s\\n' %s > %s || exit 125" % (
            shlex.quote(started_transport),
            shlex.quote(started_status_path)))
    if started_path:
        lines.append(": > %s || exit 125" % shlex.quote(started_path))
    lines.append(" ".join(exec_args))
    return ["/bin/sh", "-c", "\n".join(lines) + "\n"]


def _target_tool_from_probe(stdout):
    marker = "__CAMC_TARGET_TOOL__"
    for line in str(stdout or "").splitlines():
        if line.startswith(marker):
            path = line[len(marker):].strip()
            if os.path.isabs(path) and "\n" not in path and "\r" not in path:
                return path
    return ""


def _ssh_launcher_prefix(ssh, host, tty=True):
    return [ssh, "-tt" if tty else "-T", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1", host]


def _auto_remaining_plan(candidates, battach, ssh, nvproject, qsub,
                         current_project, first_battach=0, first_ssh=0):
    """Keep fallbacks private; SSH is an independent host transport."""
    plan = []
    if battach:
        for candidate in candidates[first_battach:]:
            plan.append({"transport": "battach", "argv": [
                battach, "-L", "{command}", candidate["job_id"]]})
    if ssh:
        for candidate in candidates[first_ssh:]:
            plan.append({"transport": "ssh", "argv":
                         _ssh_launcher_prefix(ssh, candidate["host"]) +
                         ["{command}"]})
    if nvproject and qsub:
        plan.append({"transport": "qsub", "nvproject": nvproject,
                     "qsub": qsub, "project": current_project})
    return plan


def resolve_named_tool_launcher(preset_info, selected_tool, workdir, env,
                                target_tool=None, target_tool_path="",
                                target_tool_version_args=None, progress=None):
    """Resolve a named launcher to argv plus secret-free metadata/notices."""
    preset = preset_info[0]
    if preset != "auto":
        raise ValueError("unsupported named tool launcher: %s" % preset)
    report = progress or (lambda message: None)

    bjobs = _launcher_binary(env, ("/usr/local/lsf/bin/bjobs", "bjobs"))
    battach = _launcher_binary(
        env, ("/usr/local/lsf/bin/battach", "battach"))
    ssh = _launcher_binary(env, ("/bin/ssh", "ssh"))
    qsub = _launcher_binary(env, ("/home/nv/bin/qsub", "qsub"))
    nvproject = _launcher_binary(env, _NVPROJECTNAME_BINARIES)
    current_project = ""
    notices = []
    discovery_failure = ""
    target_tool = str(target_tool or selected_tool)
    if nvproject:
        try:
            current_project = _resolve_launcher_project(
                workdir, env, nvproject)
        except ValueError as exc:
            notices.append(str(exc))

    candidates = []
    user = ""
    report("battach: checking reusable autorun jobs")
    if bjobs:
        user = str((env or {}).get("USER") or
                   (env or {}).get("LOGNAME") or "")
        if not user:
            try:
                import pwd
                user = pwd.getpwuid(os.getuid()).pw_name
            except (ImportError, KeyError):
                user = str(os.getuid())
        argv = [bjobs, "-noheader", "-o", _BJOBS_FORMAT, "-u", user]
        rc, stdout, stderr = _launcher_process(
            argv, env=env, cwd=workdir, timeout=15)
        if rc:
            report("battach: job query failed (exit %s)" % rc)
            discovery_failure = (stderr or stdout or
                                 "exit %d" % rc).strip()[:200]
            notices.append("%s: bjobs discovery failed: %s" %
                           (preset, discovery_failure))
        else:
            candidates = _parse_autorun_jobs(
                stdout, selected_tool, current_project=current_project)
    else:
        report("battach: bjobs unavailable")
        notices.append("%s: bjobs not found" % preset)

    verified = []
    for candidate in candidates:
        long_rc, long_stdout, _long_stderr = _launcher_process(
            [bjobs, "-l", "-UF", candidate["job_id"]],
            env=env, cwd=workdir, timeout=10)
        if (long_rc or not _verify_autorun_job_long(
                long_stdout, candidate, expected_user=user)):
            notices.append(
                "%s: job %s metadata/EXEC_HOST changed; skipping" %
                (preset, candidate["job_id"]))
            continue
        verified.append(candidate)

    preferred_tool_path = target_tool_path
    if not verified:
        report("battach: no verified autorun job; trying ssh")
    if verified and not battach:
        report("battach: executable unavailable; trying ssh")
        notices.append("%s: battach not found" % preset)
    if battach:
        for candidate_index, candidate in enumerate(verified):
            report("battach: probing job %s on %s" %
                   (candidate["job_id"], candidate["host"]))
            probe_dir = ""
            try:
                probe_dir, probe_path = _private_script(
                    workdir, ".camc-battach-probe-",
                    _target_tool_probe_script(
                        target_tool, workdir, selected_tool=selected_tool,
                        preferred_tool_path=preferred_tool_path,
                        version_args=target_tool_version_args,
                        target_path=launcher_target_environment(env)["PATH"]))
                rc, stdout, stderr = _launcher_process(
                    [battach, "-L", probe_path, candidate["job_id"]],
                    env=env, cwd=workdir, timeout=15)
            except (IOError, OSError) as exc:
                rc, stdout, stderr = 1, "", str(exc)
            finally:
                if probe_dir:
                    shutil.rmtree(probe_dir, ignore_errors=True)
            target_tool_path = _target_tool_from_probe(stdout)
            probe_diagnostics = "%s\n%s" % (stdout or "", stderr or "")
            if rc == 0 and target_tool_path:
                report("battach: selected job %s on %s" %
                       (candidate["job_id"], candidate["host"]))
                notices.append(
                    "%s: using job %s on %s (%s)" %
                    (preset, candidate["job_id"], candidate["host"],
                     candidate["virtual_queue"] or "virtual queue unknown"))
                return ([battach, "-L", "{command}", candidate["job_id"]], {
                    "preset": preset,
                    "transport": "battach",
                    "job_id": candidate["job_id"],
                    "host": candidate["host"],
                    "project": candidate["project"],
                    "virtual_queue": candidate["virtual_queue"],
                    "fallback": candidate != candidates[0],
                    "_auto_plan": _auto_remaining_plan(
                        verified, battach, ssh, nvproject, qsub,
                        current_project, first_battach=candidate_index + 1),
                    # Consumed by cmd_run to pin launch to the executable
                    # resolved inside this same LSF job context. The CLI
                    # removes this private field before persistence.
                    "_target_tool_path": target_tool_path,
                }, notices)
            report("battach: job %s probe failed (exit %s); continuing" %
                   (candidate["job_id"], rc))
            if ("__CAMC_TARGET_TOOL_MISSING__" in probe_diagnostics or
                    "__CAMC_TARGET_TOOL_UNRUNNABLE__" in probe_diagnostics):
                if "__CAMC_TARGET_TOOL_UNRUNNABLE__" in probe_diagnostics:
                    reason = ("target tool %s could not start in the LSF job "
                              "environment" % selected_tool)
                else:
                    reason = ("target tool %s is not executable in the LSF job "
                              "environment" % target_tool)
                notices.append(
                    "%s: job %s host %s skipped: %s" %
                    (preset, candidate["job_id"], candidate["host"], reason))
            else:
                detail = (stderr or stdout or "").strip()[:200]
                notices.append(
                    "%s: job %s host %s unavailable%s" %
                    (preset, candidate["job_id"], candidate["host"],
                     (": " + detail) if detail else ""))
    if ssh:
        for candidate_index, candidate in enumerate(verified):
            report("ssh: probing %s" % candidate["host"])
            probe_body = _target_tool_probe_script(
                target_tool, workdir, selected_tool=selected_tool,
                preferred_tool_path=preferred_tool_path,
                version_args=target_tool_version_args,
                target_path=launcher_target_environment(env)["PATH"])
            probe_command = "/bin/sh -c %s" % shlex.quote(probe_body)
            rc, stdout, stderr = _launcher_process(
                _ssh_launcher_prefix(ssh, candidate["host"], tty=False) +
                [probe_command], env=env, cwd=workdir, timeout=15)
            resolved_path = _target_tool_from_probe(stdout)
            if rc == 0 and resolved_path:
                report("ssh: selected %s (host shell)" % candidate["host"])
                notices.append("auto: using SSH host %s (job %s); "
                               "host shell, not an allocation attachment" %
                               (candidate["host"], candidate["job_id"]))
                return (_ssh_launcher_prefix(ssh, candidate["host"]) +
                        ["{command}"], {
                            "preset": "auto", "transport": "ssh",
                            "job_id": candidate["job_id"],
                            "host": candidate["host"], "fallback": True,
                            "_target_tool_path": resolved_path,
                            "_auto_plan": _auto_remaining_plan(
                                verified, "", ssh, nvproject, qsub,
                                current_project, first_ssh=candidate_index + 1),
                        }, notices)
            report("ssh: %s probe failed (exit %s); continuing" %
                   (candidate["host"], rc))
            notices.append("auto: SSH host %s unavailable: %s" %
                           (candidate["host"],
                            (stderr or stdout or "exit %s" % rc).strip()[:200]))
    if preset == "auto":
        report("ssh: no usable target; trying qsub")
        report("qsub: checking executable and project")
        try:
            launcher, metadata, qsub_notices = _qsub_launcher(
                workdir, env, preset=preset)
            metadata["_auto_plan"] = []
            report("qsub: prepared; submission will run in tmux")
            return launcher, metadata, notices + qsub_notices
        except ValueError as exc:
            detail = str(exc)
            reason = ("nvprojectname unavailable" if "requires nvprojectname" in detail else
                      "qsub unavailable" if "requires qsub" in detail else
                      "project setup failed")
            report("qsub: %s; launch failed" % reason)
            raise ValueError(
                "auto could not prepare an LSF launcher (%s); "
                "use --tool-launcher none for local execution" % reason)
    raise ValueError("unsupported named tool launcher: %s" % preset)


def build_launcher_command(launcher_argv, tool_argv, bootstrap_dirs=None,
                           target_env=None, target_unset=None,
                           exec_command=True):
    """Append the safely quoted target command to a launcher prefix.

    A single standalone ``{command}`` remains supported for compatibility
    with older custom launcher templates and for internal launchers.
    """
    exec_argv = ["env"]
    for name in sorted(set(str(item) for item in (target_unset or [])
                           if _TOOL_ENV_NAME_RE.match(str(item)))):
        exec_argv += ["-u", name]
    for name in sorted((target_env or {})):
        if not _TOOL_ENV_NAME_RE.match(str(name)):
            raise ValueError("invalid launcher target environment name: %s" % name)
        if name != "PATH":
            exec_argv.append("%s=%s" % (name, str(target_env[name])))
    if len(exec_argv) == 1:
        exec_argv = []
    tool_argv = list(tool_argv)
    path_lines = _target_path_lines((target_env or {}).get("PATH"))
    if path_lines and tool_argv[:2] == ["/bin/sh", "-c"] and len(tool_argv) == 3:
        tool_argv[2] = "\n".join(path_lines + [tool_argv[2]])
        path_lines = []
    tool_command = " ".join(
        shlex.quote(str(item)) for item in exec_argv + list(tool_argv))
    directories = sorted(set(str(path) for path in (bootstrap_dirs or [])
                             if path))
    if directories:
        quoted = " ".join(shlex.quote(path) for path in directories)
        command_prefix = "exec " if exec_command else ""
        command = ("umask 077 && mkdir -p -- %s && chmod 700 -- %s && %s%s"
                   % (quoted, quoted, command_prefix, tool_command))
    else:
        command = tool_command
    if path_lines:
        command = "\n".join(path_lines + [command])
    if "{command}" in launcher_argv:
        return [command if item == "{command}" else item
                for item in launcher_argv]
    return list(launcher_argv) + [command]


def build_battach_launcher_command(launcher_argv, tool_argv, workdir,
                                   bootstrap_dirs=None, target_env=None,
                                   target_unset=None,
                                   exit_status_path=None):
    """Build a private one-shot executable for ``battach -L``.

    LSF's ``battach -L`` accepts an executable path rather than argv. A
    mode-0700 script in the shared workdir bridges that interface without
    relying on a login shell or a reconstructed PATH. The tmux pane runs the
    battach command directly. The script unlinks itself in the attached job
    before starting the tool and can record the tool's exit status on shared
    storage for camc's monitor.
    """
    placeholders = [i for i, item in enumerate(launcher_argv)
                    if item == "{command}"]
    if (len(placeholders) != 1 or placeholders[0] == 0 or
            launcher_argv[placeholders[0] - 1] != "-L"):
        raise ValueError("battach launcher must use -L {command}")

    root = os.path.abspath(workdir)
    command = build_launcher_command(
        ["{command}"], tool_argv, bootstrap_dirs=bootstrap_dirs,
        target_env=target_env, target_unset=target_unset,
        exec_command=not bool(exit_status_path))[0]
    directory = tempfile.mkdtemp(
        prefix=".camc-battach-launch-", dir=root)
    script_path = os.path.join(directory, "run")
    script_lines = [
        "#!/bin/sh",
        "/bin/rm -f -- \"$0\" || exit 126",
        "/bin/rmdir -- %s 2>/dev/null || :" % shlex.quote(directory),
        "cd %s || exit 125" % shlex.quote(root),
        command,
    ]
    if exit_status_path:
        quoted_status = shlex.quote(str(exit_status_path))
        script_lines.extend((
            "_camc_rc=$?",
            "printf '%%s\\n' \"$_camc_rc\" > %s.tmp.$$ && "
            "mv -f %s.tmp.$$ %s" % (
                quoted_status, quoted_status, quoted_status),
            "exit \"$_camc_rc\"",
        ))
    script_lines.append("")
    script_body = "\n".join(script_lines)
    try:
        with open(script_path, "w") as script:
            script.write(script_body)
        os.chmod(script_path, 0o700)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    actual_launcher = list(launcher_argv)
    actual_launcher[placeholders[0]] = script_path
    return actual_launcher, directory


def build_auto_launcher_command(launcher_argv, metadata, remote_tool_argv,
                                workdir, target_tool,
                                preferred_tool_path="", version_args=None,
                                bootstrap_dirs=None, target_env=None,
                                target_unset=None, exit_status_path=None):
    """Try transports on the submit host until a target starts the tool.

    A private shared marker distinguishes connection/submission failures from
    an agent that actually started and subsequently exited. Only the former
    advance the chain. No SSH is involved in the battach stages.
    """
    directory = tempfile.mkdtemp(prefix=".camc-auto-launch-", dir=workdir)
    marker = os.path.join(directory, "started")
    files = [marker]
    lines = ["#!/bin/sh", "umask 077"]
    primary_tool_path = launcher_target_binary(remote_tool_argv)
    stages = [{"transport": metadata["transport"], "argv": launcher_argv,
               "tool_path": (primary_tool_path
                             if os.path.isabs(primary_tool_path) else
                             preferred_tool_path)}]
    stages.extend(metadata.get("_auto_plan") or [])

    def write_script(name, body):
        path = os.path.join(directory, name)
        with open(path, "w") as script:
            script.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o700)
        files.append(path)
        return path

    def record_exit():
        if not exit_status_path:
            return []
        quoted = shlex.quote(exit_status_path)
        return ["printf '%%s\\n' \"$_camc_rc\" > %s.tmp.$$ && "
                "mv -f %s.tmp.$$ %s" % (quoted, quoted, quoted)]

    try:
        for index, stage in enumerate(stages):
            transport = stage["transport"]
            target_command = build_resolved_tool_command(
                remote_tool_argv, target_tool, workdir,
                preferred_tool_path=stage.get("tool_path") or
                preferred_tool_path, version_args=version_args,
                started_path=marker, started_transport=transport,
                started_status_path=(exit_status_path + ".launcher"
                                     if exit_status_path else ""))
            target_body = build_launcher_command(
                ["{command}"], target_command,
                bootstrap_dirs=bootstrap_dirs, target_env=target_env,
                target_unset=target_unset)[0]
            target_path = write_script("target-%d" % index, target_body)
            if transport == "battach":
                command = [target_path if item == "{command}" else item
                           for item in stage["argv"]]
                launch_line = " ".join(shlex.quote(item) for item in command)
            elif stage.get("argv"):
                command = build_launcher_command(
                    stage["argv"], ["/bin/sh", target_path])
                launch_line = " ".join(shlex.quote(item) for item in command)
            else:
                # Resolve/save the project only if the chain reaches qsub.
                nvproject = shlex.quote(stage["nvproject"])
                qsub = shlex.quote(stage["qsub"])
                project = stage.get("project") or ""
                qsub_lines = ["cd %s || return 125" % shlex.quote(workdir)]
                if project:
                    qsub_lines.append("_camc_project=%s" % shlex.quote(project))
                else:
                    qsub_lines.extend([
                        "_camc_output=$(%s resolve_validate 2>&1)" % nvproject,
                        "_camc_project_rc=$?",
                        "if [ \"$_camc_project_rc\" -ne 0 ]; then",
                        "  case \"$_camc_output\" in",
                        "    *UnregisteredProjectNameException*)",
                        "      case \"$_camc_output\" in",
                        "        *\"project_name: ''\"*|*'project_name: \"\"'*)",
                        "          %s save ./ gpu_gr102g_hw_mmplex_riscv || return $?" % nvproject,
                        "          _camc_output=$(%s resolve_validate 2>&1) || return $?" % nvproject,
                        "          ;;",
                        "        *) printf '%s\\n' \"$_camc_output\" >&2; return 1 ;;",
                        "      esac ;;",
                        "    *) printf '%s\\n' \"$_camc_output\" >&2; return 1 ;;",
                        "  esac",
                        "fi",
                        "_camc_project=$(printf '%s\\n' \"$_camc_output\" | "
                        "awk 'NF {last=$0} END {print last}')",
                        "case \"$_camc_project\" in ''|*[!A-Za-z0-9_.-]*) return 1 ;; esac",
                    ])
                target_arg = shlex.quote("/bin/sh %s" % shlex.quote(target_path))
                qsub_lines.append(
                    '%s run "$_camc_project" %s -q o_cpu_4G -m rel8x '
                    '-app r89docker --qsla autorun_sla -app affinity -Is %s' %
                    (nvproject, qsub, target_arg))
                lines.extend(["_camc_qsub() {"] + qsub_lines + ["}"])
                launch_line = "_camc_qsub"
            lines.append("printf '%s\\n' %s" % (
                "%s", shlex.quote("[launcher] %s: starting" % transport)))
            lines.append(launch_line)
            lines.append("_camc_rc=$?")
            lines.append("if [ -f %s ]; then" % shlex.quote(marker))
            if exit_status_path:
                lines.append("printf '%s\\n' %s > %s" % (
                    "%s", shlex.quote(transport),
                    shlex.quote(exit_status_path + ".launcher")))
            lines.extend(record_exit())
            lines.extend(["exit \"$_camc_rc\"", "fi"])
            lines.append("printf '[launcher] %s: failed (exit %%s); trying next\\n' \"$_camc_rc\"" % transport)
        lines.append("printf '%s\\n' 'camc: no LSF launcher started the tool; local fallback disabled' >&2")
        lines.append("_camc_rc=125")
        lines.extend(record_exit())
        lines.append("exit \"$_camc_rc\"")
        runner = os.path.join(directory, "run")
        files.append(runner)
        cleanup = "/bin/rm -f -- %s; /bin/rmdir -- %s 2>/dev/null || :" % (
            " ".join(shlex.quote(path) for path in files),
            shlex.quote(directory))
        lines.insert(2, "trap %s EXIT" % shlex.quote(cleanup))
        write_script("run", "\n".join(lines[1:]))
        return [runner], directory
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def launcher_target_binary(command):
    """Return the target-side executable name from an adapter command."""
    index = 0
    if command and command[0] == "env":
        index = 1
        while (index < len(command) and "=" in command[index]
               and not command[index].startswith("-")):
            index += 1
    return command[index] if index < len(command) else ""


def replace_launcher_target_binary(command, target_binary):
    """Return a copy of adapter argv with its executable slot replaced."""
    result = list(command or [])
    index = 0
    if result and result[0] == "env":
        index = 1
        while (index < len(result) and "=" in result[index]
               and not result[index].startswith("-")):
            index += 1
    if index < len(result) and target_binary:
        result[index] = str(target_binary)
    return result


def without_launcher_auto_permission(command):
    """Remove Claude's legacy auto mode before adding native autorun."""
    result = list(command)
    for index in range(len(result) - 1):
        if result[index:index + 2] == ["--permission-mode", "auto"]:
            del result[index:index + 2]
            break
    return result
