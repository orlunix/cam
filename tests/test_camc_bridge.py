#!/usr/bin/env python3
"""Tests for camc_pkg.bridge (camc fleet-bridge ferry, mailbox transport).

End-to-end tests run `python -m camc_pkg` as a subprocess with HOME pointed
at a tmp dir (env override) and PYTHONPATH at src/; unit tests import
camc_pkg.bridge directly. No third-party deps: plain pytest-style test
functions plus a __main__ runner (this WSL has no pytest).
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

from camc_pkg import bridge  # noqa: E402


def _run_camc(args, home, env_extra=None, stdin=None):
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
    )


def _outbox(home):
    return os.path.join(home, ".cam", "camb", "outbox")


def _inbox(home):
    return os.path.join(home, ".cam", "camb", "inbox")


def _deploy_mailbox(home):
    """Simulate a bridge host having connected: drain bootstraps dirs."""
    os.makedirs(_outbox(home))
    os.makedirs(_inbox(home))


def _write_agents(home, agents):
    os.makedirs(os.path.join(home, ".cam"), exist_ok=True)
    with open(os.path.join(home, ".cam", "agents.json"), "w") as fh:
        json.dump(agents, fh)


def _ls(d):
    return os.listdir(d) if os.path.isdir(d) else []


def _read_single_request(home):
    names = [n for n in _ls(_outbox(home)) if n.endswith(".json")]
    assert len(names) == 1, "expected exactly 1 outbox request, got %r" % names
    with open(os.path.join(_outbox(home), names[0]), encoding="utf-8") as fh:
        return names[0][:-5], json.load(fh)


def _place_response(home, req_id, resp):
    os.makedirs(_inbox(home), exist_ok=True)
    with open(os.path.join(_inbox(home), req_id + ".json"), "w",
              encoding="utf-8") as fh:
        json.dump(resp, fh)


def _auto_hub(home):
    """Pre-place inbox responses the moment a request lands in the outbox.

    Returns a watcher class with .start()/.stop(); run the camc subprocess
    while the watcher thread plays the bridge host."""
    import threading

    class Watcher(object):
        def __init__(self, respond):
            self.respond = respond
            self._stop = threading.Event()
            self._t = threading.Thread(target=self._loop)
            self._t.daemon = True

        def start(self):
            self._t.start()

        def stop(self):
            self._stop.set()
            self._t.join(5)

        def _loop(self):
            placed = set()
            while not self._stop.is_set():
                for n in _ls(_outbox(home)):
                    if not n.endswith(".json"):
                        continue
                    req_id = n[:-5]
                    if req_id not in placed and self.respond:
                        _place_response(home, req_id, self.respond)
                        placed.add(req_id)
                time.sleep(0.05)

    return Watcher


class _FakeStore(object):
    def __init__(self, hit):
        self._hit = hit

    def get(self, ref):
        return {"id": self._hit} if self._hit else None


# --------------------------------------------------------------- unit: route


def test_route_list_all_ferries_stripped():
    assert bridge.route(["list", "-a"]) == ["list"]
    assert bridge.route(["list", "--all", "--status", "running"]) == \
        ["list", "--status", "running"]
    assert bridge.route(["--json", "list", "-a"]) == ["list", "--json"]


def test_route_plain_list_stays_local():
    assert bridge.route(["list"]) is None
    assert bridge.route(["list", "--status", "running"]) is None


def test_route_node_prefix_always_ferries():
    assert bridge.route(["status", "node2:abc123"]) == ["status", "node2:abc123"]
    assert bridge.route(["capture", "n1:af12", "--lines", "3"]) == \
        ["capture", "n1:af12", "--lines", "3"]
    assert bridge.route(["msg", "send", "n1:af12", "-t", "hi"]) == \
        ["msg", "send", "n1:af12", "-t", "hi"]
    assert bridge.route(["key", "n2:x", "--key", "C-c"]) == \
        ["key", "n2:x", "--key", "C-c"]


def test_route_msg_non_send_never_ferries():
    assert bridge.route(["msg", "wait", "deadbeef"]) is None
    assert bridge.route(["msg", "reply", "deadbeef", "-t", "x"]) is None
    assert bridge.route(["msg", "read"]) is None


def test_route_bare_ref_local_first():
    # local hit → local dispatch, even when the mailbox is deployed
    assert bridge.route(["status", "myfix"], store=_FakeStore("af12be34")) is None
    # local miss + no mailbox → local dispatch (normal not-found error)
    assert bridge.route(["status", "ghost"], store=_FakeStore(None)) is None


def test_route_bare_ref_miss_ferries_when_deployed():
    with tempfile.TemporaryDirectory() as home:
        old = bridge.CAM_DIR
        bridge.CAM_DIR = os.path.join(home, ".cam")
        try:
            _deploy_mailbox(home)
            assert bridge.route(["status", "ghost"],
                                store=_FakeStore(None)) == ["status", "ghost"]
        finally:
            bridge.CAM_DIR = old


def test_route_global_json_folded_after_verb():
    assert bridge.route(["--json", "status", "n1:abc"]) == \
        ["status", "n1:abc", "--json"]
    assert bridge.route(["-v", "--verbose", "status", "n1:abc"]) == \
        ["status", "n1:abc"]


def test_route_non_bridge_verbs_stay_local():
    assert bridge.route(["run", "do thing"]) is None
    assert bridge.route(["stop", "node1:x"]) is None  # not allowlisted in v1
    assert bridge.route(["attach", "n1:x"]) is None
    assert bridge.route([]) is None


def test_route_missing_target_stays_local():
    # argparse must produce the usage error, not the ferry
    assert bridge.route(["send"]) is None
    assert bridge.route(["capture", "--lines", "3"]) is None
    assert bridge.route(["msg", "send"]) is None


def test_from_agent_tmux_parsing():
    old = os.environ.get("TMUX")
    try:
        os.environ["TMUX"] = "/tmp/cam-sockets/cam-af12be34.sock,1234,0"
        assert bridge._from_agent() == "af12be34"
        os.environ["TMUX"] = "/tmp/tmux-1000/default,5678,0"
        assert bridge._from_agent() is None
        os.environ.pop("TMUX", None)
        assert bridge._from_agent() is None
    finally:
        if old is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old


# ------------------------------------------------------------- submit/atomic


def test_submit_atomic_request_shape():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        # no inbox response ever -> exit 2, but the request must persist
        p = _run_camc(["status", "node1:x"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, p.stderr
        names = _ls(_outbox(home))
        assert len(names) == 1 and names[0].endswith(".json"), names
        assert not any(n.endswith(".tmp") for n in names), names
        req_id, req = _read_single_request(home)
        assert req["id"] == req_id and len(req["id"]) == 32
        assert req["argv"] == ["status", "node1:x"]  # verb included, verbatim
        assert req["from"] == {"agent": None}  # TMUX stripped in _run_camc
        assert isinstance(req["ts"], float)
        assert abs(req["ts"] - time.time()) < 60


def test_roundtrip_ok_prints_and_cleans_inbox():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        watcher = _auto_hub(home)(respond={"id": "?", "ok": True,
                                           "text": "hello back"})
        watcher.start()
        try:
            p = _run_camc(["status", "node1:x"], home)
        finally:
            watcher.stop()
        assert p.returncode == 0, p.stderr
        assert p.stdout == "hello back\n"
        assert _ls(_inbox(home)) == [], _ls(_inbox(home))


def test_roundtrip_error_exit_1_with_candidates():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        resp = {"ok": False, "error": "ambiguous target",
                "candidates": ["n1:af12 alpha", "n2:af12 beta"]}
        watcher = _auto_hub(home)(respond=resp)
        watcher.start()
        try:
            p = _run_camc(["capture", "af12"], home)
        finally:
            watcher.stop()
        assert p.returncode == 1
        assert "camc: ambiguous target" in p.stderr
        assert "n1:af12 alpha" in p.stderr
        assert "n2:af12 beta" in p.stderr
        assert _ls(_inbox(home)) == []


def test_timeout_exit_2_message_and_request_kept():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        t0 = time.time()
        p = _run_camc(["list", "-a"], home, env_extra={"CAMB_TIMEOUT_MS": "500"})
        dt = time.time() - t0
        assert p.returncode == 2
        assert ("camc: bridge unavailable (hub not connected or bridge "
                "disabled on this node)") in p.stderr
        assert dt < 10, "timeout not honored: %.1fs" % dt
        assert len(_ls(_outbox(home))) == 1  # request stays for a later drain


def test_not_deployed_fails_fast():
    with tempfile.TemporaryDirectory() as home:
        t0 = time.time()
        p = _run_camc(["list", "-a"], home)
        dt = time.time() - t0
        assert p.returncode == 2
        assert "no bridge host mailbox" in p.stderr
        assert dt < 10
        assert _ls(_outbox(home)) == []  # nothing written


def test_submit_from_agent_via_tmux():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        p = _run_camc(
            ["status", "node1:x"], home,
            env_extra={"CAMB_TIMEOUT_MS": "300",
                       "TMUX": "/tmp/cam-sockets/cam-af12be34.sock,1,0"})
        assert p.returncode == 2
        _, req = _read_single_request(home)
        assert req["from"] == {"agent": "af12be34"}


def test_alias_ls_all_ferries():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        p = _run_camc(["ls", "-a"], home, env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, p.stderr
        _, req = _read_single_request(home)
        assert req["argv"] == ["list"]


# ------------------------------------------------------------------ hub-drain


def test_hub_drain_empty_bootstraps_dirs():
    with tempfile.TemporaryDirectory() as home:
        p = _run_camc(["--hub-drain"], home)
        assert p.returncode == 0
        assert json.loads(p.stdout) == []
        for sub in ("outbox", "inbox", "hubs"):
            d = os.path.join(home, ".cam", "camb", sub)
            assert os.path.isdir(d), d
            mode = os.stat(d).st_mode & 0o777
            assert mode == 0o700, "%s mode is %o" % (d, mode)


def test_hub_drain_three_plus_stray_tmp():
    with tempfile.TemporaryDirectory() as home:
        os.makedirs(_outbox(home))
        reqs = [{"id": "a" * 32, "argv": ["list"], "from": {"agent": None},
                 "ts": 1.0},
                {"id": "b" * 32, "argv": ["send", "x", "--text", "hi"],
                 "from": {"agent": "af12"}, "ts": 2.0},
                {"id": "c" * 32, "argv": ["capture", "y", "-n", "5"],
                 "from": {"agent": None}, "ts": 3.0}]
        for r in reqs:
            with open(os.path.join(_outbox(home), r["id"] + ".json"), "w") as fh:
                json.dump(r, fh)
        stray = os.path.join(_outbox(home), "d" * 32 + ".json.tmp")
        with open(stray, "w") as fh:
            fh.write('{"incomplete":')
        p = _run_camc(["--hub-drain"], home)
        assert p.returncode == 0
        got = json.loads(p.stdout)
        assert len(got) == 3, got
        assert sorted(r["id"] for r in got) == ["a" * 32, "b" * 32, "c" * 32]
        # drained files gone, stray .tmp untouched
        assert _ls(_outbox(home)) == ["d" * 32 + ".json.tmp"], _ls(_outbox(home))
        with open(stray) as fh:
            assert fh.read() == '{"incomplete":'


def test_hub_drain_leaves_unreadable():
    with tempfile.TemporaryDirectory() as home:
        os.makedirs(_outbox(home))
        bad = os.path.join(_outbox(home), "e" * 32 + ".json")
        with open(bad, "w") as fh:
            fh.write("not json{")
        p = _run_camc(["--hub-drain"], home)
        assert p.returncode == 0
        assert json.loads(p.stdout) == []
        assert os.path.exists(bad)  # unreadable file left in place


# ------------------------------------------------------------- file/stdin


def test_send_file_resolved_to_text():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        payload = os.path.join(home, "payload.txt")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write("héllo file\n")
        p = _run_camc(["send", "node1:x", "--file", payload], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, p.stderr
        _, req = _read_single_request(home)
        assert req["argv"] == ["send", "node1:x", "--text", "héllo file\n"], \
            req["argv"]


def test_send_stdin_resolved_to_text():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        p = _run_camc(["send", "node1:x", "--stdin", "--no-enter"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"},
                      stdin="typed text")
        assert p.returncode == 2, p.stderr
        _, req = _read_single_request(home)
        assert req["argv"] == ["send", "node1:x", "--text", "typed text",
                               "--no-enter"]


def test_msg_send_file_flag_not_resolved():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        # --file resolution applies to the `send` verb only; msg send's -t
        # passes through untouched
        p = _run_camc(["msg", "send", "n1:x", "-t", "hi"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2
        _, req = _read_single_request(home)
        assert req["argv"] == ["msg", "send", "n1:x", "-t", "hi"]


# ------------------------------------------------------- camb interop (hub)


CAMB = os.path.join(REPO_ROOT, "apps", "cam-desktop", "resources", "camb",
                    "camb")


def _run_camb(args, home, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("TMUX", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, CAMB] + args,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, cwd=home,
    )


def test_camb_drains_camc_request():
    # hub still runs `camb --hub-drain` on nodes today — a camc-ferried
    # request must be indistinguishable from a camb one
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        p = _run_camc(["status", "node1:x"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2
        d = _run_camb(["--hub-drain"], home)
        assert d.returncode == 0
        got = json.loads(d.stdout)
        assert len(got) == 1, got
        assert got[0]["argv"] == ["status", "node1:x"]
        assert got[0]["from"] == {"agent": None}
        assert _ls(_outbox(home)) == []


def test_camc_drains_camb_request():
    # and the forward path: once the hub switches to `camc --hub-drain`,
    # requests left by an old camb client must still drain
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        p = _run_camb(["list"], home, env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2
        d = _run_camc(["--hub-drain"], home)
        assert d.returncode == 0
        got = json.loads(d.stdout)
        assert len(got) == 1, got
        assert got[0]["argv"] == ["list"]
        assert _ls(_outbox(home)) == []


# ------------------------------------------------------- local-first via CLI


_LOCAL_AGENT = {
    "id": "af12be34",
    "task": {"name": "myfix", "tool": "codex", "prompt": "fix it"},
    "status": "alive",
    "tmux_session": "cam-af12be34",
    "created_at": "2026-09-30T00:00:00Z",
    "updated_at": "2026-09-30T00:00:00Z",
}


def test_local_hit_never_touches_mailbox():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        _write_agents(home, [_LOCAL_AGENT])
        # both name and id-prefix resolve locally → no ferry, whatever the
        # local command itself does with the record
        for ref in ("myfix", "af12"):
            p = _run_camc(["status", ref], home,
                          env_extra={"CAMB_TIMEOUT_MS": "300"})
            assert _ls(_outbox(home)) == [], (ref, p.stdout, p.stderr)
            assert "bridge unavailable" not in p.stderr


def test_local_miss_ferries_when_deployed():
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        _write_agents(home, [_LOCAL_AGENT])
        watcher = _auto_hub(home)(respond={"ok": False,
                                           "error": "no agent matches 'ghost'"})
        watcher.start()
        try:
            p = _run_camc(["status", "ghost"], home)
        finally:
            watcher.stop()
        assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
        assert "camc: no agent matches 'ghost'" in p.stderr


def test_local_miss_without_mailbox_errors_locally():
    with tempfile.TemporaryDirectory() as home:
        _write_agents(home, [_LOCAL_AGENT])
        t0 = time.time()
        p = _run_camc(["status", "ghost"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        dt = time.time() - t0
        assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
        assert "not found" in p.stderr
        assert dt < 10  # no ferry wait
        assert _ls(_outbox(home)) == []


def test_capture_exact_hex_remote_miss_ferries():
    # early_capture_main handles exact-id local captures before cli.main;
    # a remote-only exact hex id must fall through to the ferry
    with tempfile.TemporaryDirectory() as home:
        _deploy_mailbox(home)
        _write_agents(home, [])
        p = _run_camc(["capture", "af12be34", "--lines", "3"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, p.stderr
        _, req = _read_single_request(home)
        assert req["argv"] == ["capture", "af12be34", "--lines", "3"]


# ------------------------------------------------------- v1.1 claim + router
#
# The drain is the multi-hub router surface (bridge-protocol-v1 §9.2):
# --bridge-id claims atomically (rename → .claimed-<id>), coverage cards
# in ~/.cam/camb/hubs/ filter explicit `node:ref` targets to the hub that
# covers them, own claimed leftovers re-drain (at-least-once above the
# host's seen-id idempotency), and orphans of card-less (dead) hubs are
# adopted. Every scenario runs against BOTH ferries (camc and camb) to
# pin their byte-compatible behaviour.


def _write_event(home, req):
    os.makedirs(_outbox(home), exist_ok=True)
    with open(os.path.join(_outbox(home), req["id"] + ".json"), "w") as fh:
        json.dump(req, fh)


def _write_card(home, hub_id, covers, ts=None, tunnel=None, token=""):
    d = os.path.join(home, ".cam", "camb", "hubs")
    os.makedirs(d, exist_ok=True)
    card = {"hub_id": hub_id,
            "ts": ts if ts is not None else time.time(),
            "covers": covers, "proto": 1}
    if tunnel:
        card["tunnel"] = tunnel
    with open(os.path.join(d, hub_id + ".json"), "w") as fh:
        json.dump(card, fh)
    if token:
        with open(os.path.join(d, hub_id + ".token"), "w") as fh:
            fh.write(token + "\n")


def _both_drainers():
    return [("camc", _run_camc), ("camb", _run_camb)]


def _drain_with(impl, home, hub):
    p = impl(["--hub-drain", "--bridge-id", hub], home)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_drain_claim_collects_and_deletes():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "a" * 8, "argv": ["list"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_event(home, {"id": "b" * 8, "argv": ["send", "x", "--text", "h"],
                                "from": {"agent": None}, "ts": 2.0})
            got = _drain_with(impl, home, "hubx")
            assert sorted(r["id"] for r in got) == ["a" * 8, "b" * 8], (name, got)
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


def test_drain_skips_peer_covered_target():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "c" * 8,
                                "argv": ["msg", "send", "nodeA:af12", "-t", "x"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_card(home, "hubP", [{"name": "nodeA", "key": "u@10.0.0.1:22"}])
            # hubx does not cover nodeA → the event stays for hubP
            got = _drain_with(impl, home, "hubx")
            assert got == [], (name, got)
            assert _ls(_outbox(home)) == ["c" * 8 + ".json"], (name, _ls(_outbox(home)))
            # hubP claims it (its card covers nodeA)
            got = _drain_with(impl, home, "hubP")
            assert [r["id"] for r in got] == ["c" * 8], (name, got)
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


def test_drain_claims_when_nobody_covers():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "d" * 8, "argv": ["status", "nodeZ:q1"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_card(home, "hubP", [{"name": "nodeA", "key": "u@10.0.0.1:22"}])
            got = _drain_with(impl, home, "hubx")
            assert [r["id"] for r in got] == ["d" * 8], (name, got)


def test_drain_stale_peer_card_does_not_block():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "e" * 8, "argv": ["status", "nodeA:q1"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_card(home, "hubP", [{"name": "nodeA"}],
                        ts=time.time() - 200)  # older than CARD_TTL_S=90
            got = _drain_with(impl, home, "hubx")
            assert [r["id"] for r in got] == ["e" * 8], (name, got)


def test_drain_endpoint_key_target_filtered():
    # explicit targets in canonical-key form (user@host:port:agent) must
    # route by the card's key entry, surviving the inner colons
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "f" * 8,
                                "argv": ["status", "u@10.0.0.1:22:af12"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_card(home, "hubP", [{"name": "nodeA", "key": "u@10.0.0.1:22"}])
            got = _drain_with(impl, home, "hubx")
            assert got == [], (name, got)
            got = _drain_with(impl, home, "hubP")
            assert [r["id"] for r in got] == ["f" * 8], (name, got)


def test_drain_bare_ref_never_filtered():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "0" * 8, "argv": ["status", "somename"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_card(home, "hubP", [{"name": "nodeA", "key": "u@10.0.0.1:22"}])
            got = _drain_with(impl, home, "hubx")
            assert [r["id"] for r in got] == ["0" * 8], (name, got)


def test_drain_own_claimed_leftover_reprinted():
    # a drain interrupted after claim but before deletion is re-collected
    # by the SAME hub on its next drain (at-least-once; the host's seen-id
    # cache turns the duplicate dispatch into a re-delivered response)
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            req = {"id": "1" * 8, "argv": ["list"], "from": {"agent": None},
                   "ts": 1.0}
            leftover = os.path.join(_outbox(home), "1" * 8 + ".json.claimed-hubx")
            with open(leftover, "w") as fh:
                json.dump(req, fh)
            got = _drain_with(impl, home, "hubx")
            assert [r["id"] for r in got] == ["1" * 8], (name, got)
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


def test_drain_peer_claimed_leftover_rules():
    # live owner (fresh card): untouched + not printed; dead owner
    # (no/stale card): adopted by whoever drains next
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            req = {"id": "2" * 8, "argv": ["list"], "from": {"agent": None},
                   "ts": 1.0}
            fname = "2" * 8 + ".json.claimed-hubP"
            with open(os.path.join(_outbox(home), fname), "w") as fh:
                json.dump(req, fh)
            _write_card(home, "hubP", [])  # fresh → hubP alive
            got = _drain_with(impl, home, "hubx")
            assert got == [], (name, got)
            assert _ls(_outbox(home)) == [fname], (name, _ls(_outbox(home)))
            _write_card(home, "hubP", [], ts=time.time() - 200)  # now dead
            got = _drain_with(impl, home, "hubx")
            assert [r["id"] for r in got] == ["2" * 8], (name, got)
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


def test_drain_legacy_without_bridge_id_unchanged():
    # old hosts call plain --hub-drain: everything prints, no claiming,
    # dead-hub orphans adopted (cards absent/ignored for unnamed drains)
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_event(home, {"id": "3" * 8, "argv": ["status", "nodeA:q1"],
                                "from": {"agent": None}, "ts": 1.0})
            _write_card(home, "hubP", [{"name": "nodeA"}])
            p = impl(["--hub-drain"], home)
            assert p.returncode == 0, p.stderr
            got = json.loads(p.stdout)
            assert [r["id"] for r in got] == ["3" * 8], (name, got)
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


# ------------------------------------------------------- v2 tunnel fast path
#
# Ferry priority chain (§10): local → tunnels (coverage-filtered,
# priority-desc) → mailbox. A real 127.0.0.1 socket stands in for the
# hub's forwardIn endpoint; each scenario runs against BOTH ferries.


class _FakeTunnel(threading.Thread):
    """One-shot fake hub tunnel: one JSON line in, responder(req) out."""

    def __init__(self, home, hub_id, responder, covers=None, token="tok-test"):
        threading.Thread.__init__(self, daemon=True)
        self.received = []
        self.responder = responder
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        _write_card(home, hub_id, covers if covers is not None else [],
                    tunnel={"port": self.port}, token=token)
        self.token = token
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
                    payload = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
                    self.received.append(payload)
                    resp = self.responder(payload.get("request") or {})
                    conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            finally:
                conn.close()
        finally:
            self.sock.close()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _submit_both(impl, argv, home, timeout_ms="3000"):
    return impl(argv, home, env_extra={"CAMB_TIMEOUT_MS": timeout_ms})


def test_tunnel_fast_path_ok():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            tun = _FakeTunnel(home, "hubT",
                              lambda req: {"id": req.get("id"), "ok": True,
                                           "text": "TUNNEL-PONG"},
                              covers=[{"name": "nodeA", "key": "u@10.0.0.1:22"}])
            p = _submit_both(impl, ["status", "nodeA:af12"], home)
            assert p.returncode == 0, (name, p.stderr)
            assert p.stdout.strip() == "TUNNEL-PONG", (name, p.stdout)
            # fast path: no mailbox file left behind
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))
            # the hub side got our request with the per-node token
            assert len(tun.received) == 1
            assert tun.received[0]["token"] == "tok-test"
            assert tun.received[0]["request"]["argv"] == ["status", "nodeA:af12"]


def test_tunnel_app_error_is_terminal_no_mailbox():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _FakeTunnel(home, "hubT",
                        lambda req: {"id": req.get("id"), "ok": False,
                                     "error": "unauthorized"},
                        covers=[{"name": "nodeA"}])
            p = _submit_both(impl, ["status", "nodeA:af12"], home)
            assert p.returncode == 1, (name, p.returncode, p.stderr)
            assert "unauthorized" in p.stderr, (name, p.stderr)
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


def test_tunnel_down_falls_back_to_mailbox():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            _write_card(home, "hubT", [{"name": "nodeA"}],
                        tunnel={"port": _closed_port()}, token="tok-test")
            p = _submit_both(impl, ["status", "nodeA:af12"], home,
                             timeout_ms="300")
            assert p.returncode == 2, (name, p.returncode, p.stderr)
            rid, req = _read_single_request(home)
            assert req["argv"] == ["status", "nodeA:af12"], (name, req["argv"])


def test_tunnel_not_my_fleet_cascades_to_next_hub():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            first = _FakeTunnel(home, "hubA",
                                lambda req: {"id": req.get("id"), "ok": False,
                                             "error": "not_my_fleet: no agent"},
                                covers=[{"name": "nodeA"}], token="tok-a")
            second = _FakeTunnel(home, "hubB",
                                 lambda req: {"id": req.get("id"), "ok": True,
                                              "text": "SECOND-HUB"},
                                 covers=[{"name": "nodeA"}], token="tok-b")
            p = _submit_both(impl, ["status", "nodeA:af12"], home)
            assert p.returncode == 0, (name, p.stderr)
            assert p.stdout.strip() == "SECOND-HUB", (name, p.stdout)
            assert len(first.received) == 1 and len(second.received) == 1
            assert _ls(_outbox(home)) == [], (name, _ls(_outbox(home)))


def test_tunnel_skipped_when_target_not_covered():
    for name, impl in _both_drainers():
        with tempfile.TemporaryDirectory() as home:
            _deploy_mailbox(home)
            tun = _FakeTunnel(home, "hubT",
                              lambda req: {"id": req.get("id"), "ok": True},
                              covers=[{"name": "nodeA"}])  # covers nodeA only
            p = _submit_both(impl, ["status", "nodeZ:q1"], home,
                             timeout_ms="300")
            assert p.returncode == 2, (name, p.returncode, p.stderr)
            assert tun.received == [], (name, tun.received)
            _read_single_request(home)  # landed in the mailbox instead


# -------------------------------------------------------------------- runner


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print("PASS %s" % name)
        except Exception as e:
            failed += 1
            import traceback
            print("FAIL %s: %s" % (name, e))
            traceback.print_exc()
    print("\n%d passed, %d failed, %d total" % (passed, failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
