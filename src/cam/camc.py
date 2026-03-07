#!/usr/bin/env python3
"""camc — Standalone local coding agent CLI.

Thin CLI wrapper over cam-client.py (deployed at ~/.cam/cam-client.py).
Manages coding agents locally via tmux, no server needed.
Python 3.6+, stdlib only.

Usage:
    ~/.cam/camc run claude "write hello.py"
    ~/.cam/camc list
    ~/.cam/camc logs <id> [-f]
    ~/.cam/camc stop <id>
    ~/.cam/camc attach <id>
    ~/.cam/camc add <tmux-session> --tool claude
    ~/.cam/camc rm <id> [--kill]
"""

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

# ---------------------------------------------------------------------------
# Import shared code from cam-client.py (same directory)
# ---------------------------------------------------------------------------

CAM_DIR = os.path.expanduser("~/.cam")
_client_path = os.path.join(CAM_DIR, "cam-client.py")

# When running from source (development), import directly
try:
    from cam.client import (
        AgentStore,
        AdapterConfig, load_toml, capture_tmux, tmux_session_exists,
        tmux_send_input, tmux_kill_session, create_tmux_session,
        should_auto_confirm, detect_state, detect_completion,
        is_ready_for_input, strip_ansi, _find_tmux_socket,
    )
except ImportError:
    # On target: load from ~/.cam/cam-client.py via importlib
    if not os.path.exists(_client_path):
        sys.stderr.write("Error: %s not found\n" % _client_path)
        sys.exit(1)
    _spec = importlib.util.spec_from_file_location("cam_client", _client_path)
    _cl = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_cl)
    AgentStore = _cl.AgentStore
    AdapterConfig = _cl.AdapterConfig
    load_toml = _cl.load_toml
    capture_tmux = _cl.capture_tmux
    tmux_session_exists = _cl.tmux_session_exists
    tmux_send_input = _cl.tmux_send_input
    tmux_kill_session = _cl.tmux_kill_session
    create_tmux_session = _cl.create_tmux_session
    should_auto_confirm = _cl.should_auto_confirm
    detect_state = _cl.detect_state
    detect_completion = _cl.detect_completion
    is_ready_for_input = _cl.is_ready_for_input
    strip_ansi = _cl.strip_ansi
    _find_tmux_socket = _cl._find_tmux_socket

# ---------------------------------------------------------------------------
# Logging & constants
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [camc] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("camc")

CONFIGS_DIR = os.path.join(CAM_DIR, "configs")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_ago(iso_str):
    if not iso_str:
        return ""
    try:
        clean = iso_str.replace("Z", "").replace("+00:00", "")
        dt = datetime.strptime(clean[:19], "%Y-%m-%dT%H:%M:%S")
        diff = (datetime.utcnow() - dt).total_seconds()
        if diff < 60: return "%ds ago" % int(diff)
        if diff < 3600: return "%dm ago" % int(diff / 60)
        if diff < 86400: return "%dh ago" % int(diff / 3600)
        return "%dd ago" % int(diff / 86400)
    except Exception:
        return iso_str[:16]


def _build_command(config, prompt, path):
    replacements = {"{prompt}": prompt, "{path}": path}
    result = []
    for part in config.command:
        for key, value in replacements.items():
            if key in part:
                part = part.replace(key, value)
                break
        result.append(part)
    return result


def _kill_monitor(agent):
    pid = agent.get("monitor_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    pid_path = "/tmp/camc-%s.pid" % agent["id"]
    if os.path.exists(pid_path):
        try:
            with open(pid_path) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except (ValueError, OSError):
            pass
        try:
            os.unlink(pid_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Background monitor (hidden _monitor subcommand)
# ---------------------------------------------------------------------------

def _run_monitor(agent_id):
    store = AgentStore()
    agent = store.get(agent_id)
    if not agent:
        sys.exit(1)

    config = AdapterConfig(load_toml(os.path.join(CONFIGS_DIR, "%s.toml" % agent["tool"])))
    session = agent["session"]

    pid_path = "/tmp/camc-%s.pid" % agent_id
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))

    running = [True]
    signal.signal(signal.SIGTERM, lambda s, f: running.__setitem__(0, False))

    prev_hash = ""
    last_change = last_health = time.time()
    last_confirm = 0.0
    current_state = None
    has_worked = False
    prompt_disappeared = False
    empty_count = 0

    try:
        while running[0]:
            now = time.time()

            if now - last_health >= 15:
                last_health = now
                if not tmux_session_exists(session):
                    status = "completed" if has_worked else "failed"
                    store.update(agent_id, status=status,
                                 exit_reason="Session exited", completed_at=_now_iso())
                    return

            output = capture_tmux(session)
            h = hashlib.md5(output.encode()).hexdigest()[:8]
            changed = h != prev_hash
            if changed:
                last_change = now
            prev_hash = h

            if not output.strip():
                empty_count += 1
                # Early health check: if output empty for 3+ cycles, session may have died
                if empty_count >= 3 and not tmux_session_exists(session):
                    status = "completed" if has_worked else "failed"
                    store.update(agent_id, status=status,
                                 exit_reason="Session exited", completed_at=_now_iso())
                    return
                time.sleep(1)
                continue
            empty_count = 0

            if now - last_confirm >= 5.0:
                confirm = should_auto_confirm(output, config)
                if confirm:
                    tmux_send_input(session, confirm[0], send_enter=confirm[1])
                    last_confirm = now
                    time.sleep(0.5)
                    continue

            ns = detect_state(output, config)
            if ns and ns != current_state:
                if ns != "initializing":
                    has_worked = True
                current_state = ns
                store.update(agent_id, state=ns)

            done = reason = None
            if not changed and now - last_change >= 3.0:
                done = detect_completion(output, config)
                if done:
                    reason = done

            if config.prompt_after_launch and config.ready_pattern and has_worked:
                clean = strip_ansi(output) if config.strip_ansi else output
                pv = bool(config.ready_pattern.search(clean))
                # Don't count prompt as "returned" if a confirm dialog is active
                # (the prompt char ❯ appears in Ink select menus too)
                if pv and config.confirm_rules:
                    for cp, _r, _e in config.confirm_rules:
                        if cp.search(clean):
                            pv = False
                            break
                if not pv:
                    prompt_disappeared = True
                if pv and prompt_disappeared:
                    done, reason = "completed", "Prompt returned"

            if done in ("completed", "failed"):
                store.update(agent_id, status=done, exit_reason=reason, completed_at=_now_iso())
                return

            time.sleep(1)
    finally:
        try:
            os.unlink(pid_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_run(args):
    tool, prompt = args.tool, args.prompt
    workdir = os.path.abspath(args.path)
    config = AdapterConfig(load_toml(os.path.join(CONFIGS_DIR, "%s.toml" % tool)))

    agent_id = uuid4().hex[:8]
    session = "cam-%s" % agent_id
    launch_cmd = _build_command(config, prompt, workdir)

    print("Starting %s agent %s..." % (tool, agent_id))
    if not create_tmux_session(session, launch_cmd, workdir):
        sys.stderr.write("Error: failed to create tmux session\n")
        sys.exit(1)

    # Startup: wait for readiness, auto-confirm, send prompt
    if config.prompt_after_launch:
        elapsed, confirmed = 0.0, False
        while elapsed < config.startup_wait:
            time.sleep(1); elapsed += 1
            output = capture_tmux(session)
            if not output.strip():
                continue
            if confirmed and is_ready_for_input(output, config):
                break
            confirm = should_auto_confirm(output, config)
            if confirm:
                tmux_send_input(session, confirm[0], send_enter=confirm[1])
                confirmed = True
                time.sleep(3); elapsed += 3
                continue
            if is_ready_for_input(output, config):
                break
        if prompt.strip():
            tmux_send_input(session, prompt, send_enter=True)

    store = AgentStore()
    store.save({"id": agent_id, "tool": tool, "session": session, "status": "running",
                "state": "initializing", "prompt": prompt, "path": workdir,
                "context": {"name": None, "host": None, "port": None},
                "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
                "monitor_pid": None})

    # Spawn background monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_monitor", agent_id],
            stdout=subprocess.DEVNULL,
            stderr=open("/tmp/camc-%s.log" % agent_id, "a"),
            start_new_session=True)
        store.update(agent_id, monitor_pid=proc.pid)
    except Exception:
        pass

    print("  ID: %s  Tool: %s  Session: %s" % (agent_id, tool, session))
    print("  Path: %s" % workdir)
    if prompt:
        print("  Prompt: %s" % (prompt[:60] + "..." if len(prompt) > 60 else prompt))
    else:
        print("  Prompt: (interactive)")
    print("\n  Attach: ~/.cam/camc attach %s" % agent_id)
    print("  Logs:   ~/.cam/camc logs %s -f" % agent_id)


def cmd_list(args):
    store = AgentStore()
    agents = store.list()
    if not agents:
        print("No agents."); return

    for a in agents:
        if a["status"] == "running" and not tmux_session_exists(a["session"]):
            # If no work was done (still initializing), mark as failed
            state = a.get("state") or "initializing"
            status = "failed" if state == "initializing" else "completed"
            a.update(status=status, exit_reason="Session gone", completed_at=_now_iso())
            store.update(a["id"], **{k: a[k] for k in ("status", "exit_reason", "completed_at")})

    colors = {"running": "\033[32m", "completed": "\033[36m", "failed": "\033[31m", "stopped": "\033[33m"}
    reset = "\033[0m"
    use_color = sys.stdout.isatty()

    print("%-10s %-10s %-12s %-12s %-30s %s" % ("ID", "TOOL", "STATUS", "STATE", "PROMPT", "STARTED"))
    print("-" * 90)
    for a in sorted(agents, key=lambda x: x.get("started_at", ""), reverse=True):
        s = a.get("status", "?")
        ss = "%s%-12s%s" % (colors.get(s, ""), s, reset) if use_color else "%-12s" % s
        print("%-10s %-10s %s %-12s %-30s %s" % (
            a.get("id", "?"), a.get("tool", "?"), ss,
            a.get("state", "") or "", (a.get("prompt", "") or "")[:30],
            _time_ago(a.get("started_at"))))


def cmd_logs(args):
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    if args.follow:
        prev = ""
        try:
            while True:
                out = capture_tmux(a["session"], lines=200)
                if out != prev:
                    os.system("clear"); print(out); prev = out
                if not tmux_session_exists(a["session"]):
                    print("\n--- session ended ---"); break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        out = capture_tmux(a["session"], lines=200)
        print(out or ("(session not found)" if not tmux_session_exists(a["session"]) else "(no output)"))


def cmd_stop(args):
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    _kill_monitor(a)
    tmux_kill_session(a["session"])
    store.update(a["id"], status="stopped", exit_reason="Stopped by user", completed_at=_now_iso())
    print("Stopped agent %s" % a["id"])


def cmd_add(args):
    if not tmux_session_exists(args.session):
        sys.stderr.write("Error: tmux session '%s' not found\n" % args.session); sys.exit(1)
    store = AgentStore()
    agent_id = uuid4().hex[:8]
    store.save({"id": agent_id, "tool": args.tool, "session": args.session, "status": "running",
                "state": None, "prompt": "(adopted)", "path": os.getcwd(),
                "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
                "monitor_pid": None})
    # Spawn monitor if config exists
    if os.path.exists(os.path.join(CONFIGS_DIR, "%s.toml" % args.tool)):
        try:
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "_monitor", agent_id],
                stdout=subprocess.DEVNULL, stderr=open("/tmp/camc-%s.log" % agent_id, "a"),
                start_new_session=True)
            store.update(agent_id, monitor_pid=proc.pid)
        except Exception:
            pass
    print("Adopted '%s' as agent %s (tool=%s)" % (args.session, agent_id, args.tool))


def cmd_rm(args):
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    if args.kill:
        _kill_monitor(a); tmux_kill_session(a["session"])
    store.remove(a["id"])
    print("Removed agent %s%s" % (a["id"], " (killed)" if args.kill else ""))


def cmd_attach(args):
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    sock = _find_tmux_socket(a["session"])
    if sock:
        os.execvp("tmux", ["tmux", "-u", "-S", sock, "attach", "-t", a["session"]])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", a["session"]])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="camc",
        description="Standalone local coding agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  camc run claude "fix lint errors"   Launch claude with a prompt
  camc run claude                     Launch claude interactively
  camc run codex "add tests" -p /app  Launch codex in /app
  camc list                           List all agents
  camc logs abc1 -f                   Follow agent output
  camc attach abc1                    Attach to agent tmux
  camc stop abc1                      Stop an agent
  camc add my-session --tool claude   Adopt existing tmux session
  camc rm abc1 --kill                 Remove and kill agent""")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="Launch an agent")
    r.add_argument("tool"); r.add_argument("prompt", nargs="?", default=""); r.add_argument("--path", "-p", default=os.getcwd())

    sub.add_parser("list", aliases=["ls"], help="List agents")

    l = sub.add_parser("logs", help="Show agent output")
    l.add_argument("id"); l.add_argument("-f", "--follow", action="store_true")

    s = sub.add_parser("stop", help="Stop an agent")
    s.add_argument("id")

    a = sub.add_parser("add", help="Adopt existing tmux session")
    a.add_argument("session"); a.add_argument("--tool", "-t", default="claude")

    rm = sub.add_parser("rm", help="Remove from management")
    rm.add_argument("id"); rm.add_argument("--kill", "-k", action="store_true")

    at = sub.add_parser("attach", help="Attach to agent tmux")
    at.add_argument("id")

    # Hidden _monitor subcommand — handle before argparse
    if len(sys.argv) >= 3 and sys.argv[1] == "_monitor":
        _run_monitor(sys.argv[2])
        return

    args = p.parse_args()
    cmds = {"run": cmd_run, "list": cmd_list, "ls": cmd_list, "logs": cmd_logs,
            "stop": cmd_stop, "add": cmd_add, "rm": cmd_rm, "attach": cmd_attach}
    if args.command in cmds:
        cmds[args.command](args)
    else:
        p.print_help(); sys.exit(1)


if __name__ == "__main__":
    main()
