"""Offline lifecycle smoke for the generated single-file ``dist/camc``."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAMC = ROOT / "dist" / "camc"


FAKE_TMUX = r'''#!{python}
import json
import os
import sys

args = sys.argv[1:]
log_path = os.environ["FAKE_TMUX_LOG"]
state_dir = os.environ["FAKE_TMUX_STATE"]

def emit(event):
    with open(log_path, "a") as stream:
        stream.write(json.dumps(event) + "\n")

if args == ["-V"]:
    print("tmux 3.4")
    raise SystemExit(0)

socket_path = ""
if "-S" in args:
    index = args.index("-S")
    socket_path = args[index + 1]

command = next((name for name in (
    "new-session", "send-keys", "capture-pane", "display-message",
    "has-session", "kill-session") if name in args), "")
session = ""
if "-s" in args:
    session = args[args.index("-s") + 1]
elif "-t" in args:
    session = args[args.index("-t") + 1].split(":", 1)[0]
state_path = os.path.join(state_dir, session + ".json") if session else ""

def load():
    try:
        with open(state_path) as stream:
            return json.load(stream)
    except Exception:
        return {{"phase": "trust", "typed": ""}}

def save(state):
    with open(state_path, "w") as stream:
        json.dump(state, stream)

if command == "new-session":
    os.makedirs(state_dir, exist_ok=True)
    if socket_path:
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        open(socket_path, "a").close()
    save({{"phase": "trust", "typed": ""}})
    emit({{"command": command, "session": session, "args": args}})
elif command == "send-keys":
    state = load()
    literal = ""
    if "-l" in args and "--" in args:
        literal = args[args.index("--") + 1]
        state["typed"] = literal
    key = args[-1] if "-l" not in args else ""
    if key == "Enter":
        if state.get("typed") == "1":
            state["phase"] = "ready"
        elif state.get("typed"):
            state["enters"] = state.get("enters", 0) + 1
            if not (os.environ.get("FAKE_DROP_FIRST_ENTER")
                    and state["enters"] == 1):
                state["phase"] = "submitted"
    save(state)
    emit({{"command": command, "session": session,
          "literal": literal, "key": key, "phase": state.get("phase")}})
elif command == "capture-pane":
    state = load()
    if state.get("phase") == "trust":
        print("Do you trust the contents of this directory?")
    elif state.get("phase") == "ready":
        if state.get("typed") and state.get("typed") != "1":
            print("› " + state.get("typed"))
        else:
            # Ready marker without a cursor-line shape avoids the production
            # ten-second boot-stability delay in this offline smoke.
            print("›ready")
    else:
        print("Working on: " + state.get("typed", ""))
elif command == "display-message":
    state = load()
    template = args[-1] if args else ""
    if "cursor_flag" in template:
        print("1" if state.get("phase") == "ready" else "0")
    elif "session_attached" in template:
        print("0")
    else:
        print(os.environ.get("FAKE_TMUX_CURRENT_SESSION", session))
elif command == "has-session":
    raise SystemExit(0 if state_path and os.path.exists(state_path) else 1)
elif command == "kill-session":
    if state_path and os.path.exists(state_path):
        os.unlink(state_path)
    emit({{"command": command, "session": session}})
else:
    emit({{"command": command or "other", "args": args}})
'''


def _run(env, *args):
    return subprocess.run(
        [str(CAMC)] + list(args), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=15,
    )


def test_built_artifact_offline_run_confirm_msg_read_rm(tmp_path):
    assert CAMC.is_file(), "build dist/camc before running artifact smoke"
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    state_dir = tmp_path / "tmux-state"
    for path in (home, fake_bin, state_dir):
        path.mkdir(exist_ok=True)
    log_path = tmp_path / "tmux.jsonl"

    tmux = fake_bin / "tmux"
    tmux.write_text(FAKE_TMUX.format(python=sys.executable))
    tmux.chmod(0o755)
    codex = fake_bin / "codex"
    codex.write_text("#!/bin/sh\necho 'codex 1.0'\n")
    codex.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "SHELL": "/bin/sh",
        "PATH": str(fake_bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        "FAKE_TMUX_LOG": str(log_path),
        "FAKE_TMUX_STATE": str(state_dir),
        "CAMC_PRELUDE_DISABLE": "all",
    })
    for name in ("TMUX", "TMUX_PANE", "CLAUDECODE", "OPENAI_API_KEY"):
        env.pop(name, None)

    run = _run(
        env, "run", "artifact prompt", "--name", "artifact-agent",
        "--path", str(tmp_path / "work"), "--tool-dir", str(fake_bin),
        "--use-env-tool", "--no-default-api",
        "--tool-launcher", "none",
    )
    assert run.returncode == 0, "stdout=%s\nstderr=%s" % (run.stdout, run.stderr)

    deadline = time.time() + 8.0
    rows = []
    while time.time() < deadline:
        if log_path.exists():
            rows = [json.loads(line) for line in log_path.read_text().splitlines()]
        literals = [row.get("literal") for row in rows if row.get("literal")]
        if "1" in literals:
            break
        time.sleep(0.05)
    assert "1" in literals, rows
    # The initial prompt travels in the startup argv; the monitor must
    # not paste it again after the trust confirmation.
    assert literals.count("artifact prompt") == 0
    assert json.dumps(rows).count("artifact prompt") == 1

    message = _run(env, "msg", "send", "artifact-agent", "-t",
                   "artifact message", "--no-wait")
    assert message.returncode == 0, message.stderr
    assert "STATUS=sent" in message.stdout
    inbox = _run(env, "msg", "read", "--for", "artifact-agent", "--json")
    assert inbox.returncode == 0, inbox.stderr
    assert [row["preview"] for row in json.loads(inbox.stdout)] == ["artifact message"]

    remove = _run(env, "rm", "artifact-agent")
    assert remove.returncode == 0, remove.stderr
    assert json.loads((home / ".cam" / "agents.json").read_text()) == []


def test_built_artifact_reply_submits_after_first_enter_is_dropped(tmp_path):
    home = tmp_path / "home"
    cam_home = home / ".cam"
    fake_bin = tmp_path / "bin"
    state_dir = tmp_path / "tmux-state"
    sockets = tmp_path / "sockets"
    for path in (cam_home, fake_bin, state_dir, sockets):
        path.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "tmux.jsonl"
    tmux = fake_bin / "tmux"
    tmux.write_text(FAKE_TMUX.format(python=sys.executable))
    tmux.chmod(0o755)
    agents = []
    for agent_id, name in (("11111111", "sender"), ("22222222", "replier")):
        session = "cam-" + agent_id
        sock = sockets / (session + ".sock")
        sock.touch()
        agents.append({
            "id": agent_id, "task": {"name": name, "tool": "codex"},
            "hostname": os.uname().nodename, "tmux_session": session,
            "tmux_socket": str(sock), "tmux_bin": str(tmux),
        })
        (state_dir / (session + ".json")).write_text(json.dumps({
            "phase": "ready", "typed": "",
        }))
    (cam_home / "agents.json").write_text(json.dumps(agents))
    msg_id = "abcd1234"
    initial = [
        {"msg_id": msg_id, "status": "sent", "sender_id": "11111111",
         "sender_name": "sender", "sender_tmux_session": "cam-11111111",
         "target_id": "22222222", "target_name": "replier",
         "tmux_session": "cam-22222222", "to": "replier", "text": "hello"},
        {"record": "turn", "msg_id": msg_id, "seq": 1,
         "from_id": "11111111", "from_name": "sender",
         "to_id": "22222222", "to_name": "replier", "text": "hello"},
    ]
    ledger = cam_home / "messages.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in initial))
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "SHELL": "/bin/sh",
        "PATH": str(fake_bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        "TMUX": str(sockets / "cam-22222222.sock") + ",1,0", "TMUX_PANE": "%0",
        "FAKE_TMUX_CURRENT_SESSION": "cam-22222222",
        "FAKE_TMUX_LOG": str(log_path), "FAKE_TMUX_STATE": str(state_dir),
        "FAKE_DROP_FIRST_ENTER": "1", "CAMC_PRELUDE_DISABLE": "all",
    })
    reply_text = "长消息提交验证。" * 100
    result = _run(env, "msg", "reply", msg_id, "-t", reply_text)
    assert result.returncode == 0, result.stderr
    assert "SEQ=2" in result.stdout
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    literals = [r for r in rows if r.get("literal")]
    assert len(literals) == 1
    assert literals[0]["literal"].endswith(reply_text)
    enters = [r for r in rows if r.get("key") == "Enter"]
    assert [r["phase"] for r in enters] == ["ready", "submitted"]
    turns = [json.loads(line) for line in ledger.read_text().splitlines()
             if json.loads(line).get("record") == "turn"]
    assert [(r["seq"], r["text"]) for r in turns] == [
        (1, "hello"), (2, reply_text),
    ]
    target = json.loads((cam_home / "agents.json").read_text())[0]
    assert target["screen_fallback"]["msg_id"] == msg_id


def test_built_artifact_includes_bridge_for_normal_and_drain_commands(tmp_path):
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path), "CAMC_PRELUDE_DISABLE": "all"})
    help_result = _run(env, "--help")
    assert help_result.returncode == 0, help_result.stderr
    drained = _run(env, "--hub-drain")
    assert drained.returncode == 0, drained.stderr
    assert json.loads(drained.stdout) == []
