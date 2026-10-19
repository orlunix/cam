"""Isolated protocol/CLI tests; no user agent, SSH session or job is touched."""

import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

from camc_pkg.server_protocol import (
    CAM_AGENT_OPERATIONS, CamProtocolError, cam_encode_frame,
    cam_message_expiry, cam_operation_result, cam_read_frame,
    validate_cam_message,
)


ROOT = Path(__file__).resolve().parents[1]


def message(operation="agent.capture", args=None):
    return {"version": 1, "type": "message", "message_id": "request-1",
            "source": {"node_id": "node-a"},
            "target": {"node_id": "node-b", "agent_id": "beefcafe"},
            "operation": operation, "payload": {"args": args or {}},
            "expires_at": cam_message_expiry(60)}


@pytest.fixture
def ipc(tmp_path):
    # Keep AF_UNIX paths below the platform's 108-byte limit.
    with tempfile.TemporaryDirectory(prefix="cam-ipc-") as directory:
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    hello = cam_read_frame(self.rfile)
                    assert hello == {"type": "hello", "version": 1, "role": "camc"}
                    self.wfile.write(cam_encode_frame({"type": "hello", "version": 1, "node_id": "node-a"}))
                    while True:
                        frame = cam_read_frame(self.rfile)
                        if frame["type"] == "resolve":
                            self.server.resolutions.append(frame)
                            self.wfile.write(cam_encode_frame({"type": "resolved", "node_id": "node-b",
                                                              "agent_id": self.server.agent_id if frame["agent"] else None}))
                            continue
                        self.server.messages.append(frame)
                        if frame["type"] == "publish":
                            self.wfile.write(cam_encode_frame({"type": "accepted", "message_id": frame["message"]["message_id"]}))
                            break
                        answer = self.server.respond(frame)
                        for response in answer if isinstance(answer, list) else [answer]:
                            self.wfile.write(cam_encode_frame(response))
                        break
                except (OSError, CamProtocolError):
                    pass

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        server = Server(os.path.join(directory, "server.sock"), Handler)
        server.messages, server.resolutions = [], []
        server.agent_id = "beefcafe"
        server.respond = lambda value: cam_operation_result(value, "OK\n")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        home = tmp_path / "home"
        (home / ".cam").mkdir(parents=True)
        (home / "logs").mkdir()
        (home / ".cam" / "logs").symlink_to(home / "logs")
        env = dict(os.environ, HOME=str(home), PYTHONPATH=str(ROOT / "src"),
                   CAM_SERVER_SOCKET=server.server_address, CAM_SERVER_TIMEOUT="2")
        env.pop("TMUX", None)
        server.env, server.home = env, home
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


def run_cli(ipc, *args, stdin=None, env=None):
    return subprocess.run([sys.executable, "-m", "camc_pkg"] + list(args),
                          env=env or ipc.env, cwd=str(ipc.home), input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=12)


@pytest.fixture(scope="module")
def standalone_camc(tmp_path_factory):
    """Build in /tmp without changing the version or the checked-in dist."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("camc_server_test_build", str(ROOT / "build_camc.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    artifact = tmp_path_factory.mktemp("cam-server-artifact") / "camc"
    artifact.write_text(builder.build())
    artifact.chmod(0o700)
    return artifact


@pytest.mark.parametrize("argv", [["--json", "status", "node-b:beefcafe"],
                                  ["capture", "node-b:beefcafe"],
                                  ["run", "--node", "node-b", "--tool", "codex"],
                                  ["msg", "send", "node-b:beefcafe", "-t", "hello", "--no-wait"]])
def test_standalone_artifact_uses_same_protocol(ipc, standalone_camc, argv):
    ipc.respond = lambda value: cam_operation_result(value, "raw output\n", "warning\n", 7)
    result = subprocess.run([str(standalone_camc)] + argv, env=ipc.env,
                            cwd=str(ipc.home), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=15)
    assert (result.stdout, result.stderr, result.returncode) == ("raw output\n", "warning\n", 7)
    assert len(ipc.messages) == 1


def test_standalone_local_agent_with_server_enabled_stays_local(ipc, standalone_camc):
    (ipc.home / ".cam/agents.json").write_text(json.dumps([
        {"id": "beefcafe", "status": "dead", "task": {"name": "local-worker"}}
    ]))
    result = subprocess.run([str(standalone_camc), "--json", "status", "local-worker"],
                            env=ipc.env, cwd=str(ipc.home), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "beefcafe"
    assert not ipc.messages


@pytest.mark.parametrize("argv,operation", [
    (["capture", "node-b:beefcafe", "--lines", "12"], "agent.capture"),
    (["--json", "status", "node-b:beefcafe"], "agent.status"),
    (["send", "node-b:beefcafe", "--text", "hello", "--no-enter"], "agent.send"),
    (["key", "node-b:beefcafe", "--key", "Escape"], "agent.key"),
    (["stop", "node-b:beefcafe"], "agent.stop"),
    (["kill", "node-b:beefcafe"], "agent.kill"),
    (["rm", "node-b:beefcafe"], "agent.remove"),
    (["reboot", "node-b:beefcafe", "--tool-launcher", "none"], "agent.reboot"),
    (["run", "--node", "node-b", "--tool", "codex", "--path", "/tmp/test"], "agent.run"),
    (["msg", "send", "node-b:beefcafe", "-t", "hello", "--no-wait"], "message.send"),
])
def test_real_cli_serializes_all_ten_operations(ipc, argv, operation):
    result = run_cli(ipc, *argv)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK\n"
    sent, = ipc.messages
    assert sent["operation"] == operation
    assert sent["source"]["node_id"] == "node-a"
    assert sent["target"]["node_id"] == "node-b"
    assert sent["target"]["agent_id"]
    validate_cam_message(sent, "node-b")
    if operation == "agent.run":
        assert len(sent["target"]["agent_id"]) == 8
        assert sent["payload"]["args"]["tool_launcher"] is None


def test_stdout_stderr_exit_code_are_not_reformatted(ipc):
    output, errors = "\x1b[31m颜色\x1b[0m\n\n", "warning\n"
    ipc.respond = lambda value: cam_operation_result(value, output, errors, 7)
    result = run_cli(ipc, "capture", "node-b:beefcafe", "--format", "ansi")
    assert (result.stdout, result.stderr, result.returncode) == (output, errors, 7)


def test_json_output_is_clean(ipc):
    ipc.respond = lambda value: cam_operation_result(value, '{"state":"idle"}\n')
    result = run_cli(ipc, "--json", "status", "node-b:beefcafe")
    assert json.loads(result.stdout) == {"state": "idle"}
    assert result.stderr == ""


def test_send_stdin_is_resolved_before_transport(ipc):
    text = "a\n$HOME; echo 'literal'\n中文"
    result = run_cli(ipc, "send", "node-b:beefcafe", "--stdin", stdin=text)
    assert result.returncode == 0
    payload = ipc.messages[0]["payload"]["args"]
    assert payload["text"] == text and payload["stdin"] is False and payload["file"] is None


def test_local_first_does_not_connect_to_server(ipc):
    (ipc.home / ".cam" / "agents.json").write_text(json.dumps([
        {"id": "beefcafe", "status": "dead", "state": "stopped", "task": {"tool": "codex"}}
    ]))
    result = run_cli(ipc, "--json", "status", "beefcafe")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "beefcafe"
    assert ipc.messages == [] and ipc.resolutions == []


def test_shared_home_foreign_record_does_not_shadow_local_agent(ipc):
    (ipc.home / ".cam" / "agents.json").write_text(json.dumps([
        {"id": "aaaa0001", "hostname": "foreign-machine", "task": {"name": "worker"}},
        {"id": "beefcafe", "status": "dead", "task": {"name": "worker"}},
    ]))
    result = run_cli(ipc, "--json", "status", "worker")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "beefcafe"
    assert not ipc.messages


def test_foreign_record_is_resolved_through_server(ipc):
    (ipc.home / ".cam" / "agents.json").write_text(json.dumps([
        {"id": "beefcafe", "hostname": "foreign-machine", "task": {"name": "worker"}},
    ]))
    result = run_cli(ipc, "status", "beefcafe")
    assert result.returncode == 0 and len(ipc.messages) == 1


def test_remote_run_without_endpoint_never_starts_locally(ipc):
    env = dict(ipc.env)
    env.pop("CAM_SERVER_SOCKET")
    result = run_cli(ipc, "run", "--node", "node-b", env=env)
    assert result.returncode == 2 and "requires a local CAM Server" in result.stderr
    assert not (ipc.home / ".cam" / "agents.json").exists()


def test_remote_list_rejected_without_sending_message(ipc):
    result = run_cli(ipc, "list", "--all")
    assert result.returncode == 2 and "not supported" in result.stderr
    assert ipc.messages == []


@pytest.mark.parametrize("field,value", [("source", {"node_id": "impostor", "agent_id": "beefcafe"}),
                                        ("reply_to", "other-request")])
def test_unrelated_result_cannot_become_stdout(ipc, field, value):
    def respond(request):
        answer = cam_operation_result(request, "must not print")
        answer[field] = value
        return answer
    ipc.respond = respond
    result = run_cli(ipc, "capture", "node-b:beefcafe")
    assert result.returncode == 2 and result.stdout == ""
    assert len(ipc.messages) == 1  # no legacy replay after Server submission


def test_delivery_ack_does_not_complete_blocking_msg(ipc):
    def respond(request):
        ack = cam_operation_result(request, "delivery only\n")
        ack["payload"]["kind"] = "delivery"
        reply = cam_operation_result(request, "actual answer\n")
        reply["payload"]["kind"] = "reply"
        return [ack, reply]
    ipc.respond = respond
    result = run_cli(ipc, "msg", "send", "node-b:beefcafe", "-t", "hello")
    assert result.returncode == 0 and result.stdout == "actual answer\n"


def test_protocol_validation_keeps_envelope_unchanged():
    value = message()
    original = copy.deepcopy(value)
    validate_cam_message(value, "node-b")
    assert value == original


@pytest.mark.parametrize("change", [
    {"operation": "agent.list"}, {"target": {"node_id": "node-b"}},
    {"target": {"node_id": "node-b", "agent_id": "*"}},
    {"expires_at": "2000-01-01T00:00:00Z"},
])
def test_invalid_or_node_wide_operations_are_rejected(change):
    value = message()
    value.update(change)
    with pytest.raises(CamProtocolError):
        validate_cam_message(value, "node-b")


@pytest.mark.parametrize("data", [b"{}", b"[]\n", b"broken\n", b'{"x":NaN}\n'])
def test_bad_frames_are_rejected(data):
    with pytest.raises(CamProtocolError):
        cam_read_frame(io.BytesIO(data))


def _effect(message_value):
    path = message_value["payload"]["args"]["id"]
    with open(path, "a") as stream:
        stream.write("executed\n")
    print("output")
    sys.stderr.write("error\n")
    raise SystemExit(9)


def _slow_effect(message_value):
    with open(message_value["payload"]["args"]["id"], "a") as stream:
        stream.write("executed\n")
    time.sleep(30)


def test_timeout_is_unknown_persisted_and_does_not_replay(tmp_path):
    import multiprocessing
    from camc_pkg.server_operations import CamOperationReceiver
    calls = tmp_path / "calls"
    receiver = CamOperationReceiver("node-b", str(tmp_path / "timeout.db"),
                                    handler=_slow_effect, timeout=1)
    value = message(args={"id": str(calls)})
    children_before = {p.pid for p in multiprocessing.active_children()}
    first = receiver.execute(value)
    assert first["payload"]["status"] == "unknown"
    assert first["payload"]["exit_code"] != 0
    assert receiver.execute(value) == first
    assert calls.read_text() == "executed\n"
    assert {p.pid for p in multiprocessing.active_children()} == children_before


def test_receiver_rejects_unaddressable_frames_without_fabricated_results(tmp_path):
    from camc_pkg.server_operations import CamOperationReceiver
    receiver = CamOperationReceiver("node-b", str(tmp_path / "reject.db"))
    for value in (None, {}, dict(message(), target={"node_id": "elsewhere"})):
        with pytest.raises(CamProtocolError):
            receiver.execute(value)
    assert receiver.execute(message("agent.list"))["payload"]["exit_code"] == 2


def test_native_receiver_persists_results_and_never_replays(tmp_path):
    from camc_pkg.server_operations import CamOperationReceiver
    effect = tmp_path / "calls"
    value = message(args={"id": str(effect)})
    receiver = CamOperationReceiver("node-b", str(tmp_path / "operations.db"), handler=_effect)
    first = receiver.execute(value)
    second = CamOperationReceiver("node-b", receiver.path, handler=_effect).execute(value)
    assert first == second
    assert first["payload"] == {"status": "error", "stdout": "output\n", "stderr": "error\n", "exit_code": 9}
    assert effect.read_text() == "executed\n"
    changed = copy.deepcopy(value)
    changed["payload"]["args"]["id"] = str(tmp_path / "different")
    assert "MESSAGE_CONFLICT" in receiver.execute(changed)["payload"]["stderr"]
    assert not (tmp_path / "different").exists()


def test_concurrent_duplicate_operations_execute_once(tmp_path):
    from camc_pkg.server_operations import CamOperationReceiver
    effect = tmp_path / "calls"
    receiver = CamOperationReceiver("node-b", str(tmp_path / "concurrent.db"), handler=_effect)
    value = message(args={"id": str(effect)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(receiver.execute, [value] * 4))
    assert effect.read_text() == "executed\n"
    assert sum(result["payload"]["status"] == "error" for result in results) >= 1
    assert receiver.execute(value)["payload"]["stdout"] == "output\n"


def test_ambiguous_local_target_cannot_be_forwarded(ipc):
    (ipc.home / ".cam" / "agents.json").write_text(json.dumps([
        {"id": "beef0001", "task": {"name": "worker-a"}},
        {"id": "beef0002", "task": {"name": "worker-b"}},
    ]))
    result = run_cli(ipc, "status", "worker")
    assert result.returncode == 2 and "ambiguous local" in result.stderr
    assert not ipc.messages and not ipc.resolutions


def test_msg_timeout_follows_its_own_option(ipc):
    env = dict(ipc.env)
    env.pop("CAM_SERVER_TIMEOUT")
    start = time.time()
    result = run_cli(ipc, "msg", "send", "node-b:beefcafe", "-t", "hello", "--timeout", "600", env=env)
    # Use a business reply, rather than a delivery ack, to finish immediately.
    assert result.returncode == 0, result.stderr
    from camc_pkg.server_protocol import cam_message_time
    assert cam_message_time(ipc.messages[0]["expires_at"]) - start > 590


def test_real_cli_to_native_status_backend(ipc, tmp_path, monkeypatch):
    from camc_pkg.server_operations import CamOperationReceiver
    target_home = tmp_path / "target"
    (target_home / ".cam").mkdir(parents=True)
    records = target_home / ".cam" / "agents.json"
    records.write_text(json.dumps([{"id": "beefcafe", "status": "dead", "state": "stopped",
                                    "task": {"tool": "codex", "name": "target-only"}}]))
    monkeypatch.setenv("HOME", str(target_home))
    receiver = CamOperationReceiver("node-b", str(tmp_path / "native.db"))
    ipc.respond = receiver.execute
    result = run_cli(ipc, "--json", "status", "node-b:beefcafe")
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["id"] == "beefcafe" and record["task"]["name"] == "target-only"
    assert not (ipc.home / ".cam" / "agents.json").exists()


def test_native_receiver_cannot_operate_a_foreign_host_record(ipc, tmp_path, monkeypatch):
    from camc_pkg.server_operations import CamOperationReceiver
    target_home = tmp_path / "target"
    (target_home / ".cam").mkdir(parents=True)
    (target_home / ".cam/agents.json").write_text(json.dumps([
        {"id": "beefcafe", "hostname": "foreign-machine", "task": {"tool": "codex"}}
    ]))
    monkeypatch.setenv("HOME", str(target_home))
    ipc.respond = CamOperationReceiver("node-b", str(tmp_path / "foreign.db")).execute
    result = run_cli(ipc, "kill", "node-b:beefcafe")
    assert result.returncode != 0 and "owned by another machine" in result.stderr


def test_remote_reply_is_an_independent_publication(ipc, monkeypatch):
    from camc_pkg.server_client import cam_save_message_context
    original = message("message.send", {"text": "request"})
    original["source"] = {"node_id": "node-b", "agent_id": "beefcafe"}
    original["target"] = {"node_id": "node-a", "agent_id": "cafebeef"}
    monkeypatch.setenv("HOME", str(ipc.home))
    cam_save_message_context(original, "incoming")
    result = run_cli(ipc, "msg", "reply", "request-1", "-t", "the answer")
    assert result.returncode == 0, result.stderr
    publication, = ipc.messages
    reply = publication["message"]
    assert publication["type"] == "publish"
    assert reply["message_id"] != original["message_id"]
    assert reply["reply_to"] == original["message_id"]
    assert reply["target"] == original["source"]
    assert reply["payload"]["args"]["text"] == "the answer"


def test_shared_home_contexts_are_separate_for_both_nodes(ipc, monkeypatch):
    from camc_pkg.server_client import cam_save_message_context, cam_message_context_directory
    monkeypatch.setenv("HOME", str(ipc.home))
    original = message("message.send", {"text": "question"})
    original["source"]["agent_id"] = "cafebeef"
    cam_save_message_context(original, "outgoing")
    cam_save_message_context(original, "incoming")
    assert cam_message_context_directory("node-a") != cam_message_context_directory("node-b")
    result = run_cli(ipc, "msg", "reply", "request-1", "-t", "follow-up")
    assert result.returncode == 0, result.stderr
    assert ipc.messages[0]["message"]["source"] == original["source"]
    assert ipc.messages[0]["message"]["target"] == original["target"]


def test_native_message_turns_share_thread_but_keep_distinct_message_ids(tmp_path, monkeypatch):
    from camc_pkg import cli, storage
    from camc_pkg.server_client import cam_record_message_turn, cam_save_message_context, cam_message_context_directory
    from camc_pkg.server_operations import cam_native_operation
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(storage, "AGENTS_FILE", str(tmp_path / "agents.json"))
    ledger = tmp_path / "messages.jsonl"
    monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
    monkeypatch.setattr(cli, "_CAMC_SCRIPT", None)
    storage.AgentStore().save({"id": "beefcafe", "tmux_session": "disposable-test-session",
                               "task": {"name": "receiver", "tool": "codex"}})
    notifications = []
    monkeypatch.setattr(cli, "_msg_submit_notification",
                        lambda *args, **kwargs: notifications.append(args) or True)
    first = message("message.send", {"to": "beefcafe", "text": "question"})
    first["thread_id"] = first["message_id"]
    first["source"]["agent_id"] = "cafebeef"
    # Simulate two Nodes sharing Home: outgoing and incoming views must not
    # create two turns for the same transport message in the shared ledger.
    cam_save_message_context(first, "outgoing")
    cam_record_message_turn(first)
    cam_native_operation(first)
    reply = copy.deepcopy(first)
    reply.update(message_id="reply-2", reply_to=first["message_id"],
                 source=dict(first["target"]), target=dict(first["source"]))
    reply["payload"]["args"] = {"text": "answer", "to": "cafebeef"}
    cam_save_message_context(reply, "outgoing")
    cam_record_message_turn(reply)
    followup = copy.deepcopy(first)
    followup.update(message_id="followup-3", reply_to="reply-2")
    followup["payload"]["args"]["text"] = "next question"
    cam_native_operation(followup)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    turns = [r for r in rows if r.get("record") == "turn"]
    assert [(r["msg_id"], r["seq"], r["message_id"]) for r in turns] == [
        ("request-1", 1, "request-1"), ("request-1", 2, "reply-2"),
        ("request-1", 3, "followup-3")]
    assert "[camc msg#request-1]" in notifications[-1][1]
    assert "camc msg reply followup-3" in notifications[-1][1]
    assert notifications[-1][2] == "request-1"  # monitor watches the visible thread marker
    context = json.loads((Path(cam_message_context_directory("node-b")) / "request-1.json").read_text())
    assert context["message"]["message_id"] == "followup-3"


def test_real_native_lifecycle_in_isolated_home(ipc, tmp_path, monkeypatch):
    """A disposable local tool exercises real tmux; no AI login/LSF is used."""
    # Unlike mock IPC tests, this request is retried after real tool startup.
    # Keep its deadline live while testing native reboot deduplication.
    ipc.env["CAM_SERVER_TIMEOUT"] = "30"
    import shutil
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    from camc_pkg.server_operations import CamOperationReceiver
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    tool = tmp_path / "disposable-tool"
    tool.write_text("#!/bin/sh\nprintf 'CAM_NATIVE_READY\\n'\nexec /bin/cat\n")
    tool.chmod(0o700)
    monkeypatch.setenv("HOME", str(target_home))
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    monkeypatch.delenv("CAM_SERVER_SOCKET", raising=False)
    receiver = CamOperationReceiver("node-b", str(target_home / "operations.db"))
    ipc.respond = receiver.execute
    agent_id = None

    def current():
        path = target_home / ".cam" / "agents.json"
        return next((a for a in json.loads(path.read_text()) if a["id"] == agent_id), None) if path.exists() else None

    def call(*argv):
        result = run_cli(ipc, *argv)
        assert result.returncode == 0, (argv, result.stdout, result.stderr)
        return result

    def ready():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = current()
            if record and not record.get("runtime", {}).get("preparing"):
                result = call("capture", "node-b:" + agent_id, "--lines", "30")
                if "CAM_NATIVE_READY" in result.stdout:
                    return record
            time.sleep(0.1)
        raise AssertionError("disposable tool did not become ready: %r" % current())

    try:
        call("run", "--node", "node-b", "--tool", str(tool), "--path", str(tmp_path),
             "--tool-launcher", "none", "--no-default-api")
        agent_id = ipc.messages[0]["target"]["agent_id"]
        ipc.agent_id = agent_id
        original = ready()
        call("send", "node-b:" + agent_id, "-t", "CAM_NATIVE_INPUT")
        call("key", "node-b:" + agent_id, "--key", "Enter")
        call("msg", "send", "node-b:" + agent_id, "-t", "CAM_NATIVE_MESSAGE", "--no-wait")
        assert "CAM_NATIVE_MESSAGE" in call("capture", "node-b:" + agent_id).stdout
        status = json.loads(call("--json", "status", "node-b:" + agent_id).stdout)
        assert status["id"] == agent_id
        call("reboot", "node-b:" + agent_id, "--tool-launcher", "none")
        reboot_request = ipc.messages[-1]
        rebooted = ready()
        assert rebooted["id"] == agent_id and rebooted["launch_id"] != original["launch_id"]
        assert receiver.execute(reboot_request)["payload"]["exit_code"] == 0
        assert current()["launch_id"] == rebooted["launch_id"]  # native reboot is not replayed
        call("stop", "node-b:" + agent_id)
        assert (current()["status"], current()["state"]) == ("dead", "stopped")
        call("reboot", "node-b:" + agent_id, "--tool-launcher", "none")
        ready()
        call("kill", "node-b:" + agent_id)
        assert (current()["status"], current()["state"]) == ("dead", "killed")
        call("rm", "node-b:" + agent_id)
        assert current() is None
    finally:
        # Cleanup is restricted to the ID created by this test and its HOME.
        if agent_id and current():
            cleanup = message("agent.remove", {"id": agent_id})
            cleanup["message_id"] = uuid.uuid4().hex
            cleanup["target"]["agent_id"] = agent_id
            result = receiver.execute(cleanup)
            assert result["payload"]["exit_code"] == 0, result
