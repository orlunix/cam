"""Reverse-tunnel registry for cam serve.

Tracks which 127.0.0.1 port on this machine reaches which host's camb
serve endpoint. Hosts register after establishing their `ssh -N -R`
reverse tunnel (see camb's `reverse up`); the tunnel hub uses the
registry to dispatch camc operations onto hosts without per-request SSH.

Pure stdlib, no FastAPI dependency — unit-testable on its own.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

# Entries not refreshed for this long are reported as stale (the host's
# reverse maintainer re-registers on every reconnect).
STALE_AFTER_S = 300


class TunnelRegistry:
    """Thread-safe {key → entry} registry with JSON persistence.

    Entry shape: {key, name, port, token, ts}. `port` is the server-side
    127.0.0.1 port forwarded to the host's camb serve endpoint; `token`
    is the host's camb serve token (used as the §10 frame token).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._tunnels: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._tunnels = {str(k): v for k, v in data.items()
                                 if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        d = os.path.dirname(self._path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._tunnels, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, self._path)

    def register(self, entry: dict) -> dict:
        """Upsert a tunnel entry; returns the stored entry."""
        key = str(entry.get("key") or entry.get("name") or "")
        if not key:
            raise ValueError("tunnel entry needs a key")
        port = entry.get("port")
        if not isinstance(port, int) or port <= 0:
            raise ValueError("tunnel entry needs a positive int port")
        with self._lock:
            stored = {
                "key": key,
                "name": str(entry.get("name") or key),
                "port": port,
                "token": str(entry.get("token") or ""),
                "ts": float(entry.get("ts") or time.time()),
            }
            self._tunnels[key] = stored
            try:
                self._save()
            except OSError as e:
                logger.warning("tunnel registry save failed: %s", e)
            return dict(stored)

    def unregister(self, key: str) -> bool:
        with self._lock:
            if key not in self._tunnels:
                return False
            del self._tunnels[key]
            try:
                self._save()
            except OSError as e:
                logger.warning("tunnel registry save failed: %s", e)
            return True

    def get(self, key: str) -> dict | None:
        with self._lock:
            e = self._tunnels.get(key)
            return dict(e) if e else None

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._tunnels.values()]

    @staticmethod
    def port_open(port: int, timeout: float = 0.25) -> bool:
        try:
            socket.create_connection(("127.0.0.1", int(port)),
                                     timeout=timeout).close()
            return True
        except OSError:
            return False

    def alive(self, key: str, now: float | None = None) -> dict | None:
        """Entry if its port accepts connections and it is not stale."""
        e = self.get(key)
        if not e:
            return None
        now = time.time() if now is None else now
        if now - float(e.get("ts") or 0) > STALE_AFTER_S:
            return None
        if not self.port_open(e["port"]):
            return None
        return e

    def prune(self, max_age_s: float = 3600, now: float | None = None) -> int:
        """Drop entries whose port is closed and that are older than
        max_age_s. Returns the number removed."""
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            for key in list(self._tunnels):
                e = self._tunnels[key]
                if now - float(e.get("ts") or 0) <= max_age_s:
                    continue
                if self.port_open(e.get("port") or 0):
                    continue
                del self._tunnels[key]
                removed += 1
            if removed:
                try:
                    self._save()
                except OSError as e:
                    logger.warning("tunnel registry save failed: %s", e)
        return removed

    def find_for_machine(self, machine: dict) -> dict | None:
        """Match a machines.json record to a live tunnel entry. Onboarded
        hosts register with the machine name; also accept the endpoint
        key user@host:port / host forms."""
        candidates = {str(machine.get("name") or "")}
        host = machine.get("host")
        user = machine.get("user")
        port = machine.get("port") or 22
        if host:
            candidates.add(str(host))
            candidates.add("%s:%s" % (host, port))
            if user:
                candidates.add("%s@%s" % (user, host))
                candidates.add("%s@%s:%s" % (user, host, port))
        candidates.discard("")
        for e in self.list():
            keys = {str(e.get("key")), str(e.get("name"))}
            if keys & candidates:
                return e
        return None
