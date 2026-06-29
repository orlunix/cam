"""Tests for the rich-output capture path (CAM-DESK-OUT-010..013).

Covers:
- capture_tmux preserve_ansi=False vs True at the camc transport layer:
  builds the right argv (capture-pane -e for ansi) and strips ANSI only
  in the plain path.
- CamcDelegate.capture passes --format ansi via argv when fmt='ansi',
  and stays argv-identical to the old behavior when fmt='plain'
  (backward-compat with older remote camc binaries).
- API output route parses format= and rejects unknown values, with the
  default staying 'plain' so mobile/PWA is unaffected.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# camc transport
# ---------------------------------------------------------------------------


@pytest.fixture
def tmux_capture_args(monkeypatch):
    """Capture the argv that capture_tmux would pass to tmux.

    Replaces _run with a recorder that returns a deterministic payload
    containing real ANSI escapes — long enough to skip the small-output
    -a fallback path inside capture_tmux.
    """
    from camc_pkg import transport

    recorded = {"args": None, "calls": 0}

    # Provide a fake socket so the with-socket branch is taken.
    monkeypatch.setattr(transport, "_find_tmux_socket",
                        lambda sid: "/tmp/cam-sockets/" + sid + ".sock")
    monkeypatch.setattr(transport, "_tmux_bin_for_session", lambda sid: "tmux")

    # Synthetic payload with bold-red ESC[1;31m and reset ESC[0m. The
    # body has to be at least 20 chars after .strip() to avoid the
    # capture_tmux fallback that re-runs with -a.
    body = "\x1b[1;31mhello red bold world\x1b[0m\n" * 2

    def fake_run(args, timeout=5):
        recorded["args"] = list(args)
        recorded["calls"] += 1
        return 0, body

    monkeypatch.setattr(transport, "_run", fake_run)
    yield recorded


def test_capture_tmux_plain_strips_ansi(tmux_capture_args):
    from camc_pkg import transport

    out = transport.capture_tmux("cam-aaa", lines=100)
    # plain path -> no ESC sequences remain
    assert "\x1b[" not in out
    assert "hello red bold world" in out
    # tmux argv must NOT include `-e`
    assert "-e" not in tmux_capture_args["args"]


def test_capture_tmux_ansi_preserves(tmux_capture_args):
    from camc_pkg import transport

    out = transport.capture_tmux("cam-aaa", lines=100, preserve_ansi=True)
    # ansi path -> escapes survive verbatim
    assert "\x1b[1;31m" in out
    assert "\x1b[0m" in out
    # tmux argv must include `-e`
    assert "-e" in tmux_capture_args["args"]
    # -e must come before -t (it's a capture-pane flag, not a final positional)
    args = tmux_capture_args["args"]
    assert args.index("-e") < args.index("-t")


def test_capture_tmux_default_unchanged(tmux_capture_args):
    """Mobile/PWA path: capture_tmux without preserve_ansi must be
    byte-equal to the historical plain behavior (CAM-DESK-OUT-018).
    """
    from camc_pkg import transport

    out1 = transport.capture_tmux("cam-aaa", lines=100)
    out2 = transport.capture_tmux("cam-aaa", lines=100, preserve_ansi=False)
    assert out1 == out2


# ---------------------------------------------------------------------------
# CamcDelegate
# ---------------------------------------------------------------------------


class _FakeDelegate:
    """Captures the argv passed to CamcDelegate._run for assertion."""

    def __init__(self):
        self.last_args = None

    def _run(self, args, timeout=None, input_text=None):  # noqa: D401
        self.last_args = list(args)
        return 0, "out"


def test_camc_delegate_plain_argv_backcompat():
    """Plain calls must produce the original argv shape — no --format —
    so older remote camc binaries keep working (CAM-DESK-OUT-018).
    """
    from cam.core.camc_delegate import CamcDelegate

    d = object.__new__(CamcDelegate)  # bypass __init__; only _run is used
    fd = _FakeDelegate()
    d._run = fd._run  # type: ignore[attr-defined]
    d.capture("abc12345", lines=200)
    assert fd.last_args == ["capture", "abc12345", "--lines", "200"]


def test_camc_delegate_ansi_appends_format_flag():
    """ANSI calls add --format ansi as argv elements (no shell)."""
    from cam.core.camc_delegate import CamcDelegate

    d = object.__new__(CamcDelegate)
    fd = _FakeDelegate()
    d._run = fd._run  # type: ignore[attr-defined]
    d.capture("abc12345", lines=200, fmt="ansi")
    assert fd.last_args == [
        "capture", "abc12345", "--lines", "200", "--format", "ansi",
    ]


def test_camc_delegate_unknown_format_falls_back_to_plain():
    from cam.core.camc_delegate import CamcDelegate

    d = object.__new__(CamcDelegate)
    fd = _FakeDelegate()
    d._run = fd._run  # type: ignore[attr-defined]
    d.capture("abc12345", lines=10, fmt="bogus")
    # bogus -> plain -> original argv shape, no --format leaked
    assert "--format" not in fd.last_args


# ---------------------------------------------------------------------------
# camc CLI: cmd_capture honors --format
# ---------------------------------------------------------------------------


def test_camc_cli_capture_default_plain(monkeypatch, capsys):
    """`camc capture <id>` (no --format) must call capture_tmux with
    preserve_ansi=False (default) — mobile-safe.
    """
    from camc_pkg import cli

    seen = {}

    def fake_capture(session, lines=0, preserve_ansi=False):
        seen["preserve_ansi"] = preserve_ansi
        seen["lines"] = lines
        return "plain output"

    monkeypatch.setattr(cli, "capture_tmux", fake_capture)

    class _Store:
        def get(self, _id): return None

    monkeypatch.setattr(cli, "AgentStore", _Store)

    args = type("A", (), {"id": "abc12345", "lines": 0, "format": "plain", "json_output": False})()
    cli.cmd_capture(args)
    out = capsys.readouterr().out
    assert seen["preserve_ansi"] is False
    assert "plain output" in out


def test_camc_cli_capture_ansi_flag(monkeypatch, capsys):
    """`camc capture <id> --format ansi` calls capture_tmux with
    preserve_ansi=True.
    """
    from camc_pkg import cli

    seen = {}

    def fake_capture(session, lines=0, preserve_ansi=False):
        seen["preserve_ansi"] = preserve_ansi
        return "ansi output"

    monkeypatch.setattr(cli, "capture_tmux", fake_capture)

    class _Store:
        def get(self, _id): return None

    monkeypatch.setattr(cli, "AgentStore", _Store)

    args = type("A", (), {"id": "abc12345", "lines": 0, "format": "ansi", "json_output": False})()
    cli.cmd_capture(args)
    capsys.readouterr()  # drain
    assert seen["preserve_ansi"] is True


def test_camc_cli_capture_rejects_unknown_format(monkeypatch):
    from camc_pkg import cli

    class _Store:
        def get(self, _id): return None

    monkeypatch.setattr(cli, "AgentStore", _Store)

    args = type("A", (), {"id": "abc12345", "lines": 0, "format": "bogus", "json_output": False})()
    with pytest.raises(SystemExit):
        cli.cmd_capture(args)


# ---------------------------------------------------------------------------
# camc internal exact-id capture fast path
# ---------------------------------------------------------------------------


def test_fast_capture_plain_strips_ansi_and_uses_explicit_metadata(monkeypatch):
    import io
    from camc_pkg import fast_capture

    calls = []

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"\x1b[31mhello fast capture world\x1b[0m\n", b"")

    def fake_popen(args, stdout=None, stderr=None):
        calls.append(list(args))
        return _Proc()

    monkeypatch.setattr(fast_capture.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(fast_capture.subprocess, "Popen", fake_popen)

    out = io.StringIO()
    err = io.StringIO()
    rc = fast_capture.fast_capture_from_values(
        "cam-fast", socket_path="/tmp/cam-fast.sock", tmux_bin="/bin/tmux",
        lines=80, fmt="plain", stdout=out, stderr=err)

    assert rc == 0
    assert calls[0] == [
        "/bin/tmux", "-u", "-S", "/tmp/cam-fast.sock",
        "capture-pane", "-p", "-J", "-t", "cam-fast:0.0", "-S", "-80",
    ]
    assert "\x1b[" not in out.getvalue()
    assert "hello fast capture world" in out.getvalue()
    assert err.getvalue() == ""


def test_fast_capture_ansi_preserves_sgr_and_adds_e(monkeypatch):
    import io
    from camc_pkg import fast_capture

    calls = []

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"\x1b[32mhello fast ansi capture world\x1b[0m\n", b"")

    monkeypatch.setattr(fast_capture.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(fast_capture.subprocess, "Popen",
                        lambda args, stdout=None, stderr=None: calls.append(list(args)) or _Proc())

    out = io.StringIO()
    rc = fast_capture.fast_capture_from_values(
        "cam-fast", socket_path="/tmp/cam-fast.sock", tmux_bin="/bin/tmux",
        lines=80, fmt="ansi", stdout=out, stderr=io.StringIO())

    assert rc == 0
    assert "-e" in calls[0]
    assert calls[0].index("-e") < calls[0].index("-t")
    assert "\x1b[32m" in out.getvalue()


def test_early_capture_main_ignores_non_fast_capture():
    from camc_pkg import fast_capture

    assert fast_capture.early_capture_main(["capture", "abc123", "--lines", "80"]) is None


def test_early_capture_main_uses_agents_json_for_exact_id(monkeypatch):
    import io
    from camc_pkg import fast_capture

    monkeypatch.setattr(fast_capture, "lookup_agent_from_agents_json", lambda agent_id: {
        "session": "cam-deadbeef",
        "socket": "/tmp/cam-sockets/cam-deadbeef.sock",
        "tmux_bin": "/bin/tmux",
    } if agent_id == "deadbeef" else None)

    seen = {}

    def fake_fast(session, socket_path=None, tmux_bin=None, lines=0, fmt="plain",
                  json_output=False, stdout=None, stderr=None):
        seen.update({
            "session": session,
            "socket": socket_path,
            "tmux_bin": tmux_bin,
            "lines": lines,
            "fmt": fmt,
            "json_output": json_output,
        })
        stdout.write("indexed fast output")
        return 0

    monkeypatch.setattr(fast_capture, "fast_capture_from_values", fake_fast)

    out = io.StringIO()
    rc = fast_capture.early_capture_main(
        ["capture", "deadbeef", "--lines", "40", "--format", "ansi"],
        stdout=out,
        stderr=io.StringIO(),
    )
    assert rc == 0
    assert seen == {
        "session": "cam-deadbeef",
        "socket": "/tmp/cam-sockets/cam-deadbeef.sock",
        "tmux_bin": "/bin/tmux",
        "lines": 40,
        "fmt": "ansi",
        "json_output": False,
    }
    assert out.getvalue() == "indexed fast output"


def test_early_capture_main_exact_id_without_index_falls_back(monkeypatch):
    from camc_pkg import fast_capture

    monkeypatch.setattr(fast_capture, "lookup_agent_from_agents_json", lambda _id: None)
    assert fast_capture.early_capture_main(["capture", "deadbeef"]) is None


def test_early_capture_main_json_capture_falls_back():
    from camc_pkg import fast_capture

    assert fast_capture.early_capture_main(["--json", "capture", "deadbeef"]) is None


def test_early_capture_main_no_fast_path_capture_falls_back(monkeypatch):
    from camc_pkg import fast_capture

    monkeypatch.setattr(fast_capture, "lookup_agent_from_agents_json", lambda _id: {
        "session": "cam-deadbeef",
        "socket": "/tmp/cam-sockets/cam-deadbeef.sock",
        "tmux_bin": "/bin/tmux",
    })
    assert fast_capture.early_capture_main(["capture", "deadbeef", "--no-fast-path"]) is None


def test_agents_json_lookup_roundtrip(tmp_path):
    import json
    from camc_pkg import fast_capture

    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps([
        {
            "id": "aabbccdd",
            "hostname": "workstation",
            "tmux_session": "cam-aabbccdd",
            "tmux_socket": "/tmp/cam-sockets/cam-aabbccdd.sock",
            "tmux_bin": "/usr/bin/tmux",
        }
    ], indent=2), encoding="utf-8")

    meta = fast_capture.lookup_agent_from_agents_json(
        "aabbccdd", path=str(agents_file), my_hostname="workstation")
    assert meta == {
        "session": "cam-aabbccdd",
        "socket": "/tmp/cam-sockets/cam-aabbccdd.sock",
        "tmux_bin": "/usr/bin/tmux",
    }


def test_agents_json_lookup_legacy_session_and_host_gate(tmp_path):
    import json
    from camc_pkg import fast_capture

    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps([
        {
            "id": "11223344",
            "hostname": "devbox.nvidia.com",
            "session": "cam-11223344",
            "tmux_bin": "/bin/tmux",
        }
    ], indent=2), encoding="utf-8")

    same = fast_capture.lookup_agent_from_agents_json(
        "11223344", path=str(agents_file), my_hostname="devbox")
    assert same["session"] == "cam-11223344"
    assert same["tmux_bin"] == "/bin/tmux"
    assert fast_capture.lookup_agent_from_agents_json(
        "11223344", path=str(agents_file), my_hostname="otherhost") is None


def test_agents_json_lookup_rejects_bad_shapes(tmp_path):
    import json
    from camc_pkg import fast_capture

    missing_session = tmp_path / "missing.json"
    missing_session.write_text(json.dumps([
        {"id": "55667788", "hostname": "devbox"}
    ], indent=2), encoding="utf-8")
    assert fast_capture.lookup_agent_from_agents_json(
        "55667788", path=str(missing_session), my_hostname="devbox") is None

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert fast_capture.lookup_agent_from_agents_json(
        "55667788", path=str(bad), my_hostname="devbox") is None

def test_early_capture_main_explicit_fast_is_not_a_public_fast_path():
    from camc_pkg import fast_capture

    assert fast_capture.early_capture_main([
        "capture", "--fast", "--tmux-session", "cam-fast"
    ]) is None


def test_camc_cli_capture_missing_id_exits(capsys):
    from camc_pkg import cli

    args = type("A", (), {
        "id": None,
        "lines": 0,
        "format": "plain",
        "json_output": False,
    })()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_capture(args)
    assert exc.value.code == 2
    assert "requires an agent id" in capsys.readouterr().err
