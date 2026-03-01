"""Tests for Agent transport implementation (cam-agent over SSH)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from cam.transport.agent import AgentTransport


@pytest.fixture
def agent_transport():
    """Create an AgentTransport for testing."""
    return AgentTransport(host="remote.example.com", user="dev", port=22)


def _make_agent_response(data: dict) -> str:
    """Helper: build JSON response string as cam-agent would return."""
    return json.dumps(data) + "\n"


class TestInit:
    def test_requires_host(self):
        with pytest.raises(ValueError, match="requires a host"):
            AgentTransport(host=None)

    def test_defaults(self, agent_transport):
        assert agent_transport._host == "remote.example.com"
        assert agent_transport._user == "dev"
        assert agent_transport._port == 22
        assert agent_transport._key_file is None
        assert agent_transport._agent_bin == "cam-agent"
        assert agent_transport._env_setup is None

    def test_custom_agent_bin(self):
        t = AgentTransport(host="h", agent_bin="/usr/local/bin/cam-agent")
        assert t._agent_bin == "/usr/local/bin/cam-agent"

    def test_env_setup(self):
        t = AgentTransport(host="h", env_setup="source /opt/env.sh")
        assert t._env_setup == "source /opt/env.sh"


class TestSSHBaseArgs:
    def test_basic_args(self, agent_transport):
        args = agent_transport._ssh_base_args()
        assert args[0] == "ssh"
        assert "dev@remote.example.com" in args
        assert "ControlMaster=auto" in args
        assert "ControlPersist=600" in args

    def test_custom_port(self):
        t = AgentTransport(host="h", user="u", port=2222)
        args = t._ssh_base_args()
        assert "-p" in args
        idx = args.index("-p")
        assert args[idx + 1] == "2222"

    def test_key_file(self):
        t = AgentTransport(host="h", user="u", key_file="/path/to/key")
        args = t._ssh_base_args()
        assert "-i" in args
        idx = args.index("-i")
        assert args[idx + 1] == "/path/to/key"

    def test_no_user(self):
        t = AgentTransport(host="remote.example.com")
        args = t._ssh_base_args()
        assert args[-1] == "remote.example.com"

    def test_control_path_is_stable(self):
        """Same host/user/port produces same control path."""
        t1 = AgentTransport(host="h", user="u", port=22)
        t2 = AgentTransport(host="h", user="u", port=22)
        assert t1._control_path == t2._control_path

    def test_control_path_differs_for_different_hosts(self):
        t1 = AgentTransport(host="host1", user="u")
        t2 = AgentTransport(host="host2", user="u")
        assert t1._control_path != t2._control_path


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_session(self, agent_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_agent_response({"ok": True})

        agent_transport._run_agent = mock_run

        result = await agent_transport.create_session(
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
        # After --, the command args
        dash_idx = cmd.index("--")
        assert cmd[dash_idx + 1] == "claude"
        assert cmd[dash_idx + 2] == "--allowed-tools"

    @pytest.mark.asyncio
    async def test_creates_session_with_env_setup(self):
        t = AgentTransport(host="h", env_setup="export PATH=/opt:$PATH")
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_agent_response({"ok": True})

        t._run_agent = mock_run

        await t.create_session("cam-x", ["claude"], "/tmp")
        cmd = calls[0]
        assert "--env-setup" in cmd
        idx = cmd.index("--env-setup")
        assert cmd[idx + 1] == "export PATH=/opt:$PATH"

    @pytest.mark.asyncio
    async def test_create_session_failure(self, agent_transport):
        async def mock_run(args, **kwargs):
            return False, {}

        agent_transport._run_agent = AsyncMock(return_value=(False, "error"))
        agent_transport._run_agent_json = AsyncMock(return_value=(False, {}))

        with pytest.raises(RuntimeError, match="cam-agent session create failed"):
            await agent_transport.create_session("cam-x", ["cmd"], "/tmp")


class TestSendInput:
    @pytest.mark.asyncio
    async def test_send_with_enter(self, agent_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_agent_response({"ok": True})

        agent_transport._run_agent = mock_run

        result = await agent_transport.send_input("cam-abc123", "hello world", send_enter=True)
        assert result is True
        cmd = calls[0]
        assert "--text" in cmd
        assert "hello world" in cmd
        assert "--no-enter" not in cmd

    @pytest.mark.asyncio
    async def test_send_without_enter(self, agent_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_agent_response({"ok": True})

        agent_transport._run_agent = mock_run

        result = await agent_transport.send_input("cam-abc123", "1", send_enter=False)
        assert result is True
        cmd = calls[0]
        assert "--no-enter" in cmd


class TestSendKey:
    @pytest.mark.asyncio
    async def test_send_key(self, agent_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, _make_agent_response({"ok": True})

        agent_transport._run_agent = mock_run

        result = await agent_transport.send_key("cam-abc123", "Enter")
        assert result is True
        cmd = calls[0]
        assert "--key" in cmd
        assert "Enter" in cmd


class TestCaptureOutput:
    @pytest.mark.asyncio
    async def test_capture_output(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, "● Edit(main.py)\nSome output here that is long enough"

        agent_transport._run_agent = mock_run

        output = await agent_transport.capture_output("cam-abc123")
        assert "Edit" in output

    @pytest.mark.asyncio
    async def test_capture_returns_plain_text(self, agent_transport):
        """capture_output returns plain text, not JSON."""
        async def mock_run(args, **kwargs):
            return True, "line1\nline2\nline3\n"

        agent_transport._run_agent = mock_run

        output = await agent_transport.capture_output("cam-abc123")
        assert output == "line1\nline2\nline3"  # trailing whitespace stripped

    @pytest.mark.asyncio
    async def test_capture_failure_returns_empty(self, agent_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        agent_transport._run_agent = mock_run

        output = await agent_transport.capture_output("cam-abc123")
        assert output == ""

    @pytest.mark.asyncio
    async def test_capture_custom_lines(self, agent_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append(args)
            return True, "output"

        agent_transport._run_agent = mock_run

        await agent_transport.capture_output("cam-abc123", lines=200)
        assert "200" in calls[0]


class TestSessionExists:
    @pytest.mark.asyncio
    async def test_session_exists(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, ""

        agent_transport._run_agent = mock_run
        assert await agent_transport.session_exists("cam-abc123") is True

    @pytest.mark.asyncio
    async def test_session_not_exists(self, agent_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        agent_transport._run_agent = mock_run
        assert await agent_transport.session_exists("cam-abc123") is False


class TestKillSession:
    @pytest.mark.asyncio
    async def test_kill_session(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, _make_agent_response({"ok": True})

        agent_transport._run_agent = mock_run

        result = await agent_transport.kill_session("cam-abc123")
        assert result is True


class TestTestConnection:
    @pytest.mark.asyncio
    async def test_success(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, _make_agent_response({
                "ok": True, "version": "0.1.0", "platform": "linux/amd64"
            })

        agent_transport._run_agent = mock_run

        ok, msg = await agent_transport.test_connection()
        assert ok is True
        assert "0.1.0" in msg
        assert "linux/amd64" in msg

    @pytest.mark.asyncio
    async def test_failure(self, agent_transport):
        async def mock_run(args, **kwargs):
            return False, "Connection refused"

        agent_transport._run_agent = mock_run

        ok, msg = await agent_transport.test_connection()
        assert ok is False
        assert "Cannot reach" in msg


class TestGetLatency:
    @pytest.mark.asyncio
    async def test_returns_float(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, ""

        agent_transport._run_agent = mock_run

        latency = await agent_transport.get_latency()
        assert isinstance(latency, float)
        assert latency >= 0


class TestGetAttachCommand:
    def test_basic(self, agent_transport):
        cmd = agent_transport.get_attach_command("cam-abc123")
        assert "ssh" in cmd
        assert "-t" in cmd
        assert "dev@remote.example.com" in cmd
        assert "cam-abc123" in cmd
        assert "attach" in cmd
        # Uses cam-agent socket dir, not /tmp/cam-sockets
        assert "cam-agent-sockets" in cmd

    def test_custom_port(self):
        t = AgentTransport(host="h", user="u", port=2222)
        cmd = t.get_attach_command("cam-x")
        assert "-p" in cmd
        assert "2222" in cmd


class TestListFiles:
    @pytest.mark.asyncio
    async def test_list_files(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, _make_agent_response({
                "entries": [
                    {"name": "main.py", "type": "file", "size": 1234, "mtime": 1700000000},
                    {"name": "src", "type": "dir", "size": 0, "mtime": 1700000000},
                ]
            })

        agent_transport._run_agent = mock_run

        entries = await agent_transport.list_files("/home/dev/project")
        assert len(entries) == 2
        assert entries[0]["name"] == "main.py"
        assert entries[1]["type"] == "dir"

    @pytest.mark.asyncio
    async def test_list_files_failure(self, agent_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        agent_transport._run_agent = mock_run

        entries = await agent_transport.list_files("/nonexistent")
        assert entries == []


class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_file(self, agent_transport):
        calls = []

        async def mock_run(args, **kwargs):
            calls.append((args, kwargs))
            return True, _make_agent_response({"ok": True})

        agent_transport._run_agent = mock_run

        result = await agent_transport.write_file("/tmp/test.txt", b"hello world")
        assert result is True
        # Check that stdin_data was passed
        assert calls[0][1].get("stdin_data") == b"hello world"


class TestStartLogging:
    @pytest.mark.asyncio
    async def test_start_logging(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, _make_agent_response({
                "ok": True, "path": "/tmp/cam-agent-logs/cam-abc123.output.log"
            })

        agent_transport._run_agent = mock_run

        result = await agent_transport.start_logging("cam-abc123")
        assert result is True


class TestReadOutputLog:
    @pytest.mark.asyncio
    async def test_read_log(self, agent_transport):
        async def mock_run(args, **kwargs):
            return True, "offset:150\nSome log content here"

        agent_transport._run_agent = mock_run

        content, new_offset = await agent_transport.read_output_log("cam-abc123", offset=0)
        assert content == "Some log content here"
        assert new_offset == 150

    @pytest.mark.asyncio
    async def test_read_log_failure(self, agent_transport):
        async def mock_run(args, **kwargs):
            return False, ""

        agent_transport._run_agent = mock_run

        content, new_offset = await agent_transport.read_output_log("cam-abc123", offset=50)
        assert content == ""
        assert new_offset == 50


class TestFactoryIntegration:
    """Test that AgentTransport is properly registered in the factory."""

    def test_agent_transport_type_exists(self):
        from cam.core.models import TransportType
        assert TransportType.AGENT == "agent"

    def test_factory_creates_agent_transport(self):
        from cam.core.models import MachineConfig, TransportType
        from cam.transport.factory import create_transport

        config = MachineConfig(type=TransportType.AGENT, host="remote.example.com", user="dev")
        transport = create_transport(config)
        assert isinstance(transport, AgentTransport)
        assert transport._host == "remote.example.com"
        assert transport._user == "dev"

    def test_factory_agent_requires_host(self):
        from cam.core.models import MachineConfig, TransportType
        with pytest.raises(ValueError, match="AGENT transport requires 'host'"):
            MachineConfig(type=TransportType.AGENT)

    def test_factory_agent_with_env_setup(self):
        from cam.core.models import MachineConfig, TransportType
        from cam.transport.factory import create_transport

        config = MachineConfig(
            type=TransportType.AGENT,
            host="h",
            user="u",
            env_setup="source /opt/env.sh",
        )
        transport = create_transport(config)
        assert transport._env_setup == "source /opt/env.sh"
