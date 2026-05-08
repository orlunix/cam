"""Tests for camc — standalone local agent CLI."""

import hashlib
import json
import os
import sys
from types import SimpleNamespace
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
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        monkeypatch.setattr(cli, "_msg_target_identity", lambda to: None)
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
        # Marker injected, ledger has sent + delivered status records (V0 also
        # writes turn/delivery records that don't carry a `status` key).
        assert sent_calls and ("[camc msg#%s]:" % msg_id) in sent_calls[0][1]
        statuses = [r["status"] for r in cli._msg_ledger_iter(msg_id=msg_id)
                    if "status" in r]
        assert statuses == ["sent", "delivered"]

    def test_send_bare_anchor_when_neither_resolves(self, monkeypatch, tmp_path, capsys):
        # Neither sender nor target resolvable → wire payload has NO
        # attribution blocks and NO extra leading space; reduces to the
        # bare-anchor form `[camc msg#<id>]: <text>`.
        import argparse, re as _re
        cli, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        args = argparse.Namespace(to="cam-rawsession", text="bare ping",
                                  timeout=600, no_wait=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})",
                            capsys.readouterr().out).group(1)
        assert sent_calls[0][1] == "[camc msg#%s]: bare ping" % msg_id

    def test_send_includes_sender_identity_when_known(self, monkeypatch, tmp_path, capsys):
        import argparse, re as _re
        cli, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "camflow-review",
            "sender_id": "5f765953",
            "sender_tmux_session": "cam-5f765953",
        })
        args = argparse.Namespace(to="fake", text="review the patch",
                                  timeout=600, no_wait=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})", out).group(1)
        assert sent_calls[0][1] == (
            "[camc msg#%s]: [from:camflow-review#5f765953] review the patch"
            % msg_id
        )
        sent = next(r for r in cli._msg_ledger_iter(msg_id=msg_id)
                    if r["status"] == "sent")
        assert sent["text"] == "review the patch"
        assert sent["sender_name"] == "camflow-review"
        assert sent["sender_id"] == "5f765953"
        assert sent["sender_tmux_session"] == "cam-5f765953"

    def test_send_includes_target_identity_when_known(self, monkeypatch, tmp_path, capsys):
        # Visible target attribution: when the target resolves to a known
        # agent, the wire payload includes a [to ...] block alongside the
        # existing [from ...] block. Anchor stays unchanged so old-format
        # replies still extract correctly.
        import argparse, re as _re
        cli, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "camflow-review",
            "sender_id": "5f765953",
            "sender_tmux_session": "cam-5f765953",
        })
        monkeypatch.setattr(cli, "_msg_target_identity", lambda to: {
            "target_name": "camflow-dev",
            "target_id": "836208f0",
            "target_tmux_session": "cam-836208f0",
        })
        args = argparse.Namespace(to="camflow-dev", text="please review",
                                  timeout=600, no_wait=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})",
                            capsys.readouterr().out).group(1)
        # Both blocks adjacent, no inter-block space; exactly one space
        # before the user text.
        assert sent_calls[0][1] == (
            "[camc msg#%s]: [from:camflow-review#5f765953]"
            "[to:camflow-dev#836208f0] please review" % msg_id
        )
        sent = next(r for r in cli._msg_ledger_iter(msg_id=msg_id)
                    if r["status"] == "sent")
        assert sent["target_name"] == "camflow-dev"
        assert sent["target_id"] == "836208f0"
        assert sent["target_tmux_session"] == "cam-836208f0"
        # `to` label (the user-supplied argument) is NOT overwritten.
        assert sent["to"] == "camflow-dev"
        # Sender fields remain untouched.
        assert sent["sender_name"] == "camflow-review"

    def test_send_omits_target_block_when_unresolved(self, monkeypatch, tmp_path, capsys):
        # Raw tmux session / unknown name: no [to ...] block, no target_*
        # fields in the ledger record. Wire payload looks the same as
        # before this feature shipped.
        import argparse, re as _re
        cli, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "camflow-review",
            "sender_id": "5f765953",
            "sender_tmux_session": "cam-5f765953",
        })
        # _msg_target_identity already mocked to None in _setup.
        args = argparse.Namespace(to="cam-rawsession", text="ping",
                                  timeout=600, no_wait=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})",
                            capsys.readouterr().out).group(1)
        # Sender resolves, target does not → only [from:…] block, single
        # space before user text.
        assert sent_calls[0][1] == (
            "[camc msg#%s]: [from:camflow-review#5f765953] ping" % msg_id
        )
        sent = next(r for r in cli._msg_ledger_iter(msg_id=msg_id)
                    if r["status"] == "sent")
        assert "target_name" not in sent
        assert "target_id" not in sent
        assert "target_tmux_session" not in sent

    def test_target_identity_resolves_known_agent(self, monkeypatch):
        # _msg_target_identity should look up via AgentStore and return a
        # normalized dict. Mirrors the sender-side resolver.
        from camc_pkg import cli

        class FakeStore:
            def get(self, key):
                assert key == "camflow-dev"
                return {
                    "id": "836208f0",
                    "task": {"name": "camflow-dev"},
                    "tmux_session": "cam-836208f0",
                }

        monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore())
        target = cli._msg_target_identity("camflow-dev")
        assert target == {
            "target_id": "836208f0",
            "target_name": "camflow-dev",
            "target_tmux_session": "cam-836208f0",
        }

    def test_target_identity_unknown_returns_none(self, monkeypatch):
        from camc_pkg import cli

        class FakeStore:
            def get(self, key):
                return None

        monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore())
        assert cli._msg_target_identity("not-an-agent") is None
        assert cli._msg_target_identity("") is None
        assert cli._msg_target_identity(None) is None

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
        statuses = [r["status"] for r in cli._msg_ledger_iter()
                    if "status" in r]
        assert "deliver_failed" in statuses

    def test_sender_identity_resolves_current_tmux_agent(self, monkeypatch):
        from camc_pkg import cli
        monkeypatch.setenv("TMUX", "/tmp/cam-agent-sockets/cam-abcd1234.sock,1,0")
        monkeypatch.setenv("TMUX_PANE", "%7")
        seen = {}
        monkeypatch.setattr(cli, "_run", lambda args, timeout=2:
                            (seen.setdefault("args", args) and 0, "cam-abcd1234\n"))

        class FakeStore:
            def get(self, key):
                assert key == "cam-abcd1234"
                return {
                    "id": "abcd1234",
                    "task": {"name": "sender-agent"},
                    "tmux_session": "cam-abcd1234",
                }

        monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore())
        sender = cli._msg_sender_identity()
        assert sender == {
            "sender_id": "abcd1234",
            "sender_name": "sender-agent",
            "sender_tmux_session": "cam-abcd1234",
        }
        assert "-S" in seen["args"]
        assert "%7" in seen["args"]


class TestMsgExpectReply:
    """`--expect-reply` Phase 1 async: receiver instruction appended to
    wire, sent record flagged, `wait` switches to ledger-poll, and
    `camc msg reply <id>` commits the reply + (when sender resolves)
    fires a notification correlated by `[reply_to:<orig_id>]`."""

    def _setup(self, monkeypatch, tmp_path, deliver_ok=True):
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda to: (None, "cam-fake", None))
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        monkeypatch.setattr(cli, "_msg_target_identity", lambda to: None)
        sent_calls = []
        monkeypatch.setattr(cli, "tmux_send_input",
                            lambda s, t, send_enter=True: (sent_calls.append((s, t)), deliver_ok)[1])
        return cli, ledger, sent_calls

    def test_expect_reply_no_wait_appends_instruction(self, monkeypatch, tmp_path, capsys):
        import argparse, re as _re
        cli, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        args = argparse.Namespace(to="fake", text="please review src/foo.py",
                                  timeout=600, no_wait=True, expect_reply=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})", out).group(1)
        assert "STATUS=sent" in out
        assert "EXPECT_REPLY=yes" in out
        # Wire payload contains the user text AND the reply-via instruction.
        wire = sent_calls[0][1]
        assert "please review src/foo.py" in wire
        assert ('[Reply via: camc msg reply %s -t "<your final answer>"]' % msg_id) in wire
        # Sent record carries expect_reply=true. Ledger `text` is the
        # original (instruction is wire-only, not stored as user text).
        sent = next(r for r in cli._msg_ledger_iter(msg_id=msg_id)
                    if r["status"] == "sent")
        assert sent["expect_reply"] is True
        assert sent["text"] == "please review src/foo.py"

    def test_blocking_expect_reply_polls_ledger(self, monkeypatch, tmp_path, capsys):
        # Without --no-wait + --expect-reply: send injects, then blocks
        # on ledger polling. Simulate the receiver running `msg reply`
        # by pre-seeding a `replied` record before send is called → the
        # poll should see it on the first tick and return immediately.
        import argparse
        from camc_pkg import cli
        cli_mod, ledger, sent_calls = self._setup(monkeypatch, tmp_path)
        # Force _msg_inject to use a deterministic msg_id so we can
        # pre-seed the replied record.
        monkeypatch.setattr(cli_mod, "uuid4",
                            lambda: type("U", (), {"hex": "deadbeefcafe1111"})())
        # Pre-seed: a replied record will be on disk by the time the
        # blocking-send finishes inject and enters wait.
        # We need to seed AFTER send writes its own `sent` record but
        # ledger is append-only so order doesn't actually matter for
        # _msg_find_replied. Pre-seed now:
        cli_mod._msg_ledger_append({
            "msg_id": "deadbeef", "status": "replied", "reply": "RECORDED",
        })
        args = argparse.Namespace(to="fake", text="ping", timeout=10,
                                  no_wait=False, expect_reply=True)
        with pytest.raises(SystemExit) as ei:
            cli_mod.cmd_msg_send(args)
        assert ei.value.code == 0
        assert "RECORDED" in capsys.readouterr().out


def _replied_count(cli, msg_id):
    """How many legacy `status=replied` records exist for msg_id."""
    return sum(1 for r in cli._msg_ledger_iter(msg_id=msg_id)
               if r.get("status") == "replied")


class TestMsgReply:
    """`camc msg reply <msg_id> -t "..."` commits the reply against the
    original ledger entry and (if sender resolves) fires a correlated
    notification message."""

    def _setup(self, monkeypatch, tmp_path):
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        # _msg_inject path: mock so notification injection succeeds without
        # real tmux. _msg_resolve_session decides whether sender is reachable.
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        monkeypatch.setattr(cli, "_msg_target_identity", lambda to: None)
        sent_calls = []
        monkeypatch.setattr(cli, "tmux_send_input",
                            lambda s, t, send_enter=True: (sent_calls.append((s, t)), True)[1])
        return cli, ledger, sent_calls

    def test_reply_unknown_id_exits_1(self, monkeypatch, tmp_path, capsys):
        import argparse
        cli, _, _ = self._setup(monkeypatch, tmp_path)
        # Empty ledger; no sent record for this id.
        args = argparse.Namespace(msg_id="00000000", text="x", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_reply(args)
        assert ei.value.code == 1
        assert "00000000" in capsys.readouterr().err

    def test_reply_appends_seq_2_under_same_msg_id(self, monkeypatch, tmp_path, capsys):
        # V0 mailbox/thread: msg_id is the THREAD id. Reply does NOT mint a
        # new logical id — it appends a turn(seq=2) and a delivery to the
        # original sender's mailbox under the SAME msg_id.
        import argparse
        cli, _, sent_calls = self._setup(monkeypatch, tmp_path)
        # Original send (seq=1) records — turn + delivery + sent status.
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "camflow-dev",
            "tmux_session": "cam-camflow-dev", "text": "please review",
            "status": "sent", "timeout_s": 600,
            "sender_name": "camflow-review", "sender_id": "5f765953",
            "sender_tmux_session": "cam-5f765953",
            "target_name": "camflow-dev", "target_id": "836208f0",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "5f765953", "from_name": "camflow-review",
            "to_id": "836208f0", "to_name": "camflow-dev",
            "text": "please review",
        })
        # Resolver: original sender's id resolves to a session (best-effort
        # notification will fire); also stub _msg_target_identity for the
        # [to:…] prefix lookup of the SAME id.
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda lbl: (None, "cam-5f765953", None) if lbl == "5f765953" else (None, None, None))
        monkeypatch.setattr(cli, "_msg_target_identity", lambda lbl: {
            "target_name": "camflow-review", "target_id": "5f765953",
            "target_tmux_session": "cam-5f765953",
        } if lbl == "5f765953" else None)
        # Current sender = the original target (camflow-dev).
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "camflow-dev", "sender_id": "836208f0",
            "sender_tmux_session": "cam-camflow-dev",
        })
        args = argparse.Namespace(msg_id="abc12345", text="LGTM, ship it", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_reply(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        # New stdout shape — no REPLY_MSG_ID, just SEQ + MAILBOX.
        assert "REPLIED_TO=abc12345" in out
        assert "SEQ=2" in out
        assert "MAILBOX=agent:5f765953" in out
        assert "REPLY_MSG_ID=" not in out
        # New turn under SAME msg_id with seq=2, recipient=original sender.
        turns = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                 if r.get("record") == "turn"]
        assert [t["seq"] for t in turns] == [1, 2]
        t2 = turns[1]
        assert t2["from_id"] == "836208f0"     # current agent
        assert t2["to_id"] == "5f765953"       # original sender
        assert t2["text"] == "LGTM, ship it"
        # Delivery to original sender's mailbox.
        deliveries = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                      if r.get("record") == "delivery"]
        assert any(d["seq"] == 2 and d["mailbox_id"] == "agent:5f765953"
                   for d in deliveries)
        # Best-effort wire injection on the SAME thread marker — no new id.
        assert sent_calls, "best-effort notification not injected"
        wire = sent_calls[0][1]
        assert "[camc msg#abc12345]:" in wire   # SAME logical id
        assert "[reply_to:" not in wire         # no legacy correlation block
        assert "LGTM, ship it" in wire
        # Legacy first-reply compat: status=replied should also exist.
        assert _replied_count(cli, "abc12345") == 1

    def test_third_reply_appends_seq_3_even_when_replied_exists(
            self, monkeypatch, tmp_path, capsys):
        # V0 spec: "second reply/follow-up appends seq=3 under same msg_id
        # even if status=replied already exists." No idempotency block.
        import argparse
        cli, _, sent_calls = self._setup(monkeypatch, tmp_path)
        # Pre-seed: send (seq=1), first reply already committed (seq=2 +
        # legacy status=replied).
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "q", "status": "sent", "timeout_s": 600,
            "sender_name": "A", "sender_id": "aaaa1111",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B",
            "text": "q",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 2, "kind": "message",
            "from_id": "bbbb2222", "from_name": "B",
            "to_id": "aaaa1111", "to_name": "A",
            "text": "first reply",
        })
        cli._msg_ledger_append({"msg_id": "abc12345", "status": "replied",
                                "reply": "first reply"})
        # Now A follows up (seq=3 under SAME msg_id).
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "A", "sender_id": "aaaa1111",
            "sender_tmux_session": "cam-A",
        })
        # Recipient session unresolvable — that's fine, mailbox still
        # records the delivery.
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda lbl: (None, None, None))
        args = argparse.Namespace(msg_id="abc12345", text="follow-up", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_reply(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "SEQ=3" in out
        # Three turns under the SAME msg_id.
        turns = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                 if r.get("record") == "turn"]
        assert [t["seq"] for t in turns] == [1, 2, 3]
        # Other side of seq=2 was A; A is replying; recipient is `from`
        # of seq=2 NOT — wait: A is the from_id of last turn (seq=2 was
        # B→A). A is the TO of seq=2, so other side = from = B. Reply
        # goes to B.
        assert turns[2]["to_id"] == "bbbb2222"
        # Subsequent reply does NOT add another legacy `replied` record.
        assert _replied_count(cli, "abc12345") == 1

    def test_reply_routing_prefers_id_then_session_then_name(
            self, monkeypatch, tmp_path, capsys):
        # Routing preference is preserved from prior implementation:
        # id > session > name. id resolves first → no further probes.
        import argparse
        cli, _, sent_calls = self._setup(monkeypatch, tmp_path)
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "q", "status": "sent", "timeout_s": 600,
            "sender_name": "A", "sender_id": "aaaa1111",
            "sender_tmux_session": "cam-A",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B",
            "text": "q",
        })
        probes = []
        def _resolver(lbl):
            probes.append(lbl)
            return (None, "cam-A", None) if lbl == "aaaa1111" else (None, None, None)
        monkeypatch.setattr(cli, "_msg_resolve_session", _resolver)
        monkeypatch.setattr(cli, "_msg_target_identity",
                            lambda lbl: {"target_name": "A", "target_id": "aaaa1111",
                                         "target_tmux_session": "cam-A"})
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "B", "sender_id": "bbbb2222",
            "sender_tmux_session": "cam-B",
        })
        args = argparse.Namespace(msg_id="abc12345", text="ack", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_reply(args)
        assert ei.value.code == 0
        # id resolved on the first probe → no fallback to session/name.
        assert probes == ["aaaa1111"]

    def test_reply_with_unresolvable_recipient_still_records_thread(
            self, monkeypatch, tmp_path, capsys):
        # No reachable session for the recipient: wire injection skipped,
        # but turn + delivery still go into the ledger so `read` works.
        import argparse
        cli, _, sent_calls = self._setup(monkeypatch, tmp_path)
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "q", "status": "sent", "timeout_s": 600,
            "sender_name": "ghost", "sender_id": "deadbeef",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "deadbeef", "from_name": "ghost",
            "to_id": "bbbb2222", "to_name": "B",
            "text": "q",
        })
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda lbl: (None, None, None))
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "B", "sender_id": "bbbb2222",
            "sender_tmux_session": "cam-B",
        })
        args = argparse.Namespace(msg_id="abc12345", text="silent", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_reply(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "REPLIED_TO=abc12345" in out
        assert "SEQ=2" in out
        # Mailbox falls back to agent:<id> since to_id is known.
        assert "MAILBOX=agent:deadbeef" in out
        assert sent_calls == []   # no wire injection
        # turn + delivery still recorded.
        turns = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                 if r.get("record") == "turn"]
        deliveries = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                      if r.get("record") == "delivery"]
        assert len(turns) == 2
        assert any(d["seq"] == 2 for d in deliveries)

    def test_wait_with_expect_reply_uses_ledger_polling(self, monkeypatch, tmp_path, capsys):
        # When the original sent record has expect_reply=true, `camc msg
        # wait` polls the ledger — never calls capture_tmux. Pre-seed a
        # `replied` record so wait returns on the first tick.
        import argparse
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(tmp_path / "messages.jsonl"))
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "x", "tmux_session": "cam-x",
            "text": "q", "status": "sent", "timeout_s": 600,
            "expect_reply": True,
        })
        # capture_tmux MUST NOT be called on the ledger-poll path.
        def _explode(*_a, **_kw):
            raise AssertionError("capture_tmux called on ledger-poll path")
        monkeypatch.setattr(cli, "capture_tmux", _explode)
        # Seed the reply that ledger-poll will find.
        cli._msg_ledger_append({"msg_id": "abc12345", "status": "replied",
                                "reply": "ASYNC_REPLY"})
        args = argparse.Namespace(msg_id="abc12345", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_wait(args)
        assert ei.value.code == 0
        assert "ASYNC_REPLY" in capsys.readouterr().out


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
# camc msg V0 mailbox/thread — send writes turn/delivery + camc msg read
# ---------------------------------------------------------------------------

class TestMsgSendMailbox:
    """`camc msg send` writes turn(seq=1) + delivery(mailbox_id=…) records
    alongside the legacy sent/delivered status records for backward
    compatibility."""

    def _setup(self, monkeypatch, tmp_path):
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda to: (None, "cam-fake", None))
        sent_calls = []
        monkeypatch.setattr(cli, "tmux_send_input",
                            lambda s, t, send_enter=True: (sent_calls.append((s, t)), True)[1])
        return cli, ledger, sent_calls

    def test_send_writes_turn_seq1_and_delivery(self, monkeypatch, tmp_path, capsys):
        # When both sender and target resolve, the mailbox_id should be
        # `agent:<target_id>` and the turn record carries from/to ids.
        import argparse, re as _re
        cli, _, sent_calls = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "A", "sender_id": "aaaa1111",
            "sender_tmux_session": "cam-A",
        })
        monkeypatch.setattr(cli, "_msg_target_identity", lambda lbl: {
            "target_name": "B", "target_id": "bbbb2222",
            "target_tmux_session": "cam-B",
        })
        args = argparse.Namespace(to="B", text="hello",
                                  timeout=600, no_wait=True, expect_reply=False)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})",
                            capsys.readouterr().out).group(1)
        turns = [r for r in cli._msg_ledger_iter(msg_id=msg_id)
                 if r.get("record") == "turn"]
        assert len(turns) == 1
        t = turns[0]
        assert t["seq"] == 1
        assert t["kind"] == "message"
        assert t["from_id"] == "aaaa1111"
        assert t["to_id"] == "bbbb2222"
        assert t["text"] == "hello"
        assert t.get("schema") == "camc-msg/1"
        deliveries = [r for r in cli._msg_ledger_iter(msg_id=msg_id)
                      if r.get("record") == "delivery"]
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d["seq"] == 1
        assert d["mailbox_id"] == "agent:bbbb2222"
        # Legacy sent/delivered status records still present.
        statuses = [r.get("status") for r in cli._msg_ledger_iter(msg_id=msg_id)
                    if "status" in r]
        assert "sent" in statuses
        assert "delivered" in statuses

    def test_send_mailbox_falls_back_to_session_then_label(
            self, monkeypatch, tmp_path, capsys):
        # No target identity but session is known → mailbox = session:<…>.
        import argparse, re as _re
        cli, _, _ = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        monkeypatch.setattr(cli, "_msg_target_identity", lambda lbl: None)
        args = argparse.Namespace(to="cam-rawsession", text="ping",
                                  timeout=600, no_wait=True, expect_reply=False)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_send(args)
        assert ei.value.code == 0
        msg_id = _re.search(r"MSG_ID=([0-9a-f]{8})",
                            capsys.readouterr().out).group(1)
        d = next(r for r in cli._msg_ledger_iter(msg_id=msg_id)
                 if r.get("record") == "delivery")
        # Resolver mock returned session=cam-fake; that beats the label.
        assert d["mailbox_id"] == "session:cam-fake"


class TestMsgRead:
    """`camc msg read` — inbox listing, --next, thread replay, --mark."""

    def _setup(self, monkeypatch, tmp_path, current_id="bbbb2222"):
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        # Current agent identity (drives mailbox candidates).
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_id": current_id, "sender_name": "B",
            "sender_tmux_session": "cam-B",
        })
        return cli

    def _seed_thread(self, cli, msg_id, my_id="bbbb2222"):
        # Send (seq=1) + delivery to my mailbox.
        cli._msg_ledger_append({
            "msg_id": msg_id, "to": "B", "tmux_session": "cam-B",
            "text": "q", "status": "sent", "timeout_s": 600,
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": msg_id, "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": my_id, "to_name": "B",
            "text": "q-text",
        })
        cli._msg_ledger_append({
            "record": "delivery", "schema": "camc-msg/1",
            "msg_id": msg_id, "seq": 1,
            "mailbox_id": "agent:%s" % my_id,
            "to_id": my_id, "to_name": "B",
        })

    def test_read_lists_unread_for_current_mailbox(
            self, monkeypatch, tmp_path, capsys):
        import argparse
        cli = self._setup(monkeypatch, tmp_path)
        self._seed_thread(cli, "abc11111")
        self._seed_thread(cli, "def22222")
        args = argparse.Namespace(msg_id=None, next_msg=False, mark=False,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_read(args)
        assert ei.value.code == 0
        rows = json.loads(capsys.readouterr().out)
        assert {r["msg_id"] for r in rows} == {"abc11111", "def22222"}
        assert all(r["read"] is False for r in rows)
        assert all(r["seq"] == 1 for r in rows)

    def test_read_mark_then_subsequent_omits_marked(
            self, monkeypatch, tmp_path, capsys):
        import argparse
        cli = self._setup(monkeypatch, tmp_path)
        self._seed_thread(cli, "abc11111")
        self._seed_thread(cli, "def22222")
        # First call with --mark records reads for both.
        args1 = argparse.Namespace(msg_id=None, next_msg=False, mark=True,
                                   all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(args1)
        capsys.readouterr()
        # Subsequent unread listing should be empty.
        args2 = argparse.Namespace(msg_id=None, next_msg=False, mark=False,
                                   all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(args2)
        rows2 = json.loads(capsys.readouterr().out)
        assert rows2 == []
        # `--all` includes already-read messages.
        args3 = argparse.Namespace(msg_id=None, next_msg=False, mark=False,
                                   all_msgs=True, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(args3)
        rows3 = json.loads(capsys.readouterr().out)
        assert {r["msg_id"] for r in rows3} == {"abc11111", "def22222"}
        assert all(r["read"] is True for r in rows3)

    def test_read_next_prints_oldest_unread_with_header(
            self, monkeypatch, tmp_path, capsys):
        import argparse, time
        cli = self._setup(monkeypatch, tmp_path)
        # Seed two threads at distinct timestamps so the OLDEST is
        # deterministic. `_msg_ledger_append` stamps ts at write time so
        # natural insertion order matches age order.
        self._seed_thread(cli, "old11111")
        time.sleep(0.01)
        self._seed_thread(cli, "new22222")
        args = argparse.Namespace(msg_id=None, next_msg=True, mark=False,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_read(args)
        assert ei.value.code == 0
        body = json.loads(capsys.readouterr().out)
        assert body["msg_id"] == "old11111"
        assert body["seq"] == 1
        assert body["from_id"] == "aaaa1111"
        assert body["text"] == "q-text"

    def test_read_next_with_mark_marks_just_that_one(
            self, monkeypatch, tmp_path, capsys):
        import argparse, time
        cli = self._setup(monkeypatch, tmp_path)
        self._seed_thread(cli, "old11111")
        time.sleep(0.01)
        self._seed_thread(cli, "new22222")
        args = argparse.Namespace(msg_id=None, next_msg=True, mark=True,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(args)
        capsys.readouterr()
        # After --mark, only old11111 is marked read; new22222 remains unread.
        reads = [r for r in cli._msg_ledger_iter()
                 if r.get("record") == "read"]
        marked = {r["msg_id"] for r in reads}
        assert marked == {"old11111"}

    def test_read_thread_replays_ordered_by_seq(
            self, monkeypatch, tmp_path, capsys):
        import argparse
        cli = self._setup(monkeypatch, tmp_path)
        # Build a 3-turn thread: A→B (seq=1), B→A (seq=2), A→B (seq=3).
        # Insert seq=3 BEFORE seq=2 to confirm sort orders by seq, not
        # by ledger insertion order.
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "first",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 3, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "third",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 2, "kind": "message",
            "from_id": "bbbb2222", "from_name": "B",
            "to_id": "aaaa1111", "to_name": "A", "text": "second",
        })
        args = argparse.Namespace(msg_id="abc12345", next_msg=False, mark=False,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_read(args)
        assert ei.value.code == 0
        rows = json.loads(capsys.readouterr().out)
        assert [r["seq"] for r in rows] == [1, 2, 3]
        assert [r["text"] for r in rows] == ["first", "second", "third"]

    def test_read_thread_with_mark_marks_unread_in_thread(
            self, monkeypatch, tmp_path, capsys):
        import argparse
        cli = self._setup(monkeypatch, tmp_path)
        # Seed deliveries to my mailbox for two seqs; pre-mark one as read.
        for seq, text in ((1, "first"), (2, "second")):
            cli._msg_ledger_append({
                "record": "turn", "schema": "camc-msg/1",
                "msg_id": "abc12345", "seq": seq, "kind": "message",
                "from_id": "aaaa1111", "from_name": "A",
                "to_id": "bbbb2222", "to_name": "B", "text": text,
            })
            cli._msg_ledger_append({
                "record": "delivery", "schema": "camc-msg/1",
                "msg_id": "abc12345", "seq": seq,
                "mailbox_id": "agent:bbbb2222",
                "to_id": "bbbb2222", "to_name": "B",
            })
        cli._msg_ledger_append({
            "record": "read", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1,
            "mailbox_id": "agent:bbbb2222",
        })
        args = argparse.Namespace(msg_id="abc12345", next_msg=False, mark=True,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(args)
        capsys.readouterr()
        reads = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                 if r.get("record") == "read"
                 and r.get("mailbox_id") == "agent:bbbb2222"]
        seqs_marked = sorted({r["seq"] for r in reads})
        assert seqs_marked == [1, 2]

    def test_read_thread_replay_works_without_mailbox_identity(
            self, monkeypatch, tmp_path, capsys):
        # FINDING 1: thread replay must NOT require a mailbox identity.
        # Humans/scripts running outside tmux should be able to
        # `camc msg read <msg_id>` without --for. Only inbox listing,
        # --next, and --mark need a mailbox.
        import argparse
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH",
                            str(tmp_path / "messages.jsonl"))
        # No current agent identity (running outside tmux).
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "first",
        })
        args = argparse.Namespace(msg_id="abc12345", next_msg=False, mark=False,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_read(args)
        assert ei.value.code == 0  # replay succeeded — no mailbox needed
        rows = json.loads(capsys.readouterr().out)
        assert [r["text"] for r in rows] == ["first"]

    def test_read_thread_with_mark_still_requires_mailbox_identity(
            self, monkeypatch, tmp_path, capsys):
        # Counterpart to the above: --mark DOES need a mailbox so we
        # know whose read records to append. No identity → exit 1 with
        # a clear stderr.
        import argparse
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH",
                            str(tmp_path / "messages.jsonl"))
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "first",
        })
        args = argparse.Namespace(msg_id="abc12345", next_msg=False, mark=True,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_read(args)
        assert ei.value.code == 1
        assert "mailbox" in capsys.readouterr().err.lower()

    def test_resolve_mailbox_unknown_label_includes_agent_fallback(
            self, monkeypatch, tmp_path):
        # FINDING 3: when AgentStore can't resolve `--for <id>`, the
        # fallback must include `agent:<label>` (not just session/label),
        # otherwise deliveries to a stable agent_id are invisible.
        from camc_pkg import cli

        class _S:
            def get(self, key):
                return None  # unresolvable

        monkeypatch.setattr(cli, "AgentStore", lambda: _S())
        cands = cli._msg_resolve_mailbox("abcd1234")
        assert "agent:abcd1234" in cands
        assert "session:abcd1234" in cands
        assert "label:abcd1234" in cands

    def test_other_side_recipient_uses_highest_seq_not_insertion_order(
            self, monkeypatch, tmp_path):
        # FINDING 4: out-of-order ledger inserts (seq=3 written before
        # seq=2) must not confuse recipient selection. _msg_thread_turns
        # already sorts by seq, so the recipient is determined by the
        # last turn BY SEQ, not by position in the file.
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH",
                            str(tmp_path / "messages.jsonl"))
        # Insert seq=1 (A→B) and then seq=3 (A→B) and seq=2 (B→A).
        # If we used insertion order, last would be seq=2 (B→A) and the
        # recipient for the next reply (replying as A) would be B's
        # other side = A's id, which is wrong. With max-seq logic, last
        # turn is seq=3 (A→B), and a reply from B should target A.
        for seq, fid, fname, tid, tname in [
            (1, "aaaa1111", "A", "bbbb2222", "B"),
            (3, "aaaa1111", "A", "bbbb2222", "B"),
            (2, "bbbb2222", "B", "aaaa1111", "A"),
        ]:
            cli._msg_ledger_append({
                "record": "turn", "schema": "camc-msg/1",
                "msg_id": "abc12345", "seq": seq, "kind": "message",
                "from_id": fid, "from_name": fname,
                "to_id": tid, "to_name": tname, "text": "t-%d" % seq,
            })
        # Replying as B (we are TO of seq=3, since seq=3 is A→B): the
        # recipient must be A (from of seq=3), not whatever the last-
        # inserted record points to (seq=2 B→A → would say recipient=A
        # too coincidentally; pick a flip case below).
        as_b = {"sender_id": "bbbb2222", "sender_name": "B"}
        rec = cli._msg_other_side_recipient("abc12345", as_b)
        assert rec is not None
        assert rec["id"] == "aaaa1111"   # A — the from of highest-seq turn
        # Replying as A: we are FROM of seq=3, so other side = TO = B.
        as_a = {"sender_id": "aaaa1111", "sender_name": "A"}
        rec2 = cli._msg_other_side_recipient("abc12345", as_a)
        assert rec2["id"] == "bbbb2222"

    def test_read_for_label_routes_to_explicit_mailbox(
            self, monkeypatch, tmp_path, capsys):
        # `--for <label>` overrides the current-process inference.
        import argparse
        cli = self._setup(monkeypatch, tmp_path,
                          current_id="caller-not-the-recipient")
        self._seed_thread(cli, "abc12345", my_id="bbbb2222")
        # Without --for, current mailbox doesn't match agent:bbbb2222 →
        # listing is empty.
        a0 = argparse.Namespace(msg_id=None, next_msg=False, mark=False,
                                all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(a0)
        assert json.loads(capsys.readouterr().out) == []
        # With --for routing to the right agent label, _msg_resolve_mailbox
        # uses AgentStore.get(label). Mock to return that agent.
        class _S:
            def get(self, key):
                if key == "B":
                    return {"id": "bbbb2222", "task": {"name": "B"},
                            "tmux_session": "cam-B"}
                return None
        monkeypatch.setattr(cli, "AgentStore", lambda: _S())
        a1 = argparse.Namespace(msg_id=None, next_msg=False, mark=False,
                                all_msgs=False, for_label="B", json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(a1)
        rows = json.loads(capsys.readouterr().out)
        assert [r["msg_id"] for r in rows] == ["abc12345"]


class TestMsgFinalizeWaitReplay:
    """FINDING 2: pane-scraped replies via _msg_finalize_wait must be
    replayable via `camc msg read <msg_id>`. Default `camc msg send <to>`
    blocks on a pane scrape; when the answer arrives, finalize must
    append the V0 turn + delivery records (not just legacy status=replied)
    so the thread is fully reconstructable."""

    def test_finalize_appends_seq_2_turn_and_delivery_to_sender_mailbox(
            self, monkeypatch, tmp_path):
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH",
                            str(tmp_path / "messages.jsonl"))
        # Seed: send (sent record + seq=1 turn). No prior reply.
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "Q", "status": "sent", "timeout_s": 600,
            "sender_name": "A", "sender_id": "aaaa1111",
            "sender_tmux_session": "cam-A",
            "target_name": "B", "target_id": "bbbb2222",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "Q",
        })
        # Drive finalize as if the pane-scrape wait succeeded.
        with pytest.raises(SystemExit) as ei:
            cli._msg_finalize_wait("B", "abc12345", 600, "ANSWER", "replied")
        assert ei.value.code == 0
        # V0 turn(seq=2) + delivery(mailbox=agent:<sender_id>) appended.
        turns = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                 if r.get("record") == "turn"]
        assert [t["seq"] for t in turns] == [1, 2]
        t2 = turns[1]
        # Original target → from; original sender → to.
        assert t2["from_id"] == "bbbb2222"
        assert t2["from_name"] == "B"
        assert t2["to_id"] == "aaaa1111"
        assert t2["to_name"] == "A"
        assert t2["text"] == "ANSWER"
        deliveries = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                      if r.get("record") == "delivery"]
        assert any(d["seq"] == 2 and d["mailbox_id"] == "agent:aaaa1111"
                   for d in deliveries)
        # Legacy status=replied still recorded.
        assert cli._msg_find_replied("abc12345") == "ANSWER"

    def test_finalize_does_not_double_append_when_reply_turn_exists(
            self, monkeypatch, tmp_path):
        # If `camc msg reply` has already written seq=2 (or any seq>1),
        # _msg_finalize_wait must NOT append another synthetic reply turn
        # — that would duplicate the message in the thread.
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH",
                            str(tmp_path / "messages.jsonl"))
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "Q", "status": "sent", "timeout_s": 600,
            "sender_name": "A", "sender_id": "aaaa1111",
            "target_name": "B", "target_id": "bbbb2222",
        })
        for seq, fid, fname, tid, tname, text in [
            (1, "aaaa1111", "A", "bbbb2222", "B", "Q"),
            (2, "bbbb2222", "B", "aaaa1111", "A", "FROM-REPLY-CMD"),
        ]:
            cli._msg_ledger_append({
                "record": "turn", "schema": "camc-msg/1",
                "msg_id": "abc12345", "seq": seq, "kind": "message",
                "from_id": fid, "from_name": fname,
                "to_id": tid, "to_name": tname, "text": text,
            })
        with pytest.raises(SystemExit):
            cli._msg_finalize_wait("B", "abc12345", 600, "PANE-SCRAPE", "replied")
        turns = [r for r in cli._msg_ledger_iter(msg_id="abc12345")
                 if r.get("record") == "turn"]
        # No phantom seq=3 — reply was already on the thread.
        assert [t["seq"] for t in turns] == [1, 2]
        assert turns[1]["text"] == "FROM-REPLY-CMD"

    def test_read_replays_pane_scraped_reply(
            self, monkeypatch, tmp_path, capsys):
        # End-to-end shape: send → finalize_wait writes turn → read
        # replays both turns.
        import argparse
        from camc_pkg import cli
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH",
                            str(tmp_path / "messages.jsonl"))
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "Q", "status": "sent", "timeout_s": 600,
            "sender_name": "A", "sender_id": "aaaa1111",
            "target_name": "B", "target_id": "bbbb2222",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "Q",
        })
        with pytest.raises(SystemExit):
            cli._msg_finalize_wait("B", "abc12345", 600, "ANSWER", "replied")
        capsys.readouterr()  # discard finalize stdout
        # Replay (no mailbox needed per FINDING 1 fix).
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: None)
        args = argparse.Namespace(msg_id="abc12345", next_msg=False, mark=False,
                                  all_msgs=False, for_label=None, json_out=True)
        with pytest.raises(SystemExit):
            cli.cmd_msg_read(args)
        rows = json.loads(capsys.readouterr().out)
        assert [r["text"] for r in rows] == ["Q", "ANSWER"]
        assert [r["seq"] for r in rows] == [1, 2]


class TestMsgLegacyCompat:
    """Existing send/wait/show paths still work after V0 mailbox/thread
    records are added alongside legacy status records."""

    def test_first_replied_status_still_recorded(self, monkeypatch, tmp_path):
        # `wait` (and any external scripts) read `status=replied`. Even
        # though V0 mailbox is the source-of-truth, the FIRST reply must
        # still write that legacy status for backward compat.
        import argparse
        from camc_pkg import cli
        ledger = tmp_path / "messages.jsonl"
        monkeypatch.setattr(cli, "_MSG_LEDGER_PATH", str(ledger))
        cli._msg_ledger_append({
            "msg_id": "abc12345", "to": "B", "tmux_session": "cam-B",
            "text": "q", "status": "sent", "timeout_s": 600,
            "sender_name": "A", "sender_id": "aaaa1111",
        })
        cli._msg_ledger_append({
            "record": "turn", "schema": "camc-msg/1",
            "msg_id": "abc12345", "seq": 1, "kind": "message",
            "from_id": "aaaa1111", "from_name": "A",
            "to_id": "bbbb2222", "to_name": "B", "text": "q",
        })
        monkeypatch.setattr(cli, "_msg_sender_identity", lambda: {
            "sender_name": "B", "sender_id": "bbbb2222",
            "sender_tmux_session": "cam-B",
        })
        monkeypatch.setattr(cli, "_msg_resolve_session",
                            lambda lbl: (None, None, None))
        args = argparse.Namespace(msg_id="abc12345", text="ack", timeout=10)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_msg_reply(args)
        assert ei.value.code == 0
        # Legacy status is what `_msg_find_replied` checks; must be present.
        assert cli._msg_find_replied("abc12345") == "ack"


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


class TestCmdKey:
    def test_cmd_key_uses_tmux_send_key_helper(self, monkeypatch, capsys):
        from camc_pkg import cli

        class FakeStore:
            def get(self, agent_id):
                assert agent_id == "abc123"
                return {"tmux_session": "cam-abc123"}

        calls = []
        monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore())
        monkeypatch.setattr(
            cli, "tmux_send_key",
            lambda session, key: calls.append((session, key)) or True,
        )

        cli.cmd_key(SimpleNamespace(id="abc123", key="Enter"))

        assert calls == [("cam-abc123", "Enter")]
        assert "Sent key: Enter" in capsys.readouterr().out

    def test_cmd_key_exits_on_send_failure(self, monkeypatch, capsys):
        from camc_pkg import cli

        class FakeStore:
            def get(self, agent_id):
                return None

        monkeypatch.setattr(cli, "AgentStore", lambda: FakeStore())
        monkeypatch.setattr(cli, "tmux_send_key", lambda session, key: False)

        with pytest.raises(SystemExit) as ei:
            cli.cmd_key(SimpleNamespace(id="cam-missing", key="Enter"))

        assert ei.value.code == 1
        assert "Failed to send key" in capsys.readouterr().err
