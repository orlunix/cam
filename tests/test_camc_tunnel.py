#!/usr/bin/env python3
"""Tests for camc_pkg.tunnel (per-machine persistent SSH tunnels).

End-to-end tests run `python -m camc_pkg` as a subprocess with HOME
pointed at a tmp dir and PYTHONPATH at src/; unit tests import
camc_pkg.tunnel directly and monkeypatch module path globals. No
third-party deps: plain pytest-style test functions plus a __main__
runner (mirrors test_camc_bridge.py).
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
sys.path.insert(0, SRC)

from camc_pkg import bridge, tunnel  # noqa: E402


M1 = {"name": "gpu1", "type": "ssh", "host": "192.0.2.1",
      "user": "u1", "port": 22}
M2 = {"name": "dc-box", "type": "ssh", "host": "192.0.2.2",
      "user": "root", "port": 2222}


class _FakeStore:
    def __init__(self, machines):
        self._machines = machines

    def list_ssh(self):
        return [m for m in self._machines if m.get("type") == "ssh"]


def _run_camc(args, home, env_extra=None, stdin=None, timeout=60):
    env = dict(os.environ)
    env["HOME"] = home
    env["PYTHONPATH"] = SRC
    env.pop("TMUX", None)  # never leak the real tmux socket into tests
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "camc_pkg"] + args,
        env=env,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        cwd=home,
        timeout=timeout,
    )


def _write_machines(home, machines):
    os.makedirs(os.path.join(home, ".cam"), exist_ok=True)
    with open(os.path.join(home, ".cam", "machines.json"), "w") as fh:
        json.dump(machines, fh)


def _tunnels(home):
    return os.path.join(home, ".cam", "tunnels")


def _write_card(home, name, card):
    os.makedirs(_tunnels(home), exist_ok=True)
    card = dict(card)
    card.setdefault("machine", name)
    card.setdefault("ts", time.time())
    with open(os.path.join(_tunnels(home), name + ".json"), "w") as fh:
        json.dump(card, fh)


def _write_token(home, name, token="tok-test"):
    os.makedirs(_tunnels(home), exist_ok=True)
    with open(os.path.join(_tunnels(home), name + ".token"), "w") as fh:
        fh.write(token + "\n")


def _outbox(home, name):
    return os.path.join(_tunnels(home), name, "outbox")


def _inbox(home, name):
    return os.path.join(_tunnels(home), name, "inbox")


def _ls(d):
    return os.listdir(d) if os.path.isdir(d) else []


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeServe(threading.Thread):
    """One-shot fake tunnel endpoint: one JSON line in, responder out."""

    def __init__(self, responder):
        threading.Thread.__init__(self, daemon=True)
        self.received = []
        self.responder = responder
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.start()

    def run(self):
        try:
            conn, _ = self.sock.accept()
            try:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    payload = json.loads(data.split(b"\n", 1)[0].decode())
                    self.received.append(payload)
                    resp = self.responder(payload.get("request") or {})
                    conn.sendall(json.dumps(resp).encode() + b"\n")
            finally:
                conn.close()
        finally:
            self.sock.close()


# ------------------------------------------------------------------ route

def test_route_machine_name():
    hit = tunnel.tunnel_route(["capture", "gpu1:af12"], store=_FakeStore([M1]))
    assert hit is not None
    m, argv = hit
    assert m["name"] == "gpu1"
    assert argv == ["capture", "gpu1:af12"]


def test_route_endpoint_forms():
    store = _FakeStore([M2])
    for ref in ("dc-box:af12", "root@192.0.2.2:2222:af12",
                "192.0.2.2:2222:af12", "root@192.0.2.2:af12",
                "192.0.2.2:af12"):
        hit = tunnel.tunnel_route(["status", ref], store=store)
        assert hit is not None, ref
        assert hit[0]["name"] == "dc-box", ref


def test_route_passes_through_non_machines():
    store = _FakeStore([M1])
    assert tunnel.tunnel_route(["capture", "othernode:af12"],
                        store=store) is None  # hub-only node name
    assert tunnel.tunnel_route(["capture", "bare-ref"], store=store) is None
    assert tunnel.tunnel_route(["list"], store=store) is None
    assert tunnel.tunnel_route(["list", "-a"], store=store) is None
    assert tunnel.tunnel_route(["status"], store=store) is None
    assert tunnel.tunnel_route([], store=store) is None


def test_route_msg_send_target_position():
    hit = tunnel.tunnel_route(["msg", "send", "gpu1:af12", "hello"],
                       store=_FakeStore([M1]))
    assert hit is not None
    assert hit[1] == ["msg", "send", "gpu1:af12", "hello"]
    # msg read/reply/wait never route
    assert tunnel.tunnel_route(["msg", "read", "gpu1:af12"],
                        store=_FakeStore([M1])) is None


def test_route_global_json_flag_preserved():
    hit = tunnel.tunnel_route(["--json", "status", "gpu1:af12"],
                       store=_FakeStore([M1]))
    assert hit is not None
    assert "--json" in hit[1]


def test_strip_target_prefix():
    argv = ["capture", "gpu1:af12"]
    assert tunnel._strip_target_prefix(argv, M1) == ["capture", "af12"]
    argv = ["msg", "send", "root@192.0.2.2:2222:af12", "hi"]
    assert tunnel._strip_target_prefix(argv, M2) == \
        ["msg", "send", "af12", "hi"]
    # bare ref: no-op
    assert tunnel._strip_target_prefix(["capture", "af12"], M1) == \
        ["capture", "af12"]


# --------------------------------------------------------------- state card

def test_card_fresh_ttl():
    assert tunnel.card_fresh({"ts": time.time()})
    assert not tunnel.card_fresh({"ts": time.time() - 120})
    assert not tunnel.card_fresh({"ts": "junk"})
    assert not tunnel.card_fresh({})
    assert not tunnel.card_fresh(None)


# ------------------------------------------------------------- serve token

def test_serve_token_create_persist_mode():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tunnel", "token")
        tok1 = tunnel._load_or_create_serve_token(path)
        assert len(tok1) == 32
        assert (os.stat(path).st_mode & 0o777) == 0o600
        tok2 = tunnel._load_or_create_serve_token(path)
        assert tok1 == tok2  # persisted, not regenerated


# ------------------------------------------------------------------- serve

def _start_serve(exec_fn, token="tok-serve", audit_path=None):
    srv = tunnel.TunnelServer(0, token, exec_fn=exec_fn,
                              audit_path=audit_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _call(port, token, req, timeout=5):
    return bridge._tunnel_call(port, token, req, time.time() + timeout)


def test_serve_ok_and_audit():
    with tempfile.TemporaryDirectory() as d:
        audit = os.path.join(d, "audit.jsonl")
        seen = []

        def exec_fn(argv, timeout):
            seen.append(argv)
            return 0, "PONG\n", ""

        srv, port = _start_serve(exec_fn, audit_path=audit)
        try:
            resp = _call(port, "tok-serve",
                         {"id": "r1", "argv": ["list"], "proto": 1})
            assert resp["ok"] is True, resp
            assert resp["text"] == "PONG"
            assert resp["id"] == "r1"
            assert seen == [["list"]]
        finally:
            srv.shutdown()
            srv.server_close()
        with open(audit) as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        assert len(lines) == 1
        assert lines[0]["ok"] is True
        assert "tok" not in json.dumps(lines)  # never audit tokens


def test_serve_rejects_bad_token():
    srv, port = _start_serve(lambda a, t: (0, "", ""))
    try:
        resp = _call(port, "wrong", {"id": "r1", "argv": ["list"]})
        assert resp["ok"] is False
        assert resp["error"] == "unauthorized"
        assert resp["id"] == "r1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_verb_allowlist_and_guards():
    srv, port = _start_serve(lambda a, t: (0, "", ""))
    try:
        resp = _call(port, "tok-serve", {"id": "1", "argv": ["run", "x"]})
        assert "unsupported verb" in resp["error"]
        resp = _call(port, "tok-serve",
                     {"id": "2", "argv": ["msg", "read", "x"]})
        assert "unsupported msg form" in resp["error"]
        # client must strip the machine prefix (hub strips node: the same
        # way); a colon surviving to the endpoint is a client bug
        resp = _call(port, "tok-serve",
                     {"id": "3", "argv": ["capture", "gpu1:af12"]})
        assert "machine prefix not stripped" in resp["error"]
        resp = _call(port, "tok-serve",
                     {"id": "4", "argv": ["list"], "proto": 2})
        assert "unsupported proto" in resp["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_exec_failure_passthrough():
    srv, port = _start_serve(lambda a, t: (1, "", "camc: No agent 'zz'\n"))
    try:
        resp = _call(port, "tok-serve", {"id": "9", "argv": ["status", "zz"]})
        assert resp["ok"] is False
        assert "No agent" in resp["error"]
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------- submit (e2e)

def test_submit_tunnel_fast_path_ok():
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])
        fake = _FakeServe(lambda req: {"id": req.get("id"), "ok": True,
                                       "text": "REMOTE-CAPTURE"})
        _write_card(home, "gpu1", {"state": "up", "local_port": fake.port})
        _write_token(home, "gpu1")
        p = _run_camc(["capture", "gpu1:af12"], home)
        assert p.returncode == 0, (p.returncode, p.stderr)
        assert p.stdout.strip() == "REMOTE-CAPTURE"
        # prefix stripped on the wire, no mailbox residue
        assert fake.received[0]["request"]["argv"] == ["capture", "af12"]
        assert fake.received[0]["token"] == "tok-test"
        assert _ls(_outbox(home, "gpu1")) == []


def test_submit_app_error_terminal():
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])
        fake = _FakeServe(lambda req: {"id": req.get("id"), "ok": False,
                                       "error": "camc: No agent 'af12'"})
        _write_card(home, "gpu1", {"state": "up", "local_port": fake.port})
        _write_token(home, "gpu1")
        p = _run_camc(["capture", "gpu1:af12"], home)
        assert p.returncode == 1, (p.returncode, p.stderr)
        assert "No agent" in p.stderr
        assert _ls(_outbox(home, "gpu1")) == []


def test_submit_tunnel_down_queues_mailbox():
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])
        _write_card(home, "gpu1",
                    {"state": "up", "local_port": _closed_port()})
        _write_token(home, "gpu1")
        p = _run_camc(["capture", "gpu1:af12"], home,
                      env_extra={"CAMC_TUNNEL_MAILBOX_MS": "300"})
        assert p.returncode == 2, (p.returncode, p.stderr)
        assert "queued to mailbox" in p.stderr
        names = [n for n in _ls(_outbox(home, "gpu1")) if n.endswith(".json")]
        assert len(names) == 1
        with open(os.path.join(_outbox(home, "gpu1"), names[0])) as fh:
            req = json.load(fh)
        # request file keeps the original (prefixed) argv for audit;
        # stripping happens at the transport boundary
        assert req["argv"] == ["capture", "gpu1:af12"]
        assert req["machine"] == "gpu1"


def test_submit_stale_card_goes_mailbox():
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])
        _write_card(home, "gpu1",
                    {"state": "up", "local_port": _closed_port(),
                     "ts": time.time() - 3600})
        _write_token(home, "gpu1")
        p = _run_camc(["status", "gpu1:af12"], home,
                      env_extra={"CAMC_TUNNEL_MAILBOX_MS": "300"})
        assert p.returncode == 2
        assert len([n for n in _ls(_outbox(home, "gpu1"))
                    if n.endswith(".json")]) == 1


def test_submit_msg_send_via_tunnel():
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])
        fake = _FakeServe(lambda req: {"id": req.get("id"), "ok": True,
                                       "text": "MSG_ID=abcd1234"})
        _write_card(home, "gpu1", {"state": "up", "local_port": fake.port})
        _write_token(home, "gpu1")
        p = _run_camc(["msg", "send", "gpu1:af12", "hello there",
                       "--no-wait"], home)
        assert p.returncode == 0, (p.returncode, p.stderr)
        assert fake.received[0]["request"]["argv"][:3] == \
            ["msg", "send", "af12"]


def test_unknown_machine_falls_through_to_bridge():
    # 'othernode' is not in machines.json: tunnel route passes, fleet
    # bridge takes it (no hub mailbox deployed here → exit 2).
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])
        p = _run_camc(["status", "othernode:af12"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, (p.returncode, p.stderr)
        assert "bridge unavailable" in p.stderr
        # nothing queued in the machine tunnel mailbox
        assert _ls(_outbox(home, "gpu1")) == []


# ------------------------------------------------------------------- flush

def _patch_paths(home):
    saved = {"TUNNELS_DIR": tunnel.TUNNELS_DIR}
    tunnel.TUNNELS_DIR = _tunnels(home)
    return saved


def _unpatch_paths(saved):
    tunnel.TUNNELS_DIR = saved["TUNNELS_DIR"]


def _seed_outbox(home, name, reqs):
    os.makedirs(_outbox(home, name), exist_ok=True)
    for r in reqs:
        with open(os.path.join(_outbox(home, name), r["id"] + ".json"),
                  "w") as fh:
            json.dump(r, fh)


def test_flush_delivers_and_clears():
    with tempfile.TemporaryDirectory() as home:
        saved = _patch_paths(home)
        try:
            _write_card(home, "gpu1", {"state": "up", "local_port": 1})
            _write_token(home, "gpu1")
            reqs = [{"id": "a" * 8, "argv": ["capture", "gpu1:af12"],
                     "ts": 1.0},
                    {"id": "b" * 8, "argv": ["status", "gpu1:af12"],
                     "ts": 2.0}]
            _seed_outbox(home, "gpu1", reqs)
            seen = []

            def call_fn(port, token, send_req):
                seen.append(send_req)
                return {"id": send_req["id"], "ok": True, "text": "OK"}

            sent, remaining = tunnel.tunnel_flush(M1, call_fn=call_fn)
            assert (sent, remaining) == (2, 0)
            assert [r["argv"] for r in seen] == [["capture", "af12"],
                                                 ["status", "af12"]]
            assert [n for n in _ls(_outbox(home, "gpu1"))
                    if n.endswith(".json")] == []
            inbox = [n for n in _ls(_inbox(home, "gpu1"))
                     if n.endswith(".json")]
            assert len(inbox) == 2
            with open(os.path.join(_inbox(home, "gpu1"),
                                   sorted(inbox)[0])) as fh:
                resp = json.load(fh)
            assert resp["ok"] is True
            assert resp["machine"] == "gpu1"
            assert resp["argv"] == ["capture", "gpu1:af12"]  # original kept
        finally:
            _unpatch_paths(saved)


def test_flush_stops_on_transport_failure():
    with tempfile.TemporaryDirectory() as home:
        saved = _patch_paths(home)
        try:
            _write_card(home, "gpu1", {"state": "up", "local_port": 1})
            _write_token(home, "gpu1")
            _seed_outbox(home, "gpu1", [
                {"id": "a" * 8, "argv": ["capture", "gpu1:x"], "ts": 1.0},
                {"id": "b" * 8, "argv": ["capture", "gpu1:y"], "ts": 2.0}])
            sent, remaining = tunnel.tunnel_flush(
                M1, call_fn=lambda p, t, r: None)
            assert (sent, remaining) == (0, 2)
            # tunnel marked down mid-pass: second request survives
            calls = []

            def flaky(port, token, req):
                calls.append(req["id"])
                return ({"id": req["id"], "ok": True, "text": "OK"}
                        if len(calls) == 1 else None)

            sent, remaining = tunnel.tunnel_flush(M1, call_fn=flaky)
            assert (sent, remaining) == (1, 1)
            left = [n for n in _ls(_outbox(home, "gpu1"))
                    if n.endswith(".json")]
            assert left == ["b" * 8 + ".json"]
        finally:
            _unpatch_paths(saved)


def test_flush_skips_when_down():
    with tempfile.TemporaryDirectory() as home:
        saved = _patch_paths(home)
        try:
            _write_card(home, "gpu1", {"state": "down"})
            _write_token(home, "gpu1")
            _seed_outbox(home, "gpu1", [
                {"id": "a" * 8, "argv": ["capture", "gpu1:x"], "ts": 1.0}])
            sent, remaining = tunnel.tunnel_flush(
                M1, call_fn=lambda p, t, r: {"id": r["id"], "ok": True})
            assert (sent, remaining) == (0, 1)
        finally:
            _unpatch_paths(saved)


def test_inbox_read_returns_and_deletes():
    with tempfile.TemporaryDirectory() as home:
        saved = _patch_paths(home)
        try:
            os.makedirs(_inbox(home, "gpu1"), exist_ok=True)
            with open(os.path.join(_inbox(home, "gpu1"), "c" * 8 + ".json"),
                      "w") as fh:
                json.dump({"id": "c" * 8, "ok": True, "text": "T"}, fh)
            out = tunnel.tunnel_inbox_read("gpu1")
            assert len(out) == 1 and out[0]["text"] == "T"
            assert _ls(_inbox(home, "gpu1")) == []
            assert tunnel.tunnel_inbox_read("gpu1") == []
        finally:
            _unpatch_paths(saved)


# ------------------------------------------------------- serve e2e (real)

def test_serve_e2e_exec_real_camc():
    with tempfile.TemporaryDirectory() as home:
        port = _closed_port()
        env = dict(os.environ)
        env["HOME"] = home
        env["PYTHONPATH"] = SRC
        proc = subprocess.Popen(
            [sys.executable, "-m", "camc_pkg", "tunnel", "serve",
             "--port", str(port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        try:
            deadline = time.time() + 15
            token = ""
            while time.time() < deadline:
                if tunnel._port_open(port):
                    token = tunnel._load_or_create_serve_token(
                        os.path.join(home, ".cam", "tunnel", "token"))
                    break
                time.sleep(0.3)
            assert token, "serve did not come up"
            resp = _call(port, token, {"id": "e1", "argv": ["list"]})
            assert resp["ok"] is True, resp
            # empty agents.json → camc list prints its empty message
            assert "No agents" in resp["text"] or resp["text"] == ""
            resp = _call(port, "nope", {"id": "e2", "argv": ["list"]})
            assert resp["ok"] is False and resp["error"] == "unauthorized"
        finally:
            proc.kill()
            proc.wait()


# ------------------------------------------------------- up/down lifecycle

def test_up_status_down_lifecycle():
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])  # 192.0.2.1 is TEST-NET: ssh fails
        p = _run_camc(["tunnel", "up", "gpu1"], home)
        assert p.returncode == 0, (p.returncode, p.stderr)
        assert "started" in p.stdout
        pid_path = os.path.join(home, ".cam", "pids", "tunnel-gpu1.pid")
        assert os.path.isfile(pid_path)
        with open(pid_path) as fh:
            pid = json.load(fh)["pid"]
        assert tunnel._pid_alive(pid)

        p = _run_camc(["tunnel", "up", "gpu1"], home)
        assert p.returncode == 0
        assert "already running" in p.stdout

        p = _run_camc(["tunnel", "status", "--json"], home)
        assert p.returncode == 0, p.stderr
        rows = json.loads(p.stdout)
        assert rows[0]["name"] == "gpu1"
        assert rows[0]["keeper"] == "running"

        p = _run_camc(["tunnel", "down", "gpu1"], home)
        assert p.returncode == 0, (p.returncode, p.stderr)
        assert "stopped" in p.stdout
        deadline = time.time() + 10
        while time.time() < deadline and tunnel._pid_alive(pid):
            time.sleep(0.2)
        assert not tunnel._pid_alive(pid), "keeper did not die"


def test_tunnel_status_empty():
    with tempfile.TemporaryDirectory() as home:
        p = _run_camc(["tunnel", "status"], home)
        assert p.returncode == 0
        assert "No SSH machines" in p.stdout


def test_tunnel_ssh_cmd_wellformed():
    cmd = tunnel._tunnel_ssh_cmd(M2, 42200, 42177)
    assert cmd[0] == "ssh" and "-N" in cmd and "-L" in cmd
    assert "127.0.0.1:42200:127.0.0.1:42177" in cmd
    assert "root@192.0.2.2" == cmd[-1]
    assert "-p" in cmd and "2222" in cmd
    # every -o must be followed by a value (a bare trailing -o broke the
    # keeper's ssh invocation)
    for i, a in enumerate(cmd):
        if a == "-o":
            assert i + 1 < len(cmd) and "=" in cmd[i + 1], cmd
    # dedicated control path/master: no auto-master or 600s persist
    assert not any("ControlMaster=auto" in a for a in cmd)
    assert not any("ControlPersist=600" in a for a in cmd)
    assert any("ControlMaster=yes" in a for a in cmd)
    assert any("cam-ssh-tun-" in a for a in cmd)


def test_bundle_keeper_smoke():
    """The single-file bundle strips `from camc_pkg import remote as
    _remote`-style aliases; keeper code must use bare-name imports. Only
    the built artifact catches a violation, so run the keeper lifecycle
    against dist/camc when it exists."""
    bundle = os.path.join(REPO_ROOT, "dist", "camc")
    if not os.path.isfile(bundle):
        return
    with tempfile.TemporaryDirectory() as home:
        _write_machines(home, [M1])  # 192.0.2.1 is TEST-NET: ssh fails
        env = dict(os.environ)
        env["HOME"] = home
        env.pop("TMUX", None)

        def run(*args):
            return subprocess.run(
                [bundle] + list(args), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True,
                timeout=60)

        p = run("tunnel", "up", "gpu1")
        assert p.returncode == 0, (p.returncode, p.stderr)
        time.sleep(3)  # keeper would die immediately on a stripped import
        p = run("tunnel", "status", "--json")
        rows = json.loads(p.stdout)
        assert rows[0]["keeper"] == "running", rows
        p = run("tunnel", "down", "gpu1")
        assert p.returncode == 0, p.stderr


# ------------------------------------------------------------------ runner

if __name__ == "__main__":
    mod = sys.modules[__name__]
    tests = sorted(n for n in dir(mod) if n.startswith("test_"))
    failures = 0
    for n in tests:
        try:
            getattr(mod, n)()
            print("PASS %s" % n)
        except Exception as e:
            failures += 1
            print("FAIL %s: %s" % (n, e))
    sys.exit(1 if failures else 0)
