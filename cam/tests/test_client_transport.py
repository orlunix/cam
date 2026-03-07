"""Tests for Client transport implementation (cam-client.py over SSH)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from cam.transport.client import ClientTransport


@pytest.fixture
def client_transport():
    """Create a ClientTransport for testing."""
    return ClientTransport(host="remote.example.com", user="dev", port=22)


def _make_json_response(data: dict) -> str:
    """Helper: build JSON response string as cam-client.py would return."""
    return json.dumps(data) + "\n"


class TestInit:
    def test_requires_host(self):
        with pytest.raises(ValueError, match="requires a host"):
            ClientTransport(host=None)

    def test_defaults(self, client_transport):
        assert client_transport._host == "remote.example.com"
        assert client_transport._user == "dev"
        assert client_transport._port == 22
        assert client_transport._key_file is None
        assert client_transport._client_script == "~/.cam/cam-client.py"
        assert client_transport._env_setup is None

    def test_custom_client_script(self):
        t = ClientTransport(host="h", client_script="/opt/cam-client.py")
        assert t._client_script == "/opt/cam-client.py"

    def test_env_setup(self):
        t = ClientTransport(host="h", env_setup="source /opt/env.sh")
        assert t._env_setup == "source /opt/env.sh"


class TestSSHBaseArgs:
    def test_basic_args(self, client_transport):
        args = client_transport._ssh_base_args()
        assert args[0] == "ssh"
        assert "dev@remote.example.com" in args
        assert "ControlMaster=auto" in args
        assert "ControlPersist=600" in args

    def test_custom_port(self):
        t = ClientTransport(host="h", user="u", port=2222)
        args = t._ssh_base_args()
        assert "-p" in args
        idx = args.index("-p")
        assert args[idx + 1] == "2222"

    def test_key_file(self):
        t = ClientTransport(host="h", user="u", key_file="/path/to/key")
        args = t._ssh_base_args()
        assert "-i" in args
        idx = args.index("-i")
        assert args[idx + 1] == "/path/to/key"

    def test_no_user(self):
        t = ClientTransport(host="remote.example.com")
        args = t._ssh_base_args()
        assert args[-1] == "remote.example.com"

    def test_control_path_is_stable(self):
        """Same host/user/port produces same control path."""
        t1 = ClientTransport(host="h", user="u", port=22)
        t2 = ClientTransport(host="h", user="u", port=22)
        assert t1._control_path == t2._control_path

    def test_control_path_differs_for_different_hosts(self):
        t1 = ClientTransport(host="host1", user="u")
        t2 = ClientTransport(host="host2", user="u")
        assert t1._control_path != t2._control_path


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_session(self, client_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        result = await client_transport.create_session(
            "cam-abc123", ["claude", "--allowed-tools", "Bash"], "/home/dev/project"
        )

        assert result is True
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "session"
        assert cmd[1] == "create"
        assert "--id" in cmd
        assert "cam-abc123" in cmd
        assert "--workdir" in cmd
        assert "/home/dev/project" in cmd
        assert "--" in cmd
        dash_idx = cmd.index("--")
        assert cmd[dash_idx + 1] == "claude"

    @pytest.mark.asyncio
    async def test_creates_session_with_env_setup(self):
        t = ClientTransport(host="h", env_setup="export PATH=/opt:$PATH")
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_json_response({"ok": True})

        t._run_client = mock_run

        await t.create_session("cam-x", ["claude"], "/tmp")
        cmd = calls[0]
        assert "--env-setup" in cmd
        idx = cmd.index("--env-setup")
        assert cmd[idx + 1] == "export PATH=/opt:$PATH"

    @pytest.mark.asyncio
    async def test_create_session_failure(self, client_transport):
        client_transport._run_client = AsyncMock(return_value=(False, "error"))
        client_transport._run_client_json = AsyncMock(return_value=(False, {}))

        with pytest.raises(RuntimeError, match="cam-client session create failed"):
            await client_transport.create_session("cam-x", ["cmd"], "/tmp")


class TestSendInput:
    @pytest.mark.asyncio
    async def test_send_with_enter(self, client_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        result = await client_transport.send_input("cam-abc123", "hello world", send_enter=True)
        assert result is True
        cmd = calls[0]
        assert "--text" in cmd
        assert "hello world" in cmd
        assert "--no-enter" not in cmd

    @pytest.mark.asyncio
    async def test_send_without_enter(self, client_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        result = await client_transport.send_input("cam-abc123", "1", send_enter=False)
        assert result is True
        cmd = calls[0]
        assert "--no-enter" in cmd


class TestSendKey:
    @pytest.mark.asyncio
    async def test_send_key(self, client_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        result = await client_transport.send_key("cam-abc123", "Enter")
        assert result is True
        cmd = calls[0]
        assert "--key" in cmd
        assert "Enter" in cmd


class TestCaptureOutput:
    @pytest.mark.asyncio
    async def test_capture_output(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, "hash:abcd1234\nSome output here"

        client_transport._run_client = mock_run

        output = await client_transport.capture_output("cam-abc123")
        assert output == "Some output here"

    @pytest.mark.asyncio
    async def test_capture_caches_hash(self, client_transport):
        """After first capture, subsequent calls send the hash."""
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, "hash:abcd1234\nSome output"

        client_transport._run_client = mock_run

        # First call — no hash sent
        await client_transport.capture_output("cam-abc123")
        assert "--hash" not in calls[0]

        # Second call — hash should be sent
        await client_transport.capture_output("cam-abc123")
        assert "--hash" in calls[1]
        hash_idx = calls[1].index("--hash")
        assert calls[1][hash_idx + 1] == "abcd1234"

    @pytest.mark.asyncio
    async def test_capture_unchanged_returns_cache(self, client_transport):
        """When hash matches, cam-client returns only hash line; transport returns cache."""
        call_count = [0]

        async def mock_run(args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return True, "hash:abcd1234\nOriginal output"
            else:
                # Only hash header, no content — means unchanged
                return True, "hash:abcd1234\n"

        client_transport._run_client = mock_run

        # First capture
        output1 = await client_transport.capture_output("cam-abc123")
        assert output1 == "Original output"

        # Second capture — unchanged
        output2 = await client_transport.capture_output("cam-abc123")
        assert output2 == "Original output"  # Returns cached

    @pytest.mark.asyncio
    async def test_capture_failure_returns_cached(self, client_transport):
        """On failure, return last cached output."""
        # Seed cache
        client_transport._capture_cache["cam-abc123"] = "cached output"

        async def mock_run(args, **kwargs):
            return False, ""

        client_transport._run_client = mock_run

        output = await client_transport.capture_output("cam-abc123")
        assert output == "cached output"

    @pytest.mark.asyncio
    async def test_capture_failure_empty_cache(self, client_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        client_transport._run_client = mock_run

        output = await client_transport.capture_output("cam-abc123")
        assert output == ""


class TestSessionExists:
    @pytest.mark.asyncio
    async def test_session_exists(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, ""

        client_transport._run_client = mock_run
        assert await client_transport.session_exists("cam-abc123") is True

    @pytest.mark.asyncio
    async def test_session_not_exists(self, client_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        client_transport._run_client = mock_run
        assert await client_transport.session_exists("cam-abc123") is False


class TestKillSession:
    @pytest.mark.asyncio
    async def test_kill_session(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        result = await client_transport.kill_session("cam-abc123")
        assert result is True

    @pytest.mark.asyncio
    async def test_kill_session_clears_cache(self, client_transport):
        """Killing a session clears its capture cache."""
        client_transport._capture_hashes["cam-abc123"] = "old"
        client_transport._capture_cache["cam-abc123"] = "old output"

        async def mock_run(args, **kwargs):
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        await client_transport.kill_session("cam-abc123")
        assert "cam-abc123" not in client_transport._capture_hashes
        assert "cam-abc123" not in client_transport._capture_cache


class TestGetAgentStatus:
    @pytest.mark.asyncio
    async def test_status_changed(self, client_transport):
        agents = [{"id": "abc", "status": "running"}]

        async def mock_run(args, **kwargs):
            return True, _make_json_response({"agents": agents, "hash": "new123"})

        client_transport._run_client = mock_run

        changed, data = await client_transport.get_agent_status()
        assert changed is True
        assert data["agents"] == agents
        assert data["hash"] == "new123"

    @pytest.mark.asyncio
    async def test_status_unchanged(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, _make_json_response({"unchanged": True, "hash": "same123"})

        client_transport._run_client = mock_run

        changed, data = await client_transport.get_agent_status(prev_hash="same123")
        assert changed is False
        assert data["unchanged"] is True

    @pytest.mark.asyncio
    async def test_status_sends_hash(self, client_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_json_response({"unchanged": True, "hash": "h"})

        client_transport._run_client = mock_run

        await client_transport.get_agent_status(prev_hash="myhash")
        assert "--hash" in calls[0]
        idx = calls[0].index("--hash")
        assert calls[0][idx + 1] == "myhash"


class TestTestConnection:
    @pytest.mark.asyncio
    async def test_success(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, _make_json_response({
                "ok": True, "version": "0.2.0", "platform": "Linux"
            })

        client_transport._run_client = mock_run

        ok, msg = await client_transport.test_connection()
        assert ok is True
        assert "0.2.0" in msg
        assert "Linux" in msg

    @pytest.mark.asyncio
    async def test_failure(self, client_transport):
        async def mock_run(args, **kwargs):
            return False, "Connection refused"

        client_transport._run_client = mock_run

        ok, msg = await client_transport.test_connection()
        assert ok is False
        assert "Cannot reach" in msg


class TestGetLatency:
    @pytest.mark.asyncio
    async def test_returns_float(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, ""

        client_transport._run_client = mock_run

        latency = await client_transport.get_latency()
        assert isinstance(latency, float)
        assert latency >= 0


class TestGetAttachCommand:
    def test_basic(self, client_transport):
        cmd = client_transport.get_attach_command("cam-abc123")
        assert "ssh" in cmd
        assert "-t" in cmd
        assert "dev@remote.example.com" in cmd
        assert "cam-abc123" in cmd
        assert "attach" in cmd
        assert "cam-sockets" in cmd

    def test_custom_port(self):
        t = ClientTransport(host="h", user="u", port=2222)
        cmd = t.get_attach_command("cam-x")
        assert "-p" in cmd
        assert "2222" in cmd


class TestListFiles:
    @pytest.mark.asyncio
    async def test_list_files(self, client_transport):
        async def mock_run(args, **kwargs):
            return True, _make_json_response({
                "entries": [
                    {"name": "main.py", "type": "file", "size": 1234, "mtime": 1700000000},
                    {"name": "src", "type": "dir", "size": 0, "mtime": 1700000000},
                ]
            })

        client_transport._run_client = mock_run

        entries = await client_transport.list_files("/home/dev/project")
        assert len(entries) == 2
        assert entries[0]["name"] == "main.py"

    @pytest.mark.asyncio
    async def test_list_files_failure(self, client_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        client_transport._run_client = mock_run

        entries = await client_transport.list_files("/nonexistent")
        assert entries == []


class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_file(self, client_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append((args, kwargs))
            return True, _make_json_response({"ok": True})

        client_transport._run_client = mock_run

        result = await client_transport.write_file("/tmp/test.txt", b"hello world")
        assert result is True
        assert calls[0][1].get("stdin_data") == b"hello world"


class TestFactoryIntegration:
    """Test that ClientTransport is properly registered in the factory."""

    def test_client_transport_type_exists(self):
        from cam.core.models import TransportType
        assert TransportType.CLIENT == "client"

    def test_factory_creates_client_transport(self):
        from cam.core.models import MachineConfig, TransportType
        from cam.transport.factory import create_transport

        config = MachineConfig(type=TransportType.CLIENT, host="remote.example.com", user="dev")
        transport = create_transport(config)
        assert isinstance(transport, ClientTransport)
        assert transport._host == "remote.example.com"
        assert transport._user == "dev"

    def test_factory_client_requires_host(self):
        from cam.core.models import MachineConfig, TransportType
        with pytest.raises(ValueError, match="CLIENT transport requires 'host'"):
            MachineConfig(type=TransportType.CLIENT)

    def test_factory_client_with_env_setup(self):
        from cam.core.models import MachineConfig, TransportType
        from cam.transport.factory import create_transport

        config = MachineConfig(
            type=TransportType.CLIENT,
            host="h",
            user="u",
            env_setup="source /opt/env.sh",
        )
        transport = create_transport(config)
        assert transport._env_setup == "source /opt/env.sh"
