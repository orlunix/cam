"""camc's local CAM Server connection; never falls back after submission.

Only an explicitly configured/discovered local endpoint enables this path.
The Server owns remote resolution, persistence and result correlation. The
Client relay sees ordinary messages and does not wait for a CLI's result.
"""

import contextlib
import hashlib
import json
import os
import socket
import stat
import sys
import time
import uuid

from camc_pkg.server_protocol import (
    CAM_AGENT_OPERATIONS, CamProtocolError, cam_encode_frame,
    cam_message_expiry, cam_read_frame, validate_cam_envelope, validate_cam_message,
)


def cam_server_socket(env=None):
    env = os.environ if env is None else env
    if env.get("CAM_SERVER_SOCKET"):
        return os.path.expanduser(env["CAM_SERVER_SOCKET"])
    card = os.path.join(os.path.expanduser("~"), ".cam", "server",
                        "endpoint-%s.json" % socket.gethostname())
    try:
        info = os.stat(card)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return None
        with open(card, "r") as stream:
            value = json.load(stream)
        return value.get("socket") if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


class CamServerConnection:
    def __init__(self, path, timeout=30):
        self.path, self.timeout = path, timeout
        self.socket = None
        self.stream = None
        self.node_id = None

    def __enter__(self):
        try:
            info = os.stat(self.path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise CamProtocolError("local Server socket is not owned by this user")
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect(self.path)
            self.stream = self.socket.makefile("rb")
            self.send({"type": "hello", "version": 1, "role": "camc"})
            hello = cam_read_frame(self.stream)
            self.node_id = hello.get("node_id")
            if (hello.get("type") != "hello" or hello.get("version") != 1 or
                    not isinstance(self.node_id, str) or not self.node_id):
                raise CamProtocolError("incompatible local CAM Server")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        if self.stream is not None:
            self.stream.close()
        if self.socket is not None:
            self.socket.close()

    def send(self, frame):
        self.socket.sendall(cam_encode_frame(frame))

    def resolve(self, node, agent):
        self.send({"type": "resolve", "node": node, "agent": agent})
        answer = cam_read_frame(self.stream)
        if answer.get("type") != "resolved":
            raise CamProtocolError(str(answer.get("error") or "TARGET_UNRESOLVED"))
        target = {"node_id": answer.get("node_id"), "agent_id": answer.get("agent_id")}
        if (not isinstance(target["node_id"], str) or not target["node_id"] or
                (agent is not None and
                 (not isinstance(target["agent_id"], str) or not target["agent_id"]))):
            raise CamProtocolError("TARGET_UNRESOLVED")
        return target

    def publish(self, message):
        """Durable local acceptance for an asynchronous reply."""
        self.send({"type": "publish", "message": message})
        answer = cam_read_frame(self.stream)
        if answer.get("type") != "accepted" or answer.get("message_id") != message["message_id"]:
            raise CamProtocolError(str(answer.get("error") or "reply was not accepted"))

    def submit(self, message, wait_for_reply=False):
        validate_cam_message(message)
        self.send(message)
        return self.await_result(message, wait_for_reply)

    def await_result(self, message, wait_for_reply=False):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CamProtocolError("result timed out; delivery may be unknown")
            self.socket.settimeout(remaining)
            result = cam_read_frame(self.stream)
            if result.get("type") == "error":
                raise CamProtocolError(str(result.get("error") or "Server rejected request"))
            validate_cam_envelope(result, self.node_id)
            if wait_for_reply and result.get("operation") == "message.send":
                if (result.get("reply_to") != message["message_id"] or
                        result["source"].get("node_id") != message["target"]["node_id"] or
                        result["source"].get("agent_id") != message["target"].get("agent_id") or
                        result["target"].get("agent_id") != message["source"].get("agent_id")):
                    raise CamProtocolError("reply source/target mismatch")
                value = result["payload"].get("args", {}).get("text")
                if not isinstance(value, str):
                    raise CamProtocolError("invalid reply text")
                return value + "\n", "", 0
            if (result.get("type") != "message" or
                    result.get("operation") != "operation.result" or
                    result.get("message_id") == message["message_id"] or
                    result.get("reply_to") != message["message_id"]):
                raise CamProtocolError("unrelated or invalid result")
            source, target = result.get("source"), result.get("target")
            if (result.get("version") != 1 or not isinstance(source, dict) or
                    not isinstance(target, dict) or
                    source.get("node_id") != message["target"]["node_id"] or
                    source.get("agent_id") != message["target"]["agent_id"] or
                    target.get("node_id") != self.node_id or
                    target.get("receiver_id") != "operation.results"):
                raise CamProtocolError("result source/target mismatch")
            payload = result.get("payload")
            if not isinstance(payload, dict):
                raise CamProtocolError("missing result payload")
            if payload.get("status") == "pending":
                continue
            if wait_for_reply and payload.get("kind") == "delivery":
                if payload.get("exit_code") == 0:
                    continue
            out, err, code = (payload.get("stdout"), payload.get("stderr"),
                              payload.get("exit_code"))
            if (not isinstance(out, str) or not isinstance(err, str) or
                    type(code) is not int or not 0 <= code <= 255):
                raise CamProtocolError("invalid stdout/stderr/exit_code result")
            if payload.get("status") not in ("ok", "error", "unknown"):
                raise CamProtocolError("invalid result status")
            if payload["status"] in ("unknown", "error") and code == 0:
                raise CamProtocolError("failed or unknown result cannot succeed")
            return out, err, code


def _cam_local_agent(store, ref):
    """Keep local lookup precedence, but never send an ambiguous miss away."""
    def local(agent):
        host = agent.get("hostname")
        return not host or host.split(".", 1)[0] == socket.gethostname().split(".", 1)[0]
    agents = [a for a in store.list() if local(a)]
    for agent in agents:
        if ref in (agent["id"], agent.get("tmux_session"), agent.get("session")):
            return agent
    groups = [[a for a in agents if a["id"].startswith(ref)]]
    names = [(a, ((a.get("task") or {}).get("name") or a.get("name") or "").lower())
             for a in agents]
    groups.extend([[a for a, name in names if name == ref.lower()],
                   [a for a, name in names if name.startswith(ref.lower())]])
    for matches in groups:
        if len(matches) > 1:
            raise CamProtocolError("ambiguous local agent; use its full ID")
        if matches:
            return matches[0]
    agent = store.get(ref)  # Preserve legacy numeric-index lookup.
    return agent if agent and local(agent) else None


def cam_server_route(parser, argv, store=None, env=None):
    """Return an exit code if Server routing handled argv, otherwise None."""
    env = os.environ if env is None else env
    path = cam_server_socket(env)
    if "--help" in argv or "-h" in argv:
        return None
    if not path:
        if any(v == "--node" or v.startswith("--node=") for v in argv):
            sys.stderr.write("camc: --node requires a local CAM Server endpoint\n")
            return 2
        return None
    command = next((v for v in argv if not v.startswith("-")), "")
    if command not in set(CAM_AGENT_OPERATIONS.values()):
        if command in ("list", "ls") and any(v in argv for v in ("-a", "--all")):
            sys.stderr.write("camc: remote list is not supported by the CAM Server protocol\n")
            return 2
        return None
    args = parser.parse_args(argv)
    command = args.command
    if command == "msg" and getattr(args, "msg_cmd", None) != "send":
        return None  # replies/reads are handled by their message-context adapter
    field = ("to" if command == "msg" else
             "agent_id" if command == "status" else "id")
    ref = getattr(args, field, None) if command != "run" else None
    node = getattr(args, "node", None) if command == "run" else None
    if command == "run" and not node:
        return None
    if command != "run":
        if not ref:
            return None  # preserve local status without an agent argument
        if ":" in ref:
            node, ref = ref.rsplit(":", 1)
            if not node or not ref:
                sys.stderr.write("camc: expected Node:agent\n")
                return 2
        else:
            if store is None:
                from camc_pkg.storage import AgentStore
                store = AgentStore()
            try:
                local_agent = _cam_local_agent(store, ref)
                if local_agent is not None:
                    # Shared Home may contain a foreign machine's same-name
                    # record. Dispatch the resolved local ID to the existing
                    # handler, without repeating a global name lookup.
                    setattr(args, field, local_agent["id"])
                    from camc_pkg.cli import (cmd_capture, cmd_status, cmd_send, cmd_key,
                                              cmd_stop, cmd_kill, cmd_rm, cmd_migrate, cmd_msg_send)
                    handlers = {"capture": cmd_capture, "status": cmd_status,
                                "send": cmd_send, "key": cmd_key,
                                "stop": cmd_stop, "kill": cmd_kill,
                                "rm": cmd_rm, "reboot": cmd_migrate, "msg": cmd_msg_send}
                    handlers[command](args)
                    return 0
            except CamProtocolError as exc:
                sys.stderr.write("camc: %s\n" % exc)
                return 2
    message = None
    try:
        default_timeout = getattr(args, "timeout", 600) if command == "msg" and not args.no_wait else 30
        timeout = float(env.get("CAM_SERVER_TIMEOUT", default_timeout))
        if not 0 < timeout <= 86400:
            raise ValueError("CAM_SERVER_TIMEOUT must be between 0 and 86400")
        with CamServerConnection(path, timeout) as server:
            if command == "run":
                target = server.resolve(node, None)
                from camc_pkg.cli import _gen_agent_id
                target["agent_id"] = _gen_agent_id()
            else:
                target = server.resolve(node, ref)
            values = vars(args).copy()
            for key in ("command", "node", "verbose"):
                values.pop(key, None)
            if command == "send":
                from camc_pkg.cli import _send_text_from_args
                values["text"] = _send_text_from_args(args)
                values["file"], values["stdin"] = None, False
            if command == "run":
                values["path"] = os.path.abspath(values["path"])
                if not any(v == "--tool-launcher" or v.startswith("--tool-launcher=") for v in argv):
                    values["tool_launcher"] = None  # resolve default on target Node
            if command != "run":
                values[field] = target["agent_id"]
            operation = next(k for k, v in CAM_AGENT_OPERATIONS.items() if v == command)
            message = {"version": 1, "type": "message", "message_id": uuid.uuid4().hex,
                       "source": {"node_id": server.node_id}, "target": target,
                       "operation": operation, "payload": {"args": values},
                       "expires_at": cam_message_expiry(timeout)}
            if command == "msg":
                from camc_pkg.cli import _msg_sender_identity, _msg_ledger_append
                sender = _msg_sender_identity()
                if sender:
                    message["source"]["agent_id"] = sender["sender_id"]
                message["payload"]["expect_response"] = not args.no_wait
                message["thread_id"] = message["message_id"]
                cam_save_message_context(message, "outgoing")
                _msg_ledger_append({"msg_id": message["message_id"], "status": "sent",
                                    "to": args.to, "expect_reply": True,
                                    "server_message": True, "text": args.text})
                cam_record_message_turn(message)
            output, errors, code = server.submit(
                message, wait_for_reply=(command == "msg" and not args.no_wait))
            if command == "msg" and code == 0 and not args.no_wait:
                _msg_ledger_append({"msg_id": message["message_id"],
                                    "status": "replied", "reply": output.rstrip("\n")})
            sys.stdout.write(output)
            sys.stderr.write(errors)
            return code
    except (OSError, ValueError, TypeError) as exc:
        correlation = (" (message_id=%s, target=%s:%s)" %
                       (message["message_id"], message["target"]["node_id"],
                        message["target"]["agent_id"])) if message else ""
        sys.stderr.write("camc: CAM Server: %s%s; no legacy retry was attempted\n" %
                         (exc, correlation))
        return 2


def cam_message_context_directory(node_id=None):
    """Shared Home must not mix opposite ends of the same message."""
    directory = os.path.join(os.path.expanduser("~"), ".cam", "server", "messages")
    if node_id is not None:
        directory = os.path.join(directory, hashlib.sha256(node_id.encode("utf-8")).hexdigest())
    return directory


@contextlib.contextmanager
def cam_message_thread_lock(thread_id):
    from camc_pkg.server_protocol import _CAM_MESSAGE_ID
    if not isinstance(thread_id, str) or not _CAM_MESSAGE_ID.fullmatch(thread_id):
        raise CamProtocolError("invalid thread ID")
    # The legacy ledger can itself live on shared Home. Lock across Nodes so
    # each transport message maps to one turn, with a unique local sequence.
    import fcntl
    directory = os.path.join(os.path.expanduser("~"), ".cam", "server", "thread-locks")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(directory, thread_id + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def cam_save_message_context(message, direction):
    """Persist only message correlation; this is not a Client relay table."""
    from camc_pkg.server_protocol import _CAM_MESSAGE_ID
    mid = message.get("message_id", "")
    thread = message.get("thread_id", mid)
    if not all(isinstance(value, str) and _CAM_MESSAGE_ID.fullmatch(value) for value in (mid, thread)):
        raise CamProtocolError("invalid message context ID")
    local = message["target"] if direction == "incoming" else message["source"]
    directory = cam_message_context_directory(local["node_id"])
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # The stable thread alias points to its newest turn; individual transport
    # message contexts remain available for explicit reply_to correlation.
    for context_id in set((mid, thread)):
        path = os.path.join(directory, context_id + ".json")
        temporary = path + "." + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"direction": direction, "message": message}, stream)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def cam_record_message_turn(message):
    """Map an outgoing transport message to the existing local thread ledger."""
    from camc_pkg.cli import _MSG_SCHEMA, _msg_ledger_append, _msg_thread_seq, _msg_thread_turns
    thread = message.get("thread_id", message["message_id"])
    source, target = message["source"], message["target"]
    text = message["payload"].get("args", {}).get("text")
    if text is None:
        text = message["payload"].get("stdout", "").rstrip("\n")
    with cam_message_thread_lock(thread):
        if any(turn.get("message_id") == message["message_id"] for turn in _msg_thread_turns(thread)):
            return
        _msg_ledger_append({"record": "turn", "schema": _MSG_SCHEMA,
                        "msg_id": thread, "seq": _msg_thread_seq(thread) + 1,
                        "message_id": message["message_id"], "reply_to": message.get("reply_to"),
                        "kind": "message", "text": text,
                        "from_id": source.get("agent_id", ""), "from_name": source.get("name", ""),
                        "to_id": target.get("agent_id", ""), "to_name": target.get("name", ""),
                        "from_node": source["node_id"], "to_node": target["node_id"]})


def cam_server_reply(args):
    from camc_pkg.server_protocol import _CAM_MESSAGE_ID
    if not _CAM_MESSAGE_ID.fullmatch(args.msg_id):
        return None
    try:
        directory = cam_message_context_directory()
        if not any(os.path.isfile(os.path.join(directory, node, args.msg_id + ".json"))
                   for node in os.listdir(directory)):
            return None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write("camc: CAM Server message context: %s\n" % exc)
        return 2
    try:
        endpoint = cam_server_socket()
        if not endpoint:
            raise CamProtocolError("local CAM Server endpoint is required for this reply")
        with CamServerConnection(endpoint) as server:
            path = os.path.join(cam_message_context_directory(server.node_id), args.msg_id + ".json")
            info = os.stat(path)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CamProtocolError("message context must be private")
            with open(path, "r") as stream:
                context = json.load(stream)
            original = context["message"]
            incoming = context["direction"] == "incoming"
            remote = original["source"] if incoming else original["target"]
            local = original["target"] if incoming else original["source"]
            if server.node_id != local["node_id"]:
                raise CamProtocolError("reply context belongs to another Node")
            message = {"version": 1, "type": "message", "message_id": uuid.uuid4().hex,
                       "reply_to": original["message_id"], "source": dict(local),
                       "thread_id": original.get("thread_id", original["message_id"]),
                       "expires_at": cam_message_expiry(86400)}
            if remote.get("agent_id"):
                message.update(target=dict(remote), operation="message.send",
                               payload={"args": {"text": args.text, "to": remote["agent_id"],
                                                 "msg_cmd": "send", "no_wait": True}})
            else:
                message.update(target={"node_id": remote["node_id"], "receiver_id": "operation.results"},
                               operation="operation.result",
                               payload={"status": "ok", "kind": "reply", "stdout": args.text + "\n",
                                        "stderr": "", "exit_code": 0})
            server.publish(message)
            cam_save_message_context(message, "outgoing")
            cam_record_message_turn(message)
            sys.stdout.write("REPLIED_TO=%s\nMESSAGE_ID=%s\nTHREAD_ID=%s\n" %
                             (message["reply_to"], message["message_id"], message["thread_id"]))
            return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.stderr.write("camc: CAM Server reply: %s\n" % exc)
        return 2


def cam_server_wait(args):
    """Wait on the Source Server's durable result without resending a task."""
    from camc_pkg.server_protocol import _CAM_MESSAGE_ID
    if not _CAM_MESSAGE_ID.fullmatch(args.msg_id):
        return None
    directory = cam_message_context_directory()
    try:
        if not any(os.path.isfile(os.path.join(directory, node, args.msg_id + ".json"))
                   for node in os.listdir(directory)):
            return None
    except FileNotFoundError:
        return None
    try:
        endpoint = cam_server_socket()
        if not endpoint:
            raise CamProtocolError("local CAM Server is required to wait on this message")
        timeout = max(5, min(86400, int(getattr(args, "timeout", 600) or 600)))
        with CamServerConnection(endpoint, timeout) as server:
            path = os.path.join(cam_message_context_directory(server.node_id), args.msg_id + ".json")
            info = os.stat(path)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CamProtocolError("message context must be private")
            with open(path, "r") as stream:
                context = json.load(stream)
            saved = context["message"]
            incoming = context["direction"] == "incoming"
            local = saved["target"] if incoming else saved["source"]
            remote = saved["source"] if incoming else saved["target"]
            if local["node_id"] != server.node_id:
                raise CamProtocolError("wait context belongs to another Node")
            original = {"message_id": args.msg_id, "source": local, "target": remote}
            server.send({"type": "wait", "message_id": args.msg_id, "timeout": timeout})
            output, errors, code = server.await_result(original, wait_for_reply=True)
            if code == 0:
                from camc_pkg.cli import _msg_ledger_append
                _msg_ledger_append({"msg_id": args.msg_id, "status": "replied", "reply": output.rstrip("\n")})
            sys.stdout.write(output)
            sys.stderr.write(errors)
            return code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.stderr.write("camc: CAM Server wait: %s; original was not resubmitted\n" % exc)
        return 2
