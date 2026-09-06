"""Observable auto-confirm behavior through the real monitor loop."""

import os

import pytest

from camc_pkg import adapters, monitor


class Store(object):
    def __init__(self, path, tool, state="running", enabled=True, prompt=""):
        self._path = str(path)
        self.agent = {
            "id": "agent1",
            "state": state,
            "status": "running",
            "task": {
                "name": "baseline", "tool": tool, "prompt": prompt,
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
                   on_sleep=None, on_input=None, on_key=None, prompt="",
                   insert_prompt_override=None):
    config = adapters._load_config(tool)
    config.health_check_interval = 10000.0
    config.confirm_cooldown = 5.0
    config.confirm_sleep = 0.25
    boot_config = adapters._load_boot_config(tool) if boot else None
    store = Store(
        tmp_path / (tool + "-agents.json"), tool, state, enabled, prompt)
    events = Events()
    clock = [100.0]
    sleeps = [0]
    sends = []

    monkeypatch.setattr(
        monitor, "capture_tmux",
        lambda session, lines=128: screen() if callable(screen) else screen,
    )
    monkeypatch.setattr(
        monitor, "tmux_cursor_flag",
        lambda session: cursor_flag() if callable(cursor_flag) else cursor_flag,
    )
    monkeypatch.setattr(monitor, "tmux_session_exists", lambda session: True)
    monkeypatch.setattr(monitor, "tmux_is_attached", lambda session: False)
    monkeypatch.setattr(monitor, "tmux_kill_session", lambda session: None)
    def send_input(session, text, send_enter=False):
        sends.append(("input", text, send_enter))
        if on_input:
            on_input(text, send_enter)
        return True

    def send_key(session, key):
        sends.append(("key", key))
        if on_key:
            on_key(key)
        return True

    monkeypatch.setattr(monitor, "tmux_send_input", send_input)
    monkeypatch.setattr(monitor, "tmux_send_key", send_key)
    if insert_prompt_override is not None:
        monkeypatch.setattr(monitor, "insert_prompt", insert_prompt_override)
    else:
        real_insert_prompt = monitor.insert_prompt

        def insert_prompt(session, text, **kwargs):
            kwargs.update(
                send_input_fn=send_input,
                send_key_fn=send_key,
                sleep_fn=monitor.time.sleep,
            )
            return real_insert_prompt(session, text, **kwargs)

        monkeypatch.setattr(monitor, "insert_prompt", insert_prompt)
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


def test_boot_trust_is_confirmed_before_original_prompt_is_submitted_once(
        monkeypatch, tmp_path):
    from camc_pkg import detection

    phase = ["trust"]
    prompt = "original launch prompt"
    stale_trust = "Do you trust the contents of this directory?"

    def screen():
        return {
            "trust": stale_trust + "\n›",
            "ready": (stale_trust + "\nTrust accepted\n"
                      "Tip: New Build faster with Codex.\n"
                      "› Ask Codex to do anything\n"
                      "nvidia/project"),
            "typed": (stale_trust + "\nTrust accepted\n"
                      "Tip: New Build faster with Codex.\n› " + prompt),
            "submitted": stale_trust + "\nWorking on: " + prompt,
        }[phase[0]]

    def cursor_flag():
        return 0 if phase[0] == "trust" else (
            1 if phase[0] in ("ready", "typed") else 0)

    def on_input(text, send_enter):
        if text == "1":
            assert phase[0] == "trust"
            assert send_enter is True
            phase[0] = "ready"
        elif text == prompt:
            assert phase[0] == "ready"
            assert send_enter is False
            phase[0] = "typed"

    def on_key(key):
        if key == "Enter" and phase[0] == "typed":
            phase[0] = "submitted"

    monkeypatch.setattr(detection, "_READY_STABLE_SECONDS", 0.0)
    sends, events, store = _drive_monitor(
        monkeypatch, tmp_path, "codex", screen,
        cursor_flag=cursor_flag, state="initializing", boot=True,
        max_sleeps=5, on_input=on_input, on_key=on_key, prompt=prompt,
    )

    assert sends == [
        ("input", "1", True),
        ("input", prompt, False),
        ("key", "Enter"),
    ]
    assert phase == ["submitted"]
    assert store.agent["state"] == "idle"
    ready = [row for row in events if row[1] == "boot_ready"]
    assert len(ready) == 1
    assert ready[0][2]["prompt_sent"] is True


def test_late_trust_confirmation_renews_wait_for_delayed_ready_prompt(
        monkeypatch, tmp_path):
    from camc_pkg import detection

    phase = ["loading"]
    prompt = "task after slow model load"
    stale_trust = "Do you trust the contents of this directory?"

    def screen():
        return {
            "loading": "Starting Codex...",
            "trust": stale_trust + "\n› 1. Yes, continue\n  2. No, quit",
            "waiting": stale_trust + "\nTrust accepted\nLoading model...",
            "ready": stale_trust + "\nTrust accepted\n› Ask Codex to do anything",
            "typed": stale_trust + "\nTrust accepted\n› " + prompt,
            "submitted": stale_trust + "\nWorking on: " + prompt,
        }[phase[0]]

    def cursor_flag():
        return 0 if phase[0] in ("loading", "trust", "submitted") else 1

    def on_sleep(now, _store, _sends):
        # The trust screen arrives near the original 20-second deadline.
        if now >= 118.0 and phase[0] == "loading":
            phase[0] = "trust"
        # A real API-backed Codex launch can spend roughly 45 seconds after
        # trust loading its model before it exposes the real input prompt.
        if now >= 163.0 and phase[0] == "waiting":
            phase[0] = "ready"

    def on_input(text, send_enter):
        if text == "1":
            assert phase[0] == "trust"
            assert send_enter is True
            phase[0] = "waiting"
        elif text == prompt:
            assert phase[0] == "ready"
            assert send_enter is False
            phase[0] = "typed"

    def on_key(key):
        if key == "Enter" and phase[0] == "typed":
            phase[0] = "submitted"

    monkeypatch.setattr(detection, "_READY_STABLE_SECONDS", 0.0)
    sends, events, store = _drive_monitor(
        monkeypatch, tmp_path, "codex", screen,
        cursor_flag=cursor_flag, state="initializing", boot=True,
        max_sleeps=75, on_sleep=on_sleep, on_input=on_input,
        on_key=on_key, prompt=prompt,
    )

    assert sends == [
        ("input", "1", True),
        ("input", prompt, False),
        ("key", "Enter"),
    ]
    assert phase == ["submitted"]
    assert store.agent["state"] == "idle"
    ready = [row for row in events if row[1] == "boot_ready"]
    assert len(ready) == 1
    assert ready[0][2]["prompt_sent"] is True


def test_failed_prompt_insert_is_not_claimed_and_retries_before_deadline(
        monkeypatch, tmp_path, caplog):
    from camc_pkg import detection

    attempts = []

    def insert_prompt(_session, text, **_kwargs):
        attempts.append(text)
        if len(attempts) == 1:
            return {"ok": False, "phase": "ready", "reason": "ready_timeout"}
        return {"ok": True, "phase": "submit", "reason": "screen_changed"}

    monkeypatch.setattr(detection, "_READY_STABLE_SECONDS", 0.0)
    _sends, events, store = _drive_monitor(
        monkeypatch, tmp_path, "codex", "› ", cursor_flag=1,
        state="initializing", boot=True, max_sleeps=4,
        prompt="retry launch prompt", insert_prompt_override=insert_prompt,
    )

    assert attempts == ["retry launch prompt", "retry launch prompt"]
    assert store.agent["state"] == "idle"
    assert len([row for row in events if row[1] == "boot_ready"]) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert len([msg for msg in messages if "Prompt insertion failed" in msg]) == 1
    assert len([msg for msg in messages if "Boot: prompt injected" in msg]) == 1


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
