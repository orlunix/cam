import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from camc_pkg import remote  # noqa: E402


def test_sync_context_json_uses_scp_temp_without_heredoc(tmp_path, monkeypatch):
    camc = tmp_path / "camc"
    camc.write_text("#!/usr/bin/env python3\n")
    configs = tmp_path / "configs"
    configs.mkdir()

    ssh_commands = []
    context_payloads = []
    context_temp_paths = []

    def fake_ssh_run(machine, remote_cmd, timeout=30, input_data=None):
        ssh_commands.append(remote_cmd)
        return 0, ""

    def fake_sync_file(machine, local_path, remote_path, timeout=30):
        if remote_path == "~/.cam/context.json":
            context_temp_paths.append(local_path)
            with open(local_path) as f:
                context_payloads.append(f.read())
        return True

    monkeypatch.setattr(remote, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(remote, "sync_file", fake_sync_file)

    result = remote.sync_camc_to_machine(
        {"host": "pdx", "env_setup": "source /home/hren/.bashrc"},
        camc_path=str(camc), configs_dir=str(configs))

    assert result["context.json"] == "deployed"
    assert context_payloads
    assert json.loads(context_payloads[0]) == {"env_setup": "source /home/hren/.bashrc"}
    assert "CAMEOF" not in context_payloads[0]
    assert not any(cmd and "cat > ~/.cam/context.json" in cmd for cmd in ssh_commands)
    assert context_temp_paths
    assert not os.path.exists(context_temp_paths[0])
