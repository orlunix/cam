"""Tests for SSH transport implementation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cam.transport.ssh import SSHTransport


@pytest.fixture
def ssh_transport(tmp_path, monkeypatch):
    """Create an SSHTransport with a temporary socket dir."""
    import cam.constants as c

    orig = c.SOCKET_DIR
    c.SOCKET_DIR = tmp_path / "sockets"
    c.SOCKET_DIR.mkdir()

    transport = SSHTransport(host="remote.example.com", user="dev", port=22)

    yield transport

    c.SOCKET_DIR = orig


def _make_proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> AsyncMock:
    """Create a mock async subprocess."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(stdout.encode(), stderr.encode())
    )
    return proc


class TestInit:
    def test_requires_host(self, tmp_path, monkeypatch):
        import cam.constants as c

        orig = c.SOCKET_DIR
        c.SOCKET_DIR = tmp_path / "sockets"
        c.SOCKET_DIR.mkdir()
        try:
            with pytest.raises(ValueError, match="requires a host"):
                SSHTransport(host=None)
        finally:
            c.SOCKET_DIR = orig

    def test_defaults(self, ssh_transport):
        assert ssh_transport._host == "remote.example.com"
        assert ssh_transport._user == "dev"
        assert ssh_transport._port == 22
        assert ssh_transport._key_file is None


class TestSSHBaseArgs:
    def test_basic_args(self, ssh_transport):
        args = ssh_transport._ssh_base_args()
        assert args[0] == "ssh"
        assert "dev@remote.example.com" in args
        assert "-o" in args
        assert "ControlMaster=auto" in args
        assert "ControlPersist=600" in args

    def test_custom_port(self, tmp_path, monkeypatch):
        import cam.constants as c

        orig = c.SOCKET_DIR
        c.SOCKET_DIR = tmp_path / "sockets"
        c.SOCKET_DIR.mkdir()
        try:
            t = SSHTransport(host="h", user="u", port=2222)
            args = t._ssh_base_args()
            assert "-p" in args
            idx = args.index("-p")
            assert args[idx + 1] == "2222"
        finally:
            c.SOCKET_DIR = orig

    def test_key_file(self, tmp_path, monkeypatch):
        import cam.constants as c

        orig = c.SOCKET_DIR
        c.SOCKET_DIR = tmp_path / "sockets"
        c.SOCKET_DIR.mkdir()
        try:
            t = SSHTransport(host="h", user="u", key_file="/path/to/key")
            args = t._ssh_base_args()
            assert "-i" in args
            idx = args.index("-i")
            assert args[idx + 1] == "/path/to/key"
        finally:
            c.SOCKET_DIR = orig

    def test_no_user(self, tmp_path, monkeypatch):
        import cam.constants as c

        orig = c.SOCKET_DIR
        c.SOCKET_DIR = tmp_path / "sockets"
        c.SOCKET_DIR.mkdir()
        try:
            t = SSHTransport(host="remote.example.com")
            args = t._ssh_base_args()
            # Last arg should be bare hostname (not user@host)
            assert args[-1] == "remote.example.com"
        finally:
            c.SOCKET_DIR = orig


class TestRemoteTmuxCmd:
    def test_builds_command(self, ssh_transport):
        cmd = ssh_transport._remote_tmux_cmd("cam-abc123", ["has-session", "-t", "cam-abc123"])
        assert "tmux" in cmd
        assert "/tmp/cam-sockets/cam-abc123.sock" in cmd
        assert "has-session" in cmd


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_session_with_command(self, ssh_transport):
        """create_session passes command as positional arg to new-session."""
        calls = []

        async def mock_run_ssh(remote_cmd, check=True):
            calls.append(remote_cmd)
            return True, ""

        ssh_transport._run_ssh = mock_run_ssh

        result = await ssh_transport.create_session(
            "cam-abc123", ["claude", "--allowed-tools", "Bash"], "/home/dev/project"
        )

        assert result is True
        assert len(calls) == 2  # mkdir + new-session

        # The new-session command should contain the command as positional arg
        create_cmd = calls[1]
        assert "new-session" in create_cmd
        assert "cam-abc123" in create_cmd
        assert "/home/dev/project" in create_cmd
        assert "claude" in create_cmd
        assert "--allowed-tools" in create_cmd

    @pytest.mark.asyncio
    async def test_create_session_with_env_setup(self, tmp_path, monkeypatch):
        """create_session wraps command with env_setup when configured."""
        import cam.constants as c

        orig = c.SOCKET_DIR
        c.SOCKET_DIR = tmp_path / "sockets"
        c.SOCKET_DIR.mkdir()
        try:
            t = SSHTransport(
                host="h", user="u",
                env_setup="export PATH=/opt/tools/bin:$PATH",
            )
            calls = []

            async def mock_run_ssh(remote_cmd, check=True):
                calls.append(remote_cmd)
                return True, ""

            t._run_ssh = mock_run_ssh

            result = await t.create_session(
                "cam-abc123", ["claude", "--allowed-tools", "Bash"], "/home/dev"
            )

            assert result is True
            create_cmd = calls[1]
            # Should wrap with bash -c "env_setup && exec command"
            assert "bash -c" in create_cmd
            assert "export PATH=/opt/tools/bin:$PATH" in create_cmd
            assert "exec" in create_cmd
            assert "claude" in create_cmd
        finally:
            c.SOCKET_DIR = orig

    @pytest.mark.asyncio
    async def test_create_session_failure(self, ssh_transport):
        call_count = 0

        async def mock_run_ssh(remote_cmd, check=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # mkdir
                return True, ""
            return False, "tmux error"  # new-session fails

        ssh_transport._run_ssh = mock_run_ssh
        result = await ssh_transport.create_session("cam-x", ["cmd"], "/tmp")
        assert result is False


class TestSendInput:
    @pytest.mark.asyncio
    async def test_send_with_enter(self, ssh_transport):
        calls = []

        async def mock_run_ssh(remote_cmd, check=True):
            calls.append(remote_cmd)
            return True, ""

        ssh_transport._run_ssh = mock_run_ssh

        result = await ssh_transport.send_input("cam-abc123", "hello world", send_enter=True)
        assert result is True
        assert len(calls) == 2  # send-keys + Enter
        assert "send-keys" in calls[0]
        assert "Enter" in calls[1]

    @pytest.mark.asyncio
    async def test_send_without_enter(self, ssh_transport):
        calls = []

        async def mock_run_ssh(remote_cmd, check=True):
            calls.append(remote_cmd)
            return True, ""

        ssh_transport._run_ssh = mock_run_ssh

        result = await ssh_transport.send_input("cam-abc123", "1", send_enter=False)
        assert result is True
        assert len(calls) == 1  # Only send-keys, no Enter


class TestCaptureOutput:
    @pytest.mark.asyncio
    async def test_capture_output(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return True, "● Edit(main.py)\nSome output here that is long enough"

        ssh_transport._run_ssh = mock_run_ssh

        output = await ssh_transport.capture_output("cam-abc123")
        assert "Edit" in output

    @pytest.mark.asyncio
    async def test_capture_fallback_to_alternate(self, ssh_transport):
        """Falls back to -a flag when primary returns near-empty."""
        call_count = 0

        async def mock_run_ssh(remote_cmd, check=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # Primary capture
                return True, "short"
            else:  # Alternate screen
                return True, "Much longer output from alternate screen buffer"

        ssh_transport._run_ssh = mock_run_ssh

        output = await ssh_transport.capture_output("cam-abc123")
        assert call_count == 2
        assert "alternate screen" in output

    @pytest.mark.asyncio
    async def test_capture_failure_returns_empty(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return False, ""

        ssh_transport._run_ssh = mock_run_ssh

        output = await ssh_transport.capture_output("cam-abc123")
        assert output == ""


class TestSessionExists:
    @pytest.mark.asyncio
    async def test_session_exists(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return True, ""

        ssh_transport._run_ssh = mock_run_ssh
        assert await ssh_transport.session_exists("cam-abc123") is True

    @pytest.mark.asyncio
    async def test_session_not_exists(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return False, ""

        ssh_transport._run_ssh = mock_run_ssh
        assert await ssh_transport.session_exists("cam-abc123") is False


class TestKillSession:
    @pytest.mark.asyncio
    async def test_kill_session(self, ssh_transport):
        calls = []

        async def mock_run_ssh(remote_cmd, check=True):
            calls.append(remote_cmd)
            return True, ""

        ssh_transport._run_ssh = mock_run_ssh

        result = await ssh_transport.kill_session("cam-abc123")
        assert result is True
        assert len(calls) == 2  # kill-session + rm socket
        assert "kill-session" in calls[0]
        assert "rm -f" in calls[1]


class TestTestConnection:
    @pytest.mark.asyncio
    async def test_success(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return True, "ok\ntmux 3.4\n"

        ssh_transport._run_ssh = mock_run_ssh

        ok, msg = await ssh_transport.test_connection()
        assert ok is True
        assert "tmux 3.4" in msg

    @pytest.mark.asyncio
    async def test_no_tmux(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return True, "ok\n"

        ssh_transport._run_ssh = mock_run_ssh

        ok, msg = await ssh_transport.test_connection()
        assert ok is False
        assert "tmux not found" in msg

    @pytest.mark.asyncio
    async def test_ssh_failure(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return False, "Connection refused"

        ssh_transport._run_ssh = mock_run_ssh

        ok, msg = await ssh_transport.test_connection()
        assert ok is False
        assert "Cannot connect" in msg


class TestGetAttachCommand:
    def test_basic(self, ssh_transport):
        cmd = ssh_transport.get_attach_command("cam-abc123")
        assert "ssh" in cmd
        assert "-t" in cmd
        assert "dev@remote.example.com" in cmd
        assert "cam-abc123" in cmd
        assert "attach" in cmd

    def test_custom_port(self, tmp_path):
        import cam.constants as c

        orig = c.SOCKET_DIR
        c.SOCKET_DIR = tmp_path / "sockets"
        c.SOCKET_DIR.mkdir()
        try:
            t = SSHTransport(host="h", user="u", port=2222)
            cmd = t.get_attach_command("cam-x")
            assert "-p" in cmd
            assert "2222" in cmd
        finally:
            c.SOCKET_DIR = orig


class TestGetLatency:
    @pytest.mark.asyncio
    async def test_returns_float(self, ssh_transport):
        async def mock_run_ssh(remote_cmd, check=True):
            return True, ""

        ssh_transport._run_ssh = mock_run_ssh

        latency = await ssh_transport.get_latency()
        assert isinstance(latency, float)
        assert latency >= 0
