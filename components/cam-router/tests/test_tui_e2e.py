"""Real daemon/Server/tmux/control/PTY tests; only isolated disposable agents."""
import base64
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import socket
import socketserver
import struct
import sys
import subprocess
import termios
import time
import threading
import unittest
import uuid

import test_router_e2e as routing

TUI = routing.BINDIR / "cam-tui"
DAEMON = routing.BINDIR / "cam-clientd"


class TuiTests(unittest.TestCase):
    spawn = routing.RouterTests.spawn
    until = routing.RouterTests.until
    node = routing.RouterTests.node
    echo = routing.RouterTests.echo
    answer = routing.RouterTests.answer

    def setUp(self):
        routing.RouterTests.setUp(self)
        self.daemon = None
        self.agents_created = []
        self.env = dict(os.environ, TERM="xterm-256color")
        self.runtime = self.base / "client"
        self.runtime.mkdir(mode=0o700)
        self.client_config = self.base / "client.json"
        self.client_socket = self.runtime / "client.sock"
        self.ui_fds = []

    def tearDown(self):
        # Close UI views first; daemon shutdown does not own the tmux tool.
        for process, fd in self.ui_fds:
            if process.poll() is None:
                os.write(fd, b"\x11")
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            os.close(fd)
        for node, agent in self.agents_created:
            result = node.cli("rm", agent)
            self.assertEqual(result.returncode, 0, result.stderr)
        if self.daemon and self.daemon.poll() is None:
            try:
                self.rpc("shutdown")
                self.daemon.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                self.daemon.terminate()
                self.daemon.wait(timeout=8)
        routing.RouterTests.tearDown(self)

    def managed_node(self, label="a", peers="b"):
        node = self.node(label, peers, native=True)
        for grant in node.config["clients"]:
            grant["management"] = grant["client_id"] != "injector"
        routing.private_json(node.config_path, node.config)
        return node.start()

    def start_daemon(self, nodes, stdio=False, poll_seconds=1):
        endpoints = []
        for node in nodes:
            endpoint = {"node_id": node.node_id, "token_file": str(self.tokens["c1"])}
            if stdio:
                endpoint["command"] = [str(routing.SERVER), "connect", "--socket", str(node.socket)]
            else:
                endpoint["socket"] = str(node.socket)
            endpoints.append(endpoint)
        config = {"client_id": "c1", "runtime_dir": str(self.runtime), "endpoints": endpoints}
        if poll_seconds is not None:
            config["poll_seconds"] = poll_seconds
        routing.private_json(self.client_config, config)
        self.daemon = self.spawn([str(DAEMON), "serve", "--config", str(self.client_config)], self.env)
        self.until(lambda: self.client_socket.exists() and all(n["ready"] for n in self.rpc("snapshot")["nodes"]))
        return self.daemon

    def rpc(self, action, args=None, identity=None):
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(30)
            channel.connect(str(self.client_socket))
            channel.sendall(json.dumps({"version": 1, "id": identity or uuid.uuid4().hex,
                                        "action": action, "args": args or {}}).encode() + b"\n")
            with channel.makefile("rb") as reader:
                reply = json.loads(reader.readline(4 * 1024 * 1024))
        if "error" in reply:
            raise ValueError(reply["error"])
        return reply["data"]

    def operation(self, node, agent, operation, **options):
        result = self.rpc("agent.operation", {"node_id": node.node_id, "agent_id": agent,
                                              "operation": "agent." + operation, "options": options})
        self.assertEqual(result["payload"]["exit_code"], 0, result)
        return result["payload"]

    def create_agent(self, node, name="one"):
        tool = self.base / ("tool-" + name)
        tool.write_text("#!/bin/sh\nprintf '%s\\n' '%begin 1 2 0' '%output %9 literal' '%end 1 2 0'\nprintf '\\033[32mTUI_READY_" + name + " 中文\\033[0m\\n'\nexec /bin/cat\n")
        tool.chmod(0o700)
        agent = uuid.uuid4().hex[:8]
        self.operation(node, agent, "run", tool=str(tool), name=name, path=str(self.base),
                       tool_launcher="none", no_default_api=True)
        self.agents_created.append((node, agent))
        self.until(lambda: "TUI_READY_" + name in self.operation(node, agent, "capture")["stdout"], timeout=20)
        self.until(lambda: any(a["id"] == agent for a in self.rpc("snapshot")["agents"]))
        return agent

    def test_native_management_capability_and_foreign_record_filter(self):
        node = self.managed_node()
        routing.private_json(node.home / ".cam/agents.json", [
            {"id": "11111111", "name": "visible", "status": "alive", "state": "idle"},
            {"id": "22222222", "name": "foreign", "hostname": "another-machine", "status": "alive"}])
        with routing.IPC(node, "injector", self.tokens["injector"].read_text()) as ipc:
            ipc.send({"type": "sdk.request", "id": "deny", "action": "agents.snapshot", "args": {}})
            self.assertIn("not granted", ipc.read()["error"])
        self.start_daemon([node])
        snapshot = self.until(lambda: self.rpc("snapshot") if self.rpc("snapshot")["agents"] else None)
        self.assertEqual([a["id"] for a in snapshot["agents"]], ["11111111"])
        self.assertEqual(snapshot["agents"][0]["state"], "idle")
        node.stop()
        self.until(lambda: not self.rpc("snapshot")["nodes"][0]["ready"])
        self.assertTrue(self.rpc("snapshot")["agents"][0]["stale"])
        self.assertEqual(self.rpc("snapshot")["agents"][0]["status"], "alive")

    def test_old_router_without_management_capability_is_not_disconnected_by_polling(self):
        node = self.node("a", "b")
        unexpected = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                self.wfile.write(json.dumps({"type": "hello", "version": 1, "node_id": node.node_id,
                                             "server_epoch": "old", "session_id": uuid.uuid4().hex,
                                             "route_ttl_seconds": 6, "allowed_targets": []}).encode() + b"\n")
                self.wfile.flush()
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    frame = json.loads(line)
                    if frame["type"] == "ping":
                        self.wfile.write(b'{"type":"pong"}\n')
                        self.wfile.flush()
                    elif frame["type"] != "routes.snapshot":
                        unexpected.append(frame["type"])
                        return
        class Legacy(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        server = Legacy(str(node.socket), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.start_daemon([node])
            time.sleep(1.5)
            self.assertEqual(unexpected, [])
            self.assertTrue(self.rpc("snapshot")["nodes"][0]["ready"])
            self.assertEqual(self.rpc("snapshot")["agents"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_real_terminal_control_two_tabs_and_ui_exit_leaves_daemon_alive(self):
        self.env["NO_COLOR"] = "1"
        node = self.managed_node()
        self.start_daemon([node], stdio=True)
        first, second = self.create_agent(node, "alpha"), self.create_agent(node, "beta")
        terminal = self.rpc("terminal.attach", {"node_id": node.node_id, "agent_id": first})
        tid = terminal["terminal_id"]
        initial = self.until(lambda: self.rpc("terminal.read", {"terminal_id": tid, "initial": True})
                             if self.rpc("terminal.read", {"terminal_id": tid, "initial": True})["snapshot"] else None)
        self.assertIn("TUI_READY_alpha", base64.b64decode(initial["snapshot"]["data"]).decode())
        self.assertIn("%output %9 literal", base64.b64decode(initial["snapshot"]["data"]).decode())
        self.rpc("terminal.input", {"terminal_id": tid, "data": base64.b64encode(b"DIRECT_CONTROL_INPUT\r").decode()})
        self.until(lambda: "DIRECT_CONTROL_INPUT" in self.operation(node, first, "capture")["stdout"])
        for piece in (b"OLD_TMUX_", b";", b"_LITERAL\r"):
            self.rpc("terminal.input", {"terminal_id": tid, "data": base64.b64encode(piece).decode()})
        self.until(lambda: "OLD_TMUX_;_LITERAL" in self.operation(node, first, "capture")["stdout"])
        observer = self.rpc("terminal.attach", {"node_id": node.node_id, "agent_id": first, "view_id": "observer"})
        self.assertTrue(observer["read_only"])
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.rpc("terminal.input", {"terminal_id": tid, "view_id": "observer", "data": base64.b64encode(b"WRONG_VIEW").decode()})
        self.rpc("terminal.release", {"terminal_id": tid})

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        ui = subprocess.Popen([str(TUI), "--config", str(self.client_config)], env=self.env,
                              stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.ui_fds.append((ui, master))
        capture = bytearray()

        def pump(delay=0.25):
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.04)
                if readable:
                    capture.extend(os.read(master, 65536))

        pump(1.5)
        self.assertIsNone(ui.poll(), capture.decode(errors="replace"))
        # Click alpha then beta in the real TUI sidebar, each gets its own input.
        os.write(master, b"\x1b[<0;5;8M\x1b[<0;5;8m")
        self.until(lambda: (pump(0.1), b"TUI_READY_alpha" in capture)[1])
        os.write(master, b"REAL_UI_ALPHA\r")
        pump(0.5)
        self.until(lambda: "REAL_UI_ALPHA" in self.operation(node, first, "capture")["stdout"])
        os.write(master, b"\x1b[<0;5;10M\x1b[<0;5;10m")
        self.until(lambda: (pump(0.1), b"TUI_READY_beta" in capture)[1])
        os.write(master, b"REAL_UI_BETA\r")
        pump(0.5)
        self.until(lambda: "REAL_UI_BETA" in self.operation(node, second, "capture")["stdout"])
        self.assertNotIn("REAL_UI_BETA", self.operation(node, first, "capture")["stdout"])
        # Use the actual right-click menu to reboot beta, then type into the
        # fresh tool through its automatically reattached tab.
        os.write(master, b"\x1b[<2;5;10M\x1b[<2;5;10m")
        pump(0.2)
        os.write(master, b"\x1b[<0;20;11M\x1b[<0;20;11m")
        pump(2)
        os.write(master, b"\x1b")
        pump(5.5)
        os.write(master, b"REAL_UI_AFTER_REBOOT\r")
        pump(0.4)
        self.until(lambda: "REAL_UI_AFTER_REBOOT" in self.operation(node, second, "capture")["stdout"])

        # The real New form uses an explicit local launcher in this isolated
        # test, leaving the production NV/default launcher policy unchanged.
        gamma_tool = self.base / "tool-gamma"
        gamma_tool.write_text("#!/bin/sh\nprintf 'TUI_READY_gamma\\n'\nexec /bin/cat\n")
        gamma_tool.chmod(0o700)
        os.write(master, b"\x1bOP")
        pump(0.2)
        fields = b"\tgamma\t" + b"\x7f" * 5 + str(gamma_tool).encode() + b"\t" + str(self.base).encode() + b"\tnone\t\r"
        os.write(master, fields)
        pump(2)
        gamma = self.until(lambda: next((a for a in self.rpc("snapshot")["agents"] if a["name"] == "gamma"), None))
        self.agents_created.append((node, gamma["id"]))
        self.until(lambda: "TUI_READY_gamma" in self.operation(node, gamma["id"], "capture")["stdout"])
        pump(1)
        os.write(master, b"REAL_UI_NEW_GAMMA\r")
        pump(0.4)
        self.until(lambda: "REAL_UI_NEW_GAMMA" in self.operation(node, gamma["id"], "capture")["stdout"])
        # Page/wheel are intercepted above tmux and do not type their protocol.
        os.write(master, b"\x1b[5~\x1b[<64;60;16M")
        pump(0.2)
        selection_start = len(capture)
        os.write(master, b"\x1b[<0;35;16M\x1b[<32;46;16M\x1b[<0;46;16m")
        pump(0.3)
        selection_output = bytes(capture[selection_start:])
        self.assertIn(b"\x1b[7m", selection_output)
        self.assertNotIn(b"\x1b[7m\x1b[m", selection_output,
                         "NO_COLOR reset the selection attribute before text was drawn")
        os.write(master, b"\x11")
        ui.wait(timeout=5)
        self.assertEqual(ui.returncode, 0)
        self.assertIsNone(self.daemon.poll())
        self.assertEqual(len(self.rpc("snapshot")["agents"]), 3)

    def test_tool_mouse_passthrough_delivers_press_and_release(self):
        node = self.managed_node()
        self.start_daemon([node])
        received = self.base / "mouse-bytes"
        tool = self.base / "mouse-tool"
        tool.write_text("#!" + sys.executable + "\nimport os, tty\n"
                        "tty.setraw(0)\nos.write(1, b'RAW_MOUSE_READY\\r\\n')\n"
                        "while True:\n"
                        "    data = os.read(0, 4096)\n"
                        "    if not data: break\n"
                        "    with open(" + repr(str(received)) + ", 'ab') as stream: stream.write(data)\n")
        tool.chmod(0o700)
        agent = uuid.uuid4().hex[:8]
        self.agents_created.append((node, agent))
        self.operation(node, agent, "run", tool=str(tool), name="mouse-test", path=str(self.base),
                       tool_launcher="none", no_default_api=True)
        self.until(lambda: "RAW_MOUSE_READY" in self.operation(node, agent, "capture")["stdout"])
        self.until(lambda: any(a["id"] == agent for a in self.rpc("snapshot")["agents"]))
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        ui = subprocess.Popen([str(TUI), "--config", str(self.client_config)], env=self.env,
                              stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.ui_fds.append((ui, master))
        output = bytearray()
        def pump():
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                output.extend(os.read(master, 65536))
            return output
        self.until(lambda: b"mouse-test" in pump())
        os.write(master, b"\x1b[<0;5;8M\x1b[<0;5;8m")
        self.until(lambda: b"RAW_MOUSE_READY" in pump())
        os.write(master, b"\x1b[19~")  # F8: explicit mouse passthrough.
        self.until(lambda: b"TOOL MOUSE" in pump())
        os.write(master, b"\x1b[<0;60;16M\x1b[<0;60;16m")
        expected = b"\x1b[<0;27;12M\x1b[<0;27;12m"
        self.until(lambda: (pump(), received.exists() and received.read_bytes() == expected)[1])

    def test_slow_background_poll_refreshes_only_when_operations_need_it(self):
        node = self.managed_node()
        routing.private_json(node.home / ".cam/agents.json", [{
            "id": "11111111", "name": "old-initializing", "status": "alive",
            "state": "running", "phase": "initializing"}])
        self.start_daemon([node], poll_seconds=None)  # Exercise the production default.
        self.until(lambda: self.rpc("health")["nodes"][0]["status_health"] == "healthy")
        before = time.monotonic()
        agent = self.create_agent(node)
        self.until(lambda: any(a["id"] == agent and a["phase"] == "started"
                               for a in self.rpc("snapshot")["agents"]))
        self.assertLess(time.monotonic() - before, 10, "run waited for the 30-second background poll")
        # Once started, the 2-second startup tick must not keep polling it.
        time.sleep(0.3)
        updated = next(a["updated_at"] for a in self.rpc("snapshot")["agents"] if a["id"] == agent)
        time.sleep(2.5)
        self.assertEqual(next(a["updated_at"] for a in self.rpc("snapshot")["agents"] if a["id"] == agent), updated)
        self.operation(node, agent, "stop")
        self.until(lambda: any(a["id"] == agent and a["state"] == "stopped"
                               for a in self.rpc("snapshot")["agents"]))

        # A canceled startup must not leave a ten-minute fast-poll watch.
        # Use an isolated Claude adapter fixture; no real model is launched.
        config_dir = node.home / ".cam/configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "claude.toml").write_text(
            '[launch]\ncommand = ["/bin/sh", "-c", "exec /bin/cat", "test-fixture"]\n'
            'ready_pattern = "^NEVER_READY$"\nstartup_wait = 120.0\n')
        pending = uuid.uuid4().hex[:8]
        self.operation(node, pending, "run", tool="claude", path=str(self.base),
                       tool_launcher="none", no_default_api=True)
        self.agents_created.append((node, pending))
        self.until(lambda: any(a["id"] == pending and a["phase"] == "initializing"
                               for a in self.rpc("snapshot")["agents"]))
        self.operation(node, pending, "remove")
        self.agents_created.remove((node, pending))
        self.until(lambda: not any(a["id"] == pending for a in self.rpc("snapshot")["agents"]))
        time.sleep(0.3)
        updated = next(a["updated_at"] for a in self.rpc("snapshot")["agents"] if a["id"] == agent)
        time.sleep(2.5)
        self.assertEqual(next(a["updated_at"] for a in self.rpc("snapshot")["agents"] if a["id"] == agent), updated)

    def test_scheduler_persists_and_loop_checks_owner_at_target(self):
        node = self.managed_node()
        self.start_daemon([node])
        agent = self.create_agent(node)
        marker = self.base / "job-runs"
        job = self.rpc("job.save", {"kind": "command", "name": "one-time", "next_due": int(time.time()),
                                    "argv": ["/bin/sh", "-c", "printf tick >> " + str(marker)]})
        self.until(lambda: marker.exists())
        self.until(lambda: self.rpc("snapshot")["jobs"][0]["last_status"] == "ok")
        self.assertEqual(marker.read_text(), "tick")
        self.assertFalse(self.rpc("snapshot")["jobs"][0]["enabled"])
        # Busy owner must defer; a second Client cannot claim that same loop.
        loop_id = uuid.uuid4().hex
        with routing.IPC(node, "c2", self.tokens["c2"].read_text()) as ipc:
            ipc.send({"type": "sdk.request", "id": uuid.uuid4().hex, "action": "loop.deliver",
                      "args": {"loop_id": loop_id, "agent_id": agent, "options": {"text": "CONTINUE"}}})
            result = ipc.read()
            self.assertTrue(result["data"]["deferred"], result)
        with routing.IPC(node, "injector", self.tokens["injector"].read_text()) as ipc:
            ipc.send({"type": "sdk.request", "id": uuid.uuid4().hex, "action": "loop.deliver",
                      "args": {"loop_id": loop_id, "agent_id": agent, "options": {"text": "CONTINUE"}}})
            self.assertIn("error", ipc.read())

    def test_client_daemon_carries_bidirectional_forwarding(self):
        a, b = self.managed_node("a", "b"), self.managed_node("b", "a")
        self.start_daemon([a, b], stdio=True)
        for source, target in ((a, b), (b, a)):
            self.until(lambda: any(c["ready"] and target.node_id in c["reachable"] for c in source.status()["clients"]))
            message = self.echo(source=source.label, target=target.label, text="via local TUI daemon")
            source.publish(message)
            self.assertEqual(self.answer(source, message)["payload"]["stdout"], "via local TUI daemon")
        self.rpc("node.disconnect", {"node_id": a.node_id})
        self.until(lambda: next(n for n in self.rpc("snapshot")["nodes"] if n["node_id"] == a.node_id)["ready"] is False)
        self.assertTrue(next(n for n in self.rpc("snapshot")["nodes"] if n["node_id"] == b.node_id)["ready"])
        self.rpc("node.connect", {"node_id": a.node_id})
        self.until(lambda: next(n for n in self.rpc("snapshot")["nodes"] if n["node_id"] == a.node_id)["ready"])

    def test_daemon_restart_preserves_jobs_and_unknown_work_is_not_replayed(self):
        self.start_daemon([])
        marker = self.base / "completed"
        self.rpc("job.save", {"kind": "command", "name": "crash-window", "every_seconds": 1,
                              "next_due": int(time.time()),
                              "argv": ["/bin/sh", "-c", "sleep 2; printf once >> " + str(marker)]})
        self.until(lambda: self.rpc("snapshot")["jobs"][0]["last_status"] == "running")
        self.daemon.kill()
        self.daemon.wait(timeout=5)
        self.start_daemon([])
        self.assertEqual(self.rpc("snapshot")["jobs"][0]["last_status"], "unknown")
        self.until(lambda: marker.exists())
        time.sleep(1.2)
        self.assertEqual(marker.read_text(), "once")
        self.assertEqual(self.rpc("snapshot")["jobs"][0]["last_status"], "unknown")

    def test_disposable_demo_runs_real_shell_and_reclaims_services(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        demo = routing.ROOT / "components/cam-router/scripts/demo-tui.py"
        process = subprocess.Popen([sys.executable, str(demo), "--bin-dir", str(routing.BINDIR),
                                    "--workspace", str(self.base)], env=self.env,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.ui_fds.append((process, master))
        output = bytearray()
        def pump():
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                output.extend(os.read(master, 65536))
            if process.poll() is not None:
                self.fail(output.decode(errors="replace"))
            return b"shell-two" in output
        self.until(pump, timeout=15)
        children = Path("/proc/%d/task/%d/children" % (process.pid, process.pid)).read_text().split()
        agent_sockets = []
        for child in children:
            argv = Path("/proc/%s/cmdline" % child).read_bytes().split(b"\0")
            if any(arg.endswith(b"cam-server") for arg in argv):
                config = Path(os.fsdecode(argv[argv.index(b"--config") + 1]))
                records = json.loads((config.parent / "home/.cam/agents.json").read_text())
                self.assertEqual(len(records), 2)
                for record in records:
                    self.assertEqual(record["status"], "alive", record.get("exit_reason"))
                    self.assertEqual(record["phase"], "started")
                    self.assertEqual(record["runtime"]["custom_launch"]["tool_args"],
                                     ["--noprofile", "--norc"])
                agent_sockets = [Path(r.get("tmux_socket") or r["runtime"]["tmux"]["socket"])
                                 for r in records]
        os.write(master, b"\x1b[<0;5;8M\x1b[<0;5;8m")
        # Rendering the sidebar does not prove control attach is ready. Wait
        # for the real shell prompt before typing, including legacy mode probe.
        self.until(lambda: (pump(), b"bash-" in output)[1])
        marker = self.base / "demo-shell-ok"
        os.write(master, ("printf done > " + str(marker) + "\r").encode())
        try:
            self.until(lambda: (pump(), marker.exists() and marker.read_text() == "done")[1])
        except AssertionError as exc:
            panes = []
            for record, sock in zip(records, agent_sockets):
                panes.append(subprocess.check_output([
                    record["tmux_bin"], "-S", str(sock), "capture-pane", "-p", "-S", "-30",
                    "-t", record["tmux_session"] + ":0.0"]).decode(errors="replace"))
            raise AssertionError(str(exc) + "\nActual demo panes:\n" + "\n".join(panes)) from exc
        self.assertEqual(marker.read_text(), "done")
        os.write(master, b"\x11")
        process.wait(timeout=12)
        self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
        for child in children:
            self.assertFalse(Path("/proc").joinpath(child).exists(), "demo child still running: " + child)
        self.assertTrue(agent_sockets)
        self.assertFalse(any(path.exists() for path in agent_sockets))

    def test_loop_delivers_once_to_real_tool_and_does_not_inject_while_busy(self):
        node = self.managed_node()
        self.start_daemon([node])
        agent = self.create_agent(node)
        loop_id = uuid.uuid4().hex
        args = {"node_id": node.node_id, "agent_id": agent, "loop_id": loop_id,
                "options": {"text": "ENGINEERING_CONTINUE_ONCE"}}
        self.assertTrue(self.rpc("loop.deliver", args)["deferred"])
        # Simulate the authoritative idle transition in this isolated fixture;
        # the target Server must re-read it rather than trusting Client cache.
        def deliver_at_idle():
            path = node.home / ".cam/agents.json"
            records = json.loads(path.read_text())
            for record in records:
                if record["id"] == agent:
                    record.update(status="alive", state="idle", phase="started")
            routing.private_json(path, records)
            return self.rpc("loop.deliver", args).get("result")
        result = self.until(deliver_at_idle)
        self.assertEqual(result["payload"]["exit_code"], 0, result)
        self.until(lambda: "ENGINEERING_CONTINUE_ONCE" in self.operation(node, agent, "capture")["stdout"])
        self.assertTrue(self.rpc("loop.deliver", args)["deferred"])
        self.assertEqual(self.operation(node, agent, "capture")["stdout"].count("ENGINEERING_CONTINUE_ONCE"), 2)  # TTY echo + cat
        with routing.IPC(node, "c2", self.tokens["c2"].read_text()) as ipc:
            ipc.send({"type": "sdk.request", "id": uuid.uuid4().hex, "action": "loop.deliver", "args": args})
            self.assertIn("another scheduling owner", ipc.read()["error"])


if __name__ == "__main__":
    unittest.main()
