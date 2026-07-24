"""Contract tests for safe tmux renaming used by legacy migration."""

from camc_pkg import transport


def test_rename_private_session_adds_new_socket_alias(tmp_path, monkeypatch):
    old_socket = tmp_path / "legacy.sock"
    old_socket.write_text("socket placeholder")
    monkeypatch.setattr(transport, "SOCKETS_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(transport, "_run",
                        lambda args, **_kw: (calls.append(args) or (0, "")))

    assert transport.tmux_rename_session("legacy", "cam-a1b2c3d4-m") is True

    alias = tmp_path / "cam-a1b2c3d4-m.sock"
    assert alias.is_symlink()
    assert alias.readlink().name == "legacy.sock"
    assert calls[-1][-4:] == ["rename-session", "-t", "legacy", "cam-a1b2c3d4-m"]
