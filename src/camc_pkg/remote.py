"""Remote execution: SSH to machines, run camc commands, sync files."""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile


def _ssh_control_path(user, host, port):
    """Compute SSH ControlMaster socket path (matches CamcDelegate)."""
    conn_key = "%s@%s:%s" % (user or "default", host, port or 22)
    conn_hash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
    return "/tmp/cam-ssh-%s" % conn_hash


def _ssh_conn_opts(machine, control_path=None):
    """SSH connection option list for a machine (-p/-i/-o ...).

    control_path overrides the ControlMaster socket location; the default
    is the shared auto-master path used by ssh_run/scp."""
    port = machine.get("port")
    key_file = machine.get("key_file")
    control = control_path or _ssh_control_path(
        machine.get("user"), machine.get("host"), port)
    opts = []
    if port:
        opts += ["-p", str(port)]
    if key_file:
        opts += ["-i", key_file]
    opts += [
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ControlPath=%s" % control,
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
    ]
    return opts


def _ssh_base_cmd(machine):
    """Build base SSH command list for a machine."""
    host = machine.get("host")
    user = machine.get("user")
    cmd = ["ssh"] + _ssh_conn_opts(machine)
    target = "%s@%s" % (user, host) if user else host
    return cmd, target


def ssh_run(machine, remote_cmd, timeout=30, input_data=None):
    """Run a command on a remote machine via SSH.

    Returns (returncode, stdout_str).
    """
    cmd, target = _ssh_base_cmd(machine)
    if input_data:
        cmd += ["-T", target, "bash"]
    else:
        cmd += [target, remote_cmd]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
            input=input_data,
        )
        output = proc.stdout
        if proc.returncode != 0 and not output.strip():
            output = proc.stderr
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        return -1, ""
    except Exception as e:
        return -1, str(e)


def _remote_camc_prefix(machine):
    """Remote camc invocation prefix, honoring per-machine overrides:
    camc_path (default ~/.cam/camc) and remote_home (prefixes HOME=<path>
    so the remote camc reads a different ~/.cam — used for isolated
    deployments and tests)."""
    camc_remote = machine.get("camc_path") or "~/.cam/camc"
    remote_home = machine.get("remote_home")
    env = "HOME=%s " % shlex.quote(remote_home) if remote_home else ""
    return env, camc_remote


def ssh_camc(machine, args, timeout=30):
    """Run a camc command on a remote machine.

    Returns (returncode, stdout_str).
    """
    env, camc_remote = _remote_camc_prefix(machine)
    quoted = " ".join(shlex.quote(a) for a in args)
    # Check for non-ASCII args (Chinese prompts etc)
    has_non_ascii = any(not a.isascii() for a in args)
    if has_non_ascii:
        script = "#!/bin/bash\n%spython3 %s %s\n" % (env, camc_remote, quoted)
        return ssh_run(machine, None, timeout=timeout, input_data=script)
    else:
        remote_cmd = "%spython3 %s %s" % (env, camc_remote, quoted)
        return ssh_run(machine, remote_cmd, timeout=timeout)


def ssh_camc_json(machine, args, timeout=30):
    """Run a camc --json command and parse output."""
    rc, out = ssh_camc(machine, ["--json"] + args, timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def ssh_ping(machine, timeout=10):
    """Test SSH connectivity to a machine. Returns True if reachable."""
    rc, _ = ssh_run(machine, "echo ok", timeout=timeout)
    return rc == 0


def sync_file(machine, local_path, remote_path, timeout=30):
    """Copy a local file to a remote machine via SSH. Returns True on success."""
    host = machine.get("host")
    user = machine.get("user")
    port = machine.get("port")
    key_file = machine.get("key_file")
    control = _ssh_control_path(user, host, port)
    target = "%s@%s" % (user, host) if user else host

    cmd = ["scp", "-q"]
    if port:
        cmd += ["-P", str(port)]
    if key_file:
        cmd += ["-i", key_file]
    cmd += [
        "-o", "ControlPath=%s" % control,
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
        "-o", "StrictHostKeyChecking=no",
        local_path, "%s:%s" % (target, remote_path),
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout)
        return proc.returncode == 0
    except Exception:
        return False


def sync_camc_to_machine(machine, camc_path=None, configs_dir=None):
    """Sync camc binary and configs to a remote machine.

    Returns dict of {filename: status} where status is 'deployed', 'unchanged', or 'failed'.
    """
    from camc_pkg import CAM_DIR, CONFIGS_DIR
    if camc_path is None:
        camc_path = os.path.join(CAM_DIR, "camc")
        if not os.path.exists(camc_path):
            # Development fallback: use the sole generated dist/camc artifact.
            script_dir = os.path.dirname(os.path.abspath(__file__))
            camc_path = os.path.join(os.path.dirname(os.path.dirname(script_dir)),
                                     "dist", "camc")
    if configs_dir is None:
        configs_dir = CONFIGS_DIR

    # Remote layout: honors remote_home / camc_path overrides (isolated
    # deployments, tests); defaults to the classic ~/.cam/camc layout.
    remote_base = "%s/.cam" % machine["remote_home"] if machine.get("remote_home") else "~/.cam"
    remote_camc = machine.get("camc_path") or "%s/camc" % remote_base

    results = {}

    # Ensure remote dirs exist
    ssh_run(machine, "mkdir -p %s/configs %s/logs %s/pids"
            % (remote_base, remote_base, remote_base), timeout=10)

    # Compute local hashes
    def _md5(path):
        if not os.path.exists(path):
            return None
        import hashlib
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    # Get remote hashes
    rc, remote_hashes_raw = ssh_run(machine,
        "md5sum %s %s/configs/*.toml 2>/dev/null || true" % (remote_camc, remote_base),
        timeout=10)
    remote_hashes = {}
    for line in remote_hashes_raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            remote_hashes[os.path.basename(parts[1])] = parts[0]

    # Sync camc binary
    local_hash = _md5(camc_path)
    if local_hash and local_hash != remote_hashes.get("camc"):
        if sync_file(machine, camc_path, remote_camc):
            ssh_run(machine, "chmod +x %s" % remote_camc, timeout=5)
            results["camc"] = "deployed"
        else:
            results["camc"] = "failed"
    elif local_hash:
        results["camc"] = "unchanged"

    # Sync TOML configs
    if os.path.isdir(configs_dir):
        for fname in os.listdir(configs_dir):
            if not fname.endswith(".toml"):
                continue
            local_path = os.path.join(configs_dir, fname)
            lh = _md5(local_path)
            if lh and lh != remote_hashes.get(fname):
                if sync_file(machine, local_path, "%s/configs/%s" % (remote_base, fname)):
                    results[fname] = "deployed"
                else:
                    results[fname] = "failed"
            elif lh:
                results[fname] = "unchanged"

    # Sync machine's env_setup as context.json. Use scp instead of a
    # remote heredoc because many PDX/DC accounts default to csh/tcsh,
    # where heredoc handling can leave the terminator in the file.
    env_setup = machine.get("env_setup")
    if env_setup:
        ctx_json = json.dumps({"env_setup": env_setup}, indent=2) + "\n"
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="camc-context-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                f.write(ctx_json)
            results["context.json"] = (
                "deployed" if sync_file(machine, tmp_path, "%s/context.json" % remote_base) else "failed")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return results
