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
                "task": {"name": "test", "tool": "codex"}, "tool_env": {"TOKEN": "private-value"}},
               {"id": "22222222", "hostname": "foreign-host", "status": "alive"}]
    class Store:
        def list(self):
            return records
    monkeypatch.setattr(camc_pkg.storage, "AgentStore", Store)
    result = management_request("agents.snapshot", {}, "node-a", "unused", None)
    assert [agent["id"] for agent in result["agents"]] == ["11111111"]
    assert "private-value" not in json.dumps(result)


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
