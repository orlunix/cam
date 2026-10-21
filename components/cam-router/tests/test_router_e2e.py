"""Independent Server/Client processes, private homes, sockets and real tmux.

Run after cargo build:
  CAM_ROUTER_BIN_DIR=/path/to/target/debug python3 -m unittest discover -s components/cam-router/tests -v
No existing user agent, installation, SSH configuration or LSF job is touched.
"""
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]
BINDIR = Path(os.environ.get("CAM_ROUTER_BIN_DIR", "/tmp/cam-router-target/debug"))
SERVER = BINDIR / "cam-server"
FORWARDER = BINDIR / "cam-forwarder"
TEST_NODE = Path(os.environ.get("CAM_ROUTER_TEST_NODE_BIN", str(BINDIR / "examples/router_test_node")))
OPS = ["message.send", "agent.send", "agent.capture", "agent.run", "agent.stop",
       "agent.remove", "agent.kill", "agent.reboot", "agent.key", "agent.status", "test.echo"]


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class IPC:
    def __init__(self, node, client=None, token=None):
        self.socket = socket.socket(socket.AF_UNIX)
        self.stream = None
        try:
            self.socket.settimeout(8)
            self.socket.connect(str(node.socket))
            self.stream = self.socket.makefile("rb")
            self.send({"type": "hello", "version": 1, "role": "client" if client else "camc",
                       **({"client_id": client, "token": token} if client else {})})
            self.hello = self.read()
        except BaseException:
            self.close()
            raise

    def send(self, value):
        self.socket.sendall(json.dumps(value, ensure_ascii=False).encode() + b"\n")

    def read(self):
        data = self.stream.readline(4 * 1024 * 1024 + 1)
        if not data:
            raise EOFError("Server closed connection")
        return json.loads(data)

    def close(self):
        if self.stream:
            self.stream.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class Node:
    def __init__(self, suite, label, peers, native=False, echo=True, **limits):
        self.suite, self.label = suite, label
        self.node_id = suite.ids[label]
        self.path = suite.base / label
        self.path.mkdir(mode=0o700)
        self.socket = self.path / "server.sock"
        self.home = self.path / "home"
        (self.home / ".cam").mkdir(parents=True)
        (self.home / "logs").mkdir()
        (self.home / ".cam/logs").symlink_to(self.home / "logs")
        self.env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(ROOT / "src"))
        self.env.pop("TMUX", None)
        self.env.pop("CAM_SERVER_SOCKET", None)
        self.config = {"node_id": self.node_id, "alias": label, "runtime_dir": str(self.path),
                       "discover_camc": True, "echo": echo,
                       "route_ttl_seconds": 6, "retry_ms": 100,
                       "clients": [{"client_id": name, "token_file": str(suite.tokens[name]),
                                    "source_nodes": [suite.ids[p] for p in peers]} for name in suite.tokens],
                       "peers": [{"node_id": suite.ids[p], "aliases": [p], "operations": OPS} for p in peers], **limits}
        if native:
            self.config["backend"] = {"python": sys.executable, "module_path": str(ROOT / "src")}
        self.config_path = self.path / "config.json"
        private_json(self.config_path, self.config)
        self.process = None

    def start(self, production=False):
        self.process = self.suite.spawn([str(SERVER if production else TEST_NODE), "serve", "--config", str(self.config_path)], self.env)
        self.suite.until(lambda: self.socket.exists() and self.status()["node_id"] == self.node_id)
        return self

    def stop(self, crash=False):
        if self.process and self.process.poll() is None:
            (self.process.kill if crash else self.process.terminate)()
            self.process.wait(timeout=8)

    def status(self):
        with IPC(self) as ipc:
            ipc.send({"type": "inspect"})
            return ipc.read()

    def publish(self, message):
        with IPC(self) as ipc:
            ipc.send({"type": "publish", "message": message})
            return ipc.read()

    def query(self, mid):
        with IPC(self) as ipc:
            ipc.send({"type": "query", "message_id": mid})
            return ipc.read()

    def cli(self, *argv, timeout=15):
        return subprocess.run([sys.executable, "-m", "camc_pkg"] + list(argv), env=self.env,
                              cwd=str(self.home), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)


class RouterTests(unittest.TestCase):
    def setUp(self):
        if not SERVER.is_file() or not FORWARDER.is_file() or not TEST_NODE.is_file():
            self.fail("build bins and --features test-harness --examples first; set CAM_ROUTER_BIN_DIR")
        self.temporary = tempfile.TemporaryDirectory(prefix="camr-e2e-")
        self.base = Path(self.temporary.name)
        self.ids = {label: "test-" + label + "-" + uuid.uuid4().hex[:10] for label in "abc"}
        self.tokens = {}
        for name in ("c1", "c2", "injector"):
            path = self.base / (name + ".token")
            path.write_text(uuid.uuid4().hex + uuid.uuid4().hex)
            path.chmod(0o600)
            self.tokens[name] = path
        self.processes, self.logs, self.nodes = [], [], []

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in self.logs:
            log.close()
        # Every path belongs to a freshly generated Node ID in this test.
        for node in self.nodes:
            key = hashlib.sha256(node.node_id.encode()).hexdigest()[:24]
            shutil.rmtree("/tmp/cam-server-%d-%s" % (os.getuid(), key), ignore_errors=True)
        self.temporary.cleanup()

    def spawn(self, argv, env=None):
        log = open(self.base / ("process-%d.log" % len(self.processes)), "wb")
        self.logs.append(log)
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.processes.append(process)
        return process

    def until(self, condition, timeout=12):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            try:
                value = condition()
                if value:
                    return value
            except (OSError, EOFError, ValueError, KeyError) as exc:
                last = exc
            time.sleep(0.05)
        details = "\n".join(p.read_text(errors="replace")[-1800:] for p in self.base.glob("*.log"))
        self.fail("condition timed out: %r\n%s" % (last, details))

    def node(self, label, peers, **kwargs):
        node = Node(self, label, peers, **kwargs)
        self.nodes.append(node)
        return node

    def relay(self, name, nodes, agents=None, stdio=False):
        path = self.base / (name + ".json")
        endpoints = []
        for node in nodes:
            endpoint = {"node_id": node.node_id, "token_file": str(self.tokens[name])}
            if stdio:
                endpoint["command"] = [str(SERVER), "connect", "--socket", str(node.socket)]
            else:
                endpoint["socket"] = str(node.socket)
            endpoints.append(endpoint)
        private_json(path, {"client_id": name, "endpoints": endpoints, "agents": agents or []})
        process = self.spawn([str(FORWARDER), "--config", str(path)])
        for node in nodes:
            self.until(lambda: any(c["client_id"] == name and c["ready"] for c in node.status()["clients"]))
        return process

    def echo(self, source="a", target="b", text="hello"):
        return {"type": "message", "version": 1, "message_id": uuid.uuid4().hex,
                "source": {"node_id": self.ids[source]},
                "target": {"node_id": self.ids[target], "receiver_id": "test.echo"},
                "operation": "test.echo", "payload": {"text": text},
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))}

    def fake_backend(self, node):
        """Fault-injection SDK, isolated from installed packages and real agents."""
        package = self.base / "fake-sdk/camc_pkg"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("")
        marker, pidfile = self.base / "executions", self.base / "worker.pid"
        script = '''import concurrent.futures, json, os, sys, threading, time, uuid
lock = threading.Lock()
def emit(v):
    with lock:
        print(json.dumps(v), flush=True)
open(PIDFILE, "w").write(str(os.getpid()))
emit({"type":"backend.ready", "version":1, "node_id":os.environ["CAM_SERVER_WORKER_NODE"]})
def execute(m):
    with open(MARKER, "a") as f:
        f.write(m["source"]["node_id"] + " " + m["message_id"] + "\\n")
    time.sleep(60 if m["operation"] == "agent.run" else .15)
    result = {"type":"message", "version":1, "message_id":uuid.uuid4().hex,
              "reply_to":m["message_id"], "source":m["target"],
              "target":{"node_id":m["source"]["node_id"], "receiver_id":"operation.results"},
              "operation":"operation.result", "payload":{"stdout":m["source"]["node_id"], "stderr":"", "exit_code":0, "status":"ok"},
              "expires_at":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()+3600))}
    emit({"type":"backend.result", "source_node":m["source"]["node_id"], "request_id":m["message_id"], "message":result})
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    for line in sys.stdin:
        pool.submit(execute, json.loads(line))
'''
        (package / "server_worker.py").write_text("MARKER=%r\nPIDFILE=%r\n" % (str(marker), str(pidfile)) + script)
        node.config["backend"] = {"python": sys.executable, "module_path": str(package.parent)}
        private_json(node.config_path, node.config)
        return marker, pidfile

    def answer(self, node, message):
        return self.until(lambda: next((r for r in node.query(message["message_id"])["results"]
                                       if r.get("operation") == "operation.result"), None))

    def test_independent_servers_clients_and_unmodified_frames(self):
        a, b, c = self.node("a", "bc").start(), self.node("b", "ac").start(), self.node("c", "ab").start()
        self.relay("c1", [a, b, c])
        self.relay("c2", [a, b, c], stdio=True)
        message = self.echo(text="\x1b[31m中文\x1b[0m\n")
        raw = json.dumps(message, ensure_ascii=False, separators=(", ", " : ")).encode() + b" \n"
        with IPC(a) as ipc:
            ipc.socket.sendall(raw)
            answer = ipc.read()
        self.assertEqual(answer["payload"]["stdout"], message["payload"]["text"])
        self.assertNotEqual(answer["message_id"], message["message_id"])
        with sqlite3.connect(str(b.path / "router.db")) as db:
            received, = db.execute("SELECT raw FROM messages WHERE id=? AND direction='in'", (message["message_id"],)).fetchone()
        self.assertEqual(received, raw)
        def send(index):
            msg = self.echo(target="b" if index % 2 else "c", text=str(index))
            a.publish(msg)
            return self.answer(a, msg)["payload"]["stdout"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(send, range(12))), list(map(str, range(12))))

    def test_pending_survives_source_crash_and_target_comes_online(self):
        a = self.node("a", "b").start()
        b = self.node("b", "a")
        message = self.echo()
        self.assertEqual(a.publish(message)["type"], "accepted")
        a.stop(crash=True)
        a.start()
        b.start()
        self.relay("c1", [a, b])
        self.assertEqual(self.answer(a, message)["payload"]["stdout"], "hello")
        b.stop(crash=True)
        b.start()
        a.publish(message)
        self.until(lambda: b.status()["storage"]["echo_count"] == 1)

    def test_target_dedup_across_two_authenticated_paths_and_conflict(self):
        a, b = self.node("a", "b").start(), self.node("b", "a").start()
        message = self.echo()
        a.publish(message)
        for name in ("c1", "c2"):
            with IPC(b, name, self.tokens[name].read_text()) as ipc:
                ipc.send(message)
                self.until(lambda: b.status()["storage"]["echo_count"] == 1)
        self.assertEqual(b.status()["storage"]["echo_count"], 1)
        changed = copy.deepcopy(message)
        changed["payload"]["text"] = "different"
        self.assertEqual(a.publish(changed)["type"], "error")
        self.relay("c1", [a, b])
        self.assertEqual(self.answer(a, message)["payload"]["stdout"], "hello")

    def test_lock_is_independent_of_requested_runtime_directory(self):
        a = self.node("a", "b").start()
        other = dict(a.config, runtime_dir=str(self.base / "other"))
        path = self.base / "duplicate.json"
        private_json(path, other)
        result = subprocess.run([str(SERVER), "serve", "--config", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("already running", result.stderr)

    def test_production_lock_is_machine_wide_even_for_different_node_ids(self):
        a = self.node("a", "b", discover_camc=False).start(production=True)
        b = self.node("b", "a", discover_camc=False)
        result = subprocess.run([str(SERVER), "serve", "--config", str(b.config_path)], env=b.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("machine/user is already running", result.stderr)
        self.assertEqual(a.status()["node_id"], a.node_id)

    def test_route_ttl_stale_snapshot_and_replacement_generation(self):
        a = self.node("a", "b").start()
        token = self.tokens["c1"].read_text()
        old = IPC(a, "c1", token)
        def snapshot(ipc, seq, ttl=1):
            ipc.send({"type":"routes.snapshot", "session_id":ipc.hello["session_id"],
                      "seq":seq, "ttl_seconds":ttl, "reachable":[self.ids["b"]]})
        try:
            snapshot(old, 2)
            self.until(lambda: a.status()["clients"][0]["ready"])
            snapshot(old, 1, ttl=6)  # stale sequence must not extend the lease
            self.until(lambda: not a.status()["clients"][0]["ready"], timeout=3)
            with IPC(a, "c1", token) as new:
                snapshot(new, 1, ttl=6)
                self.until(lambda: a.status()["clients"][0]["ready"])
                old.close()
                self.until(lambda: a.status()["clients"][0]["session_id"] == new.hello["session_id"])
                self.assertTrue(a.status()["clients"][0]["ready"])
        finally:
            old.close()

    def test_not_forwarded_status_then_alternate_client_delivers_same_id(self):
        a, b = self.node("a", "b").start(), self.node("b", "a").start()
        with IPC(a, "c1", self.tokens["c1"].read_text()) as broken:
            broken.send({"type":"routes.snapshot", "session_id":broken.hello["session_id"],
                         "seq":1, "ttl_seconds":6, "reachable":[b.node_id]})
            message = self.echo()
            a.publish(message)
            forwarded = broken.read()
            self.assertEqual(forwarded["message_id"], message["message_id"])
            broken.send({"type":"forward.error", "message_id":message["message_id"],
                         "target_node":b.node_id, "status":"not_forwarded"})
            self.until(lambda: a.query(message["message_id"])["error"] == "not_forwarded")
            self.relay("c2", [a, b])
            self.assertEqual(self.answer(a, message)["payload"]["stdout"], "hello")
            self.assertEqual(b.status()["storage"]["echo_count"], 1)

    def test_full_queue_rejects_admission_and_expired_work_never_executes(self):
        a = self.node("a", "b", max_messages=1).start()
        message = self.echo()
        message["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()+2))
        self.assertEqual(a.publish(message)["type"], "accepted")
        rejected = a.publish(self.echo())
        self.assertEqual(rejected["type"], "error")
        self.assertIn("QUEUE_FULL", rejected["error"])
        self.until(lambda: a.query(message["message_id"])["state"] == "expired")
        b = self.node("b", "a").start()
        self.relay("c1", [a, b])
        self.assertEqual(b.status()["storage"]["echo_count"], 0)

    def test_backend_keys_include_source_node_when_message_ids_collide(self):
        a, c = self.node("a", "b").start(), self.node("c", "b").start()
        b = self.node("b", "ac")
        marker, unused_pid = self.fake_backend(b)
        b.start()
        self.relay("c1", [a, b, c])
        first = self.echo()
        first.update(operation="agent.status", target={"node_id":b.node_id, "agent_id":"beefcafe"})
        second = copy.deepcopy(first)
        second["source"]["node_id"] = c.node_id
        a.publish(first)
        c.publish(second)
        self.assertEqual(self.answer(a, first)["payload"]["stdout"], a.node_id)
        self.assertEqual(self.answer(c, second)["payload"]["stdout"], c.node_id)
        self.assertEqual(len(marker.read_text().splitlines()), 2)

    def test_crash_during_execution_reports_unknown_without_replay(self):
        a = self.node("a", "b").start()
        b = self.node("b", "a")
        marker, pidfile = self.fake_backend(b)
        b.start()
        self.relay("c1", [a, b])
        message = self.echo()
        message.update(operation="agent.run", target={"node_id":b.node_id, "agent_id":"beefcafe"})
        a.publish(message)
        self.until(lambda: marker.exists())
        pid = int(pidfile.read_text())
        b.stop(crash=True)
        def worker_exited():
            stat = Path("/proc/%d/stat" % pid)
            return not stat.exists() or stat.read_text().rsplit(") ", 1)[1].startswith("Z")
        self.until(worker_exited)
        b.start()
        answer = self.answer(a, message)
        self.assertEqual(answer["payload"]["status"], "unknown")
        self.assertEqual(answer["payload"]["exit_code"], 2)
        a.publish(message)
        self.assertEqual(len(marker.read_text().splitlines()), 1)

    def test_auth_source_scope_expiry_and_unsupported_operations(self):
        a, b = self.node("a", "b").start(), self.node("b", "a").start()
        with self.assertRaises((EOFError, OSError)):
            IPC(b, "injector", "wrong-token")
        message = self.echo()
        message["source"]["node_id"] = "impostor"
        with IPC(b, "injector", self.tokens["injector"].read_text()) as ipc:
            ipc.send(message)
            with self.assertRaises((EOFError, OSError)):
                ipc.read()
        for mutation in ({"expires_at": "2000-01-01T00:00:00Z"}, {"operation": "agent.list"},
                         {"operation": "agent.status", "target": {"node_id": b.node_id}}):
            message = self.echo()
            message.update(mutation)
            self.assertEqual(a.publish(message)["type"], "error")
        self.assertEqual(b.status()["storage"]["echo_count"], 0)

    def test_owner_pairing_is_live_idempotent_and_preserves_static_grants(self):
        a = self.node("a", "b", native=True).start()
        original = a.config_path.read_bytes()
        epoch = a.status()["server_epoch"]
        name = "new-tui-" + uuid.uuid4().hex
        command = [str(SERVER), "pair", "--config", str(a.config_path), "--client-id", name]
        result = subprocess.run(command, env=a.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        pairing = json.loads(result.stdout)
        self.assertEqual(pairing["node_id"], a.node_id)
        self.assertEqual(a.status()["server_epoch"], epoch)
        with IPC(a, name, pairing["token"]) as client:
            self.assertTrue(client.hello["management"])
            client.send({"type":"sdk.request", "id":"paired-snapshot", "action":"agents.snapshot", "args":{}})
            self.assertEqual(client.read()["data"]["agents"], [])
        again = subprocess.run(command, env=a.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout)["token"], pairing["token"])
        self.assertEqual(a.config_path.read_bytes(), original)
        token = Path(a.config["runtime_dir"]) / "paired" / (name + ".token")
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        with self.assertRaises((EOFError, OSError)):
            IPC(a, name, "wrong-token")
        invalid = subprocess.run(command[:-1] + ["../escape"], env=a.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        a.stop()
        a.start()
        with IPC(a, name, pairing["token"]) as client:
            self.assertTrue(client.hello["management"])

    def test_metadata_cache_ambiguity_dead_agents_and_cli_error_output(self):
        a, b = self.node("a", "b").start(), self.node("b", "a", echo=False).start()
        agents = [{"node_id": b.node_id, "id": "beefcafe", "name": "dead-worker", "status": "dead"},
                  {"node_id": b.node_id, "id": "cafe0001", "name": "dup", "status": "alive"},
                  {"node_id": b.node_id, "id": "cafe0002", "name": "dup", "status": "alive"}]
        self.relay("c1", [a, b], agents=agents)
        result = a.cli("--json", "status", "dead-worker")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNSUPPORTED_OPERATION", result.stderr)
        result = a.cli("status", "dup")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(b.status()["storage"]["echo_count"], 0)

    @unittest.skipUnless(shutil.which("tmux"), "real mailbox requires tmux")
    def test_real_agent_to_agent_mailbox_replies_are_new_messages(self):
        a, b = self.node("a", "b", native=True).start(), self.node("b", "a", native=True).start()
        self.relay("c1", [a, b])
        tool = self.base / "chat-tool"
        tool.write_text("#!/bin/sh\nprintf 'CAM_CHAT_READY\\n'\nexec /bin/cat\n")
        tool.chmod(0o700)
        agents = []
        try:
            for source, target in ((a, b), (b, a)):
                run = source.cli("run", "--node", target.label, "--tool", str(tool), "--path", str(self.base),
                                 "--tool-launcher", "none", "--no-default-api")
                self.assertEqual(run.returncode, 0, run.stderr)
                record = json.loads((target.home / ".cam/agents.json").read_text())[0]
                agents.append((target, record["id"]))
                self.until(lambda: "CAM_CHAT_READY" in target.cli("capture", record["id"]).stdout)
            aid = next(agent for node, agent in agents if node is a)
            bid = next(agent for node, agent in agents if node is b)
            first = self.echo()
            first.update(source={"node_id":a.node_id, "agent_id":aid},
                         target={"node_id":b.node_id, "agent_id":bid}, operation="message.send",
                         payload={"args":{"to":bid, "text":"first turn", "msg_cmd":"send", "no_wait":True}})
            first["thread_id"] = first["message_id"]
            a.publish(first)
            self.assertEqual(self.answer(a, first)["payload"]["exit_code"], 0)
            reply = b.cli("msg", "reply", first["message_id"], "-t", "reply to first")
            self.assertEqual(reply.returncode, 0, reply.stderr)
            second_id = next(s.split("=", 1)[1] for s in reply.stdout.splitlines() if s.startswith("MESSAGE_ID="))
            self.assertNotEqual(second_id, first["message_id"])
            self.until(lambda: "reply to first" in a.cli("capture", aid).stdout)
            again = a.cli("msg", "reply", second_id, "-t", "third turn")
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("THREAD_ID=" + first["message_id"], again.stdout)
            self.until(lambda: "third turn" in b.cli("capture", bid).stdout)
        finally:
            for node, agent in agents:
                result = node.cli("rm", agent)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("tmux"), "real lifecycle requires tmux")
    def test_camc_native_lifecycle_and_reply_returns_over_other_client(self):
        a = self.node("a", "b").start()
        b = self.node("b", "a", native=True).start()
        c1, c2 = self.relay("c1", [a, b]), self.relay("c2", [a, b])
        tool = self.base / "disposable-tool"
        tool.write_text("#!/bin/sh\nprintf 'CAM_ROUTER_READY\\n'\nexec /bin/cat\n")
        tool.chmod(0o700)
        agent_id = None
        def records():
            path = b.home / ".cam/agents.json"
            return json.loads(path.read_text()) if path.exists() else []
        def call(*argv):
            result = a.cli(*argv)
            self.assertEqual(result.returncode, 0, (argv, result.stdout, result.stderr))
            return result
        def ready():
            def check():
                current = next((r for r in records() if r["id"] == agent_id), None)
                if current and not current.get("runtime", {}).get("preparing"):
                    return "CAM_ROUTER_READY" in call("capture", "b:" + agent_id).stdout
                return False
            self.until(check)
        try:
            call("run", "--node", "b", "--tool", str(tool), "--path", str(self.base),
                 "--tool-launcher", "none", "--no-default-api")
            self.until(lambda: records())
            agent_id = records()[0]["id"]
            ready()
            call("send", "b:" + agent_id, "-t", "ROUTER_INPUT")
            call("key", "b:" + agent_id, "--key", "Enter")
            record = json.loads(call("--json", "status", "b:" + agent_id).stdout)
            self.assertEqual(record["id"], agent_id)
            delivery = call("msg", "send", "b:" + agent_id, "-t", "question", "--no-wait")
            mid = next(line.split("=", 1)[1] for line in delivery.stdout.splitlines() if line.startswith("MSG_ID="))
            c1.terminate()
            c1.wait(timeout=5)
            self.until(lambda: all(c["client_id"] != "c1" for c in b.status()["clients"]))
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(call, "msg", "wait", mid, "--timeout", "8")
                # A waiting business reply never occupies the forwarding channel.
                self.assertIn("CAM_ROUTER_READY", call("capture", "b:" + agent_id).stdout)
                reply = b.cli("msg", "reply", mid, "-t", "answer via c2")
                self.assertEqual(reply.returncode, 0, reply.stderr)
                self.assertEqual(waiting.result(timeout=10).stdout, "answer via c2\n")
            self.assertIsNone(c2.poll())
            original_launch = next(r["launch_id"] for r in records() if r["id"] == agent_id)
            call("reboot", "b:" + agent_id, "--tool-launcher", "none")
            ready()
            new_launch = next(r["launch_id"] for r in records() if r["id"] == agent_id)
            self.assertNotEqual(original_launch, new_launch)
            call("stop", "b:" + agent_id)
            self.assertEqual(records()[0]["state"], "stopped")
            call("reboot", "b:" + agent_id, "--tool-launcher", "none")
            ready()
            call("kill", "b:" + agent_id)
            self.assertEqual(records()[0]["state"], "killed")
            call("rm", "b:" + agent_id)
            self.assertEqual(records(), [])
        finally:
            if agent_id and records():
                # Only the ID and HOME created by this test may be removed.
                cleanup = b.cli("rm", agent_id)
                self.assertEqual(cleanup.returncode, 0, cleanup.stderr)


if __name__ == "__main__":
    unittest.main()
