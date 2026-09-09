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
        print(session)
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
    )
    assert run.returncode == 0, "stdout=%s\nstderr=%s" % (run.stdout, run.stderr)

    deadline = time.time() + 8.0
    rows = []
    while time.time() < deadline:
        if log_path.exists():
            rows = [json.loads(line) for line in log_path.read_text().splitlines()]
        literals = [row.get("literal") for row in rows if row.get("literal")]
        if "1" in literals and "artifact prompt" in literals:
            break
        time.sleep(0.05)
    assert "1" in literals, rows
    assert literals.count("artifact prompt") == 1

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
