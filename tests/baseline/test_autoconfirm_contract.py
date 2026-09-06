"""Observable auto-confirm behavior through the real monitor loop."""

import os

import pytest

from camc_pkg import adapters, monitor


class Store(object):
    def __init__(self, path, tool, state="running", enabled=True):
        self._path = str(path)
        self.agent = {
            "id": "agent1",
            "state": state,
            "status": "running",
            "task": {
                "name": "baseline", "tool": tool, "prompt": "",
                "auto_confirm": enabled, "auto_exit": False,
            },
        }
        path.write_text("{}")
        os.utime(str(path), (1, 1))

    def get(self, agent_id):
        assert agent_id == "agent1"
        return self.agent

    def update(self, agent_id, **fields):
        assert agent_id == "agent1"
        self.agent.update(fields)
        return self.agent


class Events(object):
    def __init__(self):
        self.rows = []

    def append(self, agent_id, event_type, detail=None):
        self.rows.append((agent_id, event_type, detail))


def _drive_monitor(monkeypatch, tmp_path, tool, screen, cursor_flag=0,
                   state="running", enabled=True, boot=False, max_sleeps=4,
                   on_sleep=None):
    config = adapters._load_config(tool)
    config.health_check_interval = 10000.0
    config.confirm_cooldown = 5.0
    config.confirm_sleep = 0.25
    boot_config = adapters._load_boot_config(tool) if boot else None
    store = Store(tmp_path / (tool + "-agents.json"), tool, state, enabled)
    events = Events()
    clock = [100.0]
    sleeps = [0]
    sends = []

    monkeypatch.setattr(monitor, "capture_tmux", lambda session, lines=128: screen)
    monkeypatch.setattr(monitor, "tmux_cursor_flag", lambda session: cursor_flag)
    monkeypatch.setattr(monitor, "tmux_session_exists", lambda session: True)
    monkeypatch.setattr(monitor, "tmux_is_attached", lambda session: False)
    monkeypatch.setattr(monitor, "tmux_kill_session", lambda session: None)
    monkeypatch.setattr(
        monitor, "tmux_send_input",
        lambda session, text, send_enter=False:
        sends.append(("input", text, send_enter)) or True,
    )
    monkeypatch.setattr(
        monitor, "tmux_send_key",
        lambda session, key: sends.append(("key", key)) or True,
    )
    monkeypatch.setattr(monitor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(monitor.time, "time", lambda: clock[0])

    def sleep(seconds):
        sleeps[0] += 1
        clock[0] += float(seconds)
        if on_sleep:
            on_sleep(clock[0], store, sends)
        if sleeps[0] >= max_sleeps:
            raise StopIteration("deterministic monitor stop")

    monkeypatch.setattr(monitor.time, "sleep", sleep)
    try:
        monitor.run_monitor_loop(
            "cam-agent1", "agent1", config, store,
            events=events, boot_config=boot_config,
        )
    except StopIteration:
        pass
    return sends, events.rows, store


@pytest.mark.parametrize(
    "tool, screen, expected",
    [
        ("claude", "Permission required\n❯ 1. Yes\n  2. No", ("input", "1", False)),
        ("codex", "1. Yes, proceed\n2. Yes, always\n3. No, cancel", ("input", "1", False)),
        ("cursor", "→ Run this command? (y)", ("input", "y", False)),
    ],
)
def test_supported_runtime_dialogs_emit_exactly_one_response(
        monkeypatch, tmp_path, tool, screen, expected):
    sends, events, _store = _drive_monitor(
        monkeypatch, tmp_path, tool, screen, max_sleeps=4)

    assert sends == [expected]
    confirms = [row for row in events if row[1] == "auto_confirm"]
    assert len(confirms) == 1


@pytest.mark.parametrize(
    "tool, screen, expected",
    [
        ("codex", "Do you trust the contents of this directory?",
         ("input", "1", True)),
        ("cursor", "▶ [a] Trust this workspace",
         ("key", "Enter")),
    ],
)
def test_boot_trust_prompt_is_confirmed_only_while_initializing(
        monkeypatch, tmp_path, tool, screen, expected):
    sends, _events, _store = _drive_monitor(
        monkeypatch, tmp_path, tool, screen,
        state="initializing", boot=True, max_sleeps=2)
    assert sends == [expected]

    sends, _events, _store = _drive_monitor(
        monkeypatch, tmp_path, tool, screen,
        state="running", boot=True, max_sleeps=2)
    assert sends == []


@pytest.mark.parametrize(
    "screen, cursor_flag",
    [
        ("❯ 1. Yes\n  2. No", 1),
        ("❯ 1. Yes\n  2. No\n" + "\n".join("ordinary line %d" % i for i in range(130)), 0),
        ("The documentation says: ❯ 1. Yes\nThe normal input is below\n❯", 1),
    ],
)
def test_cursor_recent_tail_and_prose_guards_never_confirm(
        monkeypatch, tmp_path, screen, cursor_flag):
    sends, events, _store = _drive_monitor(
        monkeypatch, tmp_path, "claude", screen,
        cursor_flag=cursor_flag, max_sleeps=3)

    assert sends == []
    assert not [row for row in events if row[1] == "auto_confirm"]


def test_repeated_unchanged_dialog_is_debounced_during_cooldown(
        monkeypatch, tmp_path):
    sends, events, _store = _drive_monitor(
        monkeypatch, tmp_path, "claude", "❯ 1. Allow once\n  2. No",
        max_sleeps=8)

    assert sends == [("input", "1", False)]
    assert len([row for row in events if row[1] == "auto_confirm"]) == 1


def test_monitor_reloads_updated_auto_confirm_policy(
        monkeypatch, tmp_path):
    touched = [False]

    def enable_after_refresh_window(now, store, sends):
        if now >= 110.0 and not touched[0]:
            store.agent["task"]["auto_confirm"] = True
            os.utime(store._path, (2, 2))
            touched[0] = True

    sends, events, _store = _drive_monitor(
        monkeypatch, tmp_path, "claude", "❯ 1. Yes\n  2. No",
        enabled=False, max_sleeps=14, on_sleep=enable_after_refresh_window)

    assert touched == [True]
    assert sends == [("input", "1", False)]
    assert len([row for row in events if row[1] == "auto_confirm"]) == 1
