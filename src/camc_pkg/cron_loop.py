"""Agent-owned scheduled prompt loops.

Storage and dispatch surface for `camc cron add/list/rm --loop`. Loops
are per-agent recurring prompts: each due fire sends the loop's prompt
text to the owner agent via ``camc msg send``. Loop entries live in
``~/.cam/loops/<owner_id>/agent.loop.json`` so the cron jobs.d/
registry stays separate from the loop store.

See docs/camc-agent-loop-spec.md for the user-facing contract.

Python 3.6+, stdlib only.
"""

import json
import os
import subprocess
from datetime import datetime
from uuid import uuid4

from camc_pkg import CAM_DIR, log
from camc_pkg.cron import (
    _acquire_lock, _release_lock, _ensure_dir,
    _iso, _now_local, _parse_iso,
    _initial_next_due_at, advance_next_due_at,
)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — non-POSIX
    _fcntl = None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LOOPS_DIR = os.path.join(CAM_DIR, "loops")
LOOP_FILENAME = "agent.loop.json"
LOOP_SCHEMA_NAME = "camc-agent-loop/1"
DEFAULT_MAX_ATTEMPTS = 3


def _owner_dir(owner_id):
    return os.path.join(LOOPS_DIR, owner_id)


def _owner_file(owner_id):
    return os.path.join(_owner_dir(owner_id), LOOP_FILENAME)


def _owner_runs_file(owner_id):
    return os.path.join(_owner_dir(owner_id), "runs.jsonl")


def _owner_archive_dir(owner_id):
    return os.path.join(_owner_dir(owner_id), "archive")


def _new_loop_id():
    return uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Owner resolution
# ---------------------------------------------------------------------------

class OwnerNotFound(Exception):
    pass


def resolve_owner(name_or_id, _store=None):
    """Resolve an agent name / id / id-prefix to its canonical record.

    Returns a dict with at least ``id``, ``name``, and ``tmux_session``.
    Raises OwnerNotFound when there is no matching agent. Injectable
    ``_store`` for tests."""
    if not name_or_id:
        raise OwnerNotFound("owner is required")
    if _store is None:
        from camc_pkg.storage import AgentStore
        _store = AgentStore()
    rec = _store.get(name_or_id)
    if rec is None:
        raise OwnerNotFound("no agent matching %r" % name_or_id)
    return rec


def _agent_field(rec, field, default=""):
    """Read a task/agent field, supporting nested + legacy flat shape."""
    if not rec:
        return default
    t = rec.get("task")
    if isinstance(t, dict) and t.get(field) is not None:
        return t.get(field)
    return rec.get(field, default)


# ---------------------------------------------------------------------------
# LoopStore — per-owner agent.loop.json file
# ---------------------------------------------------------------------------

class DuplicateLoopName(Exception):
    pass


class CorruptLoopFile(Exception):
    pass


class AmbiguousLoopKey(Exception):
    def __init__(self, key, matches):
        super(AmbiguousLoopKey, self).__init__(
            "ambiguous prefix %r matches %d ids: %s"
            % (key, len(matches), ", ".join(matches)))
        self.key = key
        self.matches = matches


def _empty_envelope(owner_id, owner_name="", tmux_session=""):
    return {
        "schema": LOOP_SCHEMA_NAME,
        "version": 1,
        "agent_id": owner_id,
        "agent_name": owner_name,
        "tmux_session": tmux_session,
        "updated_at": _iso(_now_local()),
        "loops": [],
    }


class LoopStore(object):
    """Per-owner agent.loop.json with fcntl-locked read/modify/write."""

    def __init__(self, owner_id, owner_name="", tmux_session="", root=None):
        self._owner_id = owner_id
        self._owner_name = owner_name
        self._tmux_session = tmux_session
        self._root = root or LOOPS_DIR

    def _path(self):
        return os.path.join(self._root, self._owner_id, LOOP_FILENAME)

    def _archive_dir(self):
        return os.path.join(self._root, self._owner_id, "archive")

    def _runs_file(self):
        return os.path.join(self._root, self._owner_id, "runs.jsonl")

    def load(self):
        """Return the envelope dict. Missing file → empty envelope.
        Raises CorruptLoopFile when the file exists but won't parse."""
        path = self._path()
        if not os.path.exists(path):
            return _empty_envelope(
                self._owner_id, self._owner_name, self._tmux_session)
        try:
            with open(path, "r") as f:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    if _fcntl:
                        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except (ValueError, OSError) as e:
            raise CorruptLoopFile("%s: %s" % (path, e))
        if not isinstance(data, dict) or "loops" not in data:
            raise CorruptLoopFile("%s: schema mismatch" % path)
        if data.get("schema") != LOOP_SCHEMA_NAME:
            raise CorruptLoopFile(
                "%s: schema %r != %r" % (path, data.get("schema"), LOOP_SCHEMA_NAME))
        return data

    def list_loops(self):
        return list(self.load().get("loops", []))

    def find(self, key):
        if not key:
            return None
        loops = self.list_loops()
        for L in loops:
            if L.get("id") == key or L.get("name") == key:
                return L
        prefix = [L for L in loops if (L.get("id") or "").startswith(key)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise AmbiguousLoopKey(
                key, [L.get("id") or "" for L in prefix])
        return None

    def _modify(self, fn):
        """fcntl-locked read/modify/write. ``fn`` receives the envelope
        dict and may mutate it in place or return a new one. Atomic
        replace via tmp file."""
        _ensure_dir(os.path.join(self._root, self._owner_id))
        lock_path = self._path() + ".lock"
        with open(lock_path, "w") as lf:
            if _fcntl:
                _fcntl.flock(lf.fileno(), _fcntl.LOCK_EX)
            try:
                try:
                    data = self.load()
                except CorruptLoopFile:
                    raise
                new = fn(data)
                if new is None:
                    new = data
                new["updated_at"] = _iso(_now_local())
                # Keep envelope identity fields fresh.
                new["schema"] = LOOP_SCHEMA_NAME
                new["version"] = 1
                new.setdefault("agent_id", self._owner_id)
                if self._owner_name:
                    new["agent_name"] = self._owner_name
                if self._tmux_session:
                    new["tmux_session"] = self._tmux_session
                path = self._path()
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(new, f, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp, path)
                return new
            finally:
                if _fcntl:
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)

    def add(self, loop):
        name = loop.get("name")

        def _fn(data):
            for L in data.get("loops", []):
                if L.get("name") == name:
                    raise DuplicateLoopName(name)
            data.setdefault("loops", []).append(loop)
            return data

        self._modify(_fn)

    def remove(self, key):
        """Remove (archive) one loop by exact id/name/unique-prefix."""
        target_holder = {"loop": None}

        def _fn(data):
            loops = data.get("loops", [])
            picked = None
            for L in loops:
                if L.get("id") == key or L.get("name") == key:
                    picked = L
                    break
            if picked is None:
                prefix = [L for L in loops if (L.get("id") or "").startswith(key)]
                if len(prefix) > 1:
                    raise AmbiguousLoopKey(
                        key, [L.get("id") or "" for L in prefix])
                if len(prefix) == 1:
                    picked = prefix[0]
            if picked is None:
                return data
            data["loops"] = [L for L in loops if L is not picked]
            target_holder["loop"] = picked
            return data

        self._modify(_fn)
        return target_holder["loop"]

    def replace_one(self, loop):
        """Replace the loop with the same id (idempotent update)."""
        lid = loop.get("id")

        def _fn(data):
            loops = data.get("loops", [])
            data["loops"] = [
                (loop if L.get("id") == lid else L) for L in loops
            ]
            return data

        self._modify(_fn)

    def archive(self, loop, reason):
        _ensure_dir(self._archive_dir())
        ts = _now_local().strftime("%Y%m%d-%H%M%S")
        fname = "%s-%s-%s.json" % (
            ts, loop.get("name") or "anon", loop.get("id") or "noid")
        path = os.path.join(self._archive_dir(), fname)
        payload = dict(loop)
        payload["archived_at"] = _iso(_now_local())
        payload["archive_reason"] = reason
        try:
            with open(path, "w") as f:
                json.dump(payload, f, indent=2)
            return path
        except OSError as e:
            log.warning("loop archive failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Loop runs log
# ---------------------------------------------------------------------------

def append_loop_run(owner_id, record, _root=None):
    """Append a record to the owner's runs.jsonl (best-effort)."""
    record.setdefault("ts", _iso(_now_local()))
    root = _root or LOOPS_DIR
    path = os.path.join(root, owner_id, "runs.jsonl")
    _ensure_dir(os.path.dirname(path))
    try:
        with open(path, "a") as f:
            if _fcntl:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
            try:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            finally:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
    except OSError as e:
        log.warning("loop run record failed for %s: %s", owner_id, e)


# ---------------------------------------------------------------------------
# Loop record builder
# ---------------------------------------------------------------------------

def build_loop(name, schedule, prompt, owner_rec, *,
               max_attempts=None, no_wait=True, expect_reply=False):
    """Normalize a loop record matching the spec schema."""
    now_dt = _now_local()
    now_iso = _iso(now_dt)
    sched = dict(schedule)
    if "next_due_at" not in sched:
        sched["next_due_at"] = _initial_next_due_at(sched, now_dt)
    owner_id = owner_rec.get("id") or ""
    owner_name = _agent_field(owner_rec, "name") or ""
    tmux_session = _agent_field(owner_rec, "tmux_session") or ""
    return {
        "id": _new_loop_id(),
        "name": name,
        "enabled": True,
        "executor": "monitor",
        "owner": {
            "type": "agent",
            "agent_id": owner_id,
            "agent_name": owner_name,
            "tmux_session": tmux_session,
        },
        "schedule": sched,
        "action": {
            "type": "prompt",
            "text": prompt,
            "delivery": {
                "method": "camc-msg-send",
                "to": owner_id,
                "no_wait": bool(no_wait),
                "expect_reply": bool(expect_reply),
            },
        },
        "policy": {
            "ttl_days": None,
            "expires_at": None,
            "max_attempts": int(max_attempts) if max_attempts is not None
                            else DEFAULT_MAX_ATTEMPTS,
            "busy_policy": "defer",
            "misfire_policy": "skip",
        },
        "state": {
            "attempts": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_due_at": None,
            "last_dispatched_at": None,
            "last_msg_id": None,
            "last_status": None,
            "last_error": None,
        },
    }


# ---------------------------------------------------------------------------
# Dispatch — sends prompt via `camc msg send <owner> -t <text> --no-wait`
# ---------------------------------------------------------------------------

def _camc_binary():
    """Resolve a camc binary path for spawning a child msg send."""
    import shutil
    deployed = os.path.expanduser("~/.cam/camc")
    if os.path.isfile(deployed) and os.access(deployed, os.X_OK):
        return deployed
    return shutil.which("camc") or "camc"


def dispatch_loop(loop, *, runner=None, camc_path=None):
    """Send the loop's prompt to the owner agent via `camc msg send`.

    Returns (ok, msg_id_or_error). Caller is responsible for
    timestamps / state updates / runs.jsonl writes.
    """
    runner = runner or subprocess.run
    binp = camc_path or _camc_binary()
    owner_id = (loop.get("owner") or {}).get("agent_id") or ""
    if not owner_id:
        return False, "no owner agent_id"
    delivery = (loop.get("action") or {}).get("delivery") or {}
    text = (loop.get("action") or {}).get("text") or ""
    if not text:
        return False, "empty prompt"
    argv = [binp, "msg", "send", owner_id, "--text", text]
    if delivery.get("no_wait", True):
        argv.append("--no-wait")
    if delivery.get("expect_reply", False):
        argv.append("--expect-reply")
    try:
        r = runner(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return False, err[-1] if err else ("exit %d" % r.returncode)
    # MSG_ID=<8hex> appears on stdout when --no-wait. With blocking
    # mode, stdout is the reply body — fall back to the first line as
    # a best-effort id surrogate.
    msg_id = ""
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("MSG_ID="):
            msg_id = line.split("=", 1)[1].strip()
            break
    return True, msg_id


# ---------------------------------------------------------------------------
# tick_loops — scan all owner dirs, dispatch due loops
# ---------------------------------------------------------------------------

def _scan_owner_ids(root=None):
    root = root or LOOPS_DIR
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if os.path.exists(os.path.join(path, LOOP_FILENAME)):
            out.append(name)
    return out


def _check_owner_ready(owner_id, _store=None):
    """Resolve owner and decide if a loop dispatch is safe.

    Returns (ready: bool, reason_or_none: str|None).

    A loop only fires when its owner agent is present in the agent
    store, currently ``status=running``, and currently ``state=idle``.
    Anything else (missing record, busy, completed, killed, etc.)
    defers the loop until the next tick — neither attempts nor
    next_due_at advance. This matches the spec's "busy_policy:defer".
    """
    if not owner_id:
        return False, "no owner_id"
    if _store is None:
        try:
            from camc_pkg.storage import AgentStore
            _store = AgentStore()
        except Exception as e:
            return False, "agent_store_unavailable:%s" % e
    rec = None
    try:
        rec = _store.get(owner_id)
    except Exception as e:
        return False, "agent_store_lookup_failed:%s" % e
    if rec is None:
        return False, "owner_not_found"
    status = (rec.get("status") or "").lower()
    if status != "running":
        return False, "owner_status_%s" % (status or "unknown")
    state = (rec.get("state") or "").lower()
    if state != "idle":
        return False, "owner_state_%s" % (state or "unknown")
    return True, None


def _loop_is_due(loop, now_dt):
    if not loop.get("enabled", True):
        return False, None
    nda = (loop.get("schedule") or {}).get("next_due_at")
    if not nda:
        return False, None
    nda_dt = _parse_iso(nda)
    if nda_dt is None:
        return False, None
    if now_dt >= nda_dt:
        return True, nda
    return False, None


def tick_loops(now=None, dispatch=None, root=None, agent_store=None):
    """Scan all owner loop files and dispatch due loops.

    Reuses the same scheduling shape as host cron (``schedule.next_due_at``
    advances after queueing). ``dispatch`` injectable for tests; defaults
    to ``dispatch_loop``. ``agent_store`` injectable to swap AgentStore
    for the owner-idle guard.

    Guard: a due loop is dispatched ONLY when its owner agent is
    currently ``status=running`` AND ``state=idle``. Other states
    (missing record, busy, completed) produce a ``loop_deferred``
    event with the reason and leave the loop's ``next_due_at`` and
    ``state.attempts`` untouched, so a busy agent doesn't burn the
    retry budget and the prompt is delivered as soon as the agent
    next sits idle.
    """
    dispatch_fn = dispatch or dispatch_loop
    now_dt = now or _now_local()
    dispatched = 0
    for owner_id in _scan_owner_ids(root):
        store = LoopStore(owner_id, root=root)
        try:
            envelope = store.load()
        except CorruptLoopFile as e:
            log.warning("loop tick skipped (corrupt): %s", e)
            append_loop_run(owner_id, {
                "event": "loop_tick_aborted",
                "reason": "corrupt_loop_file",
                "error": str(e),
            }, _root=root)
            continue
        loops = envelope.get("loops", [])
        if not loops:
            continue
        changed = False
        for loop in loops:
            due, due_at = _loop_is_due(loop, now_dt)
            if not due:
                continue
            # Owner readiness gate — defer if the owner agent isn't
            # idle. Defer means: do NOT advance next_due_at, do NOT
            # increment attempts, just emit a loop_deferred event.
            ready, defer_reason = _check_owner_ready(
                owner_id, _store=agent_store)
            if not ready:
                append_loop_run(owner_id, {
                    "event": "loop_deferred",
                    "loop_id": loop.get("id"),
                    "loop_name": loop.get("name"),
                    "due_at": due_at,
                    "owner": owner_id,
                    "reason": defer_reason or "owner_not_ready",
                }, _root=root)
                continue
            # Append queue event first so it's visible if dispatch crashes.
            append_loop_run(owner_id, {
                "event": "loop_queued",
                "loop_id": loop.get("id"),
                "loop_name": loop.get("name"),
                "due_at": due_at,
                "owner": owner_id,
            }, _root=root)
            ok, payload = dispatch_fn(loop)
            st = loop.setdefault("state", {})
            st["last_due_at"] = due_at
            st["last_dispatched_at"] = _iso(_now_local())
            if ok:
                st["attempts"] = 0
                st["last_msg_id"] = payload or ""
                st["last_status"] = "sent"
                st["last_error"] = None
                append_loop_run(owner_id, {
                    "event": "loop_dispatched",
                    "loop_id": loop.get("id"),
                    "loop_name": loop.get("name"),
                    "msg_id": payload or "",
                }, _root=root)
            else:
                st["attempts"] = int(st.get("attempts") or 0) + 1
                st["last_status"] = "failed"
                st["last_error"] = payload or "dispatch failed"
                append_loop_run(owner_id, {
                    "event": "loop_failed",
                    "loop_id": loop.get("id"),
                    "loop_name": loop.get("name"),
                    "attempts": st["attempts"],
                    "error": payload or "",
                }, _root=root)
            # Advance the schedule regardless of success — failed
            # loops retry on later ticks until max_attempts is hit.
            loop["schedule"] = advance_next_due_at(loop["schedule"], now_dt)
            st["updated_at"] = _iso(_now_local())
            changed = True
            dispatched += 1
            # Failure recycle
            max_att = int((loop.get("policy") or {}).get(
                "max_attempts") or DEFAULT_MAX_ATTEMPTS)
            if not ok and st["attempts"] >= max_att:
                loop["enabled"] = False
                st["last_status"] = "recycled_too_many_failures"
                append_loop_run(owner_id, {
                    "event": "loop_recycled",
                    "loop_id": loop.get("id"),
                    "loop_name": loop.get("name"),
                    "reason": "too_many_failures",
                }, _root=root)
        if changed:
            # persist the envelope back (state + advanced schedules).
            store.replace_envelope(envelope)
    return dispatched


# Helper hung onto LoopStore so the persist path stays inside the lock.
def _replace_envelope(self, envelope):
    def _fn(_data):
        return envelope
    self._modify(_fn)


LoopStore.replace_envelope = _replace_envelope
