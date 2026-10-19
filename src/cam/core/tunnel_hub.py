"""Tunnel hub — cam serve acting as a bridge host (bridge-protocol-v1).

Three jobs:

1. **ferry face**: publish a coverage card + token under
   ~/.cam/camb/hubs/cam-serve.* and serve a local §10 JSON-line listener
   so camc/camb ferries on this machine get a synchronous fast path
   (the ferry's existing tunnel-first priority chain picks it up with
   zero camc changes).
2. **mailbox face**: periodically drain ~/.cam/camb/outbox via the
   stock `camc --hub-drain --bridge-id cam-serve`.
3. **dispatch**: resolve the target host and take the fastest path —
   live reverse tunnel from the registry (sub-second) → one-shot SSH
   (camc_delegate) → release back to the outbox (attempts+1).

Optionally (config `server.tunnel_onboard=true`) onboards machines.json
ssh machines marked `tunnel: true`: deploys camb to ~/.cam/bin/camb,
installs a per-host reverse ssh key, and starts `camb reverse up` on the
host.

Pure stdlib + asyncio; no FastAPI imports here so the hub is testable
standalone. The HTTP registration API lives in cam/api/routes/tunnels.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import time

from cam.core.camc_delegate import _find_camc, _run_camc, _run_camc_ssh
from cam.core.tunnel_registry import TunnelRegistry

logger = logging.getLogger(__name__)

HUB_ID = "cam-serve"
LISTEN_PORT = 42377
CARD_INTERVAL_S = 15.0
CARD_TTL_S = 90.0  # ferry-side TTL (bridge.py CARD_TTL_S)
DRAIN_INTERVAL_S = 2.0
DRAIN_BACKOFF_MAX_S = 60.0
DRAIN_TIMEOUT_S = 10
SWEEP_INTERVAL_S = 30.0
ONBOARD_INTERVAL_S = 60.0
SEEN_TTL_S = 600.0
SEEN_MAX = 500
ATTEMPTS_MAX = 3
EXEC_TIMEOUT_S = 30
MSG_TIMEOUT_S = 120
FRAME_CAP = 4 * 1024 * 1024

_ALLOWED_VERBS = ("list", "status", "capture", "send", "key", "msg")
_TARGETED_VERBS = ("status", "capture", "send", "key")


def _camb_home(cam_dir):
    return os.path.join(cam_dir, "camb")


def _event_target_ref(argv):
    """bridge-protocol-v1 §6 target positions."""
    if len(argv) < 2:
        return ""
    verb = argv[0]
    if verb in _TARGETED_VERBS:
        ref = str(argv[1])
    elif verb == "msg" and len(argv) >= 3 and argv[1] == "send":
        ref = str(argv[2])
    else:
        return ""
    return "" if ref.startswith("-") else ref


def _strip_node_prefix(argv, prefixes):
    """Remove the matched '<node>:' prefix from the target arg."""
    argv = list(argv)
    idx = None
    if argv and argv[0] in _TARGETED_VERBS and len(argv) > 1:
        idx = 1
    elif len(argv) >= 3 and argv[0] == "msg" and argv[1] == "send":
        idx = 2
    if idx is None:
        return argv
    ref = argv[idx]
    for p in sorted(prefixes, key=len, reverse=True):
        if ref.startswith(p + ":"):
            argv[idx] = ref[len(p) + 1:]
            return argv
    return argv


def _endpoint_key(machine):
    user = machine.get("user")
    host = machine.get("host")
    port = machine.get("port") or 22
    return "%s@%s:%s" % (user or "default", host, port)


def _machine_prefixes(machine):
    prefs = set()
    if machine.get("name"):
        prefs.add(str(machine["name"]))
    host = machine.get("host")
    if host:
        prefs.add(str(host))
        prefs.add("%s:%s" % (host, machine.get("port") or 22))
        if machine.get("user"):
            prefs.add("%s@%s" % (machine["user"], host))
            prefs.add(_endpoint_key(machine))
    prefs.discard("")
    return sorted(prefs, key=len, reverse=True)


class TunnelHub:
    """The cam-serve bridge-host worker. See module docstring."""

    def __init__(self, registry: TunnelRegistry, data_dir: str,
                 cam_dir: str | None = None,
                 listen_port: int = LISTEN_PORT,
                 machines_path: str | None = None,
                 do_onboard: bool = False,
                 server_ssh_target: str | None = None,
                 http_base: str | None = None,
                 auth_token: str | None = None,
                 camb_src: str | None = None,
                 authorized_keys_path: str | None = None):
        self.registry = registry
        self.data_dir = data_dir
        self.cam_dir = cam_dir or os.path.expanduser("~/.cam")
        self.listen_port = listen_port
        self.machines_path = (machines_path
                              or os.path.join(self.cam_dir, "machines.json"))
        self.do_onboard = do_onboard
        self.server_ssh_target = server_ssh_target
        self.http_base = http_base
        self.auth_token = auth_token or ""
        self.camb_src = camb_src or os.path.join(
            os.path.dirname(__file__), "..", "..", "apps", "cam-desktop",
            "resources", "camb", "camb")
        self.authorized_keys_path = (
            authorized_keys_path
            or os.path.expanduser(os.path.join("~", ".ssh",
                                               "authorized_keys")))
        self._seen: dict[str, tuple[float, dict]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._server = None
        self._stopped = False

    # ------------------------------------------------------------------
    # secrets / tokens
    # ------------------------------------------------------------------

    def _hub_secret(self) -> str:
        path = os.path.join(self.data_dir, "tunnel-hub-secret")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
            if val:
                return val
        except OSError:
            pass
        val = secrets.token_hex(16)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(val + "\n")
        return val

    def _ferry_token(self) -> str:
        """Token the local ferry uses for the §10 listener."""
        return hashlib.sha256(
            (self._hub_secret() + "/" + HUB_ID).encode()).hexdigest()

    def host_token(self, host_key: str) -> str:
        """Derived per-host token: registration Bearer + tunnel frame token
        the host's camb serve must use."""
        return hashlib.sha256(
            (self.auth_token + "/" + host_key).encode()).hexdigest()

    # ------------------------------------------------------------------
    # machines / coverage
    # ------------------------------------------------------------------

    def _load_machines(self) -> list[dict]:
        try:
            with open(self.machines_path, "r", encoding="utf-8") as fh:
                machines = json.load(fh)
            return [m for m in machines if isinstance(m, dict)]
        except (OSError, ValueError):
            return []

    def _covered_machines(self) -> list[dict]:
        """Machines currently reachable via a live reverse tunnel."""
        out = []
        for m in self._load_machines():
            if m.get("type") != "ssh":
                continue
            if self.registry.find_for_machine(m):
                out.append(m)
        return out

    def _refresh_card(self) -> None:
        home = _camb_home(self.cam_dir)
        hubs = os.path.join(home, "hubs")
        os.makedirs(hubs, exist_ok=True)
        covers = []
        for m in self._covered_machines():
            covers.append({"name": m.get("name"), "key": _endpoint_key(m)})
        card = {"hub_id": HUB_ID, "ts": time.time(), "covers": covers,
                "tunnel": {"port": self.listen_port, "priority": 10},
                "proto": 1}
        tmp = os.path.join(hubs, HUB_ID + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(card, fh)
        os.rename(tmp, os.path.join(hubs, HUB_ID + ".json"))
        token_path = os.path.join(hubs, HUB_ID + ".token")
        if not os.path.exists(token_path):
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._ferry_token() + "\n")

    # ------------------------------------------------------------------
    # §10 ferry listener
    # ------------------------------------------------------------------

    async def _handle_conn(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if len(line) > FRAME_CAP:
                return
            try:
                frame = json.loads(line.decode("utf-8"))
            except ValueError:
                frame = None
            resp = await self._handle_frame(frame)
            writer.write(json.dumps(resp).encode("utf-8") + b"\n")
            await writer.drain()
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _handle_frame(self, frame) -> dict:
        if not isinstance(frame, dict):
            return {"id": None, "ok": False, "error": "bad frame"}
        req = frame.get("request")
        req_id = req.get("id") if isinstance(req, dict) else None
        if not hmac.compare_digest(str(frame.get("token") or ""),
                                   self._ferry_token()):
            return {"id": req_id, "ok": False, "error": "unauthorized"}
        return await self.dispatch(req)

    # ------------------------------------------------------------------
    # mailbox drain
    # ------------------------------------------------------------------

    async def _drain_once(self) -> None:
        camc = _find_camc()
        proc = await asyncio.create_subprocess_exec(
            "python3", camc, "--hub-drain", "--bridge-id", HUB_ID,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(),
                                            timeout=DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("hub-drain timeout")
        reqs = json.loads(out.decode("utf-8") or "[]")
        for req in reqs:
            asyncio.create_task(self._dispatch_and_answer(req))

    async def _dispatch_and_answer(self, req: dict) -> None:
        resp = await self.dispatch(req)
        inbox = os.path.join(_camb_home(self.cam_dir), "inbox")
        os.makedirs(inbox, exist_ok=True)
        path = os.path.join(inbox, str(req.get("id")) + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(resp, fh)
        os.rename(tmp, path)

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, req: dict) -> dict:
        """seen-id idempotency + in-flight dedup (spec §9.1)."""
        req_id = str((req or {}).get("id") or "")
        if req_id:
            hit = self._seen.get(req_id)
            if hit and time.time() - hit[0] < SEEN_TTL_S:
                return dict(hit[1])
            task = self._inflight.get(req_id)
            if task is not None:
                return await task
        task = asyncio.create_task(self._do_dispatch(req or {}))
        if req_id:
            self._inflight[req_id] = task
        try:
            resp = await task
        finally:
            if req_id:
                self._inflight.pop(req_id, None)
        if req_id:
            self._seen[req_id] = (time.time(), resp)
            if len(self._seen) > SEEN_MAX:
                oldest = sorted(self._seen.items(),
                                key=lambda kv: kv[1][0])[:len(self._seen) // 2]
                for k, _ in oldest:
                    self._seen.pop(k, None)
        return resp

    async def _do_dispatch(self, req: dict) -> dict:
        req_id = req.get("id")
        argv = req.get("argv") or []
        entry = {"ts": time.time(), "id": req_id, "argv": argv,
                 "from": (req.get("from") or {}).get("agent")
                 if isinstance(req.get("from"), dict) else None,
                 "ok": False, "error": None, "path": None}
        try:
            resp = await self._route(req, entry)
            return resp
        finally:
            self._audit(entry)

    async def _route(self, req: dict, audit: dict) -> dict:
        req_id = req.get("id")
        argv = list(req.get("argv") or [])
        if not argv or argv[0] not in _ALLOWED_VERBS:
            err = ("unsupported verb '%s' via bridge (v1: list, status, "
                   "capture, send, key, msg)" % (argv[0] if argv else ""))
            audit["error"] = err
            return {"id": req_id, "ok": False, "error": err, "proto": 1}

        verb = argv[0]
        ref = _event_target_ref(argv)

        if verb == "list":
            return await self._dispatch_list(req, audit)

        if not ref:
            err = "missing target"
            audit["error"] = err
            return {"id": req_id, "ok": False, "error": err, "proto": 1}

        machine = self._resolve_target(ref)
        if machine is None and ":" in ref:
            err = "unknown node '%s'" % ref.rsplit(":", 1)[0]
            audit["error"] = err
            return {"id": req_id, "ok": False, "error": err, "proto": 1}
        if machine is None:
            machine = {"name": None, "type": "local"}  # bare ref → local

        run_argv = _strip_node_prefix(
            argv, _machine_prefixes(machine)) if machine.get("name") else argv
        timeout = MSG_TIMEOUT_S if verb == "msg" else EXEC_TIMEOUT_S

        # path 1: live reverse tunnel (fastest)
        tun = (self.registry.find_for_machine(machine)
               if machine.get("name") else None)
        if tun and self.registry.port_open(tun["port"]):
            resp = await asyncio.get_running_loop().run_in_executor(
                None, self._tunnel_exec, tun, run_argv, req, timeout)
            if resp is not None:
                audit["path"] = "tunnel"
                audit["ok"] = bool(resp.get("ok"))
                audit["error"] = resp.get("error")
                return resp

        # path 2: one-shot SSH / local camc. rc < 0 or ssh's 255 means
        # transport failure → fall through to release; other codes are
        # application-level results (terminal).
        rc, out = await asyncio.get_running_loop().run_in_executor(
            None, self._ssh_exec, machine, run_argv, timeout)
        if rc is not None and 0 <= rc < 255:
            audit["path"] = "ssh" if machine.get("name") else "local"
            audit["ok"] = rc == 0
            audit["error"] = None if rc == 0 else out.strip()
            if rc == 0:
                return {"id": req_id, "ok": True,
                        "text": out.rstrip("\n"), "proto": 1}
            return {"id": req_id, "ok": False,
                    "error": out.strip() or "exit %d" % rc, "proto": 1}

        # path 3: release back to outbox for another hub / later
        attempts = int(req.get("attempts") or 0) + 1
        if attempts < ATTEMPTS_MAX:
            self._release(req, attempts)
            err = "unroutable: released (attempt %d)" % attempts
            audit["error"] = err
            return {"id": req_id, "ok": False, "error": err, "proto": 1}
        err = "unroutable after %d attempts" % attempts
        audit["error"] = err
        return {"id": req_id, "ok": False, "error": err, "proto": 1}

    def _resolve_target(self, ref: str) -> dict | None:
        """'<node>:<agent>' → machine record by name or endpoint prefix."""
        for m in self._load_machines():
            for p in _machine_prefixes(m):
                if ref == p or ref.startswith(p + ":"):
                    return m
        return None

    async def _dispatch_list(self, req: dict, audit: dict) -> dict:
        """Fleet list: local camc + every live reverse tunnel."""
        sections = []
        loop = asyncio.get_running_loop()
        rc, out = await loop.run_in_executor(
            None, _run_camc, ["list"], EXEC_TIMEOUT_S)
        if rc == 0 and out.strip():
            sections.append(out.rstrip("\n"))
        for tun in self.registry.list():
            if not self.registry.port_open(tun["port"]):
                continue
            resp = await loop.run_in_executor(
                None, self._tunnel_exec, tun, ["list"], req, EXEC_TIMEOUT_S)
            if resp and resp.get("ok") and resp.get("text"):
                sections.append("== %s ==\n%s"
                                % (tun.get("name"), resp["text"]))
        audit["path"] = "aggregate"
        audit["ok"] = True
        return {"id": req.get("id"), "ok": True,
                "text": "\n".join(sections), "proto": 1}

    def _tunnel_exec(self, tun: dict, argv: list[str], req: dict,
                     timeout: float) -> dict | None:
        """One §10 round-trip into the reverse tunnel. None on transport
        failure (caller falls through to the next path)."""
        try:
            sock = socket.create_connection(("127.0.0.1", tun["port"]),
                                            timeout=0.5)
        except OSError:
            return None
        try:
            sock.settimeout(timeout)
            frame = json.dumps({
                "token": tun.get("token") or "",
                "request": {"id": req.get("id"), "argv": argv,
                            "from": req.get("from"), "ts": req.get("ts"),
                            "proto": 1},
            }).encode("utf-8") + b"\n"
            sock.sendall(frame)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                if len(buf) > FRAME_CAP:
                    return None
            resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            resp.setdefault("id", req.get("id"))
            return resp
        except (OSError, ValueError):
            return None
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _ssh_exec(self, machine: dict, argv: list[str],
                  timeout: float) -> tuple[int | None, str]:
        """One-shot camc execution over SSH; (None, '') when the machine
        has no ssh route at all."""
        if machine.get("type") == "local" or not machine.get("host"):
            try:
                return _run_camc(argv, timeout)
            except FileNotFoundError:
                return None, ""
        return _run_camc_ssh(machine["host"], machine.get("user"),
                             machine.get("port"), argv, timeout,
                             key_file=machine.get("key_file"),
                             camc_path=machine.get("camc_path"),
                             remote_home=machine.get("remote_home"))

    def _release(self, req: dict, attempts: int) -> None:
        outbox = os.path.join(_camb_home(self.cam_dir), "outbox")
        os.makedirs(outbox, exist_ok=True)
        rel = dict(req)
        rel["attempts"] = attempts
        path = os.path.join(outbox, str(req.get("id")) + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rel, fh)
        os.rename(tmp, path)

    def _audit(self, entry: dict) -> None:
        try:
            path = os.path.join(self.data_dir, "tunnel-hub-audit.jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # onboard (optional)
    # ------------------------------------------------------------------

    def _machine_ssh_base(self, machine: dict) -> list[str]:
        """ssh/scp option list sharing camc_delegate's ControlMaster."""
        conn_key = "%s@%s:%s" % (machine.get("user") or "default",
                                 machine.get("host"),
                                 machine.get("port") or 22)
        chash = hashlib.sha256(conn_key.encode()).hexdigest()[:12]
        opts = []
        if machine.get("port"):
            opts += ["-p", str(machine["port"])]
        if machine.get("key_file"):
            opts += ["-i", machine["key_file"]]
        opts += ["-o", "ConnectTimeout=10",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "ControlPath=/tmp/cam-ssh-%s" % chash,
                 "-o", "ControlMaster=auto",
                 "-o", "ControlPersist=600"]
        return opts

    def _onboard_ssh(self, machine: dict, remote_cmd: str,
                     timeout: float = 40) -> tuple[int, str]:
        target = ("%s@%s" % (machine["user"], machine["host"])
                  if machine.get("user") else machine["host"])
        try:
            proc = subprocess.run(
                ["ssh"] + self._machine_ssh_base(machine)
                + [target, remote_cmd],
                capture_output=True, text=True, timeout=timeout)
            return proc.returncode, proc.stdout + proc.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, str(e)

    def _onboard_scp(self, machine: dict, local: str, remote: str,
                     timeout: float = 60) -> bool:
        target = ("%s@%s" % (machine["user"], machine["host"])
                  if machine.get("user") else machine["host"])
        try:
            proc = subprocess.run(
                ["scp", "-q"] + self._machine_ssh_base(machine)
                + [local, "%s:%s" % (target, remote)],
                capture_output=True, timeout=timeout)
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _onboard_machine(self, machine: dict) -> bool:
        """Idempotent host onboarding: deploy camb to ~/.cam/bin/camb,
        install a per-host reverse ssh key into this server's
        authorized_keys, and start `camb reverse up` on the host.
        Best-effort; returns True when the host was told to connect."""
        name = str(machine.get("name") or "")
        if not name or not machine.get("host") \
                or not self.server_ssh_target or not self.http_base:
            return False

        # 1. host keypair for the reverse direction (host → this server)
        rc, _ = self._onboard_ssh(
            machine,
            'test -f ~/.cam/camb-rev-key || ssh-keygen -t ed25519 '
            '-f ~/.cam/camb-rev-key -N "" -C "camb-rev-%s"' % name)
        if rc != 0:
            logger.warning("onboard %s: keygen failed", name)
            return False
        rc, pubkey = self._onboard_ssh(machine,
                                       "cat ~/.cam/camb-rev-key.pub")
        pubkey = (pubkey or "").strip().splitlines()
        pubkey = pubkey[0] if pubkey else ""
        if rc != 0 or not pubkey.startswith("ssh-"):
            logger.warning("onboard %s: no pubkey", name)
            return False

        # 2. authorize the key on THIS server (marker comment for cleanup)
        try:
            existing = ""
            if os.path.exists(self.authorized_keys_path):
                with open(self.authorized_keys_path, "r",
                          encoding="utf-8") as fh:
                    existing = fh.read()
            if pubkey.split()[1] not in existing:
                with open(self.authorized_keys_path, "a",
                          encoding="utf-8") as fh:
                    fh.write(pubkey + "\n")
        except OSError as e:
            logger.warning("onboard %s: authorized_keys: %s", name, e)
            return False

        # 3. deploy camb (never downgrade; probe first)
        rc, out = self._onboard_ssh(
            machine, "~/.cam/bin/camb version 2>/dev/null || true")
        if "0.3.0" not in (out or ""):
            self._onboard_ssh(machine, "mkdir -p ~/.cam/bin")
            if not self._onboard_scp(machine, self.camb_src,
                                     "~/.cam/bin/camb.tmp"):
                logger.warning("onboard %s: scp failed", name)
                return False
            rc, _ = self._onboard_ssh(
                machine, "chmod 700 ~/.cam/bin/camb.tmp && "
                "mv ~/.cam/bin/camb.tmp ~/.cam/bin/camb")
            if rc != 0:
                return False

        # 4. start the reverse maintainer on the host (idempotent)
        token = self.host_token(name)
        rc, out = self._onboard_ssh(
            machine,
            "~/.cam/bin/camb reverse up %s --http %s --name %s "
            "--token %s --key-file ~/.cam/camb-rev-key"
            % (self.server_ssh_target, self.http_base, name, token))
        ok = rc == 0
        if not ok:
            logger.warning("onboard %s: reverse up failed: %s",
                           name, (out or "").strip()[:200])
        return ok

    def _onboard_tick(self) -> None:
        for m in self._load_machines():
            if m.get("type") == "ssh" and m.get("tunnel"):
                try:
                    self._onboard_machine(m)
                except Exception as e:
                    logger.warning("onboard %s failed: %s",
                                   m.get("name"), e)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_conn, "127.0.0.1", self.listen_port)
        logger.info("tunnel hub listening on 127.0.0.1:%d", self.listen_port)
        next_card = 0.0
        next_sweep = 0.0
        next_onboard = 0.0
        drain_delay = DRAIN_INTERVAL_S
        while not self._stopped:
            now = time.time()
            try:
                if now >= next_card:
                    self._refresh_card()
                    next_card = now + CARD_INTERVAL_S
                if now >= next_sweep:
                    self.registry.prune()
                    next_sweep = now + SWEEP_INTERVAL_S
                if self.do_onboard and now >= next_onboard:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._onboard_tick)
                    next_onboard = now + ONBOARD_INTERVAL_S
                try:
                    await self._drain_once()
                    drain_delay = DRAIN_INTERVAL_S
                except Exception as e:
                    logger.warning("hub-drain failed: %s", e)
                    drain_delay = min(drain_delay * 2, DRAIN_BACKOFF_MAX_S)
            except Exception as e:
                logger.warning("tunnel hub tick failed: %s", e)
            await asyncio.sleep(drain_delay)

    async def stop(self) -> None:
        self._stopped = True
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except OSError:
                pass
