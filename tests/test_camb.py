#!/usr/bin/env python3
"""Tests for apps/cam-desktop/resources/camb/camb (thin fleet-bridge client).

Runs camb as a subprocess with HOME pointed at a tmp dir (env override) and
imports it via importlib for unit-level checks. No third-party deps: plain
pytest-style test functions plus a __main__ runner (this WSL has no pytest).
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMB = os.path.join(REPO_ROOT, "apps", "cam-desktop", "resources", "camb", "camb")


def _load_camb():
    """Import the extensionless camb script as a module (fresh each call)."""
    import importlib.machinery
    name = "camb_mod_%s" % os.getpid()
    loader = importlib.machinery.SourceFileLoader(name, CAMB)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_camb(args, home, env_extra=None, stdin=None, cwd=None):
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("TMUX", None)  # never leak the real tmux socket into tests
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, CAMB] + args,
        env=env,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        cwd=cwd,
    )


def _outbox(home):
    return os.path.join(home, ".cam", "camb", "outbox")


def _inbox(home):
    return os.path.join(home, ".cam", "camb", "inbox")


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


def _failing_hub(home):
    """Pre-place inbox responses the moment a request lands in the outbox.

    Returns a watcher object with .start()/.stop(); run the camb subprocess
    from another thread so the round trip works without a real hub."""

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


# ---------------------------------------------------------------- unit-level


def test_tmux_parsing():
    mod = _load_camb()
    old = os.environ.get("TMUX")
    try:
        os.environ["TMUX"] = "/tmp/cam-sockets/cam-af12be34.sock,1234,0"
        assert mod._from_agent() == "af12be34"
        os.environ["TMUX"] = "/tmp/tmux-1000/default,5678,0"
        assert mod._from_agent() is None
        os.environ.pop("TMUX", None)
        assert mod._from_agent() is None
    finally:
        if old is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = old


def test_version_constant():
    mod = _load_camb()
    assert mod.__version__ == "0.1.0"


# ------------------------------------------------------------- submit/atomic


def test_submit_atomic_request_shape():
    with tempfile.TemporaryDirectory() as home:
        # no inbox response ever -> exit 2, but the request must persist
        p = _run_camb(["send", "x", "--text", "hi"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, p.stderr
        names = _ls(_outbox(home))
        assert len(names) == 1 and names[0].endswith(".json"), names
        assert not any(n.endswith(".tmp") for n in names), names
        req_id, req = _read_single_request(home)
        assert req["id"] == req_id and len(req["id"]) == 32
        assert req["argv"] == ["send", "x", "--text", "hi"]  # verb included, verbatim
        assert req["from"] == {"agent": None}  # TMUX stripped in _run_camb
        assert isinstance(req["ts"], float)
        assert abs(req["ts"] - time.time()) < 60


def test_submit_from_agent_via_tmux():
    with tempfile.TemporaryDirectory() as home:
        p = _run_camb(
            ["status", "x"], home,
            env_extra={"CAMB_TIMEOUT_MS": "300",
                       "TMUX": "/tmp/cam-sockets/cam-af12be34.sock,1,0"})
        assert p.returncode == 2
        _, req = _read_single_request(home)
        assert req["from"] == {"agent": "af12be34"}


def test_roundtrip_ok_prints_and_cleans_inbox():
    with tempfile.TemporaryDirectory() as home:
        watcher = _failing_hub(home)(respond={"id": "?", "ok": True,
                                              "text": "hello back"})
        watcher.start()
        try:
            p = _run_camb(["list"], home)
        finally:
            watcher.stop()
        assert p.returncode == 0, p.stderr
        assert p.stdout == "hello back\n"
        # the one outbox request was consumed by the watcher -> inbox file
        # must be deleted by camb after reading
        assert _ls(_inbox(home)) == [], _ls(_inbox(home))


def test_roundtrip_error_exit_1():
    with tempfile.TemporaryDirectory() as home:
        watcher = _failing_hub(home)(respond={"ok": False, "error": "boom"})
        watcher.start()
        try:
            p = _run_camb(["status", "x"], home)
        finally:
            watcher.stop()
        assert p.returncode == 1
        assert "camb: boom" in p.stderr
        assert _ls(_inbox(home)) == []


def test_roundtrip_candidates_listed():
    with tempfile.TemporaryDirectory() as home:
        resp = {"ok": False, "error": "ambiguous target",
                "candidates": ["n1:af12 alpha", "n2:af12 beta"]}
        watcher = _failing_hub(home)(respond=resp)
        watcher.start()
        try:
            p = _run_camb(["capture", "af12"], home)
        finally:
            watcher.stop()
        assert p.returncode == 1
        assert "camb: ambiguous target" in p.stderr
        assert "n1:af12 alpha" in p.stderr
        assert "n2:af12 beta" in p.stderr


def test_timeout_exit_2_message():
    with tempfile.TemporaryDirectory() as home:
        t0 = time.time()
        p = _run_camb(["list"], home, env_extra={"CAMB_TIMEOUT_MS": "500"})
        dt = time.time() - t0
        assert p.returncode == 2
        assert ("camb: bridge unavailable (hub not connected or bridge "
                "disabled on this node)") in p.stderr
        assert dt < 5, "timeout not honored: %.1fs" % dt


# ------------------------------------------------------------------ hub-drain


def test_hub_drain_empty():
    with tempfile.TemporaryDirectory() as home:
        p = _run_camb(["--hub-drain"], home)
        assert p.returncode == 0
        assert json.loads(p.stdout) == []


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
        p = _run_camb(["--hub-drain"], home)
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
        p = _run_camb(["--hub-drain"], home)
        assert p.returncode == 0
        assert json.loads(p.stdout) == []
        assert os.path.exists(bad)  # unreadable file left in place


# ------------------------------------------------------------- file/stdin


def test_send_file_resolved_to_text():
    with tempfile.TemporaryDirectory() as home:
        payload = os.path.join(home, "payload.txt")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write("héllo file\n")
        p = _run_camb(["send", "x", "--file", payload], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        assert p.returncode == 2, p.stderr
        _, req = _read_single_request(home)
        assert req["argv"] == ["send", "x", "--text", "héllo file\n"], req["argv"]


def test_send_stdin_resolved_to_text():
    with tempfile.TemporaryDirectory() as home:
        p = _run_camb(["send", "x", "--stdin", "--no-enter"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"},
                      stdin="typed text")
        assert p.returncode == 2, p.stderr
        _, req = _read_single_request(home)
        assert req["argv"] == ["send", "x", "--text", "typed text", "--no-enter"]


def test_send_short_f_resolved():
    with tempfile.TemporaryDirectory() as home:
        payload = os.path.join(home, "p.txt")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write("short flag")
        p = _run_camb(["send", "x", "-f", payload], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        _, req = _read_single_request(home)
        assert req["argv"] == ["send", "x", "--text", "short flag"]


def test_other_verbs_pass_through_untouched():
    with tempfile.TemporaryDirectory() as home:
        # --file must NOT be resolved for non-send verbs
        p = _run_camb(["msg", "send", "x", "-t", "hi"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        _, req = _read_single_request(home)
        assert req["argv"] == ["msg", "send", "x", "-t", "hi"]
        os.unlink(os.path.join(_outbox(home), req["id"] + ".json"))
        # node:target prefix passes through unparsed
        p = _run_camb(["capture", "node2:af12", "--lines", "3"], home,
                      env_extra={"CAMB_TIMEOUT_MS": "300"})
        _, req = _read_single_request(home)
        assert req["argv"] == ["capture", "node2:af12", "--lines", "3"]


# -------------------------------------------------------------- version/help


def test_version_and_help():
    with tempfile.TemporaryDirectory() as home:
        for args, needle in (
            (["version"], "camb 0.1.0"),
            (["--version"], "camb 0.1.0"),
            (["--help"], "unsupported verbs are rejected by the hub"),
            (["-h"], "unsupported verbs are rejected by the hub"),
            ([], "unsupported verbs are rejected by the hub"),
        ):
            p = _run_camb(args, home)
            assert p.returncode == 0, (args, p.stderr)
            assert needle in p.stdout, (args, p.stdout)
        help_out = _run_camb(["--help"], home).stdout
        for verb in ("list", "status", "capture", "send", "key", "msg"):
            assert verb in help_out
        assert "<node>:<id-or-name>" in help_out


def test_dirs_created_0700():
    with tempfile.TemporaryDirectory() as home:
        _run_camb(["list"], home, env_extra={"CAMB_TIMEOUT_MS": "200"})
        for sub in ("outbox", "inbox"):
            d = os.path.join(home, ".cam", "camb", sub)
            assert os.path.isdir(d), d
            mode = os.stat(d).st_mode & 0o777
            assert mode == 0o700, "%s mode is %o" % (d, mode)


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
