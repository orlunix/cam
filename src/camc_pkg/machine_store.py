"""Machine store: JSON file with fcntl locking.

Manages ~/.cam/machines.json — the list of machines camc can connect to.
Each machine has a unique name, type (local/ssh), and connection details.
"""

import json
import os

from camc_pkg import MACHINES_FILE

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


_DEFAULT_LOCAL = {"name": "local", "type": "local"}


class MachineStore(object):
    def __init__(self, path=None):
        self._path = path or MACHINES_FILE

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
                machines = self._read()
                machines = fn(machines)
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(machines, f, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            finally:
                if _fcntl:
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)

    def list(self):
        machines = self._read()
        if not machines:
            return [dict(_DEFAULT_LOCAL)]
        return machines

    def get(self, name):
        for m in self._read():
            if m.get("name") == name:
                return m
        if name == "local":
            return dict(_DEFAULT_LOCAL)
        return None

    def save(self, machine):
        name = machine["name"]
        def _do(machines):
            for i, m in enumerate(machines):
                if m.get("name") == name:
                    machines[i] = machine
                    return machines
            machines.append(machine)
            return machines
        self._modify(_do)

    def remove(self, name):
        found = [False]
        def _do(machines):
            new = [m for m in machines if m.get("name") != name]
            found[0] = len(new) < len(machines)
            return new
        self._modify(_do)
        return found[0]

    def list_ssh(self):
        """Return only SSH-type machines."""
        return [m for m in self.list() if m.get("type") == "ssh"]
