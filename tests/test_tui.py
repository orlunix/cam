#!/usr/bin/env python3
"""Unit tests for cam.tui pure helpers (SGR parsing, width, layout)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from cam import tui


def test_parse_plain_text_passthrough():
    rows = tui.parse_ansi_rows("hello world")
    assert rows == [[("hello world", -1, -1, 0)]]


def test_parse_sgr_color_and_reset():
    rows = tui.parse_ansi_rows("\x1b[31mred\x1b[0m plain")
    assert rows == [[("red", 1, -1, 0), (" plain", -1, -1, 0)]]


def test_parse_bold_attr():
    rows = tui.parse_ansi_rows("\x1b[1;34mbold blue")
    assert rows == [[("bold blue", 4, -1, tui.F_BOLD)]]


def test_parse_256_color():
    rows = tui.parse_ansi_rows("\x1b[38;5;196mx")
    assert rows == [[("x", 196, -1, 0)]]


def test_parse_truecolor_maps_to_256():
    rows = tui.parse_ansi_rows("\x1b[38;2;255;0;0mx")
    assert rows == [[("x", 196, -1, 0)]]  # pure red → cube corner


def test_parse_bg_color():
    rows = tui.parse_ansi_rows("\x1b[42mgreen bg")
    assert rows == [[("green bg", -1, 2, 0)]]


def test_parse_strips_non_sgr_escapes():
    rows = tui.parse_ansi_rows("a\x1b[2Kb\x1b]0;title\x07c")
    assert rows == [[("a", -1, -1, 0), ("b", -1, -1, 0), ("c", -1, -1, 0)]]


def test_parse_multiline_and_trailing_blank_trim():
    rows = tui.parse_ansi_rows("one\ntwo\n\n")
    assert ["".join(s for s, *_ in r) for r in rows] == ["one", "two"]


def test_parse_empty():
    assert tui.parse_ansi_rows("") == []


def test_rgb_to_256_gray_ramp():
    assert 232 <= tui._rgb_to_256(128, 128, 128) <= 255


def test_truncate_ascii():
    assert tui._truncate_disp("hello", 10) == "hello"
    assert tui._truncate_disp("hello world", 8) == "hello w…"


def test_truncate_cjk_width():
    # 3 CJK chars = 6 cells; truncating to 5 keeps 2 chars (4 cells) + …
    assert tui._truncate_disp("你好世界", 5) == "你好…"


def test_disp_width():
    assert tui._disp_width("ab") == 2
    assert tui._disp_width("你好") == 4


def test_layout_wide():
    r = tui.layout(40, 120)
    assert r["narrow"] is False
    iy, ix, ih, iw = r["info"]
    assert (iy, ix) == (0, 0) and iw == tui.SIDEBAR_DEFAULT
    wy, wx, wh, ww = r["work"]
    assert wx == tui.SIDEBAR_DEFAULT and wy == 1
    assert r["bar"][0] == 39


def test_layout_narrow_collapses_to_single_column():
    r = tui.layout(30, 50)
    assert r["narrow"] is True
    assert r["list"][3] == 50 and r["work"][3] == 50


def test_command_buttons_have_narrow_fallback():
    full = tui.command_buttons(24)
    compact = tui.command_buttons(23)
    assert [label for label, _ in full] == ["new", "settings", "nodes"]
    assert [label for label, _ in compact] == ["new", "set", "nod"]
    assert [action for _, action in full] == [action for _, action in compact]


def test_tab_geometry_keeps_selection_visible():
    for sel in range(10):
        top, slots, tab_w = tui.tab_geometry(10, sel, 40)
        assert slots == 2 and tab_w == 18
        assert top <= sel < top + slots
    assert tui.tab_geometry(0, 0, 40) == (0, 0, 0)
    assert tui.tab_geometry(3, 99, 7) == (2, 1, 7)


def _fake_curses(monkeypatch):
    class FakeCurses:
        pass
    fake = FakeCurses()
    for name, value in {
            "KEY_MOUSE": -100, "KEY_BTAB": -101, "KEY_UP": -102,
            "KEY_DOWN": -103, "KEY_PPAGE": -104, "KEY_NPAGE": -105,
            "KEY_HOME": -106, "KEY_END": -107, "KEY_ENTER": -108,
            "KEY_BACKSPACE": -109}.items():
        setattr(fake, name, value)
    monkeypatch.setattr(tui, "curses", fake)


def test_printable_keys_fill_composer_and_mouse_commands(monkeypatch):
    _fake_curses(monkeypatch)
    ui = tui.Tui()
    ui.agents = [{"id": "a1", "name": "alpha"}]
    for ch in ("q", "j", "k", "i", "a", "r", "N", "S", "m"):
        ui._key(ch)
        assert ui.input_buf == ch
        assert ui.form is None and ui.view == "agents"
        ui._key("\x1b")
        assert ui.input_buf == ""
    ui._key("\x0e")
    assert ui.form is None
    ui._hit(("button", "new"))
    assert ui.form["kind"] == "agent"
    ui.form = None
    attached = []
    monkeypatch.setattr(ui, "_attach", lambda: attached.append(True))
    ui._key("\n")  # empty composer: opens the action menu (attach first)
    assert ui.menu is not None
    ui._key("\n")  # menu Enter on attach
    assert ui.menu is None
    ui._hit(("button", "attach"))
    assert attached == [True, True]
    sent = []
    monkeypatch.setattr(ui, "_send_input", lambda: sent.append(True))
    ui.input_buf = "hello"
    ui._key("\n")
    assert sent == [True]


def test_confirm_uses_enter_or_escape_not_letter_keys(monkeypatch):
    _fake_curses(monkeypatch)
    ui = tui.Tui()
    called = []
    ui.confirm = ("Delete?", lambda: called.append(True))
    ui._key("y")
    assert ui.confirm is not None and not called
    ui._key("\x1b")
    assert ui.confirm is None and not called
    ui.confirm = ("Delete?", lambda: called.append(True))
    ui._key("\n")
    assert ui.confirm is None and called == [True]


def test_status_dot_fallbacks():
    assert tui._STATUS_DOT["running"] == "●"
    assert "exited" in tui._STATUS_DOT


def test_fld_reads_nested_task_dict():
    a = {"id": "abc123def", "task": {"name": "fix-bug", "prompt": "do it"}}
    assert tui._fld(a, "name") == "fix-bug"
    assert tui._fld(a, "prompt") == "do it"


def test_fld_fallbacks():
    assert tui._fld({"id": "abc123def"}, "name") == "abc123def"
    assert tui._fld({"session": "s1"}, "tmux_session") == "s1"
    assert tui._fld({"name": "top"}, "name") == "top"


def _node_vals(**kw):
    base = {"name": "gpu-1", "host": "10.0.0.5", "user": "hren",
            "port": "", "key_file": "", "env_setup": ""}
    base.update(kw)
    return base


def test_validate_node_form_ok():
    assert tui.validate_node_form(_node_vals(), set()) is None


def test_validate_node_form_required_fields():
    assert "name" in tui.validate_node_form(_node_vals(name=""), set())
    assert "host" in tui.validate_node_form(_node_vals(host=""), set())
    assert "user" in tui.validate_node_form(_node_vals(user=""), set())


def test_validate_node_form_dup_and_reserved():
    assert "already exists" in tui.validate_node_form(_node_vals(), {"gpu-1"})
    assert "reserved" in tui.validate_node_form(_node_vals(name="local"), set())


def test_validate_node_form_port():
    assert tui.validate_node_form(_node_vals(port="2222"), set()) is None
    assert "port" in tui.validate_node_form(_node_vals(port="abc"), set())
    assert "port" in tui.validate_node_form(_node_vals(port="70000"), set())


def test_validate_node_form_edit_skips_name_checks():
    assert tui.validate_node_form(_node_vals(name="gpu-1"),
                                  {"gpu-1"}, editing=True) is None


def test_node_record_from_form_defaults_and_options():
    rec = tui.node_record_from_form("gpu-1", _node_vals(key_file=" ~/.k/id "))
    assert rec == {"name": "gpu-1", "type": "ssh", "host": "10.0.0.5",
                   "user": "hren", "port": 22, "key_file": "~/.k/id"}



def _sync_thread(monkeypatch):
    class SyncThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(tui.threading, "Thread", SyncThread)


def test_agent_menu_actions_dispatch_camc(monkeypatch):
    _fake_curses(monkeypatch)
    ui = tui.Tui()
    ui.agents = [{"id": "a1", "name": "alpha"}]
    calls = []
    monkeypatch.setattr(ui, "_run_camc", lambda argv, **kw: calls.append(argv))
    ui._open_agent_menu()
    ops = [op for _label, op in ui.menu["items"]]
    assert ops == ["attach", "reboot", "rename", "exit", "stop", "kill", "rm"]
    ui.menu = None  # direct _agent_action calls bypass menu dismissal
    ui._agent_action("reboot")
    assert calls == [["reboot", "a1"]]
    # destructive ops require confirmation first
    ui._agent_action("kill")
    assert ui.confirm is not None and calls == [["reboot", "a1"]]
    ui._key("\n")  # confirm
    assert ui.confirm is None and calls[-1] == ["kill", "a1"]
    # rename opens a form and submits `camc update <id> --name`
    ui._agent_action("rename")
    assert ui.form["kind"] == "rename"
    ui.form["values"]["name"] = "beta"
    ui._form_key("\n")
    assert ui.form is None and calls[-1] == ["update", "a1", "--name", "beta"]
    # attach stays a direct tmux attach, not a camc subprocess
    attached = []
    monkeypatch.setattr(ui, "_attach", lambda: attached.append(True))
    ui._agent_action("attach")
    assert attached == [True]


def test_run_camc_builds_full_argv(monkeypatch):
    _fake_curses(monkeypatch)
    _sync_thread(monkeypatch)
    ui = tui.Tui()
    monkeypatch.setattr(ui, "_camc_argv", lambda: ["camc"])
    seen = {}

    class Proc:
        stdout = "ok\n"
        returncode = 0

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return Proc()

    monkeypatch.setattr(tui.subprocess, "run", fake_run)
    ui._run_camc(["stop", "a1"])
    assert seen["argv"] == ["camc", "stop", "a1"]
    assert "done" in ui.message


def test_command_console_runs_and_shows_output(monkeypatch):
    _fake_curses(monkeypatch)
    _sync_thread(monkeypatch)
    ui = tui.Tui()
    ui.agents = [{"id": "a1", "name": "alpha"}]
    ui._key(":")  # empty composer: opens the console
    assert ui.form["kind"] == "command"
    for ch in "camc msg list":
        ui._form_key(ch)
    seen = {}

    class Proc:
        stdout = "no messages\n"
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["shell"] = kw.get("shell")
        return Proc()

    monkeypatch.setattr(tui.subprocess, "run", fake_run)
    ui._form_key("\n")
    assert seen["cmd"] == "camc msg list" and seen["shell"] is True
    assert ui.form is None and ui.work_mode == "output"
    text = "\n".join("".join(s for s, *_ in row) for row in ui.rows)
    assert "no messages" in text and "[exit 0]" in text
    ui._key("\x1b")  # Esc leaves output mode
    assert ui.work_mode == "agent"


def test_console_empty_command_rejected(monkeypatch):
    _fake_curses(monkeypatch)
    ui = tui.Tui()
    ui._open_console()
    ui._form_key("\n")
    assert ui.form is not None and ui.form["error"]
    assert ui.work_mode == "agent"


def test_colon_types_into_nonempty_composer(monkeypatch):
    _fake_curses(monkeypatch)
    ui = tui.Tui()
    ui.agents = [{"id": "a1", "name": "alpha"}]
    ui.input_buf = "abc"
    ui._key(":")
    assert ui.form is None and ui.input_buf == "abc:"
