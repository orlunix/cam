"""Remote execution: SSH to machines, run camc commands, sync files."""

import hashlib
import json
import os
import shlex
import subprocess
import sys


def _ssh_control_path(user, host, port):
    """Compute SSH ControlMaster socket path (matches CamcDelegate)."""
    conn_key = "%s@%s:%s" % (user or "default", host, port or 22)
    conn_hash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
    return "/tmp/cam-ssh-%s" % conn_hash


def _ssh_base_cmd(machine):
    """Build base SSH command list for a machine."""
    host = machine.get("host")
    user = machine.get("user")
    port = machine.get("port")
    key_file = machine.get("key_file")
    control = _ssh_control_path(user, host, port)
    cmd = ["ssh"]
    if port:
        cmd += ["-p", str(port)]
    if key_file:
        cmd += ["-i", key_file]
    cmd += [
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ControlPath=%s" % control,
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
    ]
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
            cmd, capture_output=True, text=True, timeout=timeout,
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


def ssh_camc(machine, args, timeout=30):
    """Run a camc command on a remote machine.

    Returns (returncode, stdout_str).
    """
    camc_remote = "~/.cam/camc"
    quoted = " ".join(shlex.quote(a) for a in args)
    # Check for non-ASCII args (Chinese prompts etc)
    has_non_ascii = any(not a.isascii() for a in args)
    if has_non_ascii:
        script = "#!/bin/bash\npython3 %s %s\n" % (camc_remote, quoted)
        return ssh_run(machine, None, timeout=timeout, input_data=script)
    else:
        remote_cmd = "python3 %s %s" % (camc_remote, quoted)
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
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
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
            # Try dist/camc relative to the script
            script_dir = os.path.dirname(os.path.abspath(__file__))
            camc_path = os.path.join(os.path.dirname(script_dir), "camc")
    if configs_dir is None:
        configs_dir = CONFIGS_DIR

    results = {}

    # Ensure remote dirs exist
    ssh_run(machine, "mkdir -p ~/.cam/configs ~/.cam/logs ~/.cam/pids", timeout=10)

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
        "md5sum ~/.cam/camc ~/.cam/configs/*.toml 2>/dev/null || true", timeout=10)
    remote_hashes = {}
    for line in remote_hashes_raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            remote_hashes[os.path.basename(parts[1])] = parts[0]

    # Sync camc binary
    local_hash = _md5(camc_path)
    if local_hash and local_hash != remote_hashes.get("camc"):
        if sync_file(machine, camc_path, "~/.cam/camc"):
            ssh_run(machine, "chmod +x ~/.cam/camc", timeout=5)
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
                if sync_file(machine, local_path, "~/.cam/configs/%s" % fname):
                    results[fname] = "deployed"
                else:
                    results[fname] = "failed"
            elif lh:
                results[fname] = "unchanged"

    # Sync machine's env_setup as context.json
    env_setup = machine.get("env_setup")
    if env_setup:
        ctx_json = json.dumps({"env_setup": env_setup}, indent=2)
        rc2, _ = ssh_run(machine,
            "cat > ~/.cam/context.json << 'CAMEOF'\n%s\nCAMEOF" % ctx_json, timeout=10)
        results["context.json"] = "deployed" if rc2 == 0 else "failed"

    return results
