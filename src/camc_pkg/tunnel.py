"""Machine tunnels — persistent per-machine SSH tunnels, direct remote
delegation, and mailbox fallback.

For every SSH machine in ~/.cam/machines.json, a keeper daemon
(``camc tunnel up <name>`` → hidden ``_tunnel_keep``) maintains one
persistent ``ssh -N -L`` forward to a ``camc tunnel serve`` endpoint on
the remote node (127.0.0.1 only, token-authed). Targeted commands such
as ``camc capture mymachine:b1`` are routed here before the fleet
bridge: with a fresh tunnel the request is a synchronous JSON-line
round-trip (bridge-protocol-v1 §10 frame); with the tunnel down the
request is queued in ~/.cam/tunnels/<m>/outbox/ (exit 2, request
preserved) and flushed by the keeper when the tunnel returns — replies
land in inbox/ (``camc tunnel inbox``).

Layout:

    ~/.cam/tunnel/token                 serve auth token (0600)
    ~/.cam/tunnel/serve.json            serve instance card {pid, port, ts}
    ~/.cam/tunnels/<m>.json             keeper state card (60s TTL)
    ~/.cam/tunnels/<m>.token            fetched remote token (0600)
    ~/.cam/tunnels/<m>/outbox/<id>.json queued requests (tmp→rename)
    ~/.cam/tunnels/<m>/inbox/<id>.json  flushed replies
    ~/.cam/pids/tunnel-<m>.pid          keeper pid
    ~/.cam/logs/tunnel-<m>.log          keeper log
    ~/.cam/logs/tunnel-serve.log        serve log
    ~/.cam/logs/tunnel-serve-audit.jsonl  serve audit (never logs tokens)

stdlib-only.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import time
import uuid

from camc_pkg import CAM_DIR, LOGS_DIR, PIDS_DIR, TUNNEL_DIR, TUNNELS_DIR

DEFAULT_SERVE_PORT = 42177
LOCAL_PORT_BASE = 42200
LOCAL_PORT_SPAN = 100
CARD_TTL_S = 60
KEEPER_LOOP_S = 2.0
RECONNECT_MIN_S = 5.0
RECONNECT_MAX_S = 60.0
FRAME_CAP = 4 * 1024 * 1024
ALLOWED_VERBS = ("list", "status", "capture", "send", "key", "msg")
# Same target-position rule as the fleet bridge (bridge-protocol-v1 §6):
# status/capture/send/key take argv[1], `msg send` takes argv[2].
_TARGETED_VERBS = ("status", "capture", "send", "key")
_GLOBAL_FLAGS = ("--json", "-v", "--verbose")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _serve_token_path():
    return os.path.join(TUNNEL_DIR, "token")


def _serve_card_path():
    return os.path.join(TUNNEL_DIR, "serve.json")


def _serve_log_path():
    return os.path.join(LOGS_DIR, "tunnel-serve.log")


def _audit_path():
    return os.path.join(LOGS_DIR, "tunnel-serve-audit.jsonl")


def _check_name(name):
    if not name or not _NAME_RE.match(str(name)):
        raise ValueError("bad machine name %r" % (name,))
    return str(name)


def _machine_home(name):
    return os.path.join(TUNNELS_DIR, _check_name(name))


def _card_path(name):
    return os.path.join(TUNNELS_DIR, _check_name(name) + ".json")


def _m_token_path(name):
    return os.path.join(TUNNELS_DIR, _check_name(name) + ".token")


def _outbox_dir(name):
    return os.path.join(_machine_home(name), "outbox")


def _inbox_dir(name):
    return os.path.join(_machine_home(name), "inbox")


def _keeper_pid_path(name):
    return os.path.join(PIDS_DIR, "tunnel-%s.pid" % _check_name(name))


def _keeper_log_path(name):
    return os.path.join(LOGS_DIR, "tunnel-%s.log" % _check_name(name))


def _ensure_dir(d, mode=0o700):
    if not os.path.isdir(d):
        os.makedirs(d, mode)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _port_open(port, timeout=0.25):
    try:
        socket.create_connection(("127.0.0.1", int(port)),
                                 timeout=timeout).close()
        return True
    except OSError:
        return False


def _atomic_write_json(path, obj, mode=0o600):
    _ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.write("\n")
    os.rename(tmp, path)


def _camc_cmd():
    """Command prefix to run this camc as a subprocess."""
    deployed = os.path.join(CAM_DIR, "camc")
    if os.path.isfile(deployed):
        return [sys.executable, deployed]
    script = os.path.abspath(sys.argv[0])
    if os.path.isfile(script) and not script.endswith("__main__.py"):
        return [sys.executable, script]
    return [sys.executable, "-m", "camc_pkg"]


def _call_timeout_s():
    raw = os.environ.get("CAMC_TUNNEL_TIMEOUT_MS")
    if raw:
        try:
            return max(0.5, float(raw) / 1000.0)
        except ValueError:
            pass
    return 30.0


def _mailbox_wait_s():
    raw = os.environ.get("CAMC_TUNNEL_MAILBOX_MS")
    if raw:
        try:
            return max(0.0, float(raw) / 1000.0)
        except ValueError:
            pass
    return 5.0


# ---------------------------------------------------------------------------
# Machine prefix resolution
# ---------------------------------------------------------------------------


def _machine_prefixes(m):
    """All acceptable '<prefix>:' forms for a machine record, longest
    first: name, user@host:port, host:port, user@host, host."""
    prefs = set()
    name = m.get("name")
    host = m.get("host")
    user = m.get("user")
    port = m.get("port") or 22
    if name:
        prefs.add(str(name))
    if host:
        prefs.add("%s:%s" % (host, port))
        prefs.add(str(host))
        if user:
            prefs.add("%s@%s:%s" % (user, host, port))
            prefs.add("%s@%s" % (user, host))
    return sorted(prefs, key=len, reverse=True)


def resolve_machine(ref, store=None):
    """Longest matching machine prefix in '<prefix>:<rest>'.
    Returns (machine, prefix, rest) or None."""
    if not ref or ":" not in ref:
        return None
    if store is None:
        from camc_pkg.machine_store import MachineStore
        store = MachineStore()
    best = None
    for m in store.list_ssh():
        for p in _machine_prefixes(m):
            if ref.startswith(p + ":"):
                if best is None or len(p) > len(best[1]):
                    best = (m, p, ref[len(p) + 1:])
    return best


def _target_pos(argv):
    """Index of the target ref in a normalized verb-first argv, or None."""
    i = 0
    while i < len(argv) and argv[i] in _GLOBAL_FLAGS:
        i += 1
    if i >= len(argv):
        return None
    verb = argv[i]
    if verb in _TARGETED_VERBS:
        pos = i + 1
    elif verb == "msg" and len(argv) > i + 1 and argv[i + 1] == "send":
        pos = i + 2
    else:
        return None
    return pos if pos < len(argv) else None


def _strip_target_prefix(argv, machine):
    """Remove this machine's '<prefix>:' from the target arg so the remote
    camc sees a bare ref (the fleet host strips node: the same way)."""
    argv = list(argv)
    pos = _target_pos(argv)
    if pos is None:
        return argv
    ref = argv[pos]
    for p in _machine_prefixes(machine):
        if ref.startswith(p + ":"):
            argv[pos] = ref[len(p) + 1:]
            return argv
    return argv


# ---------------------------------------------------------------------------
# Client side: route + submit (tunnel fast path → mailbox fallback)
# ---------------------------------------------------------------------------


def tunnel_route(argv, store=None):
    """Pre-dispatch decision for machine-prefixed targets. Returns
    (machine, ferry_argv) or None to fall through to the fleet bridge /
    local dispatch. Bare refs never route here (fleet hub semantics
    unchanged)."""
    if not argv:
        return None
    globals_seen = []
    i = 0
    while i < len(argv) and argv[i] in _GLOBAL_FLAGS:
        globals_seen.append(argv[i])
        i += 1
    if i >= len(argv):
        return None
    verb = argv[i]
    rest = argv[i + 1:]
    if verb in _TARGETED_VERBS:
        if not rest or rest[0].startswith("-"):
            return None
        ref = rest[0]
    elif verb == "msg":
        if len(rest) < 2 or rest[0] != "send" or rest[1].startswith("-"):
            return None
        ref = rest[1]
    else:
        return None
    hit = resolve_machine(ref, store)
    if hit is None:
        return None
    from camc_pkg.bridge import _ferry_argv
    return (hit[0], _ferry_argv(verb, rest, globals_seen))


def _load_card(name):
    try:
        with open(_card_path(name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, IOError, ValueError):
        return None


def card_fresh(card, now=None):
    try:
        return ((time.time() if now is None else now)
                - float(card.get("ts") or 0)) <= CARD_TTL_S
    except (ValueError, TypeError, AttributeError):
        return False


def _read_m_token(name):
    try:
        with open(_m_token_path(name), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, IOError):
        return ""


def _new_request(machine, argv):
    from camc_pkg.bridge import _from_agent
    return {
        "id": uuid.uuid4().hex,
        "argv": list(argv),
        "from": {"agent": _from_agent()},
        "ts": time.time(),
        "proto": 1,
        "machine": machine.get("name"),
    }


def _write_inbox(name, req_id, resp, req=None):
    out = dict(resp)
    if req is not None:
        out.setdefault("argv", req.get("argv"))
        out.setdefault("machine", name)
    out["flushed_at"] = time.time()
    _atomic_write_json(os.path.join(_inbox_dir(name), req_id + ".json"), out)


def tunnel_flush(machine, call_fn=None):
    """Flush queued outbox requests over the tunnel; each response lands in
    inbox/ and its request is deleted. Stops at the first transport
    failure (the tunnel is effectively down; the rest wait for the next
    pass). Returns (sent, remaining)."""
    name = machine.get("name")
    outbox = _outbox_dir(name)
    try:
        names = sorted(n for n in os.listdir(outbox) if n.endswith(".json"))
    except OSError:
        return (0, 0)
    total = len(names)
    sent = 0
    for n in names:
        card = _load_card(name)
        if (not card or card.get("state") != "up"
                or not card_fresh(card)):
            break
        port = card.get("local_port")
        token = _read_m_token(name)
        if not isinstance(port, int) or port <= 0 or not token:
            break
        try:
            with open(os.path.join(outbox, n), "r", encoding="utf-8") as fh:
                req = json.load(fh)
        except (OSError, IOError, ValueError):
            continue
        send_req = dict(req)
        send_req["argv"] = _strip_target_prefix(req.get("argv") or [],
                                                machine)
        if call_fn is not None:
            resp = call_fn(port, token, send_req)
        else:
            from camc_pkg.bridge import _tunnel_call
            resp = _tunnel_call(port, token, send_req,
                                time.time() + _call_timeout_s())
        if resp is None:
            break  # transport failure: tunnel effectively down
        try:
            _write_inbox(name, req.get("id") or n[:-5], resp, req)
        except (OSError, IOError):
            break
        try:
            os.unlink(os.path.join(outbox, n))
        except OSError:
            pass
        sent += 1
    return (sent, total - sent)


def tunnel_submit(machine, argv):
    """Send argv to a remote machine's camc. Tunnel fast path (fresh state
    card + token) → synchronous response, exit 0/1. Any transport failure
    → queue into ~/.cam/tunnels/<m>/outbox/, poll inbox briefly in case
    the keeper is mid-flush, then exit 2 with the request preserved."""
    from camc_pkg.bridge import _emit_response, _resolve_send_io, _tunnel_call
    name = machine.get("name")
    argv = _resolve_send_io(argv)
    req = _new_request(machine, argv)

    card = _load_card(name)
    token = _read_m_token(name)
    port = (card or {}).get("local_port")
    if (card and card.get("state") == "up" and card_fresh(card)
            and isinstance(port, int) and port > 0 and token):
        send_req = dict(req)
        send_req["argv"] = _strip_target_prefix(argv, machine)
        resp = _tunnel_call(port, token, send_req,
                            time.time() + _call_timeout_s())
        if resp is not None:
            return _emit_response(resp)

    # Mailbox fallback (source of truth when the tunnel is down).
    _ensure_dir(_outbox_dir(name))
    _ensure_dir(_inbox_dir(name))
    tmp = os.path.join(_outbox_dir(name), req["id"] + ".json.tmp")
    final = os.path.join(_outbox_dir(name), req["id"] + ".json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(req, fh)
    os.rename(tmp, final)

    resp_path = os.path.join(_inbox_dir(name), req["id"] + ".json")
    deadline = time.time() + _mailbox_wait_s()
    while time.time() < deadline:
        if os.path.isfile(resp_path):
            try:
                with open(resp_path, "r", encoding="utf-8") as fh:
                    resp = json.load(fh)
            except (OSError, IOError, ValueError):
                time.sleep(0.2)
                continue
            try:
                os.unlink(resp_path)
            except OSError:
                pass
            return _emit_response(resp)
        time.sleep(0.2)

    sys.stderr.write(
        "camc: tunnel to '%s' unavailable; request queued to mailbox "
        "(id %s)\n" % (name, req["id"][:8]))
    sys.stderr.write(
        "camc: the keeper flushes when the tunnel is back "
        "('camc tunnel up %s'); replies: 'camc tunnel inbox %s'\n"
        % (name, name))
    return 2


# ---------------------------------------------------------------------------
# Serve side: token-authed camc executor on 127.0.0.1
# ---------------------------------------------------------------------------


def _load_or_create_serve_token(path=None):
    path = path or _serve_token_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except (OSError, IOError):
        pass
    tok = secrets.token_hex(16)
    _ensure_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tok + "\n")
    return tok


def _exec_camc_argv(argv, timeout):
    """Run camc argv locally; returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            _camc_cmd() + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ds" % timeout
    except Exception as e:
        return 1, "", str(e)


def _audit(path, entry):
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except (OSError, IOError):
        pass


def _read_frame(conn):
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = conn.recv(65536)
        except (OSError, socket.timeout):
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > FRAME_CAP:
            return None
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError:
        return None


class _TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(30)
            frame = _read_frame(self.request)
            resp = self.server.tunnel_dispatch(frame)
            self.request.sendall(
                json.dumps(resp).encode("utf-8") + b"\n")
        except OSError:
            pass


class TunnelServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, token, exec_fn=None, audit_path=None):
        self.token = token
        self.exec_fn = exec_fn or _exec_camc_argv
        self.audit = audit_path if audit_path is not None else _audit_path()
        super().__init__(("127.0.0.1", port), _TunnelHandler)

    def tunnel_dispatch(self, frame):
        if not isinstance(frame, dict):
            return {"id": None, "ok": False, "error": "bad frame"}
        req = frame.get("request")
        req_id = req.get("id") if isinstance(req, dict) else None
        if not hmac.compare_digest(str(frame.get("token") or ""),
                                   self.token):
            return {"id": req_id, "ok": False, "error": "unauthorized"}
        resp = self._handle_request(req)
        return resp

    def _handle_request(self, req):
        if not isinstance(req, dict):
            return {"id": None, "ok": False, "error": "missing request"}
        req_id = req.get("id")
        argv = req.get("argv")
        proto = req.get("proto", 1)
        entry = {"ts": time.time(), "id": req_id, "argv": argv,
                 "from": (req.get("from") or {}).get("agent"),
                 "machine": req.get("machine"), "ok": False,
                 "error": None}
        try:
            if not isinstance(proto, int) or proto > 1:
                entry["error"] = "unsupported proto %s" % proto
            elif not isinstance(argv, list) or not argv:
                entry["error"] = "missing argv"
            else:
                entry["error"] = self._validate(argv)
                if entry["error"] is None:
                    timeout = 120 if argv[0] == "msg" else 30
                    rc, out, err = self.exec_fn(argv, timeout)
                    entry["ok"] = rc == 0
                    if rc == 0:
                        return {"id": req_id, "ok": True, "proto": 1,
                                "text": (out or "").rstrip("\n")}
                    entry["error"] = (err or "").strip() or \
                        (out or "").strip() or "exit %d" % rc
            resp = {"id": req_id, "ok": False, "proto": 1,
                    "error": entry["error"]}
            return resp
        finally:
            _audit(self.audit, entry)

    @staticmethod
    def _validate(argv):
        verb = argv[0]
        if verb not in ALLOWED_VERBS:
            return ("unsupported verb '%s' via tunnel "
                    "(list, status, capture, send, key, msg)" % verb)
        if verb == "msg" and (len(argv) < 2 or argv[1] != "send"):
            return "unsupported msg form via tunnel (only 'msg send')"
        pos = _target_pos(argv)
        if pos is not None and ":" in str(argv[pos]):
            return ("machine prefix not stripped by client: '%s'"
                    % argv[pos])
        return None


def tunnel_serve(port=None, ensure=False, exec_fn=None):
    """Run the tunnel endpoint. With ensure=True, idempotently start a
    detached serve process if one is not already running, print
    'ok <port>' and return."""
    if port is None:
        raw = os.environ.get("CAMC_TUNNEL_PORT")
        if raw:
            try:
                port = int(raw)
            except ValueError:
                pass
    if port is None:
        port = DEFAULT_SERVE_PORT
    if ensure:
        return _serve_ensure(port)
    token = _load_or_create_serve_token()
    try:
        srv = TunnelServer(port, token, exec_fn=exec_fn)
    except OSError as e:
        sys.stderr.write("camc: tunnel serve cannot bind 127.0.0.1:%d: %s\n"
                         % (port, e))
        return 1
    _atomic_write_json(_serve_card_path(),
                       {"pid": os.getpid(), "port": port, "ts": time.time()})
    sys.stderr.write("camc: tunnel serve listening on 127.0.0.1:%d\n" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        for p in (_serve_card_path(),):
            try:
                os.unlink(p)
            except OSError:
                pass
    return 0


def _serve_ensure(port):
    card = None
    try:
        with open(_serve_card_path(), "r", encoding="utf-8") as fh:
            card = json.load(fh)
    except (OSError, IOError, ValueError):
        pass
    if (card and _pid_alive(card.get("pid"))
            and _port_open(card.get("port"))):
        sys.stdout.write("ok %s\n" % card.get("port"))
        return 0
    _ensure_dir(LOGS_DIR)
    logfh = open(_serve_log_path(), "a")
    proc = subprocess.Popen(
        _camc_cmd() + ["tunnel", "serve", "--port", str(port)],
        stdout=logfh, stderr=logfh, stdin=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _port_open(port):
            sys.stdout.write("ok %d\n" % port)
            return 0
        time.sleep(0.3)
    sys.stderr.write("camc: tunnel serve failed to start on port %d; "
                     "see %s\n" % (port, _serve_log_path()))
    return 1


# ---------------------------------------------------------------------------
# Keeper: one persistent ssh -N -L per machine, with reconnect + flush
# ---------------------------------------------------------------------------


def _pick_local_port(machine, probe=None):
    probe = probe or _port_open
    want = machine.get("tunnel_port")
    if want:
        return int(want)
    card = _load_card(machine.get("name"))
    prev = (card or {}).get("local_port")
    if isinstance(prev, int) and prev > 0 and not probe(prev):
        return prev
    name = str(machine.get("name") or "")
    start = int(hashlib.sha256(name.encode("utf-8")).hexdigest(), 16)
    for i in range(LOCAL_PORT_SPAN):
        cand = LOCAL_PORT_BASE + (start + i) % LOCAL_PORT_SPAN
        if not probe(cand):
            return cand
    raise RuntimeError("no free local tunnel port")


def _tunnel_control_path(machine):
    user = machine.get("user")
    host = machine.get("host")
    port = machine.get("port")
    conn_key = "%s@%s:%s" % (user or "default", host, port or 22)
    chash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
    return "/tmp/cam-ssh-tun-%s" % chash


def _tunnel_ssh_cmd(machine, local_port, remote_port):
    """Dedicated-ControlPath ssh -N -L command (never shares remote.py's
    auto-master socket: a slave connection cannot add forwards)."""
    from camc_pkg.remote import _ssh_conn_opts
    user = machine.get("user")
    host = machine.get("host")
    control = _tunnel_control_path(machine)
    opts = _ssh_conn_opts(machine, control_path=control)
    # Replace the auto-master/persist pair with an explicit master that
    # lives exactly as long as this process. Options are flat "-o VALUE"
    # pairs, so filter pairwise to avoid orphaning a bare "-o".
    filtered = []
    i = 0
    while i < len(opts):
        if opts[i] == "-o" and i + 1 < len(opts):
            v = opts[i + 1]
            if v.startswith(("ControlMaster=", "ControlPersist=")):
                i += 2
                continue
            filtered += ["-o", v]
            i += 2
        else:
            filtered.append(opts[i])
            i += 1
    opts = filtered
    opts += ["-o", "ControlMaster=yes", "-o", "ControlPersist=yes",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
             "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes"]
    target = "%s@%s" % (user, host) if user else host
    return (["ssh", "-N", "-T",
             "-L", "127.0.0.1:%d:127.0.0.1:%d" % (local_port, remote_port)]
            + opts + [target])


def tunnel_keep_main(machine_name, store=None):
    """Keeper daemon main (hidden `_tunnel_keep <machine>`)."""
    from camc_pkg.machine_store import MachineStore
    from camc_pkg.remote import ssh_camc, ssh_run, sync_camc_to_machine
    store = store or MachineStore()
    m = store.get(machine_name)
    if not m or m.get("type") != "ssh":
        sys.stderr.write("camc: no ssh machine '%s'\n" % machine_name)
        return 1
    name = m.get("name")
    _ensure_dir(TUNNELS_DIR)
    _ensure_dir(PIDS_DIR)
    _ensure_dir(LOGS_DIR)
    _atomic_write_json(_keeper_pid_path(name), {"pid": os.getpid()})

    stopped = {"flag": False}

    def _on_term(signum, frame):
        stopped["flag"] = True
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    backoff = RECONNECT_MIN_S
    reconnects = 0
    synced = False
    proc = None
    try:
        while not stopped["flag"]:
            m = store.get(name)
            if not m or m.get("type") != "ssh":
                break  # machine record removed: self-terminate
            _atomic_write_json(_card_path(name), {
                "machine": name, "state": "connecting",
                "pid": os.getpid(), "reconnects": reconnects,
                "last_error": None, "ts": time.time()})

            rc, out = ssh_camc(
                m, ["tunnel", "serve", "--ensure"], timeout=90)
            remote_port = 0
            if rc == 0:
                mm = re.search(r"\bok\s+(\d+)\b", out or "")
                if mm:
                    remote_port = int(mm.group(1))
            if not remote_port:
                if not synced:
                    try:
                        sync_camc_to_machine(m)
                    except Exception:
                        pass
                    synced = True
                _atomic_write_json(_card_path(name), {
                    "machine": name, "state": "down", "pid": os.getpid(),
                    "reconnects": reconnects,
                    "last_error": "remote serve ensure failed (rc=%s)"
                                  % rc,
                    "ts": time.time()})
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
                continue

            rc, tok = ssh_run(
                m, "cat %s/.cam/tunnel/token 2>/dev/null"
                   % (m.get("remote_home") or "~"), timeout=15)
            token = (tok or "").strip()
            if rc != 0 or not token:
                _atomic_write_json(_card_path(name), {
                    "machine": name, "state": "down", "pid": os.getpid(),
                    "reconnects": reconnects,
                    "last_error": "token fetch failed (rc=%s)" % rc,
                    "ts": time.time()})
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
                continue
            fd = os.open(_m_token_path(name),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")

            try:
                local_port = _pick_local_port(m)
            except RuntimeError as e:
                _atomic_write_json(_card_path(name), {
                    "machine": name, "state": "down", "pid": os.getpid(),
                    "reconnects": reconnects, "last_error": str(e),
                    "ts": time.time()})
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
                continue

            logfh = open(_keeper_log_path(name), "a")
            logfh.write("%s connect: local %d -> %s:%d\n"
                        % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                           local_port, m.get("host"), remote_port))
            logfh.flush()
            proc = subprocess.Popen(
                _tunnel_ssh_cmd(m, local_port, remote_port),
                stdin=subprocess.DEVNULL, stdout=logfh, stderr=logfh,
                start_new_session=False)  # stay in keeper's kill group

            up = False
            for _ in range(20):
                if proc.poll() is not None or stopped["flag"]:
                    break
                if _port_open(local_port):
                    up = True
                    break
                time.sleep(0.5)
            if not up:
                try:
                    proc.kill()
                except OSError:
                    pass
                _atomic_write_json(_card_path(name), {
                    "machine": name, "state": "down", "pid": os.getpid(),
                    "reconnects": reconnects,
                    "last_error": "forward did not come up",
                    "ts": time.time()})
                reconnects += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
                continue

            backoff = RECONNECT_MIN_S
            synced = False
            since = time.time()
            while not stopped["flag"] and proc.poll() is None:
                _atomic_write_json(_card_path(name), {
                    "machine": name, "state": "up",
                    "local_port": local_port, "remote_port": remote_port,
                    "pid": os.getpid(), "ssh_pid": proc.pid,
                    "since": since, "reconnects": reconnects,
                    "last_error": None, "ts": time.time()})
                if os.path.isdir(_outbox_dir(name)):
                    try:
                        tunnel_flush(m)
                    except Exception:
                        pass
                time.sleep(KEEPER_LOOP_S)
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            reconnects += 1
            if not stopped["flag"]:
                _atomic_write_json(_card_path(name), {
                    "machine": name, "state": "down", "pid": os.getpid(),
                    "reconnects": reconnects,
                    "last_error": "ssh exited rc=%s" % proc.returncode,
                    "ts": time.time()})
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        state = "stopped" if stopped["flag"] else "down"
        try:
            _atomic_write_json(_card_path(name), {
                "machine": name, "state": state, "pid": os.getpid(),
                "reconnects": reconnects, "last_error": None,
                "ts": time.time()})
        except (OSError, IOError):
            pass
        try:
            os.unlink(_keeper_pid_path(name))
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------------------
# CLI-facing operations (cmd_tunnel backs these)
# ---------------------------------------------------------------------------


def tunnel_keeper_running(name):
    try:
        with open(_keeper_pid_path(name), "r", encoding="utf-8") as fh:
            pid = json.load(fh).get("pid")
    except (OSError, IOError, ValueError):
        return False
    return _pid_alive(pid)


def tunnel_up(machines):
    """Start keeper daemons. Returns (started, skipped, errors)."""
    started, skipped, errors = [], [], []
    _ensure_dir(LOGS_DIR)
    _ensure_dir(PIDS_DIR)
    for m in machines:
        name = m.get("name")
        try:
            _check_name(name)
        except ValueError:
            errors.append((name, "bad machine name"))
            continue
        if tunnel_keeper_running(name):
            skipped.append(name)
            continue
        logfh = open(_keeper_log_path(name), "a")
        try:
            proc = subprocess.Popen(
                _camc_cmd() + ["_tunnel_keep", name],
                stdout=logfh, stderr=logfh, stdin=subprocess.DEVNULL,
                start_new_session=True)
            _atomic_write_json(_keeper_pid_path(name), {"pid": proc.pid})
            started.append(name)
        except OSError as e:
            errors.append((name, str(e)))
    return started, skipped, errors


def tunnel_down(machine):
    """Stop one keeper (and its ssh child). Returns True if something was
    stopped. `machine` is the machines.json record (or a plain name)."""
    if isinstance(machine, str):
        from camc_pkg.machine_store import MachineStore
        machine = MachineStore().get(machine) or {"name": machine}
    name = machine.get("name")
    stopped_any = False
    try:
        with open(_keeper_pid_path(name), "r", encoding="utf-8") as fh:
            pid = json.load(fh).get("pid")
    except (OSError, IOError, ValueError):
        pid = None
    if pid and _pid_alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        if _pid_alive(pid):
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        stopped_any = True
    card = _load_card(name)
    ssh_pid = (card or {}).get("ssh_pid")
    if ssh_pid and _pid_alive(ssh_pid):
        try:
            os.kill(int(ssh_pid), signal.SIGTERM)
        except OSError:
            pass
        stopped_any = True
    # Sweep any orphaned tunnel master (e.g. left behind by a crashed
    # keeper whose pid file is gone): ask the control socket to exit.
    if machine.get("host"):
        try:
            subprocess.run(
                ["ssh", "-O", "exit", "-S", _tunnel_control_path(machine),
                 "%s@%s" % (machine["user"], machine["host"])
                 if machine.get("user") else machine["host"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        os.unlink(_keeper_pid_path(name))
    except OSError:
        pass
    if card:
        card["state"] = "stopped"
        card["ts"] = time.time()
        try:
            _atomic_write_json(_card_path(name), card)
        except (OSError, IOError):
            pass
    return stopped_any


def tunnel_status(store=None):
    """State rows for every SSH machine: [{name, state, ...}]."""
    from camc_pkg.machine_store import MachineStore
    store = store or MachineStore()
    rows = []
    for m in store.list_ssh():
        name = m.get("name")
        card = _load_card(name)
        alive = tunnel_keeper_running(name)
        state = (card or {}).get("state") or "down"
        if not alive and state == "up":
            state = "down"
        if card and not card_fresh(card) and state == "up":
            state = "down"
        rows.append({
            "name": name,
            "host": m.get("host"),
            "state": state,
            "keeper": "running" if alive else "stopped",
            "local_port": (card or {}).get("local_port"),
            "since": (card or {}).get("since"),
            "reconnects": (card or {}).get("reconnects") or 0,
            "last_error": (card or {}).get("last_error"),
            "queued": _outbox_count(name),
            "inbox": _inbox_count(name),
        })
    return rows


def _dir_count(d):
    try:
        return len([n for n in os.listdir(d) if n.endswith(".json")])
    except OSError:
        return 0


def _outbox_count(name):
    return _dir_count(_outbox_dir(name))


def _inbox_count(name):
    return _dir_count(_inbox_dir(name))


def tunnel_agents(machine, timeout=None):
    """Run `list` on one machine through its tunnel. Returns
    (ok, text_or_error)."""
    name = machine.get("name")
    card = _load_card(name)
    token = _read_m_token(name)
    port = (card or {}).get("local_port")
    if not (card and card.get("state") == "up" and card_fresh(card)
            and isinstance(port, int) and port > 0 and token):
        return False, "tunnel down (start it: camc tunnel up %s)" % name
    from camc_pkg.bridge import _tunnel_call
    req = _new_request(machine, ["list"])
    resp = _tunnel_call(port, token, req,
                        time.time() + (timeout or _call_timeout_s()))
    if resp is None:
        return False, "tunnel transport failure"
    if resp.get("ok"):
        return True, str(resp.get("text") or "")
    return False, str(resp.get("error") or "unknown error")


def tunnel_inbox_read(name):
    """Read and delete collected replies for one machine. Returns the
    list of response dicts."""
    out = []
    d = _inbox_dir(name)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, IOError, ValueError):
            continue
        try:
            os.unlink(p)
        except OSError:
            pass
    return out
