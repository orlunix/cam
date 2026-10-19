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
import socket
import sys
import time
import uuid

from camc_pkg import CAM_DIR

POLL_INTERVAL_S = 0.2
DEFAULT_TIMEOUT_MS = 30000
# Coverage cards (~/.cam/camb/hubs/<hub-id>.json) older than this are
# treated as dead: their coverage stops excluding events from claiming
# and their claimed leftovers become adoptable by other hubs.
CARD_TTL_S = 90

_TMUX_SOCK_RE = re.compile(r"^cam-(.+)\.sock$")
_CLAIMED_RE = re.compile(r"^(.*\.json)\.claimed-([A-Za-z0-9_.-]+)$")
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
    for sub in ("outbox", "inbox", "hubs"):
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


def _load_cards(home):
    """Read fresh coverage cards from ~/.cam/camb/hubs/*.json.
    Returns {hub_id: card}. Stale cards (older than CARD_TTL_S) and
    unreadable files are ignored. Kept byte-equivalent with camb's."""
    hubs = os.path.join(home, "hubs")
    cards = {}
    now = time.time()
    try:
        names = sorted(n for n in os.listdir(hubs) if n.endswith(".json"))
    except OSError:
        return cards
    for n in names:
        try:
            with open(os.path.join(hubs, n), "r", encoding="utf-8") as fh:
                card = json.load(fh)
            hid = str(card.get("hub_id") or n[:-5])
            ts = float(card.get("ts") or 0)
            if now - ts <= CARD_TTL_S:
                cards[hid] = card
        except (OSError, IOError, ValueError, TypeError):
            continue
    return cards


def _event_target_ref(req):
    """The raw target ref of a request argv ('' for list/malformed).
    Target positions follow bridge-protocol-v1 §6."""
    argv = req.get("argv") or []
    if len(argv) < 2:
        return ""
    verb = argv[0]
    if verb in ("status", "capture", "send", "key"):
        ref = str(argv[1])
    elif verb == "msg" and len(argv) >= 3 and argv[1] == "send":
        ref = str(argv[2])
    else:
        return ""
    return "" if ref.startswith("-") else ref


def _card_covers(card, ref):
    """ref may be '<node>:<agent>'; a cover entry matches by exact node
    or as the '<entry>:' prefix. Entries are {name, key} dicts (name is
    hub-local, key is the canonical user@host:port) or plain strings."""
    for e in card.get("covers") or []:
        if isinstance(e, dict):
            candidates = [e.get("name"), e.get("key")]
        else:
            candidates = [e]
        for n in candidates:
            if n and (ref == n or ref.startswith(str(n) + ":")):
                return True
    return False


def _should_claim(req, cards, my_id):
    """Router rule (§9.2): an event with an explicit node target covered
    by ANOTHER live hub's card is left in the outbox for that hub.
    Everything else (list, bare refs, my coverage, nobody's coverage) is
    claimable."""
    ref = _event_target_ref(req)
    if not ref or ":" not in ref:
        return True
    mine = cards.get(my_id)
    if mine and _card_covers(mine, ref):
        return True
    for hid, card in cards.items():
        if hid != my_id and _card_covers(card, ref):
            return False
    return True


def hub_drain(bridge_id=""):
    """Claim-aware drain (bridge-protocol-v1.1). With a bridge-id, each
    outbox event is atomically claimed (rename → .claimed-<id>) before
    printing; events targeting nodes covered by another live hub's card
    are skipped; this hub's own claimed leftovers from an interrupted
    drain are re-printed (at-least-once above the host's seen-id
    idempotency), and orphans of hubs without a live card are adopted.
    Without a bridge-id the legacy print+delete behaviour is kept
    verbatim. Skips unfinished .tmp files; leaves unreadable files in
    place. Always exits 0."""
    home = _ensure_dirs()
    outbox = os.path.join(home, "outbox")
    cards = _load_cards(home)
    try:
        names = sorted(os.listdir(outbox))
    except OSError:
        names = []
    reqs = []
    drained = []
    for n in names:
        p = os.path.join(outbox, n)
        m = _CLAIMED_RE.match(n)
        owner = None
        if m:
            owner = m.group(2)
            if owner != bridge_id and owner in cards:
                continue  # live owner's leftover — it will re-drain it
            # own leftover or a dead owner's orphan: re-drain/adopt.
        elif not n.endswith(".json"):
            continue  # unfinished .tmp or junk
        try:
            with open(p, "r", encoding="utf-8") as fh:
                req = json.load(fh)
        except (OSError, IOError, ValueError):
            continue  # unreadable: leave it
        if owner is None and bridge_id:
            if not _should_claim(req, cards, bridge_id):
                continue  # a live peer covers the target — leave it
            claimed = p + ".claimed-" + bridge_id
            try:
                os.rename(p, claimed)
                p = claimed
            except OSError:
                continue  # lost the claim race
        reqs.append(req)
        drained.append(p)
    sys.stdout.write(json.dumps(reqs) + "\n")
    for p in drained:
        try:
            os.unlink(p)
        except OSError:
            pass
    return 0


def _emit_response(resp):
    """Print a bridge response and return its exit code (0 ok / 1 error)."""
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


def _tunnel_token(home, hub_id):
    try:
        with open(os.path.join(home, "hubs", hub_id + ".token"),
                  "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, IOError):
        return ""


def _tunnel_call(port, token, req, deadline):
    """One synchronous request/response over a hub tunnel (§10). Returns
    the response dict, or None on any transport failure — the caller
    falls back to the next tunnel or the mailbox."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=0.25)
    except OSError:
        return None
    try:
        sock.settimeout(max(0.5, deadline - time.time()))
        frame = json.dumps({"token": token, "request": req}).encode("utf-8") + b"\n"
        sock.sendall(frame)
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 4 * 1024 * 1024:
                return None
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _try_tunnels(home, req):
    """Tunnel fast path (§10): every live card advertising a tunnel,
    coverage-filtered for explicit node targets, priority-desc. Returns
    the final response, or None when no tunnel could deliver (transport
    failures and not_my_fleet cascade; application-level responses are
    terminal). Kept byte-equivalent with camb's."""
    target = _event_target_ref(req)
    cands = []
    for hid, card in _load_cards(home).items():
        tun = card.get("tunnel") or {}
        port = tun.get("port")
        if not isinstance(port, int) or port <= 0:
            continue
        if target and ":" in target and not _card_covers(card, target):
            continue
        token = _tunnel_token(home, hid)
        if not token:
            continue
        try:
            prio = int(tun.get("priority", 0))
        except (ValueError, TypeError):
            prio = 0
        cands.append((prio, hid, port, token))
    cands.sort(key=lambda c: (-c[0], c[1]))
    deadline = time.time() + _timeout_s()
    for _, _hid, port, token in cands:
        resp = _tunnel_call(port, token, req, deadline)
        if resp is None:
            continue
        if str(resp.get("error") or "").startswith("not_my_fleet"):
            continue
        return resp
    return None


def submit(argv):
    """Ferry argv to the bridge host. Tunnel first (synchronous push when
    a hub's card advertises one), mailbox fallback (source of truth).
    Exit codes per bridge-protocol-v1: 0 ok / 1 error response / 2 bridge
    unavailable (the request stays in the outbox and can be drained
    later)."""
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

    resp = _try_tunnels(home, req)
    if resp is not None:
        return _emit_response(resp)

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
            return _emit_response(resp)
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
