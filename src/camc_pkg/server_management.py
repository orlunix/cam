"""Server-owned, non-CLI read/terminal discovery and idle-gated delivery API."""
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import socket
import sqlite3
import struct
import subprocess
import termios
import time


def management_operation(receiver, message):
    """Bind a Client operation ID to its first envelope, including its expiry.

    UI reconnects must not regenerate expiry and conflict with the SDK's durable
    digest, nor rerun a side effect after its outcome becomes unknown.
    """
    from camc_pkg.server_protocol import cam_operation_result, validate_cam_message
    validate_cam_message(message, receiver.node_id)
    source, identity = message["source"]["node_id"], message["message_id"]
    semantic = {key: message[key] for key in ("source", "target", "operation", "payload")}
    digest = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
    with receiver._connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS management_requests "
                           "(source TEXT,id TEXT,digest TEXT,message TEXT,PRIMARY KEY(source,id))")
        connection.execute("INSERT OR IGNORE INTO management_requests VALUES (?,?,?,?)",
                           (source, identity, digest, json.dumps(message)))
        saved_digest, raw = connection.execute(
            "SELECT digest,message FROM management_requests WHERE source=? AND id=?",
            (source, identity)).fetchone()
        if saved_digest != digest:
            raise ValueError("MESSAGE_CONFLICT")
        prior = connection.execute("SELECT result FROM operations WHERE source=? AND id=?",
                                   (source, identity)).fetchone()
        if prior:
            return json.loads(prior[0]) if prior[0] is not None else cam_operation_result(
                message, stderr="operation outcome unknown; not replayed\n", exit_code=2, status="unknown")
    return receiver.execute(json.loads(raw))


def _local_agent(agent):
    host = agent.get("hostname") or ""
    return not host or host.split(".")[0] == socket.gethostname().split(".")[0]


def _agent_metadata(agent, node):
    task = agent.get("task")
    task = task if isinstance(task, dict) else agent
    return dict(node_id=node, id=agent["id"],
                name=task.get("name") or agent.get("name") or agent["id"],
                tags=[tag for tag in task.get("tags", []) if isinstance(tag, str)]
                if isinstance(task.get("tags"), list) else [],
                tool=task.get("tool") or agent.get("tool", ""),
                path=agent.get("context_path") or agent.get("path", ""),
                status=agent.get("status"), state=agent.get("state"),
                phase=agent.get("phase"), backend="legacy",
                exit_reason=agent.get("exit_reason"))


def _update_attributes(store, args, node):
    # Metadata-only, authorized by the Server's management grant. Mutate the
    # current record under the store lock; never save a stale runtime snapshot.
    if set(args) - {"node_id", "agent_id", "name", "tags"} or not ({"name", "tags"} & set(args)):
        raise ValueError("only name and tags may be updated")
    if args.get("node_id", node) != node:
        raise ValueError("Node mismatch")
    agent_id = args.get("agent_id")
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent_id):
        raise ValueError("specific agent ID required")
    fields = {}
    if "name" in args:
        name = args["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(
                ord(c) < 32 or 127 <= ord(c) <= 159 for c in name):
            raise ValueError("name must contain 1-128 printable characters")
        fields["name"] = name.strip()
    if "tags" in args:
        tags = args["tags"]
        if not isinstance(tags, list) or len(tags) > 32 or any(
                not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tag)
                for tag in tags):
            raise ValueError("use up to 32 tags: letters, digits, underscore or dash (1-64 characters)")
        fields["tags"] = list(dict.fromkeys(tags))
    updated = []
    def change(records):
        for record in records:
            if record.get("id") == agent_id and _local_agent(record):
                target = record.get("task")
                target = target if isinstance(target, dict) else record
                target.update(fields)
                updated.append(_agent_metadata(record, node))
                return records
        raise ValueError("AGENT_NOT_FOUND on this Node")
    store._modify(change)
    if not updated:
        raise ValueError("agent metadata could not be saved")
    return {"agent": updated[0]}


def _legacy_paste_mode(argv, session, cols, rows):
    """Read tmux's repaint on a bounded, read-only client; never send input.

    tmux 2.7 lacks bracketed_paste_flag. Match the window's current geometry
    so this temporary observer does not resize the tool. Callers only use it
    for the current active pane/window; otherwise its mode would be ambiguous.
    """
    if not (2 <= cols <= 500 and 2 <= rows <= 201):
        return None
    master = slave = child = None
    try:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        child = subprocess.Popen(argv + ["attach-session", "-r", "-t", session],
                                 stdin=slave, stdout=slave, stderr=slave,
                                 env=env, start_new_session=True)
        os.close(slave)
        slave = None
        data = bytearray()
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline and child.poll() is None:
            if select.select([master], [], [], 0.025)[0]:
                part = os.read(master, 65536)
                if not part:
                    break
                data.extend(part)
                if len(data) > 1024 * 1024:
                    return None
        mode = None
        for params, operation in re.findall(rb"\x1b\[\?([0-9;]+)([hl])", data):
            if b"2004" in params.split(b";"):
                mode = operation == b"h"
        return mode
    except (OSError, ValueError, struct.error):
        return None
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)


def management_request(action, args, node, state_path, receiver):
    from camc_pkg.storage import AgentStore
    store = AgentStore()
    if action == "agents.snapshot":
        agents = []
        for agent in store.list():
            if not _local_agent(agent):
                continue
            agents.append(_agent_metadata(agent, node))
        return {"agents": agents}
    if action == "agent.update":
        return _update_attributes(store, args, node)
    if action == "agent.operation":
        return management_operation(receiver, args["message"])
    agent_id = args.get("agent_id", "")
    agent = store.get(agent_id)
    if not agent or agent.get("id") != agent_id or not _local_agent(agent):
        raise ValueError("AGENT_NOT_FOUND on this Node")
    if action == "terminal.resolve":
        from camc_pkg.transport import _find_tmux_socket, _tmux_bin_for_session
        session = agent.get("tmux_session") or agent.get("session")
        if not session:
            raise ValueError("agent has no terminal")
        binary = agent.get("tmux_bin") or _tmux_bin_for_session(session)
        sock = agent.get("tmux_socket") or _find_tmux_socket(session)
        argv = [binary, "-u"] + (["-S", sock] if sock else [])
        output = subprocess.check_output(
            argv + ["display-message", "-p", "-t", session + ":0.0",
                    "#{pane_id} #{window_id} #{pane_width} #{pane_height} #{status}"],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip().split()
        if len(output) != 5 or not re.fullmatch(r"%[0-9]+", output[0]) \
                or not re.fullmatch(r"@[0-9]+", output[1]):
            raise ValueError("invalid terminal identity")
        result = dict(argv=argv, session=session, pane=output[0], window=output[1],
                      cols=int(output[2]), rows=int(output[3]), status_lines=0 if output[4] == "off" else 1)
        if args.get("probe_modes"):
            modes = subprocess.check_output(
                argv + ["display-message", "-p", "-t", session + ":0.0",
                        "#{window_width};#{window_height};#{pane_active};#{window_active};#{bracketed_paste_flag}"],
                stderr=subprocess.DEVNULL, timeout=5).decode().strip().split(";")
            result["bracketed_paste"] = None
            if len(modes) == 5:
                if modes[4] in ("0", "1"):
                    result["bracketed_paste"] = modes[4] == "1"
                elif modes[2:4] == ["1", "1"]:
                    result["bracketed_paste"] = _legacy_paste_mode(
                        argv, session, int(modes[0]), int(modes[1]) + result["status_lines"])
        return result
    if action == "loop.deliver":
        # Keep this gate at the execution Node, never in a stale Client cache.
        # Legacy inputs outside Server control still have their legacy races.
        lock_path = state_path + ".loop.lock"
        with open(lock_path, "a") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            loop_id, owner = args["loop_id"], args["owner"]
            with sqlite3.connect(state_path) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS loop_owners "
                                   "(id TEXT PRIMARY KEY, agent TEXT, owner TEXT)")
                connection.execute("INSERT OR IGNORE INTO loop_owners VALUES (?,?,?)",
                                   (loop_id, agent_id, owner))
                binding = connection.execute("SELECT agent,owner FROM loop_owners WHERE id=?",
                                             (loop_id,)).fetchone()
                if binding != (agent_id, owner):
                    raise ValueError("loop has another scheduling owner")
                connection.execute("CREATE TABLE IF NOT EXISTS loop_activity "
                                   "(agent TEXT PRIMARY KEY,sent_at REAL)")
                last = connection.execute("SELECT sent_at FROM loop_activity WHERE agent=?",
                                          (agent_id,)).fetchone()
                if last and time.time() - last[0] < 60:
                    return {"deferred": True, "reason": "prior continuation is still in its activity window"}
            current = store.get(agent_id)
            if current.get("status") != "alive" or current.get("state") != "idle" \
                    or current.get("phase") != "started":
                return {"deferred": True, "reason": "target is not alive/idle/started"}
            result = management_operation(receiver, args["message"])
            if result.get("payload", {}).get("exit_code") == 0:
                with sqlite3.connect(state_path) as connection:
                    connection.execute("INSERT OR REPLACE INTO loop_activity VALUES (?,?)",
                                       (agent_id, time.time()))
            return {"result": result}
    raise ValueError("UNSUPPORTED_MANAGEMENT_ACTION")
