"""Agent store: JSON file with fcntl locking. Event store: append-only JSONL."""

import json
import os

from camc_pkg import AGENTS_FILE, EVENTS_FILE

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


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
                    return json.load(f)
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
                if os.path.exists(self._path):
                    try:
                        with open(self._path, "r") as f:
                            agents = json.load(f)
                    except (ValueError, OSError):
                        agents = []
                agents = fn(agents)
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
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
        return matches[0] if len(matches) == 1 else None

    def save(self, agent):
        def _do(agents):
            for i, a in enumerate(agents):
                if a["id"] == agent["id"]:
                    agents[i] = agent
                    return agents
            agents.append(agent)
            return agents
        self._modify(_do)

    def update(self, agent_id, **fields):
        result = [None]
        def _do(agents):
            for a in agents:
                if a["id"] == agent_id:
                    a.update(fields)
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
