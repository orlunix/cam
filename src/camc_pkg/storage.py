"""Agent store: JSON file with fcntl locking."""

import json
import os

from camc_pkg import AGENTS_FILE

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
        for a in agents:
            if a["id"] == agent_id:
                return a
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
