"""Shared, stdlib-only CAM message contract (Python 3.6+).

Routing and business execution are deliberately separate: validation does not
rewrite the envelope, resolve another target, or execute a command.
"""

import datetime as _cam_datetime
import json
import re
import uuid


CAM_MESSAGE_LIMIT = 4 * 1024 * 1024
CAM_AGENT_OPERATIONS = {
    "message.send": "msg", "agent.send": "send",
    "agent.capture": "capture", "agent.run": "run",
    "agent.stop": "stop", "agent.remove": "rm",
    "agent.kill": "kill", "agent.reboot": "reboot",
    "agent.key": "key", "agent.status": "status",
}
_CAM_MESSAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class CamProtocolError(ValueError):
    pass


def cam_message_time(value):
    try:
        return _cam_datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_cam_datetime.timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        raise CamProtocolError("invalid message expiry")


def cam_message_expiry(seconds):
    return (_cam_datetime.datetime.now(_cam_datetime.timezone.utc) +
            _cam_datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_cam_envelope(message, node_id=None, now=None):
    """Validate common addressing before accepting operations or results."""
    if (not isinstance(message, dict) or type(message.get("version")) is not int or
            message["version"] != 1):
        raise CamProtocolError("unsupported message version")
    if message.get("type") != "message":
        raise CamProtocolError("expected message")
    mid = message.get("message_id")
    if not isinstance(mid, str) or not _CAM_MESSAGE_ID.fullmatch(mid):
        raise CamProtocolError("invalid message ID")
    for field in ("reply_to", "thread_id"):
        value = message.get(field)
        if value is not None and (not isinstance(value, str) or not _CAM_MESSAGE_ID.fullmatch(value)):
            raise CamProtocolError("invalid %s" % field)
    source, target = message.get("source"), message.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise CamProtocolError("missing source or target")
    for address in (source, target):
        node = address.get("node_id")
        if (not isinstance(node, str) or not node or len(node) > 128 or
                any(ord(char) < 33 for char in node)):
            raise CamProtocolError("missing or invalid Node")
        agent = address.get("agent_id")
        if agent is not None and (not isinstance(agent, str) or
                                  not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent)):
            raise CamProtocolError("invalid agent ID")
    if node_id is not None and target["node_id"] != node_id:
        raise CamProtocolError("wrong target Node")
    if not isinstance(message.get("payload"), dict):
        raise CamProtocolError("invalid payload")
    expiry = cam_message_time(message.get("expires_at"))
    if now is None:
        import time
        now = time.time()
    if expiry <= now:
        raise CamProtocolError("message expired")
    cam_encode_frame(message)
    return message


def validate_cam_message(message, node_id=None, now=None):
    """Validate an addressed operation without mutating it."""
    validate_cam_envelope(message, node_id, now)
    if message.get("operation") not in CAM_AGENT_OPERATIONS:
        raise CamProtocolError("UNSUPPORTED_OPERATION")
    agent = message["target"].get("agent_id")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent):
        raise CamProtocolError("a specific agent ID is required")
    return message


def cam_encode_frame(value):
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CamProtocolError("invalid JSON message")
    if len(data) > CAM_MESSAGE_LIMIT:
        raise CamProtocolError("message exceeds frame limit")
    return data


def cam_read_frame(stream):
    data = stream.readline(CAM_MESSAGE_LIMIT + 1)
    if not data:
        raise CamProtocolError("connection closed before result")
    if len(data) > CAM_MESSAGE_LIMIT or not data.endswith(b"\n"):
        raise CamProtocolError("invalid or oversized frame")
    try:
        def reject_constant(unused):
            raise ValueError("non-JSON number")
        value = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise CamProtocolError("invalid JSON frame")
    if not isinstance(value, dict):
        raise CamProtocolError("expected object frame")
    return value


def cam_operation_result(message, stdout="", stderr="", exit_code=0, status=None):
    """A result is a new addressed message, not a held relay connection."""
    return {
        "version": 1, "type": "message", "message_id": uuid.uuid4().hex,
        "reply_to": message["message_id"],
        "source": dict(message["target"]),
        "target": {"node_id": message["source"]["node_id"],
                   "receiver_id": "operation.results"},
        "operation": "operation.result",
        "payload": {"status": status or ("ok" if exit_code == 0 else "error"),
                    "stdout": stdout, "stderr": stderr, "exit_code": exit_code},
        "expires_at": cam_message_expiry(86400),
    }
