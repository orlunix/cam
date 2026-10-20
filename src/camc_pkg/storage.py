"""Agent store: JSON file with fcntl locking. Event store: append-only JSONL."""

import json
import os

from camc_pkg import AGENTS_FILE, EVENTS_FILE

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


def normalize_agent(record):
    """Normalize legacy records to the two-level agent lifecycle in place."""
    status = record.get("status")
    state = record.get("state")
    if status not in ("alive", "dead"):
        if status in ("stopped", "failed", "killed", "complete", "completed"):
            record["status"] = "dead"
            record["state"] = "stopped" if status in ("complete", "completed") else status
        else:
            record["status"] = "alive"
            record["state"] = "idle" if state == "idle" else "running"
            record["phase"] = ("initializing" if status in ("pending", "starting") or
                               state == "initializing" or
                               (record.get("runtime") or {}).get("preparing") else "started")
    if record["status"] == "alive" and state == "initializing":
        record["phase"] = "initializing"
    if record["status"] == "dead":
        if record.get("state") not in ("stopped", "failed", "killed"):
            record["state"] = "failed"
        record["phase"] = None
    else:
        if record.get("state") not in ("idle", "running"):
            record["state"] = "running"
        if record.get("phase") not in ("initializing", "started"):
            record["phase"] = "started"
        if record["phase"] == "initializing":
            record["state"] = "running"
    return record


class AgentStore(object):
    def __init__(self, path=None):
        self._path = path or AGENTS_FILE

    def _read(self):
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r") as f:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_SH)
                try:
                    return [normalize_agent(a) for a in json.load(f)]
                finally:
                    if _fcntl:
                        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except (ValueError, OSError):
            return []

    def _modify(self, fn):
        d = os.path.dirname(self._path)
        try:
            os.makedirs(d)
        except OSError:
            pass
        lock_path = self._path + ".lock"
        with open(lock_path, "w") as lf:
            if _fcntl:
                _fcntl.flock(lf.fileno(), _fcntl.LOCK_EX)
            try:
                agents = []
                file_had_data = False
                if os.path.exists(self._path):
                    try:
                        sz = os.path.getsize(self._path)
                        file_had_data = sz > 4  # non-trivial content (not just "[]")
                        with open(self._path, "r") as f:
                            agents = [normalize_agent(a) for a in json.load(f)]
                    except (ValueError, OSError):
                        if file_had_data:
                            # File exists with data but JSON parse failed —
                            # likely NFS stale cache or partial read. Abort
                            # before invoking the mutation so valid data can
                            # never be replaced from a failed read.
                            return
                        agents = []
                agents = [normalize_agent(a) for a in fn(agents)]
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    os.fchmod(f.fileno(), 0o600)
                    json.dump(agents, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            finally:
                if _fcntl:
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)

    def list(self):
        return self._read()

    def get(self, agent_id):
        agents = self._read()
        # Exact ID match
        for a in agents:
            if a["id"] == agent_id:
                return a
        # Exact tmux_session match (for cam → camc delegation by tmux session name)
        # Support both new "tmux_session" and legacy "session" field names
        for a in agents:
            if a.get("tmux_session") == agent_id or a.get("session") == agent_id:
                return a
        # Prefix match on ID
        matches = [a for a in agents if a["id"].startswith(agent_id)]
        if len(matches) == 1:
            return matches[0]
        # Name match (exact then prefix, case-insensitive)
        lower = agent_id.lower()
        for a in agents:
            task = a.get("task")
            name = (task.get("name", "") if isinstance(task, dict) else a.get("name", "")) or ""
            if name.lower() == lower:
                return a
        name_matches = []
        for a in agents:
            task = a.get("task")
            name = (task.get("name", "") if isinstance(task, dict) else a.get("name", "")) or ""
            if name.lower().startswith(lower):
                name_matches.append(a)
        if len(name_matches) == 1:
            return name_matches[0]
        # Numeric index (1-based, matches `camc list` order)
        try:
            idx = int(agent_id) - 1
            if 0 <= idx < len(agents):
                return agents[idx]
        except ValueError:
            pass
        return None

    def save(self, agent):
        agent = normalize_agent(dict(agent))
        def _do(agents):
            for i, a in enumerate(agents):
                if a["id"] == agent["id"]:
                    agents[i] = agent
                    return agents
            agents.append(agent)
            return agents
        self._modify(_do)

    def update(self, agent_id, expected_launch_id=None, **fields):
        result = [None]
        def _do(agents):
            for a in agents:
                if a["id"] == agent_id:
                    if expected_launch_id is not None and a.get("launch_id") != expected_launch_id:
                        break
                    if (expected_launch_id is not None and a.get("status") == "dead"
                            and fields.get("status") == "dead"):
                        break  # The first terminal result wins within a launch.
                    if (a.get("status") == "dead" and "status" not in fields
                            and fields.get("state") in ("idle", "running")):
                        break  # Late screen updates cannot revive or rewrite a terminal tool.
                    a.update(fields)
                    normalize_agent(a)
                    result[0] = a
                    break
            return agents
        self._modify(_do)
        return result[0]

    def remove(self, agent_id):
        found = [False]
        def _do(agents):
            new = [a for a in agents if a["id"] != agent_id]
            found[0] = len(new) < len(agents)
            return new
        self._modify(_do)
        return found[0]


class EventStore(object):
    """Append-only event log (JSONL format)."""

    def __init__(self, path=None):
        self._path = path or EVENTS_FILE

    def append(self, agent_id, event_type, detail=None):
        """Append an event to events.jsonl."""
        from camc_pkg.utils import _now_iso
        event = {
            "agent_id": agent_id,
            "ts": _now_iso(),
            "type": event_type,
            "detail": detail or {},
        }
        d = os.path.dirname(self._path)
        try:
            os.makedirs(d)
        except OSError:
            pass
        try:
            with open(self._path, "a") as f:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(event, separators=(",", ":")) + "\n")
                finally:
                    if _fcntl:
                        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except OSError:
            pass

    def read(self, agent_id=None, since=None, limit=500):
        """Read events, optionally filtered by agent_id and/or since timestamp."""
        if not os.path.exists(self._path):
            return []
        events = []
        try:
            with open(self._path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if agent_id and ev.get("agent_id") != agent_id:
                        # Also match prefix
                        if not ev.get("agent_id", "").startswith(agent_id):
                            continue
                    if since and ev.get("ts", "") < since:
                        continue
                    events.append(ev)
        except OSError:
            return []
        # Return most recent events (tail)
        return events[-limit:]

    def rotate(self, max_age_days=30):
        """Remove events older than max_age_days."""
        if not os.path.exists(self._path):
            return 0
        from camc_pkg.utils import _now_iso
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        kept = []
        removed = 0
        try:
            with open(self._path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if ev.get("ts", "") < cutoff:
                        removed += 1
                    else:
                        kept.append(line)
        except OSError:
            return 0
        if removed > 0:
            try:
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    for line in kept:
                        f.write(line + "\n")
                os.replace(tmp, self._path)
            except OSError:
                pass
        return removed
