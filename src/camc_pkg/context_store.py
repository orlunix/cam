"""Context store: JSON file with fcntl locking.

Manages ~/.cam/contexts.json — the list of workspaces/projects.
Each context has a unique name, references a machine by name, and has a path.
"""

import json
import os

from camc_pkg import CONTEXTS_FILE

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


class ContextStore(object):
    def __init__(self, path=None):
        self._path = path or CONTEXTS_FILE

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
                contexts = self._read()
                contexts = fn(contexts)
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(contexts, f, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            finally:
                if _fcntl:
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)

    def list(self):
        return self._read()

    def get(self, name):
        for c in self._read():
            if c.get("name") == name:
                return c
        return None

    def list_by_machine(self, machine_name):
        """Return contexts that reference a specific machine."""
        return [c for c in self._read() if c.get("machine") == machine_name]

    def save(self, context):
        name = context["name"]
        def _do(contexts):
            for i, c in enumerate(contexts):
                if c.get("name") == name:
                    contexts[i] = context
                    return contexts
            contexts.append(context)
            return contexts
        self._modify(_do)

    def remove(self, name):
        found = [False]
        def _do(contexts):
            new = [c for c in contexts if c.get("name") != name]
            found[0] = len(new) < len(contexts)
            return new
        self._modify(_do)
        return found[0]
