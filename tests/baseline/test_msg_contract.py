"""Public `camc msg` ledger and delivery contract."""

import json
import re

from camc_pkg import cli, storage, transport


SENDER = {
    "sender_id": "11111111", "sender_name": "sender",
    "sender_tmux_session": "cam-11111111",
}
RECEIVER = {
    "sender_id": "22222222", "sender_name": "receiver",
    "sender_tmux_session": "cam-22222222",
}


def _seed_agents(paths):
    store = storage.AgentStore(str(paths.agents))
    store.save({
        "id": "11111111", "task": {"name": "sender", "tool": "codex"},
        "tmux_session": "cam-11111111",
    })
    store.save({
        "id": "22222222", "task": {"name": "receiver", "tool": "codex"},
        "tmux_session": "cam-22222222",
    })
    return store


def _ledger(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _send(invoke_cli, monkeypatch, paths, capsys, delivery=True):
    _seed_agents(paths)
    monkeypatch.setattr(cli, "_msg_sender_identity", lambda: dict(SENDER))
    observed = []

    def submit(session, payload, **kwargs):
        rows = _ledger(paths.messages)
        observed.append((session, payload, rows))
        return delivery

    monkeypatch.setattr(cli, "tmux_submit_input", submit)
    monkeypatch.setattr(cli, "capture_tmux", lambda session: "")
    code = invoke_cli("msg", "send", "receiver", "-t", "hello", "--no-wait")
    output = capsys.readouterr()
    match = re.search(r"MSG_ID=([0-9a-f]{8})", output.out)
    msg_id = match.group(1) if match else None
    return code, msg_id, observed, output


def test_msg_commits_ledger_before_one_best_effort_submission(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    code, msg_id, observed, output = _send(
        invoke_cli, monkeypatch, isolated_cam_home, capsys)

    assert code == 0
    assert msg_id
    assert "STATUS=sent" in output.out
    assert len(observed) == 1
    session, payload, rows_at_submit = observed[0]
    assert session == "cam-22222222"
    assert "[camc msg#%s]" % msg_id in payload
    assert [row.get("record") for row in rows_at_submit] == [None, "turn", "delivery"]
    assert [row.get("status") for row in _ledger(isolated_cam_home.messages)
            if row.get("status")] == ["sent", "delivered"]


def test_msg_delivery_failure_is_nonzero_but_traceable(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    code, _msg_id, observed, output = _send(
        invoke_cli, monkeypatch, isolated_cam_home, capsys, delivery=False)

    assert code == 1
    assert len(observed) == 1
    assert "failed to deliver" in output.err
    rows = _ledger(isolated_cam_home.messages)
    assert [row.get("status") for row in rows if row.get("status")] == [
        "sent", "deliver_failed",
    ]
    assert len([row for row in rows if row.get("record") == "turn"]) == 1


def test_msg_thread_replies_keep_id_and_monotonic_sequence(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    code, msg_id, _observed, _output = _send(
        invoke_cli, monkeypatch, isolated_cam_home, capsys)
    assert code == 0
    monkeypatch.setattr(cli, "tmux_send_input", lambda *args, **kwargs: False)

    monkeypatch.setattr(cli, "_msg_sender_identity", lambda: dict(RECEIVER))
    assert invoke_cli("msg", "reply", msg_id, "-t", "reply one") == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "_msg_sender_identity", lambda: dict(SENDER))
    assert invoke_cli("msg", "reply", msg_id, "-t", "reply two") == 0

    rows = _ledger(isolated_cam_home.messages)
    turns = [row for row in rows if row.get("record") == "turn"]
    assert [(row["msg_id"], row["seq"], row["text"]) for row in turns] == [
        (msg_id, 1, "hello"),
        (msg_id, 2, "reply one"),
        (msg_id, 3, "reply two"),
    ]
    assert len([row for row in rows if row.get("status") == "replied"]) == 1


def test_msg_read_next_mark_is_durable_and_idempotent(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    code, msg_id, _observed, _output = _send(
        invoke_cli, monkeypatch, isolated_cam_home, capsys)
    assert code == 0

    assert invoke_cli(
        "msg", "read", "--next", "--mark", "--json", "--for", "receiver"
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["msg_id"] == msg_id
    assert first["text"] == "hello"
    assert invoke_cli(
        "msg", "read", "--next", "--mark", "--json", "--for", "receiver"
    ) == 1
    assert len([row for row in _ledger(isolated_cam_home.messages)
                if row.get("record") == "read"]) == 1


def test_msg_submit_fallback_never_duplicates_the_turn(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    _seed_agents(isolated_cam_home)
    monkeypatch.setattr(cli, "_msg_sender_identity", lambda: dict(SENDER))
    events = []
    monkeypatch.setattr(
        cli, "tmux_send_input",
        lambda session, text, send_enter=False:
        events.append(("text", session, text, send_enter)) or True,
    )
    monkeypatch.setattr(
        cli, "tmux_send_key",
        lambda session, key: events.append(("key", session, key)) or True,
    )
    monkeypatch.setattr(cli.time, "sleep",
                        lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(cli, "capture_tmux", lambda session: "")

    assert invoke_cli(
        "msg", "send", "receiver", "-t", "fallback", "--no-wait"
    ) == 0
    capsys.readouterr()
    assert len([event for event in events if event[0] == "text"]) == 1
    assert len([event for event in events if event[0] == "key"]) == 2
    assert len([row for row in _ledger(isolated_cam_home.messages)
                if row.get("record") == "turn"]) == 1


def test_msg_reply_recovers_a_dropped_enter_without_retyping_or_duplicate_turns(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    code, msg_id, _observed, _output = _send(
        invoke_cli, monkeypatch, isolated_cam_home, capsys)
    assert code == 0
    monkeypatch.setattr(cli, "_msg_sender_identity", lambda: dict(RECEIVER))
    monkeypatch.setattr(cli, "tmux_submit_input", transport.tmux_submit_input)

    typed = []
    submitted = []
    keys = []
    sleeps = []

    def send_key(session, key):
        keys.append((session, key))
        # Model the reported failure: the TUI drops the first Enter while
        # it is still handling the paste; the next Enter submits it.
        if len(keys) == 2:
            submitted.append(typed[-1])
        return True

    def send_input(session, text, send_enter=True):
        typed.append((session, text))
        if send_enter:
            send_key(session, "Enter")
        return True

    monkeypatch.setattr(cli, "tmux_send_input", send_input)
    monkeypatch.setattr(cli, "tmux_send_key", send_key)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    monkeypatch.setattr(cli, "capture_tmux", lambda session: "Working on reply")
    monkeypatch.setattr(cli, "_msg_submit_delay", lambda target: 0.8)

    assert invoke_cli("msg", "reply", msg_id, "-t", "reply with fallback") == 0
    output = capsys.readouterr()
    assert len(typed) == 1
    assert submitted == typed
    assert submitted[0][0] == "cam-11111111"
    assert "[camc msg#%s]" % msg_id in submitted[0][1]
    assert submitted[0][1].endswith("reply with fallback")
    assert sum(sleeps) >= 0.8
    assert "SEQ=2" in output.out
    turns = [row for row in _ledger(isolated_cam_home.messages)
             if row.get("record") == "turn"]
    assert [(row["seq"], row["text"]) for row in turns] == [
        (1, "hello"), (2, "reply with fallback"),
    ]
    pending = storage.AgentStore(str(isolated_cam_home.agents)).get(
        "11111111")["screen_fallback"]
    assert pending["kind"] == "message"
    assert pending["msg_id"] == msg_id
    assert pending["session"] == "cam-11111111"
    assert pending["attempts"] == 0


def test_msg_reply_keeps_mailbox_delivery_when_terminal_submission_fails(
        invoke_cli, isolated_cam_home, monkeypatch, capsys):
    code, msg_id, _observed, _output = _send(
        invoke_cli, monkeypatch, isolated_cam_home, capsys)
    assert code == 0
    monkeypatch.setattr(cli, "_msg_sender_identity", lambda: dict(RECEIVER))
    monkeypatch.setattr(cli, "tmux_submit_input", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli, "tmux_send_input", lambda *args, **kwargs: False)

    assert invoke_cli("msg", "reply", msg_id, "-t", "durable reply") == 0
    output = capsys.readouterr()
    assert "terminal notification failed" in output.err
    assert "REPLIED_TO=%s" % msg_id in output.out
    rows = _ledger(isolated_cam_home.messages)
    assert [(r["seq"], r["text"]) for r in rows if r.get("record") == "turn"] == [
        (1, "hello"), (2, "durable reply"),
    ]
    assert len([r for r in rows if r.get("record") == "delivery"]) == 2
    assert storage.AgentStore(str(isolated_cam_home.agents)).get(
        "11111111").get("screen_fallback") is None
