"""Replaceable tool-launcher policy and command construction.

This module owns every launcher-specific decision: raw template parsing,
named presets, LSF discovery/ranking, SSH probing, qsub fallback, target
command quoting, API compatibility, and launcher-record detection.  CLI and
monitor code should contain integration hooks only.
"""

import os
import re
import shlex
import shutil
import subprocess


_NAMED_TOOL_LAUNCHER_RE = re.compile(
    r"^(auto|qsub-4g|ssh-auto(?::([0-9]+))?)$")
_AUTORUN_SHELLS = frozenset(("bash", "csh", "sh", "tcsh", "zsh"))
_TOOL_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BJOBS_FORMAT = (
    "jobid stat service_class interactive exec_host proj_name "
    "job_name command job_description delimiter='|'")


def parse_tool_launcher(value):
    """Parse one raw launcher template and enforce its argv contract."""
    if value is None:
        return []
    try:
        argv = shlex.split(str(value), posix=True)
    except ValueError as exc:
        raise ValueError("--tool-launcher: %s" % exc)
    if not argv:
        raise ValueError("--tool-launcher must not be empty")
    count = sum(1 for item in argv if item == "{command}")
    if count != 1:
        raise ValueError(
            "--tool-launcher must contain exactly one standalone {command} argv element")
    return argv


def named_tool_launcher(value):
    """Return ``(preset, explicit_job_id)`` for one named launcher."""
    text = str(value or "").strip()
    if not text or "{command}" in text:
        return None
    match = _NAMED_TOOL_LAUNCHER_RE.match(text)
    if not match:
        return None
    preset = "ssh-auto" if text.startswith("ssh-auto") else text
    return preset, (match.group(2) or "")


def validate_tool_launcher_api(value, api_plan):
    """Remote launchers cannot reach the submit host's loopback proxy."""
    if value and api_plan:
        raise ValueError(
            "--tool-launcher is not compatible with an API profile yet: "
            "the per-agent proxy listens on this host only; use "
            "--no-default-api for remote launchers")


def record_uses_tool_launcher(record):
    """Return whether an agent record was created with a tool launcher."""
    runtime = record.get("runtime") if isinstance(record, dict) else None
    return isinstance(runtime, dict) and "launcher" in runtime


def launcher_target_environment(runtime_env, storage_info=None):
    """Build the minimal explicit environment sent to the launcher target."""
    result = {}
    storage = storage_info if isinstance(storage_info, dict) else {}
    name = str(storage.get("environment") or "")
    if name and _TOOL_ENV_NAME_RE.match(name):
        result[name] = str((runtime_env or {}).get(name) or "")
    return result


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
    return match.group(1) if match else ""


def _parse_autorun_jobs(output, selected_tool, current_project="",
                        explicit_job_id=""):
    """Parse and rank safe user-owned autorun SSH candidates."""
    candidates = []
    for raw in str(output or "").splitlines():
        fields = raw.rstrip("\n").split("|", 8)
        if len(fields) != 9:
            continue
        (job_id, status, service_class, interactive, host, project,
         job_name, command, description) = [item.strip() for item in fields]
        if explicit_job_id and job_id != str(explicit_job_id):
            continue
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


def _resolve_launcher_project(workdir, env, nvproject_bin):
    rc, stdout, stderr = _launcher_process(
        [nvproject_bin, "resolve_validate"], env=env, cwd=workdir, timeout=10)
    project = next((line.strip() for line in reversed(stdout.splitlines())
                    if line.strip()), "")
    if rc or not re.match(r"^[A-Za-z0-9_.-]+$", project):
        detail = (stderr or stdout or "project could not be resolved").strip()
        raise ValueError("auto launcher project resolution failed: %s" % detail)
    return project


def _qsub_launcher(workdir, env, preset="qsub-4g"):
    nvproject = _launcher_binary(env, (
        "/home/nv/utils/nvprojectname/bin/nvprojectname",
        "/home/nv/bin/nvprojectname", "nvprojectname"))
    qsub = _launcher_binary(env, ("/home/nv/bin/qsub", "qsub"))
    if not nvproject:
        raise ValueError("%s launcher requires nvprojectname" % preset)
    if not qsub:
        raise ValueError("%s launcher requires qsub" % preset)
    project = _resolve_launcher_project(workdir, env, nvproject)
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


def _target_tool_probe_command(target_tool):
    """Return one safe remote-shell probe for a real executable file."""
    quoted_tool = shlex.quote(str(target_tool or ""))
    script = (
        "_camc_tool=$(command -v %s 2>/dev/null || true); "
        "if [ -n \"$_camc_tool\" ] && [ -f \"$_camc_tool\" ] && "
        "[ -x \"$_camc_tool\" ]; then exit 0; fi; "
        "printf '%%s\\n' __CAMC_TARGET_TOOL_MISSING__ >&2; exit 127"
        % quoted_tool)
    return "/bin/sh -c %s" % shlex.quote(script)


def resolve_named_tool_launcher(preset_info, selected_tool, workdir, env,
                                target_tool=None):
    """Resolve a named launcher to argv plus secret-free metadata/notices."""
    preset, explicit_job_id = preset_info
    if preset == "qsub-4g":
        return _qsub_launcher(workdir, env, preset=preset)

    bjobs = _launcher_binary(env, ("/usr/local/lsf/bin/bjobs", "bjobs"))
    ssh = _launcher_binary(env, ("/usr/bin/ssh", "/bin/ssh", "ssh"))
    nvproject = _launcher_binary(env, (
        "/home/nv/utils/nvprojectname/bin/nvprojectname",
        "/home/nv/bin/nvprojectname", "nvprojectname"))
    current_project = ""
    notices = []
    discovery_failure = ""
    explicit_failure = ""
    target_tool = str(target_tool or selected_tool)
    if nvproject:
        try:
            current_project = _resolve_launcher_project(
                workdir, env, nvproject)
        except ValueError as exc:
            notices.append(str(exc))

    candidates = []
    user = ""
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
        if explicit_job_id:
            argv.append(explicit_job_id)
        rc, stdout, stderr = _launcher_process(
            argv, env=env, cwd=workdir, timeout=15)
        if rc:
            discovery_failure = (stderr or stdout or
                                 "exit %d" % rc).strip()[:200]
            notices.append("%s: bjobs discovery failed: %s" %
                           (preset, discovery_failure))
        else:
            candidates = _parse_autorun_jobs(
                stdout, selected_tool, current_project=current_project,
                explicit_job_id=explicit_job_id)
    else:
        notices.append("%s: bjobs not found" % preset)

    if explicit_job_id and not candidates:
        if discovery_failure:
            raise ValueError(
                "ssh-auto job %s discovery failed: %s" %
                (explicit_job_id, discovery_failure))
        raise ValueError(
            "ssh-auto job %s is not a RUN interactive autorun_sla job" %
            explicit_job_id)
    if candidates and not ssh:
        notices.append("%s: ssh not found" % preset)
    if ssh:
        for candidate_index, candidate in enumerate(candidates):
            long_argv = [bjobs, "-l", "-UF", candidate["job_id"]]
            long_rc, long_stdout, _long_stderr = _launcher_process(
                long_argv, env=env, cwd=workdir, timeout=10)
            if (long_rc or not _verify_autorun_job_long(
                    long_stdout, candidate, expected_user=user)):
                notices.append(
                    "%s: job %s metadata/EXEC_HOST changed; skipping" %
                    (preset, candidate["job_id"]))
                if explicit_job_id:
                    raise ValueError(
                        "ssh-auto job %s metadata or EXEC_HOST changed" %
                        explicit_job_id)
                continue
            probe = [
                ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "ConnectionAttempts=1", candidate["host"],
                _target_tool_probe_command(target_tool),
            ]
            rc, _stdout, stderr = _launcher_process(
                probe, env=env, cwd=workdir, timeout=8)
            if rc == 0:
                notices.append(
                    "%s: using job %s on %s (%s)" %
                    (preset, candidate["job_id"], candidate["host"],
                     candidate["virtual_queue"] or "virtual queue unknown"))
                return ([ssh, "-tt", candidate["host"], "{command}"], {
                    "preset": preset,
                    "transport": "ssh",
                    "job_id": candidate["job_id"],
                    "host": candidate["host"],
                    "project": candidate["project"],
                    "virtual_queue": candidate["virtual_queue"],
                    "fallback": bool(candidate_index),
                }, notices)
            if "__CAMC_TARGET_TOOL_MISSING__" in stderr:
                reason = "target tool %s is not executable in SSH environment" % target_tool
                explicit_failure = "ssh-auto job %s host is reachable but %s" % (
                    candidate["job_id"], reason)
                notices.append(
                    "%s: job %s host %s skipped: %s" %
                    (preset, candidate["job_id"], candidate["host"], reason))
            else:
                notices.append(
                    "%s: job %s host %s unavailable%s" %
                    (preset, candidate["job_id"], candidate["host"],
                     (": " + stderr.strip()[:200]) if stderr.strip() else ""))
            if explicit_job_id:
                break

    if preset == "auto":
        launcher, metadata, qsub_notices = _qsub_launcher(
            workdir, env, preset=preset)
        return launcher, metadata, notices + qsub_notices
    if explicit_job_id:
        if explicit_failure:
            raise ValueError(explicit_failure)
        raise ValueError("ssh-auto job %s host is not reachable" % explicit_job_id)
    detail = "; ".join(notices[-3:])
    raise ValueError(
        "ssh-auto found no reachable autorun host%s" %
        ((": " + detail) if detail else ""))


def build_launcher_command(launcher_argv, tool_argv, bootstrap_dirs=None,
                           target_env=None, target_unset=None):
    """Replace the placeholder with one safely quoted target command argv."""
    exec_argv = ["env"]
    for name in sorted(set(str(item) for item in (target_unset or [])
                           if _TOOL_ENV_NAME_RE.match(str(item)))):
        exec_argv += ["-u", name]
    for name in sorted((target_env or {})):
        if not _TOOL_ENV_NAME_RE.match(str(name)):
            raise ValueError("invalid launcher target environment name: %s" % name)
        exec_argv.append("%s=%s" % (name, str(target_env[name])))
    if len(exec_argv) == 1:
        exec_argv = []
    tool_command = " ".join(
        shlex.quote(str(item)) for item in exec_argv + list(tool_argv))
    directories = sorted(set(str(path) for path in (bootstrap_dirs or [])
                             if path))
    if directories:
        quoted = " ".join(shlex.quote(path) for path in directories)
        command = ("umask 077 && mkdir -p -- %s && chmod 700 -- %s && exec %s"
                   % (quoted, quoted, tool_command))
    else:
        command = tool_command
    return [command if item == "{command}" else item
            for item in launcher_argv]


def launcher_target_binary(command):
    """Return the target-side executable name from an adapter command."""
    index = 0
    if command and command[0] == "env":
        index = 1
        while (index < len(command) and "=" in command[index]
               and not command[index].startswith("-")):
            index += 1
    return command[index] if index < len(command) else ""


def without_launcher_auto_permission(command):
    """Remove Claude local auto mode before launcher-owned autorun."""
    result = list(command)
    for index in range(len(result) - 1):
        if result[index:index + 2] == ["--permission-mode", "auto"]:
            del result[index:index + 2]
            break
    return result
