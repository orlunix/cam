"""CLI commands and main entry point (argparse)."""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from camc_pkg import __version__, CAM_DIR, CONFIGS_DIR, CONTEXT_FILE, log
from camc_pkg.utils import _now_iso, _time_ago, _load_default_context, _build_command, _kill_monitor
from camc_pkg.adapters import _EMBEDDED_CONFIGS, _load_config
from camc_pkg.storage import AgentStore, EventStore
from camc_pkg.transport import (
    _find_tmux_socket, capture_tmux, tmux_session_exists,
    tmux_send_input, tmux_kill_session, create_tmux_session,
)
from camc_pkg.detection import should_auto_confirm, is_ready_for_input
from camc_pkg.monitor import _run_monitor


def cmd_init(args):
    """Interactive setup wizard."""
    print("camc v%s — Coding Agent Manager (standalone)" % __version__)
    print()

    # 1. Check tmux
    print("Checking dependencies...")
    tmux_path = shutil.which("tmux")
    if tmux_path:
        print("  ✓ tmux: %s" % tmux_path)
    else:
        print("  ✗ tmux: NOT FOUND (required)")
        print("  Install: apt install tmux / brew install tmux")
        sys.exit(1)

    # Check available tools
    for tool_name in ("claude", "codex", "agent"):
        path = shutil.which(tool_name)
        if path:
            print("  ✓ %s: %s" % (tool_name, path))
        else:
            print("  · %s: not found" % tool_name)

    # 2. Create directories
    print()
    print("Setting up directories...")
    for d in (CAM_DIR, CONFIGS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    print("  Config: %s" % CAM_DIR)

    # 3. Write adapter configs
    print()
    print("Writing adapter configs...")
    for filename, content in _EMBEDDED_CONFIGS.items():
        path = os.path.join(CONFIGS_DIR, filename)
        if os.path.exists(path) and not getattr(args, "force", False):
            print("  · %s: exists (use --force to overwrite)" % filename)
        else:
            with open(path, "w") as f:
                f.write(content)
            print("  ✓ %s" % filename)

    # 4. Create context.json template
    _load_default_context()
    print()
    print("  Context template: %s" % CONTEXT_FILE)

    # 5. Done
    print()
    print("Setup complete!")
    print()
    print("Quick start:")
    print("  camc run claude \"fix the tests\"   # Launch an agent")
    print("  camc run claude                    # Interactive mode")
    print("  camc list                          # Show all agents")
    print("  camc logs <id> -f                  # Follow output")
    print("  camc attach <id>                   # Attach to tmux")
    print("  camc stop <id>                     # Stop an agent")


def _want_json(args):
    """Check if --json flag is set."""
    return getattr(args, "json", False)


def _agent_to_cam_json(a):
    """Convert internal agent dict to cam-compatible JSON format."""
    context = a.get("context") or {}
    ctx_name = context.get("name") if isinstance(context, dict) else None
    ctx_host = context.get("host") if isinstance(context, dict) else None
    ctx_port = context.get("port") if isinstance(context, dict) else None
    session = a.get("session", "")
    sock = _find_tmux_socket(session) if session else None
    transport = "ssh" if ctx_host and ctx_host != "localhost" else "local"
    return {
        "id": a.get("id", ""),
        "task": {
            "name": a.get("name") or "",
            "tool": a.get("tool", ""),
            "prompt": a.get("prompt", ""),
            "auto_confirm": True,
            "auto_exit": a.get("auto_exit", False),
        },
        "context_name": ctx_name or "",
        "context_path": a.get("path", ""),
        "transport_type": transport,
        "status": a.get("status", ""),
        "state": a.get("state", ""),
        "tmux_session": session,
        "tmux_socket": sock or "",
        "pid": a.get("monitor_pid"),
        "started_at": a.get("started_at"),
        "completed_at": a.get("completed_at"),
        "exit_reason": a.get("exit_reason"),
        "retry_count": 0,
        "cost_estimate": None,
        "files_changed": [],
    }


def cmd_run(args):
    tool = getattr(args, "tool", None) or "claude"
    prompt = getattr(args, "prompt", "") or ""
    workdir = os.path.abspath(args.path)
    os.makedirs(workdir, exist_ok=True)
    config = _load_config(tool)

    agent_id = uuid4().hex[:8]
    session = "cam-%s" % agent_id
    launch_cmd = _build_command(config, prompt, workdir)

    context = _load_default_context()
    env_setup = context.get("env_setup") or None
    inherit_env = not getattr(args, "no_inherit_env", False)
    print("Starting %s agent %s..." % (tool, agent_id))
    if not create_tmux_session(session, launch_cmd, workdir, env_setup=env_setup, inherit_env=inherit_env):
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

    name = getattr(args, "name", None) or None
    auto_exit = getattr(args, "auto_exit", False)
    store = AgentStore()
    store.save({"id": agent_id, "tool": tool, "session": session, "status": "running",
                "state": "initializing", "prompt": prompt, "path": workdir,
                "name": name, "auto_exit": auto_exit,
                "context": context,
                "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
                "monitor_pid": None})

    # Spawn background monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "camc_pkg", "_monitor", agent_id],
            stdout=subprocess.DEVNULL,
            stderr=open("/tmp/camc-%s.log" % agent_id, "a"),
            start_new_session=True)
        store.update(agent_id, monitor_pid=proc.pid)
    except Exception:
        pass

    print("  ID: %s  Tool: %s  Session: %s" % (agent_id, tool, session))
    if name:
        print("  Name: %s" % name)
    print("  Path: %s" % workdir)
    if prompt:
        print("  Prompt: %s" % (prompt[:60] + "..." if len(prompt) > 60 else prompt))
    else:
        print("  Prompt: (interactive)")
    if auto_exit:
        print("  Auto-exit: ON")
    print()
    print("  Attach: camc attach %s" % agent_id)
    print("  Logs:   camc logs %s -f" % agent_id)


def cmd_list(args):
    store = AgentStore()
    agents = store.list()
    if not agents:
        if _want_json(args):
            print("[]")
        else:
            print("No agents.")
        return

    for a in agents:
        if a["status"] == "running" and not tmux_session_exists(a["session"]):
            state = a.get("state") or "initializing"
            status = "failed" if state == "initializing" else "completed"
            a.update(status=status, exit_reason="Session gone", completed_at=_now_iso())
            store.update(a["id"], **{k: a[k] for k in ("status", "exit_reason", "completed_at")})

    # Apply filters
    status_filter = getattr(args, "status", None)
    if status_filter:
        agents = [a for a in agents if a.get("status") == status_filter]

    # Sort by started_at descending
    agents = sorted(agents, key=lambda x: x.get("started_at", ""), reverse=True)

    # Limit
    last_n = getattr(args, "last", 50) or 50
    agents = agents[:last_n]

    # JSON output — cam-compatible format
    if _want_json(args):
        print(json.dumps([_agent_to_cam_json(a) for a in agents], indent=2))
        return

    colors = {"running": "\033[32m", "completed": "\033[36m", "failed": "\033[31m", "stopped": "\033[33m"}
    reset = "\033[0m"
    use_color = sys.stdout.isatty()

    print("%-10s %-16s %-10s %-12s %-12s %-24s %s" % ("ID", "NAME", "TOOL", "STATUS", "STATE", "PROMPT", "STARTED"))
    print("-" * 100)
    for a in agents:
        s = a.get("status", "?")
        ss = "%s%-12s%s" % (colors.get(s, ""), s, reset) if use_color else "%-12s" % s
        print("%-10s %-16s %-10s %s %-12s %-24s %s" % (
            a.get("id", "?"), (a.get("name") or "")[:16],
            a.get("tool", "?"), ss,
            a.get("state", "") or "", (a.get("prompt", "") or "")[:24],
            _time_ago(a.get("started_at"))))


def cmd_logs(args):
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    tail_n = getattr(args, "tail", 50) or 50
    if args.follow:
        prev = ""
        try:
            while True:
                out = capture_tmux(a["session"], lines=max(tail_n, 200))
                if out != prev:
                    os.system("clear"); print(out); prev = out
                if not tmux_session_exists(a["session"]):
                    print("\n--- session ended ---"); break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        out = capture_tmux(a["session"], lines=tail_n)
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


def cmd_kill(args):
    """Force kill a running agent (sends SIGKILL to tmux session)."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    _kill_monitor(a)
    tmux_kill_session(a["session"])
    store.update(a["id"], status="stopped", exit_reason="Force killed by user", completed_at=_now_iso())
    print("Killed agent %s" % a["id"])


def cmd_add(args):
    if not tmux_session_exists(args.session):
        sys.stderr.write("Error: tmux session '%s' not found\n" % args.session); sys.exit(1)
    store = AgentStore()
    agent_id = uuid4().hex[:8]
    context = _load_default_context()
    store.save({"id": agent_id, "tool": args.tool, "session": args.session, "status": "running",
                "state": None, "prompt": "(adopted)", "path": os.getcwd(),
                "context": context,
                "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
                "monitor_pid": None})
    # Spawn monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "camc_pkg", "_monitor", agent_id],
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
    agent_id = getattr(args, "id", None)
    if not agent_id:
        # No ID given — attach to most recent running agent
        agents = store.list()
        running = [a for a in agents if a.get("status") == "running"]
        running.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        if not running:
            sys.stderr.write("Error: no running agents\n"); sys.exit(1)
        a = running[0]
        print("Attaching to most recent: %s (%s)" % (a.get("name") or a["id"], a["id"]))
    else:
        a = store.get(agent_id)
        if not a:
            sys.stderr.write("Error: agent '%s' not found\n" % agent_id); sys.exit(1)
    sock = _find_tmux_socket(a["session"])
    if sock:
        os.execvp("tmux", ["tmux", "-u", "-S", sock, "attach", "-t", a["session"]])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", a["session"]])


def cmd_status(args):
    """Show detailed agent status / machine-readable JSON status."""
    store = AgentStore()
    filter_id = getattr(args, "agent_id", None) or getattr(args, "id", None)
    agents = store.list()
    if filter_id:
        agents = [a for a in agents if a.get("id", "").startswith(filter_id)]

    req_hash = getattr(args, "hash", None)
    if req_hash:
        # Machine-readable mode with hash-based conditional response
        raw = json.dumps(agents, sort_keys=True)
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        if req_hash == h:
            print(json.dumps({"unchanged": True, "hash": h}))
        else:
            print(json.dumps({"agents": agents, "hash": h}))
        return

    if not agents:
        sys.stderr.write("Error: agent '%s' not found\n" % filter_id if filter_id else "No agents.\n")
        sys.exit(1 if filter_id else 0)

    # Single agent detail view (like cam status)
    if filter_id and len(agents) == 1:
        a = agents[0]
        if _want_json(args):
            print(json.dumps(_agent_to_cam_json(a), indent=2))
            return
        print("Agent: %s" % a.get("id", "?"))
        if a.get("name"):
            print("  Name:      %s" % a["name"])
        print("  Tool:      %s" % a.get("tool", "?"))
        print("  Status:    %s" % a.get("status", "?"))
        print("  State:     %s" % (a.get("state") or "-"))
        print("  Path:      %s" % a.get("path", "?"))
        print("  Session:   %s" % a.get("session", "?"))
        print("  Started:   %s" % (a.get("started_at") or "-"))
        if a.get("completed_at"):
            print("  Completed: %s" % a["completed_at"])
        if a.get("exit_reason"):
            print("  Exit:      %s" % a["exit_reason"])
        prompt = a.get("prompt") or ""
        print("  Prompt:    %s" % (prompt[:80] + "..." if len(prompt) > 80 else prompt or "(interactive)"))
        if a.get("auto_exit"):
            print("  Auto-exit: ON")
        # Check session alive
        alive = tmux_session_exists(a.get("session", ""))
        print("  Session:   %s" % ("alive" if alive else "dead"))
        return

    # Multiple agents — JSON dump
    if _want_json(args):
        print(json.dumps([_agent_to_cam_json(a) for a in agents], indent=2))
    else:
        raw = json.dumps(agents, sort_keys=True)
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        print(json.dumps({"agents": agents, "hash": h}))


def cmd_heal(args):
    """Check running agents and restart dead monitor daemons."""
    store = AgentStore()
    agents = store.list()
    running = [a for a in agents if a.get("status") == "running"]
    if not running:
        print("No running agents.")
        return

    healed = 0
    ok = 0
    for a in running:
        aid = a["id"]
        name = a.get("name") or aid
        session = a.get("session", "")

        # Check if tmux session is alive
        if not tmux_session_exists(session):
            state = a.get("state") or "initializing"
            status = "completed" if state not in ("initializing",) else "failed"
            store.update(aid, status=status, exit_reason="Session gone (heal)", completed_at=_now_iso())
            print("  %s (%s): session dead, marked %s" % (name, aid, status))
            continue

        # Check if monitor process is alive
        monitor_alive = False
        pid = a.get("monitor_pid")
        if pid:
            try:
                os.kill(pid, 0)
                monitor_alive = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if not monitor_alive:
            pid_path = "/tmp/camc-%s.pid" % aid
            if os.path.exists(pid_path):
                try:
                    with open(pid_path) as f:
                        fpid = int(f.read().strip())
                    os.kill(fpid, 0)
                    monitor_alive = True
                except (ValueError, ProcessLookupError, PermissionError, OSError):
                    try:
                        os.unlink(pid_path)
                    except OSError:
                        pass

        if monitor_alive:
            print("  %s (%s): ok" % (name, aid))
            ok += 1
            continue

        # Restart monitor
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "camc_pkg", "_monitor", aid],
                stdout=subprocess.DEVNULL,
                stderr=open("/tmp/camc-%s.log" % aid, "a"),
                start_new_session=True)
            store.update(aid, monitor_pid=proc.pid)
            print("  %s (%s): restarted (PID %d)" % (name, aid, proc.pid))
            healed += 1
        except Exception as e:
            print("  %s (%s): restart failed: %s" % (name, aid, e))

    failed = len(running) - ok - healed
    print("Heal: %d healthy, %d restarted%s" % (ok, healed, ", %d failed" % failed if failed else ""))


def cmd_apply(args):
    """Apply tasks from a YAML file (DAG scheduler)."""
    from camc_pkg.scheduler import SchedulerError, TaskGraph, load_task_file, run_dag

    task_file = args.file
    dry_run = getattr(args, "dry_run", False)
    workdir = os.path.abspath(getattr(args, "path", None) or os.getcwd())

    try:
        tasks, metadata = load_task_file(task_file)
    except SchedulerError as e:
        sys.stderr.write("Error: %s\n" % e); sys.exit(1)

    try:
        graph = TaskGraph(tasks)
    except SchedulerError as e:
        sys.stderr.write("Error: invalid task graph: %s\n" % e); sys.exit(1)

    try:
        results = run_dag(graph, workdir=workdir, dry_run=dry_run)
    except SchedulerError as e:
        sys.stderr.write("Error: %s\n" % e); sys.exit(1)

    # Exit code: non-zero if any task failed
    if results and any(r.get("status") not in ("completed", None) for r in results.values()):
        sys.exit(1)


def cmd_history(args):
    """Show event history for an agent."""
    events = EventStore()
    agent_id = getattr(args, "id", None)
    since = getattr(args, "since", None)
    limit = getattr(args, "limit", 100) or 100
    entries = events.read(agent_id=agent_id, since=since, limit=limit)

    if not entries:
        if _want_json(args):
            print("[]")
        else:
            print("No events%s." % (" for %s" % agent_id if agent_id else ""))
        return

    if _want_json(args):
        print(json.dumps(entries, indent=2))
        return

    # Human-readable table
    colors = {
        "state_change": "\033[36m",
        "auto_confirm": "\033[33m",
        "completed": "\033[32m",
        "monitor_start": "\033[34m",
    }
    reset = "\033[0m"
    use_color = sys.stdout.isatty()

    print("%-10s %-20s %-16s %s" % ("AGENT", "TIME", "EVENT", "DETAIL"))
    print("-" * 80)
    for ev in entries:
        etype = ev.get("type", "?")
        detail = ev.get("detail", {})
        if etype == "state_change":
            detail_str = "%s -> %s" % (detail.get("from", "?"), detail.get("to", "?"))
        elif etype == "auto_confirm":
            resp = detail.get("response", "")
            detail_str = "response=%r" % resp if resp else "(enter)"
        elif etype == "completed":
            detail_str = "%s: %s" % (detail.get("status", "?"), detail.get("reason", "?"))
        else:
            detail_str = json.dumps(detail) if detail else ""

        ts = ev.get("ts", "")[:19].replace("T", " ")
        c = colors.get(etype, "") if use_color else ""
        r = reset if use_color and c else ""
        print("%-10s %-20s %s%-16s%s %s" % (
            ev.get("agent_id", "?")[:8], ts, c, etype, r, detail_str))


def cmd_version(args):
    print("camc v%s" % __version__)
    print()
    print("Supported tools:")
    for key in sorted(_EMBEDDED_CONFIGS):
        name = key.replace(".toml", "")
        path = os.path.join(CONFIGS_DIR, key)
        exists = " (config: %s)" % path if os.path.exists(path) else " (embedded)"
        print("  %s%s" % (name, exists))


def cmd_capture(args):
    """Capture tmux screen output for an agent."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        print("Agent not found: %s" % args.id, file=sys.stderr)
        sys.exit(1)
    session = a.get("session", "")
    if not session:
        print("Agent has no tmux session", file=sys.stderr)
        sys.exit(1)
    lines = getattr(args, "lines", 100) or 100
    output = capture_tmux(session, lines=lines)
    if _want_json(args):
        import hashlib
        h = hashlib.md5(output.encode()).hexdigest()[:8]
        print(json.dumps({"content": output, "hash": h}, indent=2))
    else:
        sys.stdout.write(output)


def cmd_send(args):
    """Send text input to an agent's tmux session."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        print("Agent not found: %s" % args.id, file=sys.stderr)
        sys.exit(1)
    session = a.get("session", "")
    if not session:
        print("Agent has no tmux session", file=sys.stderr)
        sys.exit(1)
    send_enter = not getattr(args, "no_enter", False)
    tmux_send_input(session, args.text, send_enter=send_enter)
    print("Sent.")


def cmd_key(args):
    """Send a special key to an agent's tmux session."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        print("Agent not found: %s" % args.id, file=sys.stderr)
        sys.exit(1)
    session = a.get("session", "")
    if not session:
        print("Agent has no tmux session", file=sys.stderr)
        sys.exit(1)
    sock = _find_tmux_socket(session)
    cmd = ["tmux"]
    if sock:
        cmd += ["-S", sock]
    cmd += ["send-keys", "-t", session, args.key]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=5)
        print("Sent key: %s" % args.key)
    except Exception as e:
        print("Failed to send key: %s" % e, file=sys.stderr)
        sys.exit(1)


# ===========================================================================
# Main
# ===========================================================================

def main():
    p = argparse.ArgumentParser(
        prog="camc",
        description="CAM Client — Standalone coding agent manager. PM2 for AI coding agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  camc init                           First-time setup
  camc run "fix lint errors"          Launch claude (default) with a prompt
  camc run -t codex "add tests"       Launch codex with a prompt
  camc run                            Launch claude interactively
  camc list                           List all agents
  camc list --json                    List agents as JSON
  camc list --status running          List only running agents
  camc logs abc1 -f                   Follow agent output
  camc attach abc1                    Attach to agent tmux
  camc stop abc1                      Gracefully stop an agent
  camc kill abc1                      Force kill an agent
  camc status abc1                    Show detailed agent status
  camc add my-session --tool claude   Adopt existing tmux session
  camc rm abc1 --kill                 Remove and kill agent
  camc apply -f tasks.yaml             Run DAG task file
  camc apply -f tasks.yaml --dry-run  Validate without executing
  camc history abc1                    Show event history for agent
  camc history --since 2026-03-25     Events after date
  camc capture abc1 --lines 50        Capture agent screen output
  camc send abc1 --text "hello"       Send text to agent
  camc key abc1 --key C-c             Send special key to agent
  camc heal                           Restart dead monitors
  camc version                        Show version info""")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output (debug logging)")
    sub = p.add_subparsers(dest="command")

    # init
    init_p = sub.add_parser("init", help="First-time setup")
    init_p.add_argument("--force", "-f", action="store_true", help="Overwrite existing configs")

    # run
    r = sub.add_parser("run", help="Start a coding agent on a task")
    r.add_argument("prompt", nargs="?", default="", help="Task prompt (empty for interactive mode)")
    r.add_argument("--tool", "-t", default="claude", help="Tool name (claude, codex, cursor) [default: claude]")
    r.add_argument("--path", "-p", default=os.getcwd(), help="Working directory")
    r.add_argument("--name", "-n", default=None, help="Human-readable name")
    r.add_argument("--auto-exit", "-a", action="store_true", help="Auto-exit on completion")
    r.add_argument("--no-inherit-env", action="store_true", help="Legacy mode: wrap command with bash -c and env_setup")

    # list
    ls = sub.add_parser("list", aliases=["ls"], help="List agents")
    ls.add_argument("--status", default=None, help="Filter by status")
    ls.add_argument("--last", "-n", type=int, default=50, help="Show last N agents [default: 50]")

    # logs
    l = sub.add_parser("logs", help="View agent output logs")
    l.add_argument("id", help="Agent ID (prefix match)")
    l.add_argument("-f", "--follow", action="store_true", help="Follow output")
    l.add_argument("--tail", "-n", type=int, default=50, help="Last N lines [default: 50]")

    # stop
    s = sub.add_parser("stop", help="Gracefully stop a running agent")
    s.add_argument("id", help="Agent ID")

    # kill
    k = sub.add_parser("kill", help="Force kill a running agent")
    k.add_argument("id", help="Agent ID")

    # add
    a = sub.add_parser("add", help="Adopt existing tmux session")
    a.add_argument("session", help="tmux session name")
    a.add_argument("--tool", "-t", default="claude", help="Tool type")

    # rm
    rm = sub.add_parser("rm", help="Remove a single agent")
    rm.add_argument("id", help="Agent ID")
    rm.add_argument("--kill", "-k", action="store_true", help="Also kill the session")

    # attach
    at = sub.add_parser("attach", help="Attach to an agent's TMUX session (interactive)")
    at.add_argument("id", nargs="?", default=None, help="Agent number, name, or ID")

    # status
    st = sub.add_parser("status", help="Show detailed agent status")
    st.add_argument("agent_id", nargs="?", default=None, help="Agent ID (full or short)")
    st.add_argument("--hash", default=None, help="Return unchanged if hash matches (automation)")

    # apply
    ap = sub.add_parser("apply", help="Run tasks from a YAML file (DAG scheduler)")
    ap.add_argument("--file", "-f", required=True, help="Path to task YAML file")
    ap.add_argument("--path", "-p", default=None, help="Working directory [default: cwd]")
    ap.add_argument("--dry-run", action="store_true", help="Validate and show plan without executing")

    # history
    hi = sub.add_parser("history", help="Show event history")
    hi.add_argument("id", nargs="?", default=None, help="Agent ID (prefix match, omit for all)")
    hi.add_argument("--since", default=None, help="Only events after timestamp (ISO 8601)")
    hi.add_argument("--limit", "-n", type=int, default=100, help="Max events to show [default: 100]")

    # capture
    cap = sub.add_parser("capture", help="Capture agent tmux screen output")
    cap.add_argument("id", help="Agent ID (prefix match)")
    cap.add_argument("--lines", "-n", type=int, default=100, help="Number of lines [default: 100]")

    # send
    snd = sub.add_parser("send", help="Send text input to agent tmux session")
    snd.add_argument("id", help="Agent ID (prefix match)")
    snd.add_argument("--text", "-t", required=True, help="Text to send")
    snd.add_argument("--no-enter", action="store_true", help="Don't send Enter after text")

    # key
    ky = sub.add_parser("key", help="Send a special key to agent tmux session")
    ky.add_argument("id", help="Agent ID (prefix match)")
    ky.add_argument("--key", "-k", required=True, help="Key to send (e.g. C-c, Enter, Escape)")

    # heal
    sub.add_parser("heal", help="Check running agents and restart dead monitor daemons")

    # version
    sub.add_parser("version", help="Show version")

    # Hidden _monitor subcommand
    if len(sys.argv) >= 3 and sys.argv[1] == "_monitor":
        _run_monitor(sys.argv[2])
        return

    args = p.parse_args()
    # Enable debug logging if --verbose
    if getattr(args, "verbose", False):
        logging.getLogger("camc").setLevel(logging.DEBUG)

    cmds = {
        "init": cmd_init,
        "run": cmd_run,
        "list": cmd_list, "ls": cmd_list,
        "logs": cmd_logs,
        "stop": cmd_stop,
        "kill": cmd_kill,
        "add": cmd_add,
        "rm": cmd_rm,
        "attach": cmd_attach,
        "status": cmd_status,
        "apply": cmd_apply,
        "history": cmd_history,
        "capture": cmd_capture,
        "send": cmd_send,
        "key": cmd_key,
        "heal": cmd_heal,
        "version": cmd_version,
    }
    if args.command in cmds:
        cmds[args.command](args)
    else:
        p.print_help()
        sys.exit(1)
