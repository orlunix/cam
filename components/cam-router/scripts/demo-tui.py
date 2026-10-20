#!/usr/bin/env python3
"""Two real disposable shell agents, a production Server and the actual TUI.

No existing agent/config is read or modified. Ctrl+Q returns and cleans up.
Requires the four built binaries beside each other and the repository SDK.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import termios
import uuid


def write_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    options = parser.parse_args()
    bindir = options.bin_dir.resolve()
    for name in ("cam-server", "cam-clientd", "cam-tui"):
        if not (bindir / name).is_file():
            parser.error("missing " + str(bindir / name))
    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="cam-tui-demo-") as temporary:
        base = Path(temporary)
        home, server_dir, client_dir = base / "home", base / "server", base / "client"
        for path in (home, server_dir, client_dir):
            path.mkdir(mode=0o700)
        (home / ".cam/logs").mkdir(parents=True)
        token = base / "client.token"
        token.write_text(uuid.uuid4().hex + uuid.uuid4().hex)
        token.chmod(0o600)
        node_id = "demo-" + uuid.uuid4().hex[:12]
        config_server, config_client = base / "server.json", base / "client.json"
        write_json(config_server, {"node_id": node_id, "alias": "demo", "runtime_dir": str(server_dir),
                                  "discover_camc": False,
                                  "clients": [{"client_id": "demo-ui", "token_file": str(token), "management": True}],
                                  "backend": {"python": sys.executable, "module_path": str(root / "src")}})
        write_json(config_client, {"client_id": "demo-ui", "runtime_dir": str(client_dir), "poll_seconds": 1,
                                  "endpoints": [{"node_id": node_id, "token_file": str(token), "socket": str(server_dir / "server.sock")}]})
        env = dict(os.environ, HOME=str(home), PYTHONPATH=str(root / "src"))
        env.pop("TMUX", None)
        processes, logs, agents = [], [], []
        terminal_attrs = termios.tcgetattr(sys.stdin.fileno()) if sys.stdin.isatty() else None
        ui = None
        def interrupted(unused_signum, unused_frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, interrupted)

        def rpc(action, args=None):
            with socket.socket(socket.AF_UNIX) as stream:
                stream.settimeout(20)
                stream.connect(str(client_dir / "client.sock"))
                stream.sendall(json.dumps({"version": 1, "id": uuid.uuid4().hex,
                                           "action": action, "args": args or {}}).encode() + b"\n")
                with stream.makefile("rb") as reader:
                    answer = json.loads(reader.readline())
            if "error" in answer:
                raise RuntimeError(answer["error"])
            return answer["data"]

        try:
            for binary, config in (("cam-server", config_server), ("cam-clientd", config_client)):
                log = open(base / (binary + ".log"), "wb")
                logs.append(log)
                process = subprocess.Popen([str(bindir / binary), "serve", "--config", str(config)],
                                           env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                processes.append(process)
            deadline = time.monotonic() + 12
            while True:
                try:
                    if rpc("snapshot")["nodes"][0]["ready"]:
                        break
                except (OSError, ValueError, KeyError):
                    pass
                if any(p.poll() is not None for p in processes) or time.monotonic() > deadline:
                    details = "\n".join(p.read_text(errors="replace") for p in base.glob("*.log"))
                    raise RuntimeError("demo services could not start (an existing production instance may be active):\n" + details)
                time.sleep(0.1)
            for name in ("shell-one", "shell-two"):
                agent = uuid.uuid4().hex[:8]
                agents.append(agent)
                result = rpc("agent.operation", {"node_id": node_id, "agent_id": agent, "operation": "agent.run",
                                                "options": {"tool": "/bin/bash", "tool_args": ["--noprofile --norc"],
                                                            "name": name, "path": str(options.workspace.resolve()),
                                                            "tool_launcher": "none", "no_default_api": True}})
                if result["payload"]["exit_code"]:
                    raise RuntimeError(result["payload"]["stderr"])
            deadline = time.monotonic() + 20
            while True:
                records = {a["id"]: a for a in rpc("snapshot")["agents"] if a["id"] in agents}
                for record in records.values():
                    if record.get("status") == "dead":
                        raise RuntimeError("demo tool failed: " + str(record.get("exit_reason")))
                if len(records) == len(agents) and all(
                        a.get("status") == "alive" and a.get("phase") == "started"
                        for a in records.values()):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("demo tools did not reach started")
                time.sleep(0.1)
            print("Demo ready: click either shell; right-click an agent for operations. Ctrl+Q exits and cleans up.")
            ui = subprocess.Popen([str(bindir / "cam-tui"), "--config", str(config_client)], env=env)
            return ui.wait()
        finally:
            if ui and ui.poll() is None:
                ui.terminate()
                try:
                    ui.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    ui.kill()
                    ui.wait(timeout=4)
            if len(processes) > 1 and processes[1].poll() is None:
                for agent in agents:
                    try:
                        result = rpc("agent.operation", {"node_id": node_id, "agent_id": agent,
                                                         "operation": "agent.remove", "options": {}})
                        if result["payload"]["exit_code"]:
                            raise RuntimeError("demo SDK cleanup failed")
                    except (OSError, ValueError, RuntimeError):
                        # Only this isolated Home's native SDK is used as cleanup fallback.
                        subprocess.run([sys.executable, "-m", "camc_pkg", "rm", agent], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for log in logs:
                log.close()
            import hashlib
            key = hashlib.sha256(node_id.encode()).hexdigest()[:24]
            shutil.rmtree("/tmp/cam-server-%d-%s" % (os.getuid(), key), ignore_errors=True)
            if terminal_attrs is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, terminal_attrs)
                sys.stdout.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?1049l")
                sys.stdout.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as exc:
        sys.exit("cam-tui demo: " + str(exc))
    except KeyboardInterrupt:
        sys.exit(130)
