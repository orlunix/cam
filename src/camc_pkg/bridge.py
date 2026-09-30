"""Fleet bridge ferry — mailbox transport (bridge-protocol-v1).

Absorbs the camb client's role into camc: packs a camc-style argv into a
JSON file request under ~/.cam/camb/outbox/ and prints the bridge host's
response from ~/.cam/camb/inbox/. The host (cam-desktop embedded-hub
bridge worker) owns all fleet resolution, consent, and dispatch; this
module only adds local-first routing on top of AgentStore. Mailbox
transport only — no tunnel.

stdlib-only.
"""

import json
import os
import re
import sys
import time
import uuid

from camc_pkg import CAM_DIR

POLL_INTERVAL_S = 0.2
DEFAULT_TIMEOUT_MS = 30000

_TMUX_SOCK_RE = re.compile(r"^cam-(.+)\.sock$")
# Global flags that may precede the subcommand (`camc --json status x`).
# --json is forwarded after the verb (remote output matters); -v/--verbose
# only affect local logging and are dropped when ferrying.
_GLOBAL_FLAGS = ("--json", "-v", "--verbose")
_TARGETED_VERBS = ("status", "capture", "send", "key")


def mailbox_home():
    return os.path.join(CAM_DIR, "camb")


def mailbox_deployed():
    # The host's --hub-drain bootstraps these dirs; their presence means a
    # bridge host has connected to this node at least once.
    return os.path.isdir(os.path.join(mailbox_home(), "outbox"))


def _ensure_dirs():
    home = mailbox_home()
    for sub in ("outbox", "inbox"):
        d = os.path.join(home, sub)
        if not os.path.isdir(d):
            os.makedirs(d, 0o700)
    return home


def _from_agent():
    """Best-effort: parse agent id from $TMUX socket basename
    (cam-<agentid>.sock). Returns None for foreign sockets."""
    tmux = os.environ.get("TMUX") or ""
    sock = tmux.split(",")[0]
    m = _TMUX_SOCK_RE.match(os.path.basename(sock))
    return m.group(1) if m else None


def _resolve_send_io(argv):
    """Resolve send --file/--stdin client-side into --text <content> so the
    host only ever sees --text. Everything else passes through untouched."""
    if not argv or argv[0] != "send":
        return argv
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--file", "-f") and i + 1 < len(argv):
            path = argv[i + 1]
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, IOError) as e:
                sys.stderr.write("camc: cannot read %s: %s\n" % (path, e))
                sys.exit(1)
            out.append("--text")
            out.append(content)
            i += 2
        elif a == "--stdin":
            content = sys.stdin.read()
            out.append("--text")
            out.append(content)
            i += 1
        else:
            out.append(a)
            i += 1
    return out


def _timeout_s():
    raw = os.environ.get("CAMB_TIMEOUT_MS")
    if raw:
        try:
            return max(0.0, float(raw) / 1000.0)
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_MS / 1000.0


def hub_drain():
    """Atomically print + delete finalized outbox requests as a JSON array.
    Skips unfinished .tmp files; leaves unreadable files in place.
    Always exits 0."""
    home = _ensure_dirs()
    outbox = os.path.join(home, "outbox")
    try:
        names = sorted(
            n for n in os.listdir(outbox)
            if n.endswith(".json") and os.path.isfile(os.path.join(outbox, n))
        )
    except OSError:
        names = []
    reqs = []
    drained = []
    for n in names:
        p = os.path.join(outbox, n)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                reqs.append(json.load(fh))
            drained.append(p)
        except (OSError, IOError, ValueError):
            continue  # unreadable: leave it
    sys.stdout.write(json.dumps(reqs) + "\n")
    for p in drained:
        try:
            os.unlink(p)
        except OSError:
            pass
    return 0


def submit(argv):
    """Ferry argv to the bridge host via the mailbox. Exit codes per
    bridge-protocol-v1: 0 ok / 1 error response / 2 bridge unavailable
    (the request stays in the outbox and can be drained later)."""
    if not mailbox_deployed():
        sys.stderr.write(
            "camc: bridge unavailable (no bridge host mailbox on this node)\n")
        return 2
    home = mailbox_home()
    outbox = os.path.join(home, "outbox")
    inbox = os.path.join(home, "inbox")

    argv = _resolve_send_io(argv)
    req = {
        "id": uuid.uuid4().hex,
        "argv": list(argv),
        "from": {"agent": _from_agent()},
        "ts": time.time(),
    }

    tmp = os.path.join(outbox, req["id"] + ".json.tmp")
    final = os.path.join(outbox, req["id"] + ".json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(req, fh)
    os.rename(tmp, final)

    resp_path = os.path.join(inbox, req["id"] + ".json")
    deadline = time.time() + _timeout_s()
    while time.time() < deadline:
        if os.path.isfile(resp_path):
            try:
                with open(resp_path, "r", encoding="utf-8") as fh:
                    resp = json.load(fh)
            except (OSError, IOError, ValueError):
                time.sleep(POLL_INTERVAL_S)
                continue
            try:
                os.unlink(resp_path)
            except OSError:
                pass
            if resp.get("ok"):
                text = resp.get("text")
                if text is not None:
                    sys.stdout.write(str(text) + "\n")
                return 0
            err = resp.get("error") or "unknown error"
            sys.stderr.write("camc: %s\n" % err)
            for c in resp.get("candidates") or []:
                sys.stderr.write(str(c) + "\n")
            return 1
        time.sleep(POLL_INTERVAL_S)

    sys.stderr.write(
        "camc: bridge unavailable (hub not connected or bridge disabled "
        "on this node)\n")
    return 2


def _ferry_argv(verb, rest, globals_seen):
    out = [verb] + list(rest)
    if "--json" in globals_seen and "--json" not in out:
        out.append("--json")
    return out


def route(argv, store=None):
    """Pre-dispatch ferry decision: normalized verb-first argv to ferry, or
    None to let normal local dispatch proceed.

    - `list -a/--all` → fleet list (flag stripped; the host's list is
      always fleet-wide)
    - `<targeted verb> node:ref` / `msg send node:ref` → always ferry
      (the host strips the node: prefix before forwarding)
    - bare ref → local-first: AgentStore hit defers to local dispatch;
      miss ferries only when a bridge host mailbox exists on this node
    - msg wait/reply/read/show/list address local msg_ids → never ferry
    """
    if not argv:
        return None
    globals_seen = []
    i = 0
    while i < len(argv) and argv[i] in _GLOBAL_FLAGS:
        globals_seen.append(argv[i])
        i += 1
    if i >= len(argv):
        return None
    verb = argv[i]
    rest = argv[i + 1:]

    if verb == "list":
        if "-a" not in rest and "--all" not in rest:
            return None
        out = [t for t in rest if t not in ("-a", "--all")]
        return _ferry_argv(verb, out, globals_seen)

    if verb in _TARGETED_VERBS:
        if not rest or rest[0].startswith("-"):
            return None  # missing/exotic target: argparse reports usage
        ref = rest[0]
    elif verb == "msg":
        if len(rest) < 2 or rest[0] != "send" or rest[1].startswith("-"):
            return None
        ref = rest[1]
    else:
        return None

    if ":" in ref:
        return _ferry_argv(verb, rest, globals_seen)
    if not mailbox_deployed():
        return None
    if store is None:
        from camc_pkg.storage import AgentStore
        store = AgentStore()
    if store.get(ref) is not None:
        return None
    return _ferry_argv(verb, rest, globals_seen)
