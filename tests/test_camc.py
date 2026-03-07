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
        assert should_auto_confirm("Enter to confirm", c) == ("", True)
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
