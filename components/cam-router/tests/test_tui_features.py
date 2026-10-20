"""Workflow acceptance tests with real UI, SDK, tmux and private Nodes.

The optional clipboard case uses a real X11 display and xclip; no clipboard
helper is mocked. Set CAM_TUI_E2E_CLIPBOARD_BIN_DIR to its directory when the
desktop helper is not on PATH. Only test-owned clipboard text is read.
"""
import codecs
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest
import uuid

import pyte

import test_router_e2e as routing
import test_tui_e2e as baseline
from test_tui_e2e import DAEMON, TUI


class UI:
    def __init__(self, suite, rows=38, cols=150):
        self.suite = suite
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.process = subprocess.Popen(
            [str(TUI), "--config", str(suite.client_config)], env=suite.env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        suite.ui_fds.append((self.process, self.master))
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.raw = bytearray()
        self.until(lambda: "Client ready" in self.pump())

    def pump(self, seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], 0.025)
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.raw.extend(data)
                self.stream.feed(self.decoder.decode(data))
        return "\n".join(self.screen.display)

    def until(self, check, timeout=15):
        def poll():
            self.pump()
            return check()
        try:
            return self.suite.until(poll, timeout=timeout)
        except AssertionError as exc:
            raise AssertionError(str(exc) + '\nVisible UI:\n' + self.pump()) from exc

    def send(self, data):
        os.write(self.master, data)
        self.pump()

    def key(self, data):
        self.send(data)

    def paste(self, text):
        self.send(b"\x1b[200~" + text.encode() + b"\x1b[201~")

    def position(self, label, exact=False):
        self.pump()
        for y, line in enumerate(self.screen.display):
            if label in line and (not exact or re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", line)):
                return line.index(label), y
        raise AssertionError("UI label absent: %r\n%s" % (label, self.pump()))

    def mouse(self, x, y, button=0):
        self.send(("\x1b[<%d;%d;%dM\x1b[<%d;%d;%dm" %
                   (button, x + 1, y + 1, button, x + 1, y + 1)).encode())

    def click(self, label, button=0, exact=False):
        x, y = self.position(label, exact)
        self.mouse(x + min(2, len(label) - 1), y, button)

    def field(self, label, value):
        x, y = self.position(label, exact=True)
        self.mouse(x, y)
        old = self.screen.display[y + 1][x:].split("│", 1)[0].rstrip()
        # Command argv can extend past the form viewport.
        clear = max(len(old) + 2, 512 if label == "Prompt; or command argv JSON" else 0)
        self.send(b"\x7f" * clear)
        self.paste(value)

    def save(self):
        self.click("Save (Ctrl+Enter)")

    def filter(self, value):
        # The box is fixed; its title changes while focused.
        self.mouse(8, 4)
        old = self.screen.display[4][1:31].rstrip()
        self.send(b"\x7f" * (len(old) + 2))
        self.paste(value)
        self.key(b"\r")

    def menu(self, name, action):
        self.pump()
        row = next(y for y, line in enumerate(self.screen.display) if y >= 7 and name in line[:32])
        self.mouse(5, row, button=2)
        self.click(action)

    def close(self):
        self.key(b"\x11")
        self.process.wait(timeout=5)
        self.suite.assertEqual(self.process.returncode, 0)


class FeatureTests(unittest.TestCase):
    setUp = baseline.TuiTests.setUp
    tearDown = baseline.TuiTests.tearDown
    spawn = baseline.TuiTests.spawn
    until = baseline.TuiTests.until
    node = baseline.TuiTests.node
    managed_node = baseline.TuiTests.managed_node
    start_daemon = baseline.TuiTests.start_daemon
    rpc = baseline.TuiTests.rpc
    operation = baseline.TuiTests.operation
    create_agent = baseline.TuiTests.create_agent

    def agent_record(self, node, agent):
        path = node.home / ".cam/agents.json"
        rows = json.loads(path.read_text()) if path.exists() else []
        return next((a for a in rows
                     if a["id"] == agent), None)

    def cached(self, agent):
        return next((a for a in self.rpc("snapshot")["agents"] if a["id"] == agent), None)

    def test_nodes_gui_add_edit_auth_disconnect_remove_preserves_agent(self):
        a, b = self.managed_node("a", "b"), self.managed_node("b", "a")
        self.start_daemon([a])
        ui = UI(self)
        ui.key(b"\x1bOQ")  # F2
        ui.click("+ Add")
        ui.field("Name (optional)", "Remote test")
        ui.field("Server ID (advanced)", b.node_id)
        ui.field("Unix socket (local)", str(b.socket))
        ui.field("Token file (absolute)", str(self.tokens["c2"]))
        ui.save()
        self.until(lambda: any(n["node_id"] == b.node_id and n.get("last_error")
                               for n in self.rpc("snapshot")["nodes"]))
        self.assertFalse(next(n for n in self.rpc("snapshot")["nodes"]
                              if n["node_id"] == b.node_id)["ready"])
        ui.key(b"\x1bOQ")
        ui.click("Remote test · tunnel")
        ui.click("Edit", exact=True)
        ui.field("Token file (absolute)", str(self.tokens["c1"]))
        ui.save()
        self.until(lambda: next(n for n in self.rpc("snapshot")["nodes"]
                                if n["node_id"] == b.node_id)["ready"])
        agent = self.create_agent(b, "node-kept")
        ui.key(b"\x1bOQ")
        ui.click("Remote test · tunnel")
        ui.click("Disconnect", exact=True)
        self.until(lambda: not next(n for n in self.rpc("snapshot")["nodes"]
                                    if n["node_id"] == b.node_id)["ready"])
        ui.click("Connect", exact=True)
        self.until(lambda: next(n for n in self.rpc("snapshot")["nodes"]
                                if n["node_id"] == b.node_id)["ready"])
        ui.click("Remove", exact=True)
        self.until(lambda: all(n["node_id"] != b.node_id for n in self.rpc("snapshot")["nodes"]))
        self.assertEqual(b.status()["node_id"], b.node_id)
        self.assertIn("TUI_READY_node-kept", b.cli("capture", agent).stdout)
        self.assertEqual(json.loads(self.client_config.read_text())["endpoints"][0]["node_id"], a.node_id)

    def test_new_agent_chooses_machine_name_and_routes_to_its_original_id(self):
        a, b = self.managed_node("a", "b"), self.managed_node("b", "a")
        self.start_daemon([a, b])
        for node, name in ((a, "PDX046"), (b, "DC7")):
            self.rpc("node.upsert", {"node_id": node.node_id, "name": name,
                "socket": str(node.socket), "token_file": str(self.tokens["c1"])})
        self.until(lambda: all(n["ready"] for n in self.rpc("snapshot")["nodes"]))
        ui = UI(self)
        ui.key(b"\x1bOP")
        ui.click("Node", exact=True)
        ui.until(lambda: "Choose machine" in ui.pump())
        ui.click("DC7")
        ui.until(lambda: "New agent" in ui.pump() and "DC7" in ui.pump())
        tool = self.base / "node-picker-tool"
        tool.write_text("#!/bin/sh\necho NODE_PICKER_READY\nexec /bin/cat\n")
        tool.chmod(0o700)
        ui.field("Name", "picked-target")
        ui.field("Tool", str(tool))
        ui.field("Workspace", str(self.base))
        ui.field("Launcher (blank=default, none=local)", "none")
        ui.save()
        agent = self.until(lambda: next((r for r in self.rpc("snapshot")["agents"]
            if r["name"] == "picked-target" and r["phase"] == "started"), None))
        self.agents_created.append((b, agent["id"]))
        self.assertEqual(agent["node_id"], b.node_id)
        self.assertIsNone(self.agent_record(a, agent["id"]))
        self.assertIsNotNone(self.agent_record(b, agent["id"]))
        ui.until(lambda: "NODE_PICKER_READY" in ui.pump())
        ui.filter("DC7")
        ui.until(lambda: "picked-target" in ui.pump())
        ui.key(b"\x1bOQ")
        ui.click("DC7 · tunnel")
        ui.click("Edit", exact=True)
        ui.field("Name (optional)", "DC7 renamed")
        ui.save()
        ui.until(lambda: any(e.get("name") == "DC7 renamed" for e in self.rpc("snapshot")["connections"]))
        saved = next(e for e in json.loads(self.client_config.read_text())["endpoints"] if e["node_id"] == b.node_id)
        self.assertEqual(saved["name"], "DC7 renamed")

    def test_gui_agent_operations_and_invalid_new_agent(self):
        node = self.managed_node()
        self.start_daemon([node])
        agent = self.create_agent(node, "operations")
        ui = UI(self)
        ui.click("operations")
        ui.until(lambda: "TUI_READY_operations" in ui.pump())
        for label, expected in (("Status", agent), ("Capture output", "TUI_READY_operations")):
            ui.menu("operations", label)
            ui.until(lambda: "Operation:" in ui.pump())
            if label == "Capture output":
                for _ in range(5):
                    if expected in ui.pump():
                        break
                    ui.key(b"\x1b[6~")
            ui.until(lambda: "Operation:" in ui.pump() and expected in ui.pump())
            ui.key(b"\x1b")
        ui.menu("operations", "Send prompt")
        ui.field("Prompt", "PROMPT_FROM_GUI")
        ui.save()
        self.until(lambda: "PROMPT_FROM_GUI" in self.operation(node, agent, "capture")["stdout"])
        ui.until(lambda: "Operation:" in ui.pump())
        ui.key(b"\x1b")
        launch = self.agent_record(node, agent)["launch_id"]
        pid = self.agent_record(node, agent)["pid"]
        ui.menu("operations", "Reconnect terminal")
        ui.until(lambda: "TUI_READY_operations" in ui.pump())
        self.assertEqual(self.agent_record(node, agent)["pid"], pid)
        self.assertEqual(self.agent_record(node, agent)["launch_id"], launch)
        for label, terminal_state in (("Stop tool", "stopped"), ("Kill session", "killed")):
            ui.menu("operations", label)
            self.until(lambda: (self.cached(agent) or {}).get("state") == terminal_state)
            ui.key(b"\x1b")
            old_launch = self.agent_record(node, agent)["launch_id"]
            ui.menu("operations", "Reboot (fresh tool, same ID)")
            self.until(lambda: self.agent_record(node, agent)["launch_id"] != old_launch)
            self.until(lambda: (self.cached(agent) or {}).get("phase") == "started")
            ui.key(b"\x1b")
            ui.until(lambda: "TUI_READY_operations" in ui.pump())
        ui.menu("operations", "Remove agent")
        self.until(lambda: self.cached(agent) is None)
        self.agents_created.remove((node, agent))
        ui.until(lambda: "Removed agent" in ui.pump())
        ui.key(b"\x1b")
        ui.key(b"\x1bOP")
        ui.field("Name", "expected-start-failure")
        ui.field("Tool", str(self.base / "missing-tool"))
        ui.field("Workspace", "relative/path")
        ui.field("Launcher (blank=default, none=local)", "none")
        ui.save()
        ui.until(lambda: "absolute workspace" in ui.pump())
        self.assertFalse(any(a["name"] == "expected-start-failure" for a in self.rpc("snapshot")["agents"]))
        ui.field("Workspace", str(self.base))
        ui.save()
        failed = self.until(lambda: next((a for a in self.rpc("snapshot")["agents"]
                                         if a["name"] == "expected-start-failure" and a["status"] == "dead"), None))
        self.agents_created.append((node, failed["id"]))
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["exit_reason"])

    def test_copy_without_system_clipboard_never_emits_escape_payload(self):
        node = self.managed_node()
        self.start_daemon([node])
        self.create_agent(node, "clipboard-missing")
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "CAM_TUI_CLIPBOARD"):
            self.env.pop(name, None)
        ui = UI(self)
        ui.until(lambda: "clipboard-missing" in ui.pump())
        ui.click("clipboard-missing")
        ui.until(lambda: "TUI_READY_clipboard-missing" in ui.pump())
        x, y = ui.position("TUI_READY_clipboard-missing")
        ui.send(("\x1b[<0;%d;%dM\x1b[<32;%d;%dM\x1b[<0;%d;%dm" %
                 (x + 1, y + 1, x + 10, y + 1, x + 10, y + 1)).encode())
        begin = len(ui.raw)
        for _ in range(2):
            ui.key(b"\x03")
            ui.until(lambda: "System clipboard unavailable" in ui.pump())
        self.assertNotIn(b"]52;c;", bytes(ui.raw[begin:]),
                         "unsupported clipboard control sequence leaked to terminal")
        self.assertIn("TUI_READY_clipboard-missing", ui.pump())
        ui.close()

    def test_real_clipboard_unicode_multiline_paste_and_interrupt(self):
        extra = os.environ.get("CAM_TUI_E2E_CLIPBOARD_BIN_DIR")
        if extra:
            self.env["PATH"] = extra + os.pathsep + self.env.get("PATH", "")
        xclip = shutil.which("xclip", path=self.env.get("PATH"))
        if not xclip or not self.env.get("DISPLAY"):
            self.skipTest("explicit real X11 display and xclip are required")
        node = self.managed_node()
        self.start_daemon([node])
        log, mode = self.base / "raw-input", self.base / "mode"
        tool = self.base / "clipboard-tool"
        tool.write_text("#!" + sys.executable + "\n" +
            "import os,sys,tty,select,pathlib\n"
            "tty.setraw(0)\n"
            "os.write(1,'\\x1b[2J\\x1b[H\\x1b[?2004hCOPY_你好\\r\\nSECOND_世界\\r\\n'.encode())\n"
            "previous=''\n"
            "while True:\n"
            " p=pathlib.Path(" + repr(str(mode)) + ")\n"
            " value=p.read_text() if p.exists() else ''\n"
            " if value!=previous:\n"
            "  os.write(1, {'off':b'\\x1b[?2004l','alt':b'\\x1b[?1049h\\x1b[2JALT_FRAME','normal':b'\\x1b[?1049l'}.get(value,b''));previous=value\n"
            " if select.select([0],[],[],.02)[0]:\n"
            "  data=os.read(0,65536)\n"
            "  with open(" + repr(str(log)) + ",'ab') as f:f.write(data)\n")
        tool.chmod(0o700)
        agent = uuid.uuid4().hex[:8]
        self.operation(node, agent, "run", tool=str(tool), name="clipboard", path=str(self.base),
                       tool_launcher="none", no_default_api=True)
        self.agents_created.append((node, agent))
        self.until(lambda: self.cached(agent))
        # Force initial-mode recovery rather than relying on a live 2004h
        # arriving after attach. This reproduces old tmux deterministically.
        self.until(lambda: "SECOND_世界" in self.operation(node, agent, "capture")["stdout"])
        if (TUI.parent / "xclip").is_file():
            # A colocated helper must work even when the invoking desktop
            # shell omitted its directory from PATH.
            self.env["PATH"] = str(self.base / "no-path-helpers")
        ui = UI(self)
        ui.click("clipboard")
        ui.until(lambda: "SECOND_世界" in ui.pump())
        self.assertTrue(ui.screen.display[4][33:].startswith("COPY_你好"),
                        "initial blank rows were lost and launch history entered the viewport")
        x, y = ui.position("COPY_你好")
        x2, y2 = ui.position("SECOND_世界")
        end = x2 + len("SECOND_") + 4 - 1
        ui.send(("\x1b[<0;%d;%dM\x1b[<32;%d;%dM\x1b[<0;%d;%dm" %
                 (x + 1, y + 1, end + 1, y2 + 1, end + 1, y2 + 1)).encode())
        ui.key(b"\x03")
        ui.until(lambda: "Copied to local clipboard" in ui.pump())
        # This reads the OS selection written by the real UI, not tmux buffers.
        value = subprocess.check_output([xclip, "-selection", "clipboard", "-o"], env=self.env, timeout=3).decode()
        def release_test_clipboard():
            subprocess.run([xclip, "-selection", "clipboard", "-loops", "1"], input=b"",
                           env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            subprocess.run([xclip, "-selection", "clipboard", "-o"], env=self.env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        self.addCleanup(release_test_clipboard)
        self.assertEqual(value, "COPY_你好\nSECOND_世界")
        peer = self.create_agent(node, "clipboard-peer")
        ui.until(lambda: "clipboard-peer" in ui.pump())
        ui.click("clipboard-peer")
        ui.until(lambda: "TUI_READY_clipboard-peer" in ui.pump())
        ui.mouse(38, 1)  # Return to the first tab with its selection intact.
        ui.key(b"\x03")
        ui.until(lambda: "Copied to local clipboard" in ui.pump())
        self.assertEqual(subprocess.check_output(
            [xclip, "-selection", "clipboard", "-o"], env=self.env, timeout=3).decode(), value)
        ui.key(b"\x16")
        expected = b"\x1b[200~" + value.encode() + b"\x1b[201~"
        ui.until(lambda: log.exists() and expected in log.read_bytes())
        self.assertFalse(log.read_bytes().endswith((b"\n", b"\r")), "paste added Enter")
        self.assertNotIn(b"\x03", log.read_bytes(), "copy leaked Ctrl+C to the tool")
        ui.key(b"\x1b[F")  # End clears the CAM selection.
        ui.key(b"\x03")
        self.until(lambda: b"\x03" in log.read_bytes())
        mode.write_text("off")
        time.sleep(.25)
        before = log.read_bytes()
        ui.key(b"\x16")
        ui.until(lambda: "multiline paste was withheld" in ui.pump())
        self.assertEqual(log.read_bytes(), before)
        mode.write_text("alt")
        ui.until(lambda: "ALT_FRAME" in ui.pump())
        mode.write_text("normal")
        ui.until(lambda: "SECOND_世界" in ui.pump() and "ALT_FRAME" not in ui.pump())
        fcntl.ioctl(ui.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        ui.screen.resize(24, 100)
        os.kill(ui.process.pid, signal.SIGWINCH)
        record = self.agent_record(node, agent)
        sock = record.get("tmux_socket") or record["runtime"]["tmux"]["socket"]
        argv = [record["tmux_bin"], "-S", sock, "display-message", "-p", "-t",
                record["tmux_session"] + ":0.0", "#{pane_width};#{pane_height}"]
        geometry = []
        def resized():
            geometry[:] = [subprocess.check_output(argv).strip()]
            return geometry[0] == b"66;18"
        try:
            ui.until(resized)
        except AssertionError as exc:
            raise AssertionError("pane geometry: %r\n%s" % (geometry, exc)) from exc
        ui.until(lambda: "SECOND_世界" in ui.pump())

    def test_job_concurrency_pause_delete_timeout_and_history(self):
        self.start_daemon([])
        marker = self.base / "long-job"
        slow = self.rpc("job.save", {"kind": "command", "name": "slow", "every_seconds": 1,
            "next_due": int(time.time()), "argv": [sys.executable, "-c",
            "import pathlib,time;p=pathlib.Path(%r);p.write_text(p.read_text()+'s' if p.exists() else 's');time.sleep(3)" % str(marker)]})
        self.until(lambda: marker.exists())
        with self.assertRaisesRegex(ValueError, "active job"):
            self.rpc("job.save", dict(slow, name="must-not-edit-running"))
        fast_marker = self.base / "quick-job"
        started = time.monotonic()
        fast = self.rpc("job.save", {"kind": "command", "name": "fast", "next_due": int(time.time()),
            "argv": [sys.executable, "-c", "import pathlib;pathlib.Path(%r).touch()" % str(fast_marker)]})
        self.until(fast_marker.exists)
        self.assertLess(time.monotonic() - started, 2.5, "long job blocked another task")
        self.rpc("job.toggle", {"id": slow["id"]})
        self.until(lambda: next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == slow["id"])["last_status"] == "ok")
        self.assertEqual(marker.read_text(), "s", "same job overlapped or pause was lost")
        time.sleep(1.3)
        self.assertEqual(marker.read_text(), "s")
        self.assertTrue(self.rpc("job.history", {"id": slow["id"]}))
        failed = self.rpc("job.save", {"kind": "command", "name": "failure", "every_seconds": 1,
            "next_due": int(time.time()), "argv": [sys.executable, "-c", "raise SystemExit(7)"]})
        self.until(lambda: next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == failed["id"]).get("attempts") == 3)
        final = next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == failed["id"])
        self.assertFalse(final["enabled"])
        self.assertEqual(final["last_result"]["exit_code"], 7)
        self.assertEqual(len(self.rpc("job.history", {"id": failed["id"]})), 3)
        late = self.base / "must-not-complete"
        timed = self.rpc("job.save", {"kind": "command", "name": "timeout", "timeout": 1,
            "next_due": int(time.time()), "argv": [sys.executable, "-c",
            "import pathlib,time;time.sleep(3);pathlib.Path(%r).touch()" % str(late)]})
        self.until(lambda: next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == timed["id"])["last_status"] == "unknown")
        with self.assertRaisesRegex(ValueError, "explicit review"):
            self.rpc("job.toggle", {"id": timed["id"]})
        time.sleep(2.2)
        self.assertFalse(late.exists(), "timed out command kept running")
        finished = self.base / "deleted-in-flight"
        removed = self.rpc("job.save", {"kind": "command", "next_due": int(time.time()),
            "argv": [sys.executable, "-c", "import pathlib,time;time.sleep(1);pathlib.Path(%r).touch()" % str(finished)]})
        self.until(lambda: next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == removed["id"])["last_status"] == "running")
        self.rpc("job.remove", {"id": removed["id"]})
        self.until(finished.exists)
        self.assertFalse(any(j["id"] == removed["id"] for j in self.rpc("snapshot")["jobs"]),
                         "in-flight completion resurrected a removed job")
        for job in (slow, fast, failed, timed):
            self.rpc("job.remove", {"id": job["id"]})
        self.assertEqual(self.rpc("snapshot")["jobs"], [])

    def test_gui_tasks_edit_pause_resume_history_and_prompt_loop_registration(self):
        node = self.managed_node()
        self.start_daemon([node])
        agent = self.create_agent(node, "job-target")
        ui = UI(self)
        ui.click("job-target")
        ui.until(lambda: "TUI_READY_job-target" in ui.pump())
        ui.key(b"\x1bOR")
        ui.click("+ Task / loop")
        marker = self.base / "edited-command"
        argv = [sys.executable, "-c", "import pathlib;p=pathlib.Path(%r);p.write_text(p.read_text()+'v1' if p.exists() else 'v1')" % str(marker)]
        ui.field("Name", "editable-task")
        ui.field("Kind: loop / prompt / command", "command")
        ui.field("Every seconds; or at UNIX time", "2")
        ui.field("Prompt; or command argv JSON", json.dumps(argv))
        ui.save()
        ui.until(marker.exists)
        job = next(j for j in self.rpc("snapshot")["jobs"] if j["name"] == "editable-task")
        ui.key(b"\x1bOR")
        ui.click("editable-task")
        ui.click("Enable / pause")
        ui.until(lambda: not next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == job["id"])["enabled"])
        ui.until(lambda: next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == job["id"])["last_status"] != "running")
        saved = next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == job["id"])
        saved = self.rpc("job.save", dict(saved, name="metadata-added", timeout=5, cwd=str(self.base)))
        ui.until(lambda: "metadata-added" in ui.pump())
        ui.click("Edit", exact=True)
        ui.field("Name", "edited-task")
        argv[-1] = argv[-1].replace("v1", "v2")
        ui.field("Prompt; or command argv JSON", json.dumps(argv))
        ui.save()
        ui.until(lambda: any(j["name"] == "edited-task" for j in self.rpc("snapshot")["jobs"]))
        edited = next(j for j in self.rpc("snapshot")["jobs"] if j["id"] == job["id"])
        self.assertEqual(len(self.rpc("snapshot")["jobs"]), 1)
        self.assertEqual(edited["revision"], saved["revision"] + 1)
        self.assertEqual((edited["cwd"], edited["timeout"]), (str(self.base), 5))
        self.assertFalse(edited["enabled"], "editing silently resumed a paused job")
        ui.key(b"\x1bOR")
        ui.click("edited-task")
        ui.click("Enable / pause")
        ui.until(lambda: "v2" in marker.read_text())
        ui.click("History", exact=True)
        ui.until(lambda: "Task execution history" in ui.pump() and '"exit_code": 0' in ui.pump())
        ui.key(b"\x1b")
        ui.key(b"\x1bOR")
        ui.click("edited-task")
        ui.click("Remove", exact=True)
        ui.until(lambda: not self.rpc("snapshot")["jobs"])
        for kind in ("prompt", "loop"):
            ui.click("+ Task / loop")
            ui.field("Name", "gui-" + kind)
            ui.field("Kind: loop / prompt / command", kind)
            # Prompt executes once; loop registration remains safely in future.
            ui.field("Every seconds; or at UNIX time", "at " + str(int(time.time()) + (1 if kind == "prompt" else 3600)))
            ui.field("Prompt; or command argv JSON", "SCHEDULED_GUI_PROMPT")
            ui.save()
            job = ui.until(lambda: next((j for j in self.rpc("snapshot")["jobs"] if j["name"] == "gui-" + kind), None))
            self.assertEqual((job["node_id"], job["agent_id"]), (node.node_id, agent))
            if kind == "prompt":
                ui.until(lambda: "SCHEDULED_GUI_PROMPT" in self.operation(node, agent, "capture")["stdout"])
            ui.key(b"\x1bOR")
            ui.click("gui-" + kind)
            ui.click("Remove", exact=True)
            ui.until(lambda: not self.rpc("snapshot")["jobs"])
        ui.click("Health details")
        ui.until(lambda: "Component health" in ui.pump() and '"daemon": "healthy"' in ui.pump())

    def test_two_ui_owner_handoff_server_and_daemon_recovery(self):
        node = self.managed_node()
        self.start_daemon([node])
        agent = self.create_agent(node, "recoverable")
        first, second = UI(self), UI(self)
        first.click("recoverable")
        first.until(lambda: "TUI_READY_recoverable" in first.pump())
        second.click("recoverable")
        second.until(lambda: "Read-only:" in second.pump())
        second.send(b"MUST_NOT_WRITE\r")
        second.until(lambda: "read-only" in second.pump().lower())
        self.assertNotIn("MUST_NOT_WRITE", self.operation(node, agent, "capture")["stdout"])
        first.close()
        second.until(lambda: "Read-only:" not in second.pump())
        second.send(b"AFTER_HANDOFF\r")
        second.until(lambda: "AFTER_HANDOFF" in self.operation(node, agent, "capture")["stdout"])
        before = self.agent_record(node, agent)["launch_id"]
        node.stop()
        second.until(lambda: any(not n["ready"] for n in self.rpc("snapshot")["nodes"]))
        self.assertEqual(self.cached(agent)["status"], "alive")
        self.until(lambda: self.cached(agent)["stale"])
        node.start()
        self.until(lambda: self.rpc("snapshot")["nodes"][0]["ready"])
        self.operation(node, agent, "send", text="SERVER_RECONNECT_PROOF")
        second.until(lambda: "SERVER_RECONNECT_PROOF" in second.pump())
        second.send(b"AFTER_SERVER_RESTART\r")
        second.until(lambda: "AFTER_SERVER_RESTART" in self.operation(node, agent, "capture")["stdout"])
        old_epoch = self.rpc("snapshot")["epoch"]
        self.rpc("shutdown")
        self.daemon.wait(timeout=8)
        self.start_daemon([node])
        self.assertNotEqual(self.rpc("snapshot")["epoch"], old_epoch)
        self.operation(node, agent, "send", text="DAEMON_RECONNECT_PROOF")
        second.until(lambda: "DAEMON_RECONNECT_PROOF" in second.pump())
        second.send(b"AFTER_DAEMON_RESTART\r")
        second.until(lambda: "AFTER_DAEMON_RESTART" in self.operation(node, agent, "capture")["stdout"])
        self.assertEqual(self.agent_record(node, agent)["launch_id"], before)
        # Closing the last UI leaves the daemon and a future job operational.
        marker = self.base / "ran-without-ui"
        job = self.rpc("job.save", {"kind": "command", "next_due": int(time.time()) + 2,
            "argv": [sys.executable, "-c", "import pathlib;pathlib.Path(%r).touch()" % str(marker)]})
        second.close()
        self.until(marker.exists)
        self.assertIsNone(self.daemon.poll())
        self.assertTrue(self.rpc("job.history", {"id": job["id"]}))
        self.assertEqual(self.cached(agent)["status"], "alive")

    def test_live_transport_process_stalled_channel_is_unhealthy_and_recovers(self):
        a, b = self.managed_node("a", "b"), self.managed_node("b", "a")
        self.start_daemon([a, b])
        agent = self.create_agent(a, "health-kept")
        pause, process_id = self.base / "pause-transport", self.base / "transport.pid"
        proxy = self.base / "transport.py"
        proxy.write_text(
            "import os,sys,socket,select,time,pathlib\n"
            "pathlib.Path(sys.argv[3]).write_text(str(os.getpid()))\n"
            "s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])\n"
            "while True:\n"
            " if pathlib.Path(sys.argv[2]).exists():time.sleep(.02);continue\n"
            " for fd in select.select([0,s],[],[],.05)[0]:\n"
            "  data=os.read(0,65536) if fd==0 else s.recv(65536)\n"
            "  if not data:sys.exit(0)\n"
            "  if fd==0:s.sendall(data)\n"
            "  else:os.write(1,data)\n")
        self.rpc("node.upsert", {"node_id": a.node_id, "token_file": str(self.tokens["c1"]),
            "command": [sys.executable, str(proxy), str(a.socket), str(pause), str(process_id)]})
        self.until(lambda: next(n for n in self.rpc("health")["nodes"] if n["node_id"] == a.node_id)["tunnel_health"] == "healthy")
        pause.touch()
        self.until(lambda: next(n for n in self.rpc("health")["nodes"] if n["node_id"] == a.node_id)["tunnel_health"] == "unhealthy", timeout=30)
        self.assertTrue(Path("/proc", process_id.read_text()).exists(), "transport was dead, not stalled")
        other = next(n for n in self.rpc("health")["nodes"] if n["node_id"] == b.node_id)
        self.assertEqual(other["tunnel_health"], "healthy")
        self.assertEqual(other["status_health"], "healthy")
        self.assertEqual(self.cached(agent)["status"], "alive")
        self.until(lambda: self.cached(agent)["stale"])
        pause.unlink()
        self.until(lambda: next(n for n in self.rpc("health")["nodes"] if n["node_id"] == a.node_id)["tunnel_health"] == "healthy", timeout=25)
        self.until(lambda: not self.cached(agent)["stale"])
        self.assertEqual(self.agent_record(a, agent)["status"], "alive")


if __name__ == "__main__":
    unittest.main()
