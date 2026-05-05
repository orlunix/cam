"""Tests for camc — standalone local agent CLI."""

import hashlib
import json
import os
import sys
import pytest

from cam.camc import _time_ago, _build_command
from cam.client import (
    AgentStore,
    AdapterConfig, strip_ansi, clean_for_confirm, compile_pattern,
    detect_state, detect_completion, should_auto_confirm,
    is_ready_for_input, _parse_toml, load_toml,
    _cmd_ping, _cmd_status, _json_out, _get_arg, _has_flag,
    _VERSION,
)


# ---------------------------------------------------------------------------
# strip_ansi / clean_for_confirm / compile_pattern
# ---------------------------------------------------------------------------

class TestStripAnsi:
    def test_removes_color_codes(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_passthrough_plain(self):
        assert strip_ansi("hello world") == "hello world"


class TestCleanForConfirm:
    def test_strips_trailing_empty(self):
        assert clean_for_confirm("hello\n\n\n") == "hello"


class TestCompilePattern:
    def test_basic(self):
        assert compile_pattern(r"hello").search("hello world")

    def test_flags(self):
        assert compile_pattern(r"hello", ["IGNORECASE"]).search("HELLO")

    def test_invalid_flag(self):
        with pytest.raises(ValueError):
            compile_pattern(r"hello", ["BADFLG"])


# ---------------------------------------------------------------------------
# _parse_toml
# ---------------------------------------------------------------------------

class TestParseToml:
    def test_basic_table(self):
        assert _parse_toml('[a]\nname = "claude"')["a"]["name"] == "claude"

    def test_array_of_tables(self):
        r = _parse_toml('[[c]]\np = "x"\n[[c]]\np = "y"')
        assert len(r["c"]) == 2

    def test_dotted_table(self):
        r = _parse_toml('[[state.patterns]]\nstate = "planning"')
        assert r["state"]["patterns"][0]["state"] == "planning"

    def test_numbers_and_bools(self):
        r = _parse_toml('[s]\na = 42\nb = 1.5\nc = true\nd = false')
        assert r["s"] == {"a": 42, "b": 1.5, "c": True, "d": False}

    def test_string_array(self):
        r = _parse_toml('[s]\nf = ["A", "B"]')
        assert r["s"]["f"] == ["A", "B"]

    def test_array_string_with_inner_commas(self):
        # Regression: claude.toml `command = ["claude", "--allowed-tools",
        # "Bash,Edit,Read,..."]` was being shattered into 11 separate
        # args because the array splitter was comma-naive. The third
        # element must be preserved as a single comma-string.
        r = _parse_toml(
            '[launch]\ncommand = ["claude", "--allowed-tools", '
            '"Bash,Edit,Read,Write,NotebookEdit"]'
        )
        assert r["launch"]["command"] == [
            "claude", "--allowed-tools",
            "Bash,Edit,Read,Write,NotebookEdit",
        ]

    def test_real_config(self):
        r = _parse_toml("""
[adapter]
name = "claude"
[launch]
command = ["claude", "--flag"]
prompt_after_launch = true
startup_wait = 30.0
[completion]
strategy = "prompt_count"
prompt_count_threshold = 2
[[confirm]]
pattern = "Enter to confirm"
response = ""
send_enter = true
""")
        assert r["adapter"]["name"] == "claude"
        assert r["launch"]["startup_wait"] == 30.0
        assert len(r["confirm"]) == 1


# ---------------------------------------------------------------------------
# AdapterConfig
# ---------------------------------------------------------------------------

class TestAdapterConfig:
    def test_parses_launch(self):
        c = AdapterConfig({"launch": {"command": ["claude"], "prompt_after_launch": True,
                                       "startup_wait": 30.0, "strip_ansi": True,
                                       "ready_pattern": "^>", "ready_flags": ["MULTILINE"]}})
        assert c.prompt_after_launch and c.startup_wait == 30.0 and c.ready_pattern

    def test_defaults(self):
        c = AdapterConfig({})
        assert not c.prompt_after_launch and c.startup_wait == 2.0


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------

class TestDetection:
    def test_state_first(self):
        c = AdapterConfig({"state": {"strategy": "first", "patterns": [
            {"state": "planning", "pattern": "Think"},
            {"state": "editing", "pattern": "Edit"}]}})
        assert detect_state("Think then Edit", c) == "planning"

    def test_state_last(self):
        c = AdapterConfig({"state": {"strategy": "last", "patterns": [
            {"state": "planning", "pattern": "Think"},
            {"state": "editing", "pattern": "Edit"}]}})
        assert detect_state("Think then Edit", c) == "editing"

    def test_completion_prompt_count(self):
        c = AdapterConfig({"completion": {"strategy": "prompt_count",
                            "prompt_pattern": "^>", "prompt_flags": ["MULTILINE"],
                            "prompt_count_threshold": 2}})
        assert detect_completion("> a\nwork\n> b", c) == "completed"
        assert detect_completion("> only one", c) is None

    def test_auto_confirm(self):
        c = AdapterConfig({"confirm": [{"pattern": "Enter to confirm", "response": "", "send_enter": True}]})
        confirm = should_auto_confirm("Enter to confirm", c)
        assert confirm[:2] == ("", True)
        assert should_auto_confirm("nothing", c) is None

    def test_ready(self):
        c = AdapterConfig({"launch": {"ready_pattern": "^>", "ready_flags": ["MULTILINE"]}})
        assert is_ready_for_input("> ready", c)
        assert not is_ready_for_input("Loading...", c)


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------

class TestBuildCommand:
    def test_prompt_placeholder(self):
        c = AdapterConfig({"launch": {"command": ["codex", "{prompt}"]}})
        assert _build_command(c, "fix bugs", "/tmp") == ["codex", "fix bugs"]

    def test_no_placeholders(self):
        c = AdapterConfig({"launch": {"command": ["claude", "--flag"]}})
        assert _build_command(c, "p", "/t") == ["claude", "--flag"]


# ---------------------------------------------------------------------------
# tmux_send_input — bracketed-paste behavior for multi-line text
# ---------------------------------------------------------------------------

class TestTmuxSendInputBracketedPaste:
    """Multi-line text must wrap with \\x1b[200~ ... \\x1b[201~ so the
    receiving TUI treats it as one atomic paste, otherwise the trailing
    Enter races with tmux's still-flushing literal-write and the input
    sits unsubmitted (reproduced live: codex camflow-review d248e129)."""

    def _capture_send_keys(self, text, send_enter=True):
        """Run tmux_send_input against a mock _run; return list of arg lists
        passed to subprocess so tests can inspect what was sent."""
        from camc_pkg import transport
        calls = []
        orig = transport._run
        try:
            transport._run = lambda args, **kw: (calls.append(args), (0, ""))[1]
            transport.tmux_send_input("nope", text, send_enter=send_enter)
        finally:
            transport._run = orig
        return calls

    def test_multiline_wrapped_with_bracketed_paste(self):
        from camc_pkg.transport import _BRACKET_PASTE_OPEN, _BRACKET_PASTE_CLOSE
        calls = self._capture_send_keys("line1\nline2\nline3")
        # Bracketed-paste structure: OPEN as its own call, body chunks,
        # then CLOSE as its own call. Small body fits in one chunk →
        # exactly 3 literal-write calls.
        lit = [c for c in calls if "-l" in c]
        assert lit[0][-1] == _BRACKET_PASTE_OPEN
        assert lit[-1][-1] == _BRACKET_PASTE_CLOSE
        body = "".join(c[-1] for c in lit[1:-1])
        assert body == "line1\nline2\nline3"

    def test_singleline_not_wrapped(self):
        calls = self._capture_send_keys("just one line")
        payload = calls[0][-1]
        assert payload == "just one line"
        assert "\x1b[200~" not in payload

    def test_large_multiline_chunks_with_bracket_markers(self):
        # tmux 3.4's send-keys -l rejects ~20KB+ payloads with "command
        # too long". Multi-line inputs above the chunk size MUST be
        # split into multiple send-keys calls, with the bracketed-paste
        # OPEN as the first literal-write call and CLOSE as the last,
        # so the receiving TUI still treats the body as one atomic paste.
        from camc_pkg.transport import (
            _TMUX_SEND_CHUNK, _BRACKET_PASTE_OPEN, _BRACKET_PASTE_CLOSE,
        )
        # Build a 21KB multi-line payload like camflow planner's prompt.
        body = ("a" * 100 + "\n") * 210  # ~21K bytes, contains \n
        assert len(body) > _TMUX_SEND_CHUNK
        calls = self._capture_send_keys(body)
        # Filter to literal-write calls only (-l flag); strip the Enter call.
        lit = [c for c in calls if "-l" in c]
        assert len(lit) >= 3, lit  # at least open + 1 chunk + close
        assert lit[0][-1] == _BRACKET_PASTE_OPEN
        assert lit[-1][-1] == _BRACKET_PASTE_CLOSE
        # Body chunks in the middle: concatenated must equal original text.
        assert "".join(c[-1] for c in lit[1:-1]) == body
        # Each chunk must be <= the chunk size cap.
        for c in lit[1:-1]:
            assert len(c[-1]) <= _TMUX_SEND_CHUNK

    def test_large_singleline_chunked_without_bracket_markers(self):
        # Single-line very long input: chunk, but no bracketed-paste
        # markers (no embedded LFs to compose).
        from camc_pkg.transport import (
            _TMUX_SEND_CHUNK, _BRACKET_PASTE_OPEN, _BRACKET_PASTE_CLOSE,
        )
        body = "x" * (_TMUX_SEND_CHUNK * 2 + 100)  # > 16KB, no \n
        calls = self._capture_send_keys(body)
        lit = [c for c in calls if "-l" in c]
        assert len(lit) >= 2  # at least 2 chunks
        # NO bracketed-paste markers
        assert all(_BRACKET_PASTE_OPEN not in c[-1] for c in lit)
        assert all(_BRACKET_PASTE_CLOSE not in c[-1] for c in lit)
        # Reconstructs to original text
        assert "".join(c[-1] for c in lit) == body

    def test_empty_text_only_sends_enter(self):
        calls = self._capture_send_keys("", send_enter=True)
        # Only the Enter call, no literal-write call
        assert len(calls) == 1
        assert calls[0][-1] == "Enter"


# ---------------------------------------------------------------------------
# camc msg — non-blocking send (--no-wait) + wait subcommand
# ---------------------------------------------------------------------------

class TestMsgNoWait:
    """`camc msg send <to> -t "..." --no-wait` injects + returns immediately
    with MSG_ID=<8hex>/STATUS=sent on stdout. The blocking default is
    untouched."""

    def _setup(self, monkeypatch, tmp_path, deliver_ok=True):
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda to: (None, "cam-fake", None))
        sent_calls = []
        monkeypatch.setattr(cli, "tmux_send_input",
                            lambda s, t, send_enter=True: (sent_calls.append((s, t)), deliver_ok)[1])
        return cli, ledger, sent_calls

    def test_no_wait_returns_immediately(self, monkeypatch, tmp_path, capsys):
        import argparse, re as _re
        cli, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        args = argparse.Namespace(to="fake", text="hello world",
                                  timeout=600, no_wait=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        m = _re.search(r"MSG_ID=([0-9a-f]{8})", out)
        assert m, "stdout missing MSG_ID=<8hex>"
        msg_id = m.group(1)
        assert "STATUS=sent" in out
        # Marker injected, ledger has sent + delivered records for the same id
        assert sent_calls and ("[camc msg#%s]:" % msg_id) in sent_calls[0][1]
        statuses = [r["status"] for r in cli._msg_ledger_iter(msg_id=msg_id)]
        assert statuses == ["sent", "delivered"]

    def test_no_wait_deliver_failure_exits_1(self, monkeypatch, tmp_path, capsys):
        import argparse
        cli, ledger, _ = self._setup(monkeypatch, tmp_path, deliver_ok=False)
        args = argparse.Namespace(to="fake", text="x", timeout=600, no_wait=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 1
        # stderr names the target; ledger has sent + deliver_failed
        cap = capsys.readouterr()
        assert "fake" in cap.err
        statuses = [r["status"] for r in cli._msg_ledger_iter()]
        assert "deliver_failed" in statuses


class TestMsgWait:
    """`camc msg wait <msg_id>` first checks ledger for an existing reply,
    then falls back to a polling loop on the recorded session."""

    def test_wait_returns_existing_replied_immediately(self, monkeypatch, tmp_path, capsys):
        import argparse
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        # Pre-seed: sent → delivered → replied
        cli._msg_ledger_append({"msg_id": "abc12345", "to": "fake",
                                "tmux_session": "cam-fake", "text": "q",
                                "status": "sent", "timeout_s": 600})
        cli._msg_ledger_append({"msg_id": "abc12345", "status": "delivered"})
        cli._msg_ledger_append({"msg_id": "abc12345", "status": "replied",
                                "reply": "PRIOR_REPLY_FROM_LEDGER"})

        args = argparse.Namespace(msg_id="abc12345", timeout=10)
        # No capture_tmux mock — must not be called for ledger-hit path.
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_wait(args)
        assert ei.value.code == 0
        assert "PRIOR_REPLY_FROM_LEDGER" in capsys.readouterr().out

    def test_wait_no_sent_record_exits_1(self, monkeypatch, tmp_path, capsys):
        import argparse
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(tmp_path / "messages.jsonl"))
        args = argparse.Namespace(msg_id="00000000", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_wait(args)
        assert ei.value.code == 1
        assert "00000000" in capsys.readouterr().err

    def test_finalize_does_not_double_append_replied(self, monkeypatch, tmp_path, capsys):
        # Per peer spec: append `replied` only if not already present in
        # ledger, so a re-wait or parallel wait can't add duplicates.
        import argparse
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(tmp_path / "messages.jsonl"))
        cli._msg_ledger_append({"msg_id": "ddee0011", "to": "fake",
                                "tmux_session": "cam-fake", "text": "q",
                                "status": "sent", "timeout_s": 600})
        cli._msg_ledger_append({"msg_id": "ddee0011", "status": "replied",
                                "reply": "EXISTING"})
        # Force the slow path (reply *just* arrived via polling) and
        # confirm finalize is a no-op on the ledger when one already
        # exists. Use sys.exit catcher.
        with pytest.raises(SystemExit) as ei:
            cli._msg_finalize_wait("fake", "ddee0011", 60,
                                   "DUPLICATE_FROM_POLL", "replied")
        assert ei.value.code == 0
        records = list(cli._msg_ledger_iter(msg_id="ddee0011"))
        replied = [r for r in records if r.get("status") == "replied"]
        assert len(replied) == 1
        assert replied[0]["reply"] == "EXISTING"


class TestMsgWaitLoop:
    """`_msg_wait_loop` polling logic — extracted helper, mockable."""

    def test_finds_stable_reply(self, monkeypatch):
        from camc_pkg import cli
        pane = (
            "previous content\n"
            "› [camc msg#abc12345]: my question\n"
            "\n"
            "• short answer\n"
            "\n"
            "› placeholder\n"
            "  gpt-5.5 xhigh fast · /tmp\n"
        )
        monkeypatch.setattr(cli, "capture_tmux", lambda s, lines=500: pane)
        reply, status = cli._msg_wait_loop("cam-x", "abc12345",
                                           timeout_s=5, tool_busy=None,
                                           poll=0.02, stable_for=2)
        assert status == "replied"
        assert "short answer" in reply

    def test_no_marker_returns_no_marker(self, monkeypatch):
        from camc_pkg import cli
        monkeypatch.setattr(cli, "capture_tmux",
                            lambda s, lines=500: "no marker line in this pane\n")
        reply, status = cli._msg_wait_loop("cam-x", "deadbeef",
                                           timeout_s=0.2, tool_busy=None,
                                           poll=0.02, stable_for=2)
        assert reply is None
        assert status == "no_marker"

    def test_busy_tail_blocks_success(self, monkeypatch):
        from camc_pkg import cli
        pane = (
            "› [camc msg#abc12345]: my question\n"
            "\n"
            "• partial answer being generated\n"
            "esc to interrupt\n"
        )
        monkeypatch.setattr(cli, "capture_tmux", lambda s, lines=500: pane)
        reply, status = cli._msg_wait_loop("cam-x", "abc12345",
                                           timeout_s=0.2, tool_busy=None,
                                           poll=0.02, stable_for=2)
        # Marker IS in pane → not "no_marker"; busy tail prevents stable
        # accumulation → "reply_not_stable" timeout.
        assert reply is None
        assert status == "reply_not_stable"

    def test_marker_scrolled_out_falls_back_to_full_capture(self, monkeypatch):
        from camc_pkg import cli
        bounded = "\n".join("scrollback line %03d" % i for i in range(500))
        full = (
            "older pane content\n"
            "› [camc msg#abc12345]: long request\n"
            "\n"
            "• final answer\n"
            + "\n".join("tool output line %03d" % i for i in range(700)) +
            "\n"
            "✻ Churned for 6m 50s\n"
            "\n"
            "❯\n"
        )
        calls = []

        def fake_capture(session, lines=500):
            calls.append(lines)
            return full if not lines or lines <= 0 else bounded

        monkeypatch.setattr(cli, "capture_tmux", fake_capture)
        reply, status = cli._msg_wait_loop("cam-x", "abc12345",
                                           timeout_s=5, tool_busy=None,
                                           poll=0.02, stable_for=2)
        assert status == "replied"
        assert "final answer" in reply
        assert any(not lines or lines <= 0 for lines in calls)

    def test_marker_seen_then_static_screen_completes(self, monkeypatch):
        from camc_pkg import cli
        marker_visible = (
            "› [camc msg#abc12345]: long request\n"
            "\n"
            "• last extracted answer\n"
            "tool output before marker scrolls away\n"
        )
        marker_gone_static = (
            "tool output line 698\n"
            "tool output line 699\n"
            "✻ Churned for 6m 50s\n"
            "\n"
            "❯\n"
        )
        calls = []

        def fake_capture(session, lines=500):
            calls.append(lines)
            if len(calls) == 1 and lines == 500:
                return marker_visible
            return marker_gone_static

        monkeypatch.setattr(cli, "capture_tmux", fake_capture)
        reply, status = cli._msg_wait_loop("cam-x", "abc12345",
                                           timeout_s=5, tool_busy=None,
                                           poll=0.02, stable_for=2)
        assert status == "replied"
        assert "last extracted answer" in reply

    def test_marker_seen_then_cleared_screen_completes(self, monkeypatch):
        from camc_pkg import cli
        marker_visible = (
            "› [camc msg#abc12345]: long request\n"
            "\n"
            "• answer before compact clears the pane\n"
        )
        calls = []

        def fake_capture(session, lines=500):
            calls.append(lines)
            if len(calls) == 1 and lines == 500:
                return marker_visible
            return ""

        monkeypatch.setattr(cli, "capture_tmux", fake_capture)
        reply, status = cli._msg_wait_loop("cam-x", "abc12345",
                                           timeout_s=5, tool_busy=None,
                                           poll=0.02, stable_for=2)
        assert status == "replied"
        assert "answer before compact clears the pane" in reply

    def test_marker_seen_then_changing_screen_does_not_complete(self, monkeypatch):
        from camc_pkg import cli
        marker_visible = (
            "› [camc msg#abc12345]: long request\n"
            "\n"
            "• partial answer\n"
        )
        calls = []

        def fake_capture(session, lines=500):
            calls.append(lines)
            if len(calls) == 1 and lines == 500:
                return marker_visible
            return "still changing heartbeat %03d\n" % len(calls)

        monkeypatch.setattr(cli, "capture_tmux", fake_capture)
        reply, status = cli._msg_wait_loop("cam-x", "abc12345",
                                           timeout_s=0.2, tool_busy=None,
                                           poll=0.02, stable_for=2)
        assert reply is None
        assert status == "reply_not_stable"


# ---------------------------------------------------------------------------
# AgentStore
# ---------------------------------------------------------------------------

class TestAgentStore:
    @pytest.fixture
    def store(self, tmp_path):
        return AgentStore(str(tmp_path / "agents.json"))

    def test_empty(self, store):
        assert store.list() == []

    def test_save_and_get(self, store):
        store.save({"id": "abc12345", "tool": "claude"})
        assert store.get("abc12345")["id"] == "abc12345"

    def test_prefix_match(self, store):
        store.save({"id": "abc12345", "tool": "claude"})
        assert store.get("abc")["id"] == "abc12345"

    def test_update(self, store):
        store.save({"id": "abc12345", "status": "running"})
        store.update("abc12345", status="completed")
        assert store.get("abc12345")["status"] == "completed"

    def test_remove(self, store):
        store.save({"id": "abc12345"})
        assert store.remove("abc12345")
        assert store.list() == []

    def test_corrupted_recovery(self, store):
        os.makedirs(os.path.dirname(store._path), exist_ok=True)
        with open(store._path, "w") as f:
            f.write("{{{bad")
        assert store.list() == []
        store.save({"id": "new"})
        assert len(store.list()) == 1


# ---------------------------------------------------------------------------
# _time_ago
# ---------------------------------------------------------------------------

class TestTimeAgo:
    def test_none(self):
        assert _time_ago(None) == ""

    def test_recent(self):
        from datetime import datetime
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        assert "s ago" in _time_ago(now) or _time_ago(now) == "0s ago"


# ---------------------------------------------------------------------------
# AgentStore imported from client.py (not camc.py)
# ---------------------------------------------------------------------------

class TestAgentStoreImport:
    def test_import_from_client(self):
        """AgentStore should be importable from cam.client."""
        from cam.client import AgentStore as ClientAgentStore
        assert ClientAgentStore is AgentStore

    def test_import_from_camc(self):
        """camc should re-export AgentStore from cam.client."""
        from cam.camc import AgentStore as CamcAgentStore
        assert CamcAgentStore is AgentStore


# ---------------------------------------------------------------------------
# Subcommand functions from client.py
# ---------------------------------------------------------------------------

class TestCmdPing:
    def test_ping_output(self, capsys):
        _cmd_ping()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert "version" in data
        assert "platform" in data


class TestCmdStatus:
    def test_status_returns_agents(self, tmp_path, capsys, monkeypatch):
        store_path = str(tmp_path / "agents.json")
        store = AgentStore(store_path)
        store.save({"id": "abc123", "status": "running"})

        # Monkey-patch the module-level AGENTS_FILE
        import cam.client as cl
        monkeypatch.setattr(cl, "AGENTS_FILE", store_path)

        _cmd_status()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "agents" in data
        assert "hash" in data
        assert len(data["agents"]) == 1
        assert data["agents"][0]["id"] == "abc123"

    def test_status_hash_match(self, tmp_path, capsys, monkeypatch):
        store_path = str(tmp_path / "agents.json")
        store = AgentStore(store_path)
        store.save({"id": "abc123", "status": "running"})

        import cam.client as cl
        monkeypatch.setattr(cl, "AGENTS_FILE", store_path)

        # Compute expected hash
        agents = store.list()
        raw = json.dumps(agents, sort_keys=True)
        expected_hash = hashlib.md5(raw.encode()).hexdigest()[:8]

        # Inject --hash arg
        monkeypatch.setattr(sys, "argv", ["cam-client.py", "status", "--hash", expected_hash])

        _cmd_status()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["unchanged"] is True
        assert data["hash"] == expected_hash

    def test_status_hash_mismatch(self, tmp_path, capsys, monkeypatch):
        store_path = str(tmp_path / "agents.json")
        store = AgentStore(store_path)
        store.save({"id": "abc123", "status": "running"})

        import cam.client as cl
        monkeypatch.setattr(cl, "AGENTS_FILE", store_path)

        monkeypatch.setattr(sys, "argv", ["cam-client.py", "status", "--hash", "wronghash"])

        _cmd_status()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "agents" in data
        assert data["hash"] != "wronghash"


class TestCmdSessionCapture:
    def test_capture_hash_gated(self, capsys, monkeypatch):
        """Test _cmd_session_capture with hash-gated output."""
        import cam.client as cl

        # Mock capture_tmux
        monkeypatch.setattr(cl, "capture_tmux", lambda sid, lines=100: "test output")
        monkeypatch.setattr(sys, "argv", ["cam-client.py", "session", "capture", "--id", "test1"])

        cl._cmd_session_capture()
        captured = capsys.readouterr()
        lines = captured.out.split("\n", 1)
        assert lines[0].startswith("hash:")
        assert "test output" in captured.out

    def test_capture_unchanged(self, capsys, monkeypatch):
        """When hash matches, only hash line is returned."""
        import cam.client as cl

        output_text = "test output"
        h = hashlib.md5(output_text.encode()).hexdigest()[:8]

        monkeypatch.setattr(cl, "capture_tmux", lambda sid, lines=100: output_text)
        monkeypatch.setattr(sys, "argv", [
            "cam-client.py", "session", "capture", "--id", "test1", "--hash", h
        ])

        cl._cmd_session_capture()
        captured = capsys.readouterr()
        # Should only have the hash line, no content
        assert captured.out == "hash:%s\n" % h
