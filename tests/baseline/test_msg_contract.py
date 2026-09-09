"""Public `camc msg` ledger and delivery contract."""

import json
import re

from camc_pkg import cli, storage


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
