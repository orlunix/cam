"""Transport layer: tmux session management (create, capture, send, kill)."""

import os
import shlex
import subprocess

from camc_pkg import SOCKETS_DIR, log
from camc_pkg.utils import strip_ansi, _run


def _find_tmux_socket(session_id):
    server_sock_dir = os.path.expanduser("~/.local/share/cam/sockets")
    for sock_dir in (SOCKETS_DIR, "/tmp/cam-agent-sockets", server_sock_dir):
        sock = "%s/%s.sock" % (sock_dir, session_id)
        if os.path.exists(sock):
            return sock
    return None


def _tmux_base(session_id):
    socket = _find_tmux_socket(session_id)
    return ["tmux", "-u", "-S", socket] if socket else ["tmux"]


def capture_tmux(session_id, lines=100):
    socket = _find_tmux_socket(session_id)
    target = "%s:0.0" % session_id
    if socket:
        args = ["tmux", "-u", "-S", socket, "capture-pane", "-p", "-J",
                "-t", target, "-S", "-%d" % lines]
    else:
        args = ["tmux", "capture-pane", "-p", "-J",
                "-t", target, "-S", "-%d" % lines]
    try:
        rc, output = _run(args)
        if len(output.strip()) < 20:
            alt = list(args)
            alt.insert(alt.index("capture-pane") + 1, "-a")
            rc2, alt_out = _run(alt)
            if len(alt_out.strip()) > len(output.strip()):
                output = alt_out
        return strip_ansi(output).rstrip()
    except Exception as e:
        log.debug("capture_tmux failed: %s", e)
        return ""


def _tmux_cmd():
    """Find tmux binary — use full path for cron compatibility."""
    import shutil
    return shutil.which("tmux") or "tmux"


def tmux_session_exists(session_id):
    tmux = _tmux_cmd()
    socket = _find_tmux_socket(session_id)
    if socket:
        args = [tmux, "-u", "-S", socket, "has-session", "-t", session_id]
    else:
        args = [tmux, "has-session", "-t", session_id]
    rc, _ = _run(args, timeout=15)
    if rc != 0 and socket:
        # Socket-based check failed — retry without socket (maybe socket is stale)
        args = [tmux, "has-session", "-t", session_id]
        rc, _ = _run(args, timeout=15)
    return rc == 0


def tmux_send_input(session_id, text, send_enter=True):
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    try:
        if text:
            _run(base + ["send-keys", "-t", target, "-l", "--", text], check=True)
        if send_enter:
            _run(base + ["send-keys", "-t", target, "Enter"], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_input failed: %s", e)
        return False


def tmux_kill_session(session_id):
    socket = _find_tmux_socket(session_id)
    if socket:
        args = ["tmux", "-u", "-S", socket, "kill-session", "-t", session_id]
    else:
        args = ["tmux", "kill-session", "-t", session_id]
    rc, _ = _run(args)
    return rc == 0


def create_tmux_session(session_id, command, workdir, env_setup=None, inherit_env=True):
    try:
        os.makedirs(SOCKETS_DIR)
    except OSError:
        pass
    socket = "%s/%s.sock" % (SOCKETS_DIR, session_id)

    # Unset TMUX so we can create sessions from inside tmux (nesting)
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    # Unset CLAUDECODE to prevent nested-session detection
    env.pop("CLAUDECODE", None)

    if inherit_env:
        # Shell mode: start tmux with user's default shell, inherit all env.
        # Then send the command via send-keys.
        try:
            tmux_cmd = ["tmux", "-u", "-S", socket, "new-session",
                 "-d", "-x", "220", "-y", "50",
                 "-s", session_id, "-c", workdir]
            proc = subprocess.Popen(
                tmux_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env,
            )
            proc.wait(timeout=10)
            if proc.returncode != 0:
                return False
            _run(["tmux", "-u", "-S", socket, "set-option", "-t", session_id,
                  "history-limit", "50000"])
            # Send the command via send-keys
            inner_cmd = " ".join(shlex.quote(arg) for arg in command)
            target = "%s:0.0" % session_id
            _run(["tmux", "-u", "-S", socket, "send-keys", "-t", target,
                  "-l", "--", inner_cmd])
            _run(["tmux", "-u", "-S", socket, "send-keys", "-t", target, "Enter"])
            return True
        except Exception as e:
            log.error("Failed to create shell session %s: %s", session_id, e)
            return False

    inner_cmd = " ".join(shlex.quote(arg) for arg in command)
    if not env_setup:
        env_setup = "export PATH=%s" % shlex.quote(os.environ.get("PATH", ""))
    command_str = "env -u CLAUDECODE bash -l -c %s" % shlex.quote(env_setup + " && exec " + inner_cmd)

    try:
        proc = subprocess.Popen(
            ["tmux", "-u", "-S", socket, "new-session",
             "-d", "-x", "220", "-y", "50",
             "-s", session_id, "-c", workdir, command_str],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
        proc.wait(timeout=10)
        if proc.returncode != 0:
            return False
        _run(["tmux", "-u", "-S", socket, "set-option", "-t", session_id,
              "history-limit", "50000"])
        return True
    except Exception as e:
        log.error("Failed to create session %s: %s", session_id, e)
        return False
