"""Client management APIs do not change legacy agent ownership or replay work."""
import copy
import json
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from camc_pkg.server_management import management_operation, management_request
from camc_pkg.server_operations import CamOperationReceiver
from camc_pkg.server_protocol import cam_message_expiry


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_paste_probe_is_read_only_preserves_geometry_and_recovers_mode(tmp_path, enabled):
    from camc_pkg.server_management import _legacy_paste_mode
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux required")
    argv = [tmux, "-u", "-S", str(tmp_path / "tmux.sock"), "-f", "/dev/null"]
    sequence = "\\x1b[?2004h" if enabled else ""
    program = "import os,time;os.write(1,b'%sREADY\\n');time.sleep(10)" % sequence
    try:
        subprocess.check_call(argv + ["new-session", "-d", "-s", "mode-probe",
                                      "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(program)])
        for _ in range(100):
            if b"READY" in subprocess.check_output(argv + ["capture-pane", "-pt", "mode-probe"]):
                break
            time.sleep(.02)
        fmt = "#{window_width};#{window_height};#{status}"
        before = subprocess.check_output(argv + ["display-message", "-pt", "mode-probe", fmt]).decode().strip()
        width, height, status = before.split(";")
        assert _legacy_paste_mode(argv, "mode-probe", int(width), int(height) + (status != "off")) is enabled
        after = subprocess.check_output(argv + ["display-message", "-pt", "mode-probe", fmt]).decode().strip()
        assert after == before
        assert subprocess.check_output(argv + ["list-clients"]) == b"", "temporary observer leaked"
    finally:
        subprocess.run(argv + ["kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_legacy_paste_probe_rejects_unbounded_geometry():
    from camc_pkg.server_management import _legacy_paste_mode
    assert _legacy_paste_mode(["must-not-execute"], "unused", 501, 24) is None


def test_terminal_shells_preserve_primary_scope_creation_and_launch_identity(monkeypatch, tmp_path):
    import camc_pkg.storage
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux required")
    argv = [tmux, "-u", "-S", str(tmp_path / "terminal.sock"), "-f", "/dev/null"]
    agent = {"id":"beefcafe", "launch_id":"launch-one", "tmux_session":"agent-test",
             "tmux_socket":str(tmp_path / "terminal.sock"), "tmux_bin":tmux, "path":str(tmp_path)}
    class Store:
        def get(self, ref):
            return agent if ref == agent["id"] else None
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", Store)
    def call(action, **args):
        return management_request(action, dict(agent_id=agent["id"], **args), "node-a", str(tmp_path / "state.db"), None)
    try:
        subprocess.check_call(argv + ["new-session", "-d", "-s", "agent-test", "-x", "80", "-y", "24", "exec /bin/cat"])
        subprocess.check_call(argv + ["set-option", "-g", "default-shell", "/bin/sh"])
        first = call("terminal.list")["panes"]
        assert len(first) == 1 and first[0]["primary"]
        primary = first[0]["pane_id"]
        created = call("terminal.create", request_id="create-one", launch_id="launch-one")
        shell = created["pane_id"]
        assert shell != primary and len(created["panes"]) == 2
        assert call("terminal.create", request_id="create-one")["pane_id"] == shell
        assert len(call("terminal.list")["panes"]) == 2
        assert call("terminal.resolve", pane_id=shell)["primary"] is False
        assert call("terminal.resolve")["pane"] == primary
        subprocess.check_call(argv + ["send-keys", "-t", shell, "printf 'AUXILIARY_PROOF\\n'", "Enter"])
        for _ in range(100):
            if any(line.strip() == b"AUXILIARY_PROOF" for line in subprocess.check_output(argv + ["capture-pane", "-p", "-t", shell]).splitlines()):
                break
            time.sleep(.02)
        assert any(line.strip() == b"AUXILIARY_PROOF" for line in subprocess.check_output(argv + ["capture-pane", "-p", "-t", shell]).splitlines())
        assert b"AUXILIARY_PROOF" not in subprocess.check_output(argv + ["capture-pane", "-p", "-t", primary])
        with pytest.raises(ValueError, match="cannot be closed"):
            call("terminal.close", pane_id=primary)
        with pytest.raises(ValueError, match="launch changed"):
            call("terminal.create", request_id="old-launch", launch_id="old")
        foreign = subprocess.check_output(argv + ["new-session", "-d", "-s", "foreign", "-P", "-F", "#{pane_id}", "exec /bin/cat"]).decode().strip()
        with pytest.raises(ValueError, match="unavailable"):
            call("terminal.resolve", pane_id=foreign)
        call("terminal.close", pane_id=foreign)
        assert subprocess.call(argv + ["has-session", "-t", "foreign"]) == 0
        call("terminal.close", pane_id=shell)
        assert call("terminal.list")["panes"][0]["pane_id"] == primary
        assert len(call("terminal.list")["panes"]) == 1
        # Replaying a completed create after its shell was closed cannot
        # create another shell or revive the old one.
        call("terminal.create", request_id="create-one")
        assert len(call("terminal.list")["panes"]) == 1
        for i in range(7):
            call("terminal.create", request_id="shell-%d" % i)
        with pytest.raises(ValueError, match="8-terminal"):
            call("terminal.create", request_id="over-limit")
        assert len(call("terminal.list")["panes"]) == 8
    finally:
        subprocess.run(argv + ["kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _effect(message):
    with open(message["payload"]["args"]["id"], "a") as stream:
        stream.write("once\n")
    print("done")


def _message(effect):
    return {"version": 1, "type": "message", "message_id": "client-request",
            "source": {"node_id": "client.c1"},
            "target": {"node_id": "node-a", "agent_id": "beefcafe"},
            "operation": "agent.send", "payload": {"args": {"id": str(effect)}},
            "expires_at": cam_message_expiry(60)}


def test_management_reconnect_keeps_original_expiry_and_does_not_repeat(tmp_path):
    calls = tmp_path / "calls"
    receiver = CamOperationReceiver("node-a", str(tmp_path / "state.db"), handler=_effect)
    value = _message(calls)
    first = management_operation(receiver, value)
    later = copy.deepcopy(value)
    later["expires_at"] = cam_message_expiry(120)
    assert management_operation(receiver, later) == first
    assert calls.read_text() == "once\n"
    conflict = copy.deepcopy(later)
    conflict["payload"]["args"]["id"] = str(tmp_path / "other")
    with pytest.raises(ValueError, match="MESSAGE_CONFLICT"):
        management_operation(receiver, conflict)
    assert not (tmp_path / "other").exists()


def test_snapshot_is_secret_free_and_filters_foreign_hosts(monkeypatch):
    import camc_pkg.storage
    import socket
    records = [{"id": "11111111", "hostname": socket.gethostname(), "status": "alive", "state": "idle",
                "task": {"name": "test", "tool": "codex", "tags": ["review"]}, "tool_env": {"TOKEN": "private-value"}},
               {"id": "22222222", "hostname": "foreign-host", "status": "alive"}]
    class Store:
        def list(self):
            return records
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", Store)
    result = management_request("agents.snapshot", {}, "node-a", "unused", None)
    assert [agent["id"] for agent in result["agents"]] == ["11111111"]
    assert "private-value" not in json.dumps(result)
    assert result["agents"][0]["tags"] == ["review"]


@pytest.mark.parametrize("nested", [False, True])
def test_attributes_persist_without_changing_runtime_or_identity(monkeypatch, tmp_path, nested):
    from camc_pkg.storage import AgentStore
    import camc_pkg.storage
    import socket
    store = AgentStore(str(tmp_path / "agents.json"))
    record = {"id": "beefcafe", "hostname": socket.gethostname(), "status": "alive",
              "state": "running", "phase": "started", "pid": 123, "launch_id": "launch-one",
              "tool_env": {"TOKEN": "private-value"}}
    task = {"name": "before", "tool": "codex", "tags": ["old"], "prompt": "private prompt"}
    record.update({"task": task} if nested else task)
    store.save(record)
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", lambda: store)
    result = management_request("agent.update", {"node_id": "node-a", "agent_id": "beefcafe",
        "name": "after", "tags": ["review", "test", "review"]}, "node-a", "unused", None)
    saved = store.get("beefcafe")
    data = saved["task"] if nested else saved
    assert (data["name"], data["tags"]) == ("after", ["review", "test"])
    assert (saved["pid"], saved["launch_id"], saved["state"], saved["id"]) == (123, "launch-one", "running", "beefcafe")
    assert data["tool"] == "codex" and data["prompt"] == "private prompt"
    assert "private" not in json.dumps(result)
    management_request("agent.update", {"agent_id": "beefcafe", "tags": []}, "node-a", "unused", None)
    assert management_request("agents.snapshot", {}, "node-a", "unused", None)["agents"][0]["tags"] == []


@pytest.mark.parametrize("fields", [
    {"name": ""}, {"name": "escape\x1b"}, {"tags": "wrong"}, {"tags": ["bad tag"]},
    {"tags": ["x" * 65]}, {"tags": [str(n) for n in range(33)]}, {"status": "dead"},
    {"name": "after", "node_id": "other-node"}, {"name": "after", "agent_id": "beef"},
])
def test_attributes_reject_invalid_or_redirected_edits(monkeypatch, tmp_path, fields):
    from camc_pkg.storage import AgentStore
    import camc_pkg.storage
    store = AgentStore(str(tmp_path / "agents.json"))
    store.save({"id": "beefcafe", "name": "before", "status": "alive"})
    before = (tmp_path / "agents.json").read_bytes()
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", lambda: store)
    with pytest.raises(ValueError):
        management_request("agent.update", dict({"agent_id": "beefcafe"}, **fields), "node-a", "unused", None)
    assert (tmp_path / "agents.json").read_bytes() == before


def test_attributes_reject_foreign_host(monkeypatch, tmp_path):
    from camc_pkg.storage import AgentStore
    import camc_pkg.storage
    store = AgentStore(str(tmp_path / "agents.json"))
    store.save({"id": "beefcafe", "hostname": "foreign-host", "name": "before"})
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", lambda: store)
    with pytest.raises(ValueError, match="AGENT_NOT_FOUND"):
        management_request("agent.update", {"agent_id": "beefcafe", "name": "after"}, "node-a", "unused", None)


def test_loop_gate_checks_current_status_and_single_owner(monkeypatch, tmp_path):
    import camc_pkg.storage
    record = {"id": "beefcafe", "status": "alive", "state": "running", "phase": "started"}
    class Store:
        def get(self, unused):
            return dict(record)
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", Store)
    calls = tmp_path / "calls"
    receiver = CamOperationReceiver("node-a", str(tmp_path / "state.db"), handler=_effect)
    args = {"agent_id": "beefcafe", "loop_id": "loop-one", "owner": "client-one", "message": _message(calls)}
    assert management_request("loop.deliver", args, "node-a", receiver.path, receiver)["deferred"]
    assert not calls.exists()
    record["state"] = "idle"
    result = management_request("loop.deliver", args, "node-a", receiver.path, receiver)
    assert result["result"]["payload"]["exit_code"] == 0
    assert calls.read_text() == "once\n"
    assert management_request("loop.deliver", args, "node-a", receiver.path, receiver)["deferred"]
    other = dict(args, owner="client-two")
    with pytest.raises(ValueError, match="another scheduling owner"):
        management_request("loop.deliver", other, "node-a", receiver.path, receiver)
