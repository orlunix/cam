"""Native CAM Server adapter to existing camc backend functions.

No argv parsing, shell commands or nested camc execution. Each operation runs
in an isolated worker so legacy print/SystemExit behaviour cannot corrupt a
concurrent Server's stdout. The Server must authenticate/authorize before
calling execute(); its persistent dedup ledger prevents blind replay.
"""

import contextlib
import hashlib
import io
import json
import logging
import multiprocessing
import os
import signal
import sqlite3
from types import SimpleNamespace

from camc_pkg.server_protocol import (
    CamProtocolError, cam_operation_result,
    validate_cam_envelope, validate_cam_message,
)


_CAM_COMMON_OPTIONS = {"json"}
_CAM_OPERATION_OPTIONS = {
    "agent.capture": {"id", "lines", "format", "no_fast_path"},
    "agent.status": {"agent_id", "hash"},
    "agent.send": {"id", "text", "file", "stdin", "no_enter", "no_fast_path"},
    "agent.key": {"id", "key"},
    "agent.stop": {"id"}, "agent.kill": {"id"},
    "agent.remove": {"id", "archive", "kill"},
    "agent.reboot": {"id", "to", "tool_launcher", "tool_env", "tool_dir", "tool_args", "use_env_tool"},
    "agent.run": {"prompt", "tool", "path", "name", "auto_exit", "auto_exit_enable", "tag",
                  "resume_session", "use_env_tool", "tool_dir", "tool_env", "tool_args",
                  "tool_launcher", "isolate_tool_home", "system_prompt", "system_file",
                  "api", "no_default_api", "api_token"},
    "message.send": {"to", "msg_cmd", "text", "timeout", "no_wait", "expect_reply"},
}


def cam_native_operation(message):
    """Execute a previously authorized addressed operation in a worker."""
    from camc_pkg import cli
    from camc_pkg.storage import AgentStore
    operation = message["operation"]
    # This module is an imported backend, not the process's CLI entry point.
    # Runtime launch/monitor workers must use the camc module, never the
    # embedding server's argv[0] (or a multiprocessing bootstrap script).
    cli._CAMC_SCRIPT = None
    agent_id = message["target"]["agent_id"]
    values = dict(message["payload"].get("args") or {})
    allowed = _CAM_OPERATION_OPTIONS[operation] | _CAM_COMMON_OPTIONS
    if set(values) - allowed:
        raise CamProtocolError("unsupported operation options")
    store = AgentStore()
    agent = store.get(agent_id)
    if operation == "agent.run":
        if agent is not None:
            raise CamProtocolError("agent ID already exists")
    elif not agent or agent.get("id") != agent_id:
        raise CamProtocolError("AGENT_NOT_FOUND")
    elif not cli._is_same_host(agent.get("hostname"), cli._sock.gethostname()):
        raise CamProtocolError("agent is owned by another machine")
    if operation == "agent.send" and (values.get("file") or values.get("stdin")):
        raise CamProtocolError("send file/stdin must be resolved on the sender")
    # The wire target is authoritative; payload cannot redirect execution.
    for field in ("id", "agent_id", "to"):
        if field in values and values[field] not in (None, agent_id):
            raise CamProtocolError("payload target mismatch")
    defaults = {"json": False, "no_enter": False, "archive": False,
                "file": None, "stdin": False, "text": None, "lines": 0,
                "format": "plain", "tool_launcher": None, "tool_env": [],
                "tool_args": None, "tool_dir": None, "use_env_tool": None}
    defaults.update(values)
    defaults.update(id=agent_id, agent_id=agent_id)
    args = SimpleNamespace(**defaults)
    if operation == "agent.run":
        cli.cmd_run(args, agent_id=agent_id)
    elif operation == "message.send":
        # Native message delivery must not hold a worker waiting on a pane.
        from camc_pkg.server_client import cam_save_message_context, cam_message_thread_lock
        session = cli._sf(agent, "tmux_session")
        if not session:
            raise CamProtocolError("agent has no tmux session")
        sender = {"sender_id": message["source"].get("agent_id", ""),
                  "sender_name": message["source"].get("name", "")}
        with cam_message_thread_lock(message.get("thread_id", message["message_id"])):
            cam_save_message_context(message, "incoming")
            mid, delivered = cli._msg_inject(
                session, agent_id, values.get("text", ""), values.get("timeout", 600),
                expect_reply=True, submit_delay=cli._msg_submit_delay(agent),
                message_id=message["message_id"], sender=sender,
                thread_id=message.get("thread_id", message["message_id"]),
                reply_to=message.get("reply_to"))
        if not delivered:
            raise CamProtocolError("terminal notification failed; mailbox record may exist")
        if message.get("reply_to"):
            cli._msg_ledger_append({"msg_id": message.get("thread_id", message["reply_to"]), "status": "replied",
                                    "reply": values.get("text", "")})
        print("MSG_ID=%s\nSTATUS=sent" % mid)
    else:
        handlers = {"agent.capture": cli.cmd_capture, "agent.status": cli.cmd_status,
                    "agent.send": cli.cmd_send, "agent.key": cli.cmd_key,
                    "agent.stop": cli.cmd_stop, "agent.kill": cli.cmd_kill,
                    "agent.remove": cli.cmd_rm, "agent.reboot": cli.cmd_migrate}
        handlers[operation](args)


def _cam_operation_worker(channel, message, handler):
    output, errors, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        for logger_name in ("", "camc"):
            for log_handler in logging.getLogger(logger_name).handlers:
                if isinstance(log_handler, logging.StreamHandler):
                    log_handler.stream = errors
        try:
            handler(message)
        except SystemExit as exc:
            code = (exc.code % 256) if type(exc.code) is int else (0 if exc.code is None else 1)
            if exc.code is not None and type(exc.code) is not int:
                errors.write(str(exc.code) + "\n")
        except Exception as exc:
            errors.write("%s\n" % exc)
            code = 1
    try:
        result = cam_operation_result(message, output.getvalue(), errors.getvalue(), code)
        if message["operation"] == "message.send":
            result["payload"]["kind"] = "delivery"
        from camc_pkg.server_protocol import cam_encode_frame
        cam_encode_frame(result)
        channel.send(result)
    except Exception:
        channel.send(cam_operation_result(message, stderr="operation output exceeds frame limit\n", exit_code=1))
    finally:
        channel.close()


class CamOperationReceiver:
    """Authenticated Server-owned local receiver; persistence is per Node."""
    def __init__(self, node_id, state_path, handler=None, timeout=60):
        self.node_id, self.path = node_id, state_path
        self.handler = handler or cam_native_operation
        self.timeout = timeout
        os.makedirs(os.path.dirname(os.path.abspath(state_path)), mode=0o700, exist_ok=True)
        fd = os.open(state_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(state_path, 0o600)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS operations "
                               "(source TEXT, id TEXT, digest TEXT, result TEXT, "
                               "PRIMARY KEY(source,id))")

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def execute(self, message):
        # Malformed/unaddressable frames are transport errors. Never fabricate
        # a result to an absent, expired or unrelated return address.
        validate_cam_envelope(message, self.node_id)
        try:
            validate_cam_message(message, self.node_id)
            values = message["payload"].get("args")
            if not isinstance(values, dict):
                raise CamProtocolError("missing structured operation args")
            if set(values) - (_CAM_OPERATION_OPTIONS[message["operation"]] | _CAM_COMMON_OPTIONS):
                raise CamProtocolError("unsupported operation options")
        except (ValueError, TypeError) as exc:
            return cam_operation_result(message, stderr=str(exc) + "\n", exit_code=2)
        source = message["source"]["node_id"]
        mid = message["message_id"]
        digest = hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT digest,result FROM operations WHERE source=? AND id=?", (source, mid)).fetchone()
            if row:
                if row[0] != digest:
                    return cam_operation_result(message, stderr="MESSAGE_CONFLICT\n", exit_code=2)
                if row[1] is None:
                    return cam_operation_result(message, stderr="operation outcome unknown; not replayed\n", exit_code=2, status="unknown")
                return json.loads(row[1])
            connection.execute("INSERT INTO operations VALUES (?,?,?,NULL)", (source, mid, digest))
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        worker = context.Process(target=_cam_operation_worker, args=(child, message, self.handler))
        try:
            worker.start()
            child.close()
            if parent.poll(self.timeout):
                result = parent.recv()
            else:
                # Do not turn uncertain execution into a safe-to-retry failure.
                result = cam_operation_result(message, stderr="operation timed out; outcome unknown\n", exit_code=2, status="unknown")
        except (EOFError, OSError, ValueError, TypeError, RuntimeError):
            result = cam_operation_result(message, stderr="operation worker failed; outcome unknown\n", exit_code=2, status="unknown")
        finally:
            parent.close()
            child.close()
            if worker.pid is not None:
                worker.join(0.2)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(2)
                    if worker.is_alive():
                        os.kill(worker.pid, signal.SIGKILL)
                        worker.join(2)
        with self._connect() as connection:
            connection.execute("UPDATE operations SET result=? WHERE source=? AND id=?",
                               (json.dumps(result, ensure_ascii=False), source, mid))
        return result
