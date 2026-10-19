#!/usr/bin/env python3
"""Tests for cam.core.tunnel_registry + cam.core.tunnel_hub (stdlib-only).

No FastAPI import anywhere: the hub core must be testable standalone.
Plain pytest-style functions plus a __main__ runner (this WSL has no
pytest).
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
sys.path.insert(0, SRC)

from cam.core.tunnel_registry import TunnelRegistry  # noqa: E402
from cam.core import tunnel_hub  # noqa: E402
from cam.core.tunnel_hub import TunnelHub  # noqa: E402


M1 = {"name": "gpu1", "type": "ssh", "host": "10.0.0.1", "user": "u1",
      "port": 22}
M2 = {"name": "dcbox", "type": "ssh", "host": "10.0.0.2", "user": "root",
      "port": 2222}


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeServe(threading.Thread):
    """Fake camb --serve endpoint: §10 frame in, canned response out."""

    def __init__(self, token="tok-h", text="HOST-OUT"):
        threading.Thread.__init__(self, daemon=True)
        self.token = token
        self.text = text
        self.received = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.start()

    def run(self):
        try:
            while True:
                conn, _ = self.sock.accept()
                try:
                    buf = b""
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    if not buf:
                        continue
                    frame = json.loads(buf.split(b"\n", 1)[0].decode())
                    req = frame.get("request") or {}
                    self.received.append(frame)
                    if frame.get("token") != self.token:
                        resp = {"id": req.get("id"), "ok": False,
                                "error": "unauthorized"}
                    else:
                        resp = {"id": req.get("id"), "ok": True,
                                "text": self.text}
                    conn.sendall(json.dumps(resp).encode() + b"\n")
                finally:
                    conn.close()
        except OSError:
            pass


def _mk_hub(home, machines, **kw):
    data_dir = os.path.join(home, "data")
    os.makedirs(data_dir, exist_ok=True)
    cam_dir = os.path.join(home, ".cam")
    os.makedirs(cam_dir, exist_ok=True)
    with open(os.path.join(cam_dir, "machines.json"), "w") as fh:
        json.dump(machines, fh)
    registry = TunnelRegistry(os.path.join(data_dir, "tunnels.json"))
    return TunnelHub(registry, data_dir, cam_dir=cam_dir, **kw)


# ---------------------------------------------------------------- registry

def test_registry_crud_and_persist():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tunnels.json")
        reg = TunnelRegistry(path)
        e = reg.register({"key": "h1", "name": "host1", "port": 43400,
                          "token": "t1"})
        assert e["key"] == "h1" and e["port"] == 43400
        assert reg.get("h1")["token"] == "t1"
        assert len(reg.list()) == 1
        # token file is 0600
        assert (os.stat(path).st_mode & 0o777) == 0o600
        # reload from disk
        reg2 = TunnelRegistry(path)
        assert reg2.get("h1")["name"] == "host1"
        # upsert same key
        reg2.register({"key": "h1", "port": 43401})
        assert reg2.get("h1")["port"] == 43401
        assert reg2.unregister("h1") is True
        assert reg2.unregister("h1") is False
        assert reg2.list() == []


def test_registry_validation():
    with tempfile.TemporaryDirectory() as d:
        reg = TunnelRegistry(os.path.join(d, "t.json"))
        try:
            reg.register({"port": 43400})
            raise SystemExit("should have raised")
        except ValueError:
            pass
        try:
            reg.register({"key": "h", "port": -1})
            raise SystemExit("should have raised")
        except ValueError:
            pass


def test_registry_alive_and_prune():
    with tempfile.TemporaryDirectory() as d:
        reg = TunnelRegistry(os.path.join(d, "t.json"))
        closed = _closed_port()
        reg.register({"key": "dead", "port": closed,
                      "ts": time.time() - 1000})
        assert reg.alive("dead") is None  # port closed
        fake = _FakeServe()
        try:
            reg.register({"key": "live", "port": fake.port})
            assert reg.alive("live") is not None
            # stale ts → not alive() even with the port open
            reg.register({"key": "old", "port": fake.port,
                          "ts": time.time() - 1000})
            assert reg.alive("old") is None
            # prune drops stale+closed, keeps anything with an open port
            n = reg.prune(max_age_s=60)
            assert n == 1  # only "dead"
            assert reg.get("dead") is None
            assert reg.get("live") is not None
            assert reg.get("old") is not None
        finally:
            fake.sock.close()


def test_find_for_machine_forms():
    with tempfile.TemporaryDirectory() as d:
        reg = TunnelRegistry(os.path.join(d, "t.json"))
        fake = _FakeServe()
        try:
            reg.register({"key": "gpu1", "port": fake.port})
            assert reg.find_for_machine(M1)["key"] == "gpu1"
            # endpoint-key registration also matches
            reg.register({"key": "root@10.0.0.2:2222", "port": fake.port})
            hit = reg.find_for_machine(M2)
            assert hit and hit["key"] == "root@10.0.0.2:2222"
            assert reg.find_for_machine(
                {"name": "nope", "type": "ssh", "host": "9.9.9.9"}) is None
        finally:
            fake.sock.close()


# -------------------------------------------------------------------- hub

def test_host_token_derivation():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [], auth_token="master")
        t1 = hub.host_token("gpu1")
        assert t1 == hub.host_token("gpu1")
        assert t1 != hub.host_token("gpu2")
        hub2 = _mk_hub(home, [], auth_token="other-master")
        assert hub2.host_token("gpu1") != t1


def test_refresh_card_covers_live_machines():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1, M2])
        fake = _FakeServe()
        try:
            hub.registry.register({"key": "gpu1", "port": fake.port})
            hub._refresh_card()
            card_path = os.path.join(home, ".cam", "camb", "hubs",
                                     "cam-serve.json")
            card = json.load(open(card_path))
            assert card["hub_id"] == "cam-serve"
            assert card["tunnel"]["port"] == hub.listen_port
            assert [c["name"] for c in card["covers"]] == ["gpu1"]
            assert card["covers"][0]["key"] == "u1@10.0.0.1:22"
            token_path = card_path.replace(".json", ".token")
            assert (os.stat(token_path).st_mode & 0o777) == 0o600
        finally:
            fake.sock.close()


def test_strip_node_prefix():
    assert tunnel_hub._strip_node_prefix(
        ["capture", "gpu1:af12"], tunnel_hub._machine_prefixes(M1)) == \
        ["capture", "af12"]
    assert tunnel_hub._strip_node_prefix(
        ["msg", "send", "u1@10.0.0.1:22:af12", "hi"],
        tunnel_hub._machine_prefixes(M1)) == ["msg", "send", "af12", "hi"]


def test_resolve_target():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1, M2])
        assert hub._resolve_target("gpu1:af12")["name"] == "gpu1"
        assert hub._resolve_target("root@10.0.0.2:2222:af12")["name"] == "dcbox"
        assert hub._resolve_target("10.0.0.2:2222:af12")["name"] == "dcbox"
        assert hub._resolve_target("strange:af12") is None


def _run(coro):
    return asyncio.run(coro)


def test_dispatch_via_tunnel_fastest():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        fake = _FakeServe(token="tok-h", text="TUNNEL-OUT")
        ssh_calls = []
        try:
            hub.registry.register({"key": "gpu1", "port": fake.port,
                                   "token": "tok-h"})
            tunnel_hub._run_camc_ssh = lambda *a, **k: (ssh_calls.append(a)
                                                        or (0, "SSH-OUT"))
            resp = _run(hub.dispatch(
                {"id": "r1", "argv": ["capture", "gpu1:af12"], "ts": 1.0}))
            assert resp["ok"] is True, resp
            assert resp["text"] == "TUNNEL-OUT"
            # prefix stripped on the wire
            assert fake.received[0]["request"]["argv"] == ["capture", "af12"]
            assert ssh_calls == []  # tunnel won, ssh never tried
        finally:
            fake.sock.close()


def test_dispatch_falls_back_to_ssh():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        # registry entry points at a closed port → transport failure
        hub.registry.register({"key": "gpu1", "port": _closed_port(),
                               "token": "tok-h"})
        tunnel_hub._run_camc_ssh = lambda *a, **k: (0, "SSH-OUT\n")
        resp = _run(hub.dispatch(
            {"id": "r2", "argv": ["status", "gpu1:af12"], "ts": 1.0}))
        assert resp["ok"] is True
        assert resp["text"] == "SSH-OUT"


def test_dispatch_release_when_unroutable():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        tunnel_hub._run_camc_ssh = lambda *a, **k: (-1, "ssh down")
        resp = _run(hub.dispatch(
            {"id": "r3", "argv": ["status", "gpu1:af12"], "ts": 1.0}))
        assert resp["ok"] is False
        assert "released" in resp["error"]
        # released back to outbox with attempts+1
        outbox = os.path.join(home, ".cam", "camb", "outbox")
        names = os.listdir(outbox)
        assert len(names) == 1
        rel = json.load(open(os.path.join(outbox, names[0])))
        assert rel["attempts"] == 1
        assert rel["id"] == "r3"
        # third strike → terminal error, no release
        resp = _run(hub.dispatch(
            {"id": "r4", "argv": ["status", "gpu1:af12"], "ts": 1.0,
             "attempts": 2}))
        assert "unroutable after" in resp["error"]
        assert len(os.listdir(outbox)) == 1


def test_dispatch_seen_id_idempotent():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        calls = []
        tunnel_hub._run_camc_ssh = lambda *a, **k: (calls.append(a)
                                                    or (0, "OUT"))
        req = {"id": "r5", "argv": ["status", "gpu1:af12"], "ts": 1.0}
        r1 = _run(hub.dispatch(req))
        r2 = _run(hub.dispatch(req))
        assert r1 == r2
        assert len(calls) == 1  # second call served from seen cache


def test_dispatch_unknown_node_and_bad_verb():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        resp = _run(hub.dispatch({"id": "x", "argv": ["run", "gpu1:a"]}))
        assert "unsupported verb" in resp["error"]
        resp = _run(hub.dispatch({"id": "y", "argv": ["status", "zz:a"]}))
        assert "unknown node" in resp["error"]


def test_dispatch_list_aggregates():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        fake = _FakeServe(token="tok-h", text="REMOTE-LIST")
        try:
            hub.registry.register({"key": "gpu1", "port": fake.port,
                                   "token": "tok-h"})
            tunnel_hub._run_camc = lambda *a, **k: (0, "LOCAL-LIST")
            resp = _run(hub.dispatch({"id": "l1", "argv": ["list"],
                                      "ts": 1.0}))
            assert resp["ok"] is True
            assert "LOCAL-LIST" in resp["text"]
            assert "== gpu1 ==" in resp["text"]
            assert "REMOTE-LIST" in resp["text"]
        finally:
            fake.sock.close()


def test_handle_frame_auth():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [])
        resp = _run(hub._handle_frame({"token": "wrong",
                                       "request": {"id": "1",
                                                   "argv": ["list"]}}))
        assert resp["ok"] is False and resp["error"] == "unauthorized"
        resp = _run(hub._handle_frame("junk"))
        assert resp["error"] == "bad frame"


def test_audit_written():
    with tempfile.TemporaryDirectory() as home:
        hub = _mk_hub(home, [M1])
        tunnel_hub._run_camc_ssh = lambda *a, **k: (0, "X")
        _run(hub.dispatch({"id": "a1", "argv": ["status", "gpu1:z"],
                           "ts": 1.0}))
        with open(os.path.join(home, "data", "tunnel-hub-audit.jsonl")) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 1
        assert lines[0]["argv"] == ["status", "gpu1:z"]
        assert lines[0]["ok"] is True


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
            import traceback
            print("FAIL %s: %s" % (n, e))
            traceback.print_exc()
    print("\n%d passed, %d failed, %d total" % (len(tests) - failures,
                                                failures, len(tests)))
    sys.exit(1 if failures else 0)
