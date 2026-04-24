"""Transport layer: tmux session management (create, capture, send, kill)."""

import os
import shlex
import shutil
import subprocess

from camc_pkg import SOCKETS_DIR, log
from camc_pkg.utils import strip_ansi, _run


def _detect_tmux_bin():
    """Current `tmux` on PATH, used for new session creation and as the
    last-resort fallback when we can't figure out a session's tmux."""
    return shutil.which("tmux") or "tmux"


TMUX_BIN = _detect_tmux_bin()


def _find_tmux_socket(session_id):
    server_sock_dir = os.path.expanduser("~/.local/share/cam/sockets")
    for sock_dir in (SOCKETS_DIR, "/tmp/cam-agent-sockets", server_sock_dir):
        sock = "%s/%s.sock" % (sock_dir, session_id)
        if os.path.exists(sock):
            return sock
    return None


def _tmux_server_pid_for_socket(sock_path):
    """Return PID of a tmux server whose argv references this socket path,
    or None. Used to guard os.unlink calls: a socket file may be missing
    from ls while a tmux server process is still bound to it (then tmux
    becomes a zombie unreachable via filesystem). Always check ps before
    unlinking so we don't orphan a running server.
    """
    if not sock_path:
        return None
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(
                        "utf-8", errors="replace")
            except (OSError, IOError):
                continue
            if "tmux" not in cmdline or sock_path not in cmdline:
                continue
            try:
                return int(entry)
            except ValueError:
                return None
    except OSError:
        pass
    return None


def _tmux_bin_from_exe(sock_path):
    """Read the tmux binary from /proc/<server-pid>/exe. None if no live
    server or exe is unreadable."""
    pid = _tmux_server_pid_for_socket(sock_path)
    if not pid:
        return None
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return None
    if not exe or exe.endswith(" (deleted)"):
        return None
    return exe


def _tmux_bin_for_session(session_id):
    """Resolve which tmux binary owns this session.

    Mismatched tmux client/server versions fail silently (capture returns
    empty, attach hangs). We avoid that by sticking with whatever binary
    started the session:

      1. agents.json record's `tmux_bin` (set at creation time, or
         self-healed from a past /proc probe)
      2. readlink(/proc/<server-pid>/exe)
      3. TMUX_BIN (fallback: no record and no live server)

    On a successful /proc probe we write the result (and `tmux -V`
    string) back to the agent record — subsequent lookups skip the scan.
    """
    from camc_pkg.storage import AgentStore
    store = AgentStore()
    agent = store.get(session_id)
    if agent:
        rec = agent.get("tmux_bin")
        if rec and os.path.exists(rec):
            return rec

    socket = _find_tmux_socket(session_id)
    if socket:
        detected = _tmux_bin_from_exe(socket)
        if detected:
            if agent:
                agent["tmux_bin"] = detected
                try:
                    ver_out = subprocess.check_output(
                        [detected, "-V"], stderr=subprocess.STDOUT, timeout=2
                    ).decode(errors="replace").strip()
                    if ver_out:
                        agent["tmux_version"] = ver_out
                except Exception:
                    pass
                try:
                    store.save(agent)
                except Exception:
                    pass
            return detected
    return TMUX_BIN


def _tmux_base(session_id):
    socket = _find_tmux_socket(session_id)
    tmux = _tmux_bin_for_session(session_id)
    return [tmux, "-u", "-S", socket] if socket else [tmux]


def capture_tmux(session_id, lines=100):
    """Capture tmux pane content.

    lines > 0: last N lines (visible + scrollback up to N).
    lines <= 0 (or None): full scrollback buffer (tmux -S -). The actual
    amount returned is capped by tmux's history-limit (50000 in our
    created sessions). A full-scroll capture of a long-lived agent with
    hundreds of KB of history can take several seconds, so we use a
    generous timeout in that case — the default 5s kills mid-capture
    and returns empty.
    """
    socket = _find_tmux_socket(session_id)
    target = "%s:0.0" % session_id
    # `-S -`  = start of history; `-S -N` = N lines back. We pass one or
    # the other based on the `lines` argument.
    full_scroll = not lines or lines <= 0
    start_flag = "-" if full_scroll else "-%d" % lines
    # Users occasionally run many long-lived agents out of one workdir;
    # their scrollback can reach several MB. Give full-scroll captures
    # plenty of headroom — the operation is bounded and the caller is
    # waiting on output anyway. Bounded captures use the _run default.
    timeout = 60 if full_scroll else 15
    tmux = _tmux_bin_for_session(session_id)
    if socket:
        args = [tmux, "-u", "-S", socket, "capture-pane", "-p", "-J",
                "-t", target, "-S", start_flag]
    else:
        args = [tmux, "capture-pane", "-p", "-J",
                "-t", target, "-S", start_flag]
    try:
        rc, output = _run(args, timeout=timeout)
        if len(output.strip()) < 20:
            alt = list(args)
            alt.insert(alt.index("capture-pane") + 1, "-a")
            rc2, alt_out = _run(alt, timeout=timeout)
            if len(alt_out.strip()) > len(output.strip()):
                output = alt_out
        return strip_ansi(output).rstrip()
    except Exception as e:
        log.debug("capture_tmux failed: %s", e)
        return ""


def _tmux_cmd():
    """Backwards-compat alias; returns the already-resolved TMUX_BIN."""
    return TMUX_BIN


def tmux_session_exists(session_id):
    tmux = _tmux_bin_for_session(session_id)
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
            # Brief pause so the literal text is flushed into the pane
            # buffer before Enter arrives. On some tmux builds (notably
            # older ones over slow NFS/SSH) back-to-back subprocess
            # send-keys can race, and Enter gets swallowed before the
            # text lands — the prompt stays half-typed.
            if text:
                import time as _t
                _t.sleep(0.15)
            _run(base + ["send-keys", "-t", target, "Enter"], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_input failed: %s", e)
        return False


def tmux_send_key(session_id, key):
    """Send a tmux key (e.g. 'BSpace', 'Enter', 'Escape') to a session."""
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    try:
        _run(base + ["send-keys", "-t", target, key], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_key failed: %s", e)
        return False


def tmux_is_attached(session_id):
    """Check if a user is attached to this tmux session."""
    base = _tmux_base(session_id)
    try:
        rc, output = _run(base + ["display-message", "-p", "-t", session_id,
                                  "#{session_attached}"])
        if rc == 0 and output.strip():
            return int(output.strip()) > 0
    except (ValueError, Exception):
        pass
    return False


def tmux_kill_session(session_id):
    socket = _find_tmux_socket(session_id)
    tmux = _tmux_bin_for_session(session_id)
    if socket:
        args = [tmux, "-u", "-S", socket, "kill-session", "-t", session_id]
    else:
        args = [tmux, "kill-session", "-t", session_id]
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
            tmux_cmd = [TMUX_BIN, "-u", "-S", socket, "new-session",
                 "-d", "-x", "220", "-y", "50",
                 "-s", session_id, "-c", workdir]
            proc = subprocess.Popen(
                tmux_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            _, stderr = proc.communicate(timeout=10)
            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip() if stderr else "exit code %d" % proc.returncode
                log.error("tmux new-session failed: %s", err_msg)
                return False
            _run([TMUX_BIN, "-u", "-S", socket, "set-option", "-t", session_id,
                  "history-limit", "50000"])
            # Send the command via send-keys. Small pause before Enter
            # so the literal text flushes before Enter races in.
            inner_cmd = " ".join(shlex.quote(arg) for arg in command)
            target = "%s:0.0" % session_id
            _run([TMUX_BIN, "-u", "-S", socket, "send-keys", "-t", target,
                  "-l", "--", inner_cmd])
            import time as _t
            _t.sleep(0.15)
            _run([TMUX_BIN, "-u", "-S", socket, "send-keys", "-t", target, "Enter"])
            return True
        except subprocess.TimeoutExpired:
            log.error("tmux new-session timed out after 10s for %s", session_id)
            return False
        except Exception as e:
            log.error("Failed to create shell session %s: %s", session_id, e)
            return False

    inner_cmd = " ".join(shlex.quote(arg) for arg in command)
    if not env_setup:
        env_setup = "export PATH=%s" % shlex.quote(os.environ.get("PATH", ""))
    command_str = "env -u CLAUDECODE bash -l -c %s" % shlex.quote(env_setup + " && exec " + inner_cmd)

    try:
        proc = subprocess.Popen(
            [TMUX_BIN, "-u", "-S", socket, "new-session",
             "-d", "-x", "220", "-y", "50",
             "-s", session_id, "-c", workdir, command_str],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        _, stderr = proc.communicate(timeout=10)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip() if stderr else "exit code %d" % proc.returncode
            log.error("tmux new-session failed: %s", err_msg)
            return False
        _run([TMUX_BIN, "-u", "-S", socket, "set-option", "-t", session_id,
              "history-limit", "50000"])
        return True
    except subprocess.TimeoutExpired:
        log.error("tmux new-session timed out after 10s for %s", session_id)
        return False
    except Exception as e:
        log.error("Failed to create session %s: %s", session_id, e)
        return False
