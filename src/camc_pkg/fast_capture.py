"""Minimal capture fast path.

This module intentionally imports only stdlib modules and must not import
camc_pkg.cli/storage/formatters. It is used before the normal camc startup
path so Desktop output polling can avoid the full CLI import cost.
"""

import json
import os
import re
import subprocess
import sys




_HEX = set("0123456789abcdefABCDEF")


def is_exact_agent_id(value):
    """True when value is an exact 8-char hex agent id."""
    if not value or len(value) != 8:
        return False
    return all(ch in _HEX for ch in value)


def _same_host_short(left, right):
    if not left or not right:
        return True
    if left == right:
        return True
    return left.split(".", 1)[0] == right.split(".", 1)[0]


def _default_socket_for_session(session):
    if not session:
        return None
    candidates = [
        os.path.join("/tmp/cam-sockets", session + ".sock"),
        os.path.join("/tmp/cam-agent-sockets", session + ".sock"),
        os.path.join(os.path.expanduser("~"), ".local", "share", "cam", "sockets", session + ".sock"),
    ]
    for sock in candidates:
        if os.path.exists(sock):
            return sock
    return None


def lookup_agent_from_agents_json(agent_id, path=None, my_hostname=None):
    """Return capture metadata for an exact agent id from ~/.cam/agents.json.

    This is intentionally a narrow fast-path helper, not the full AgentStore
    resolver. It supports only exact 8-hex ids and falls back to None on any
    unreadable or unexpected JSON shape.
    """
    if not is_exact_agent_id(agent_id):
        return None
    path = path or os.path.join(os.path.expanduser("~"), ".cam", "agents.json")
    try:
        with open(path, "r") as f:
            agents = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(agents, list):
        return None
    my_hostname = my_hostname or _hostname()
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("id") != agent_id:
            continue
        if not _same_host_short(agent.get("hostname") or "", my_hostname or ""):
            return None
        session = agent.get("tmux_session") or agent.get("session") or ""
        if not session:
            return None
        socket_path = agent.get("tmux_socket") or _default_socket_for_session(session)
        tmux_bin = agent.get("tmux_bin") or ("/bin/tmux" if os.path.exists("/bin/tmux") else "tmux")
        return {
            "session": session,
            "socket": socket_path,
            "tmux_bin": tmux_bin,
        }
    return None


def _hostname():
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return ""

_FAST_ANSI_RE = re.compile(
    r"\x1B\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1B\][^\x07]*\x07"
    r"|\x1B[()][AB012]"
    r"|\x1B[@-_]"
)


def _fast_strip_ansi(text):
    return _FAST_ANSI_RE.sub("", text)


def _fast_err(stderr, msg):
    stderr.write(msg + "\n")



def _parse_id_capture_argv(argv, stderr):
    """Parse simple `capture <8hex-id> [--lines N] [--format plain|ansi]`.

    Returns None when argv needs the full CLI (prefix ids, --json, tmux
    metadata flags, or unknown options).
    """
    opts = {
        "agent_id": None,
        "lines": 0,
        "format": "plain",
    }
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--lines", "-n"):
            i += 1
            if i >= len(argv):
                _fast_err(stderr, tok + " requires a value")
                return None
            try:
                opts["lines"] = int(argv[i])
            except ValueError:
                _fast_err(stderr, tok + " must be an integer")
                return None
            i += 1
            continue
        if tok == "--format":
            i += 1
            if i >= len(argv):
                _fast_err(stderr, "--format requires a value")
                return None
            opts["format"] = argv[i].lower()
            i += 1
            continue
        if tok in ("--fast", "--no-fast-path", "--json"):
            return None
        if tok.startswith("--tmux-"):
            return None
        if tok.startswith("-"):
            return None
        if opts["agent_id"] is not None:
            return None
        if not is_exact_agent_id(tok):
            return None
        opts["agent_id"] = tok
        i += 1
        continue
    if not opts["agent_id"]:
        return None
    if opts["format"] not in ("plain", "ansi"):
        _fast_err(stderr, "--format must be plain or ansi")
        return None
    return opts


def fast_capture_from_values(session, socket_path=None, tmux_bin=None, lines=0,
                             fmt="plain", json_output=False,
                             stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if not session:
        _fast_err(stderr, "tmux session is required")
        return 2
    fmt = (fmt or "plain").lower()
    if fmt not in ("plain", "ansi"):
        _fast_err(stderr, "--format must be plain or ansi")
        return 2
    try:
        lines = int(lines or 0)
    except (TypeError, ValueError):
        _fast_err(stderr, "--lines must be an integer")
        return 2

    tmux = tmux_bin or "tmux"
    if tmux_bin and not os.path.exists(tmux_bin):
        _fast_err(stderr, "tmux binary not found: " + tmux_bin)
        return 1
    if socket_path and not os.path.exists(socket_path):
        _fast_err(stderr, "tmux socket not found: " + socket_path)
        return 1

    full_scroll = not lines or lines <= 0
    start_flag = "-" if full_scroll else "-%d" % lines
    timeout = 60 if full_scroll else 15
    target = "%s:0.0" % session
    base = [tmux, "-u"]
    if socket_path:
        base += ["-S", socket_path]
    args = base + ["capture-pane", "-p", "-J"]
    if fmt == "ansi":
        args.append("-e")
    args += ["-t", target, "-S", start_flag]

    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        _fast_err(stderr, "tmux capture timed out")
        return 1
    except OSError as e:
        _fast_err(stderr, "tmux capture failed: %s" % e)
        return 1

    if p.returncode != 0:
        msg = err.decode("utf-8", errors="replace").strip()
        _fast_err(stderr, msg or "tmux capture failed")
        return p.returncode or 1

    text = out.decode("utf-8", errors="replace")
    if len(text.strip()) < 20:
        alt = list(args)
        alt.insert(alt.index("capture-pane") + 1, "-a")
        try:
            p2 = subprocess.Popen(alt, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            alt_out, _ = p2.communicate(timeout=timeout)
            alt_text = alt_out.decode("utf-8", errors="replace")
            if p2.returncode == 0 and len(alt_text.strip()) > len(text.strip()):
                text = alt_text
        except Exception:
            pass

    if fmt != "ansi":
        text = _fast_strip_ansi(text)
    text = text.rstrip()
    if json_output:
        import hashlib
        stdout.write(json.dumps({
            "content": text,
            "hash": hashlib.md5(text.encode("utf-8")).hexdigest()[:8],
            "format": fmt,
        }, indent=2))
        stdout.write("\n")
    else:
        stdout.write(text)
    return 0


def early_capture_main(argv=None, stdout=None, stderr=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if not argv:
        return None
    # Only default exact-id capture is handled here. Complex forms, --json,
    # prefixes, names, and unsupported flags fall through to the Python CLI.
    json_output = False
    if argv and argv[0] == "--json":
        json_output = True
        argv = argv[1:]
    if not argv or argv[0] != "capture":
        return None

    rest = argv[1:]
    if json_output:
        return None

    id_opts = _parse_id_capture_argv(rest, stderr)
    if not id_opts:
        return None
    meta = lookup_agent_from_agents_json(id_opts["agent_id"])
    if not meta:
        return None
    rc = fast_capture_from_values(
        meta.get("session"),
        socket_path=meta.get("socket"),
        tmux_bin=meta.get("tmux_bin"),
        lines=id_opts.get("lines"),
        fmt=id_opts.get("format"),
        json_output=False,
        stdout=stdout,
        stderr=stderr,
    )
    if rc:
        return None
    return rc
