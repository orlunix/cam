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
import socket as _sock
from uuid import uuid4, uuid5, NAMESPACE_DNS as _UUID_NS


def _ensure_logs_on_scratch():
    """Move ~/.cam/logs to scratch if available. Silent, idempotent."""
    logs_dir = os.path.join(os.path.expanduser("~"), ".cam", "logs")
    if os.path.islink(logs_dir):
        return  # Already done
    if not os.path.isdir(logs_dir):
        return  # No logs dir yet
    # Find scratch
    user = os.environ.get("USER", "")
    if not user:
        return
    scratch = None
    # Try ypcat (NIS)
    try:
        out = subprocess.check_output(
            ["ypcat", "-k", "auto.home"], timeout=5, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace")
        for line in out.splitlines():
            if line.startswith("scratch.%s" % user):
                scratch = "/home/%s" % line.split()[0]
                break
    except Exception:
        pass
    # Fallback: check common paths
    if not scratch:
        for suffix in ["_gpu", "", "_gpu_1", "_gpu_2"]:
            candidate = "/home/scratch.%s%s" % (user, suffix)
            if os.path.isdir(candidate):
                scratch = candidate
                break
    if not scratch or not os.path.isdir(scratch):
        return
    dst = os.path.join(scratch, ".cam", "logs")
    try:
        os.makedirs(dst, exist_ok=True)
        subprocess.run(["rsync", "-a", "--quiet", logs_dir + "/", dst + "/"],
                       timeout=120, check=False)
        tmp = "%s.old.%d" % (logs_dir, os.getpid())
        os.rename(logs_dir, tmp)
        os.symlink(dst, logs_dir)
        subprocess.run(["rm", "-rf", tmp], timeout=30, check=False)
        log.info("Moved logs to scratch: %s", dst)
    except Exception:
        pass  # Silent failure — don't break camc startup


def _gen_agent_id():
    """Generate an 8-char agent ID from hostname + time + random."""
    raw = "%s-%s-%s" % (_sock.gethostname(), time.time(), uuid4().hex[:8])
    return uuid5(_UUID_NS, raw).hex[:8]

from camc_pkg import __version__, CAM_DIR, CONFIGS_DIR, CONTEXT_FILE, LOGS_DIR, PIDS_DIR, SOCKETS_DIR, log
from camc_pkg.utils import _now_iso, _time_ago, _load_default_context, _build_command, _kill_monitor, _run
from camc_pkg.adapters import _EMBEDDED_CONFIGS, _load_config
from camc_pkg.storage import AgentStore, EventStore
from camc_pkg.transport import (
    _find_tmux_socket, capture_tmux, tmux_session_exists,
    tmux_send_input, tmux_send_key, tmux_kill_session, create_tmux_session,
)
from camc_pkg.detection import should_auto_confirm, is_ready_for_input
from camc_pkg.monitor import _run_monitor
from camc_pkg.formatters import (
    print_table, print_panel, print_detail, print_success, print_error, print_warning,
    print_info, styled_status, styled_state, _c, _HAS_RICH,
)

# Resolve the camc script path for spawning monitor subprocesses.
# When running as single-file build, sys.argv[0] is the script itself.
# When running as `python -m camc_pkg`, we fall back to `-m camc_pkg`.
_CAMC_SCRIPT = os.path.abspath(sys.argv[0]) if os.path.isfile(sys.argv[0]) else None


def cmd_init(args):
    """Interactive setup wizard."""
    print("camc v%s — Coding Agent Manager (standalone)" % __version__)
    print()

    # 1. Check tmux
    print("Checking dependencies...")
    tmux_path = shutil.which("tmux")
    if tmux_path:
        print_success("tmux: %s" % tmux_path)
    else:
        print_error("tmux: NOT FOUND (required)")
        print("  Install: apt install tmux / brew install tmux")
        sys.exit(1)

    # Check available tools
    for tool_name in ("claude", "codex", "agent"):
        path = shutil.which(tool_name)
        if path:
            print_success("%s: %s" % (tool_name, path))
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
            print_success(filename)

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


def _tf(agent, field, default=""):
    """Get a task sub-field from agent record (supports both old flat and new nested format)."""
    t = agent.get("task")
    if isinstance(t, dict):
        return t.get(field, default)
    return agent.get(field, default)


_SENTINEL = object()


def _sf(agent, field, default=""):
    """Get a session-related field, handling legacy field names."""
    # Unified field names first, then legacy fallbacks
    _LEGACY = {"tmux_session": "session", "context_path": "path", "pid": "monitor_pid"}
    v = agent.get(field, _SENTINEL)
    if v is not _SENTINEL:
        return v
    legacy = _LEGACY.get(field)
    if legacy:
        return agent.get(legacy, default)
    return default


def _agent_to_cam_json(a):
    """Convert agent dict to cam-compatible JSON format.

    With the unified schema, new-format records are already cam-compatible.
    This function handles both legacy (flat fields) and new (nested task) formats.
    """
    # Handle nested task (new format) vs flat fields (legacy)
    task = a.get("task")
    if isinstance(task, dict):
        task_out = dict(task)
    else:
        task_out = {
            "name": a.get("name") or "",
            "tool": a.get("tool", ""),
            "prompt": a.get("prompt", ""),
            "auto_confirm": True,
            "auto_exit": a.get("auto_exit", False),
        }
    # Ensure tags is always present (backward compat)
    if "tags" not in task_out:
        task_out["tags"] = []

    # Session name: unified = tmux_session, legacy = session
    session = a.get("tmux_session") or a.get("session", "")
    sock = a.get("tmux_socket") or (_find_tmux_socket(session) if session else None)

    # Transport: unified has transport_type, legacy derives from context
    transport = a.get("transport_type")
    if not transport:
        context = a.get("context") or {}
        ctx_host = context.get("host") if isinstance(context, dict) else None
        transport = "ssh" if ctx_host and ctx_host not in ("localhost", "127.0.0.1") else "local"

    # Context name: unified has context_name, legacy derives from context dict
    ctx_name = a.get("context_name")
    if not ctx_name:
        context = a.get("context") or {}
        ctx_name = context.get("name", "") if isinstance(context, dict) else ""

    return {
        "id": a.get("id", ""),
        "session_id": a.get("session_id", ""),
        "task": task_out,
        "context_id": a.get("context_id", ""),
        "context_name": ctx_name,
        "context_path": a.get("context_path") or a.get("path", ""),
        "transport_type": transport,
        "status": a.get("status", ""),
        "state": a.get("state", ""),
        "tmux_session": session,
        "tmux_socket": sock or "",
        "pid": a.get("pid") if a.get("pid") is not None else a.get("monitor_pid"),
        "hostname": a.get("hostname", ""),
        "started_at": a.get("started_at"),
        "completed_at": a.get("completed_at"),
        "exit_reason": a.get("exit_reason"),
        "retry_count": a.get("retry_count", 0),
        "cost_estimate": a.get("cost_estimate"),
        "files_changed": a.get("files_changed", []),
    }


def _preflight(tool, tool_binary, workdir, env_setup=None):
    """Run environment checks before launching an agent.

    Returns a list of (level, message) tuples where level is 'error' or 'warn'.
    'error' items should block startup; 'warn' items are advisory.
    """
    issues = []

    # 1. tmux available?
    if not shutil.which("tmux"):
        issues.append(("error", "tmux not found in PATH. Install: apt install tmux / brew install tmux"))

    # 2. Tool binary in PATH? Skip if env_setup is configured — that script
    # will set PATH inside tmux, so the caller's PATH is irrelevant.
    if tool_binary and not env_setup and not shutil.which(tool_binary):
        hints = {
            "claude": "npm install -g @anthropic-ai/claude-code",
            "codex": "npm install -g @openai/codex",
        }
        hint = hints.get(tool, "ensure '%s' is in your PATH" % tool_binary)
        issues.append(("error", "'%s' not found in PATH. Install: %s" % (tool_binary, hint)))

    # 3. Working directory accessible?
    if not os.path.isdir(workdir):
        try:
            os.makedirs(workdir, exist_ok=True)
        except OSError as e:
            issues.append(("error", "Cannot create working directory %s: %s" % (workdir, e)))
    elif not os.access(workdir, os.R_OK):
        issues.append(("error", "Working directory not readable: %s" % workdir))

    # 4. Socket directory writable?
    try:
        os.makedirs(SOCKETS_DIR, exist_ok=True)
        test_path = os.path.join(SOCKETS_DIR, ".preflight-test")
        with open(test_path, "w") as f:
            f.write("")
        os.remove(test_path)
    except OSError as e:
        issues.append(("warn", "Socket directory not writable (%s): %s" % (SOCKETS_DIR, e)))

    # 5. Log directory writable?
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        test_path = os.path.join(LOGS_DIR, ".preflight-test")
        with open(test_path, "w") as f:
            f.write("")
        os.remove(test_path)
    except OSError as e:
        issues.append(("warn", "Log directory not writable (%s): %s — monitor logging will fail" % (LOGS_DIR, e)))

    # 6. Agent store writable?
    store_path = os.path.join(CAM_DIR, "agents.json")
    try:
        os.makedirs(CAM_DIR, exist_ok=True)
        # Just check we can open for append (doesn't corrupt existing data)
        with open(store_path, "a"):
            pass
    except OSError as e:
        issues.append(("warn", "Agent store not writable (%s): %s" % (store_path, e)))

    return issues


def cmd_run(args):
    tool = getattr(args, "tool", None) or "claude"
    prompt = getattr(args, "prompt", "") or ""
    workdir = os.path.abspath(args.path)
    os.makedirs(workdir, exist_ok=True)
    config = _load_config(tool)

    context = _load_default_context()
    env_setup = context.get("env_setup") or None

    # Preflight environment checks
    tool_binary = config.command[0] if config.command else None
    issues = _preflight(tool, tool_binary, workdir, env_setup=env_setup)
    has_error = False
    for level, msg in issues:
        if level == "error":
            print_error(msg)
            has_error = True
        else:
            print_warning(msg)
    if has_error:
        sys.exit(1)

    agent_id = _gen_agent_id()
    session = "cam-%s" % agent_id

    resume_session = getattr(args, "resume_session", None)

    # Guard: refuse to resume a session if another Claude is still using it.
    # Two Claude processes on the same session corrupt the same .jsonl file.
    if resume_session and tool == "claude":
        in_use = _find_session_in_use(resume_session)
        if in_use:
            print_error("Cannot resume: session %s is still in use by PID(s) %s" %
                        (resume_session, ", ".join(str(p) for p in in_use)))
            print_error("Run 'camc exit <id>' on the existing agent first.")
            sys.exit(1)

    # Session ID tracking: deterministic UUID from agent ID (Claude only).
    # When resuming, we don't mint a new one — Claude reuses the resumed session.
    session_uuid = ""
    if tool == "claude":
        if resume_session:
            session_uuid = resume_session
        else:
            session_uuid = "%s-0000-0000-0000-000000000000" % agent_id

    launch_cmd = _build_command(config, prompt, workdir)

    # Inject --session-id or --resume into Claude launch command
    if tool == "claude":
        if resume_session:
            launch_cmd += ["--resume", resume_session]
        elif session_uuid:
            launch_cmd += ["--session-id", session_uuid]

    inherit_env = not getattr(args, "no_inherit_env", False)
    # Ensure dirs exist
    for d in (LOGS_DIR, PIDS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    print("Starting %s agent %s..." % (tool, agent_id))
    if not create_tmux_session(session, launch_cmd, workdir, env_setup=env_setup, inherit_env=inherit_env):
        print_error("Failed to create tmux session for '%s'" % session)
        print_info("Debug: tmux -u -S %s/%s.sock new-session -d -s %s -c %s" % (SOCKETS_DIR, session, session, workdir))
        sys.exit(1)

    name = getattr(args, "name", None) or os.path.basename(workdir) or "%s-%s" % (tool, uuid4().hex[:6])
    auto_exit = getattr(args, "auto_exit", False)
    auto_exit_enable = getattr(args, "auto_exit_enable", False)
    ctx_name = context.get("name", "") if isinstance(context, dict) else ""
    ctx_host = context.get("host") if isinstance(context, dict) else None
    transport = "ssh" if ctx_host and ctx_host not in ("localhost", "127.0.0.1") else "local"
    tags = getattr(args, "tag", None) or []
    store = AgentStore()
    store.save({
        "id": agent_id,
        "session_id": session_uuid,
        "task": {"name": name or "", "tool": tool, "prompt": prompt,
                 "auto_confirm": True, "auto_exit": auto_exit,
                 "auto_exit_enable": auto_exit_enable,
                 "tags": tags},
        "context_id": "",
        "context_name": ctx_name,
        "context_path": workdir,
        "transport_type": transport,
        "status": "running",
        "state": "initializing",
        "tmux_session": session,
        "tmux_socket": "",
        "pid": None,
        "hostname": _sock.gethostname(),
        "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
        "retry_count": 0, "cost_estimate": None, "files_changed": [],
    })

    # Startup: wait for readiness, auto-confirm via "1"+BSpace, send prompt
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
                # Pattern matched a confirm dialog — send probe_char to select
                # option. No Enter (avoids side effects), no BSpace (dialog
                # consumes the char). probe_char is "1" for Claude (selects
                # option 1 = Yes/Allow), configurable per adapter.
                tmux_send_input(session, config.probe_char, send_enter=False)
                confirmed = True
                time.sleep(3); elapsed += 3
                continue
            if is_ready_for_input(output, config):
                break
        if prompt.strip():
            tmux_send_input(session, prompt, send_enter=True)

    # Spawn background monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, _CAMC_SCRIPT, "_monitor", agent_id] if _CAMC_SCRIPT else [sys.executable, "-m", "camc_pkg", "_monitor", agent_id],
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(LOGS_DIR, "monitor-%s.stderr" % agent_id), "a"),
            start_new_session=True)
        store.update(agent_id, pid=proc.pid)
    except Exception as e:
        print_warning("Monitor failed to start: %s" % e)
        print_warning("Agent is running but auto-confirm/idle detection won't work")
        print_warning("Check: camc logs %s -f" % agent_id)

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


def _fmt_size(nbytes):
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return "%.0f %s" % (nbytes, unit) if unit == "B" else "%.1f %s" % (nbytes, unit)
        nbytes /= 1024.0
    return "%.1f TB" % nbytes


def _iso_from_ts(ts):
    """Convert a Unix timestamp to ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_same_host(a_hostname, my_hostname):
    """Compare hostnames tolerating FQDN vs short name differences."""
    if not a_hostname or not my_hostname:
        return True  # assume local if unknown
    if a_hostname == my_hostname:
        return True
    # Compare short hostname (before first dot)
    return a_hostname.split(".")[0] == my_hostname.split(".")[0]


def cmd_list(args):
    my_hostname = _sock.gethostname()
    store = AgentStore()
    agents = store.list()

    # In shared-disk clusters, filter to agents belonging to this host.
    # Agents without a hostname field are assumed to be local (pre-upgrade).
    agents = [a for a in agents if _is_same_host(a.get("hostname"), my_hostname)]

    if not agents:
        if _want_json(args):
            print("[]")
        else:
            print("No agents.")
        return

    # Apply filters
    status_filter = getattr(args, "status", None)
    if status_filter:
        agents = [a for a in agents if a.get("status") == status_filter]

    tag_filter = getattr(args, "tag", None)
    if tag_filter:
        agents = [a for a in agents
                  if tag_filter in (_tf(a, "tags") if isinstance(_tf(a, "tags"), list) else [])]

    # Sort: tagged agents first (by tags[0] alpha), then untagged.
    # Within each group, by started_at descending.
    def _sort_key(a):
        tags = _tf(a, "tags")
        tags = tags if isinstance(tags, list) else []
        first_tag = tags[0].lower() if tags else ""
        started = a.get("started_at", "")
        # tagged=0 (first), untagged=1 (after); then by tag alpha, then newest first
        return (0 if tags else 1, first_tag, "" if not started else chr(127) + started[::-1])
    agents = sorted(agents, key=_sort_key)

    # Limit
    last_n = getattr(args, "last", 50) or 50
    agents = agents[:last_n]

    # JSON output — cam-compatible format
    if _want_json(args):
        print(json.dumps([_agent_to_cam_json(a) for a in agents], indent=2))
        return

    any_tags = any(isinstance(_tf(a, "tags"), list) and _tf(a, "tags") for a in agents)
    rows = []
    for a in agents:
        tags = _tf(a, "tags")
        tag_str = ",".join(tags) if isinstance(tags, list) else ""
        row = [
            a.get("id", "?")[:8],
            (_tf(a, "name") or "")[:16],
        ]
        if any_tags:
            row.append(tag_str[:20])
        row += [
            _tf(a, "tool", "?"),
            styled_status(a.get("status", "?")),
            styled_state(a.get("state")),
            (_tf(a, "prompt") or "")[:24],
            _time_ago(a.get("started_at")),
        ]
        rows.append(row)
    if any_tags:
        headers = ["ID", "NAME", "TAG", "TOOL", "STATUS", "STATE", "PROMPT", "STARTED"]
        col_styles = {0: "dim", 1: "bold", 2: "cyan", 4: None, 7: "dim"}
    else:
        headers = ["ID", "NAME", "TOOL", "STATUS", "STATE", "PROMPT", "STARTED"]
        col_styles = {0: "dim", 1: "bold", 3: None, 6: "dim"}
    print_table(headers, rows, title="Agents", col_styles=col_styles)


def cmd_logs(args):
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    tail_n = getattr(args, "tail", 50) or 50
    session = _sf(a, "tmux_session")
    if args.follow:
        prev = ""
        try:
            while True:
                out = capture_tmux(session, lines=max(tail_n, 200))
                if out != prev:
                    os.system("clear"); print(out); prev = out
                if not tmux_session_exists(session):
                    print("\n--- session ended ---"); break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        out = capture_tmux(session, lines=tail_n)
        print(out or ("(session not found)" if not tmux_session_exists(session) else "(no output)"))


def cmd_stop(args):
    """Kill the tool process (e.g. Claude) but leave the tmux session alive.

    The shell in tmux can then be reused with `camc run --resume` to pick up
    the conversation, or adopted as a fresh agent. Use `camc kill` to also
    tear down tmux.
    """
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    session = _sf(a, "tmux_session")
    killed_pid = None
    if session and tmux_session_exists(session):
        pid = _find_claude_pid(session)
        if pid:
            try:
                os.kill(pid, 9)
                killed_pid = pid
            except OSError as e:
                print_warning("Failed to kill PID %d: %s" % (pid, e))
        else:
            print_warning("No tool process found inside tmux session '%s'" % session)
    else:
        print_warning("tmux session not found; marking agent stopped anyway")
    _kill_monitor(a)
    store.update(a["id"], status="stopped", exit_reason="Stopped by user", completed_at=_now_iso())
    if killed_pid:
        print("Stopped agent %s (killed PID %d, tmux session still alive)" % (a["id"], killed_pid))
    else:
        print("Stopped agent %s" % a["id"])


def cmd_exit(args):
    """Gracefully ask the tool (Claude) to /exit. Leaves tmux alive so you can resume."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    session = _sf(a, "tmux_session")
    if not session or not tmux_session_exists(session):
        print_error("tmux session not found for agent %s" % a["id"])
        sys.exit(1)
    print_info("Sending graceful exit to agent %s..." % a["id"])
    clean = graceful_exit(session)
    _kill_monitor(a)
    store.update(a["id"], status="stopped",
                 exit_reason="Exited cleanly" if clean else "Exited (process killed)",
                 completed_at=_now_iso())
    if clean:
        print_success("Agent %s exited cleanly (tmux session still alive)" % a["id"])
    else:
        print_warning("Agent %s did not exit cleanly; process was killed (tmux alive)" % a["id"])


def cmd_kill(args):
    """Force kill a running agent (tears down entire tmux session)."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    _kill_monitor(a)
    tmux_kill_session(_sf(a, "tmux_session"))
    store.update(a["id"], status="killed", exit_reason="Force killed by user", completed_at=_now_iso())
    print("Killed agent %s" % a["id"])


def cmd_update(args):
    """Update agent properties (name, auto_confirm)."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)

    changed = False
    task = a.get("task")
    if not isinstance(task, dict):
        # Legacy flat format — wrap into task
        task = {"name": a.get("name", ""), "tool": a.get("tool", "claude"),
                "prompt": a.get("prompt", ""), "auto_confirm": a.get("auto_confirm", True),
                "auto_exit": a.get("auto_exit", False)}

    if args.name is not None:
        task["name"] = args.name
        print_info("Updated name to: %s" % args.name)
        changed = True
    if args.auto_confirm is not None:
        task["auto_confirm"] = args.auto_confirm
        print_info("Updated auto_confirm to: %s" % args.auto_confirm)
        changed = True

    # Tag operations
    tags = task.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    add_tags = getattr(args, "tag", None) or []
    rm_tags = getattr(args, "untag", None) or []
    for t in add_tags:
        if t not in tags:
            tags.append(t)
            print_info("Added tag: %s" % t)
            changed = True
    for t in rm_tags:
        if t in tags:
            tags.remove(t)
            print_info("Removed tag: %s" % t)
            changed = True
    task["tags"] = tags

    if not changed:
        print_error("Nothing to update. Use --name, --auto-confirm, --tag, or --untag.")
        sys.exit(1)

    a["task"] = task
    store.save(a)
    print_success("Agent %s updated" % a["id"])


def cmd_add(args):
    if not tmux_session_exists(args.session):
        sys.stderr.write("Error: tmux session '%s' not found\n" % args.session); sys.exit(1)
    store = AgentStore()

    # Check if an agent with this tmux_session already exists — reuse it
    # instead of creating a duplicate. This preserves name/prompt/task info
    # when re-adopting sessions (e.g. after NFS wipe or heal recovery).
    existing = None
    for a in store.list():
        if _sf(a, "tmux_session") == args.session:
            existing = a
            break

    if existing:
        agent_id = existing["id"]
        # Reactivate: set running, clear completion fields, update hostname
        store.update(agent_id, status="running", state="idle",
                     completed_at=None, exit_reason=None,
                     hostname=_sock.gethostname())
        # Update name if --name provided
        if getattr(args, "name", None):
            task = existing.get("task", {})
            if isinstance(task, dict):
                task["name"] = args.name
                store.update(agent_id, task=task)
        print("Re-adopted '%s' as agent %s" % (args.session, agent_id))
    else:
        agent_id = _gen_agent_id()
        context = _load_default_context()
        ctx_name = context.get("name", "") if isinstance(context, dict) else ""
        name = getattr(args, "name", None) or ""
        cwd = os.getcwd()
        # Capture Claude session UUID before any subsequent fallback logic
        # (see cmd_heal for why — reboot needs it).
        claude_pid = _find_claude_pid(args.session)
        session_uuid = _find_session_id(
            agent_id, claude_pid, workdir=cwd, tmux_session=args.session,
        ) or ""
        store.save({
            "id": agent_id,
            "session_id": session_uuid,
            "task": {"name": name, "tool": args.tool, "prompt": "(adopted)",
                     "auto_confirm": True, "auto_exit": False},
            "context_id": "",
            "context_name": ctx_name,
            "context_path": cwd,
            "transport_type": "local",
            "status": "running",
            "state": None,
            "tmux_session": args.session,
            "tmux_socket": "",
            "pid": None,
            "hostname": _sock.gethostname(),
            "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
            "retry_count": 0, "cost_estimate": None, "files_changed": [],
        })
        print("Adopted '%s' as agent %s (tool=%s)" % (args.session, agent_id, args.tool))
        if session_uuid:
            print("  Session ID: %s" % session_uuid)

    # Spawn monitor (if not already running)
    existing_pid = (existing or {}).get("pid")
    if existing_pid:
        try:
            os.kill(existing_pid, 0)
            print("  Monitor already running (PID %d)" % existing_pid)
            return
        except OSError:
            pass
    try:
        proc = subprocess.Popen(
            [sys.executable, _CAMC_SCRIPT, "_monitor", agent_id] if _CAMC_SCRIPT else [sys.executable, "-m", "camc_pkg", "_monitor", agent_id],
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(LOGS_DIR, "monitor-%s.stderr" % agent_id), "a"),
            start_new_session=True)
        store.update(agent_id, pid=proc.pid)
    except Exception:
        pass


def cmd_rm(args):
    """Remove an agent record. Always tears down the tmux session and socket.

    Previously the caller had to pass --kill to also destroy tmux; without
    it, the session and socket survived and would be re-adopted as an
    orphan on the next `camc heal` pass. There's no sensible use case for
    "forget the record but keep the session around" — if you want the
    session, don't run rm. So kill is now unconditional, and --kill is
    accepted but is a no-op for backward compatibility.
    """
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    _kill_monitor(a)
    session = _sf(a, "tmux_session")
    if session:
        tmux_kill_session(session)
        # tmux usually removes its socket when the server exits, but on some
        # filesystems (NFS) the file lingers. Unlink explicitly so heal's
        # orphan scan doesn't re-adopt the dead session later.
        sock_path = _find_tmux_socket(session)
        if sock_path:
            try:
                os.unlink(sock_path)
            except OSError:
                pass
    store.remove(a["id"])
    print("Removed agent %s" % a["id"])


def cmd_prune(args):
    """Remove all non-running agents (stopped, killed, completed, failed).

    Skips agents whose tmux session is still alive — they may be
    interactive agents that just finished a task but are still usable.
    """
    store = AgentStore()
    agents = store.list()
    removed = 0
    skipped = 0
    for a in agents:
        if a.get("status") not in ("running", "starting", "pending"):
            # Check if tmux session is still alive before pruning
            session = _sf(a, "tmux_session")
            if session:
                sock = _find_tmux_socket(session)
                if sock:
                    cmd = ["tmux", "-S", sock, "has-session", "-t", session]
                    try:
                        subprocess.run(cmd, check=True, capture_output=True, timeout=3)
                        # Session alive — don't prune, fix status instead
                        a["status"] = "running"
                        store.save(a)
                        skipped += 1
                        continue
                    except Exception:
                        pass  # Session dead, safe to prune
            store.remove(a["id"])
            removed += 1
    msg = "Pruned %d non-running agent%s" % (removed, "s" if removed != 1 else "")
    if skipped:
        msg += " (%d restored to running — tmux session still alive)" % skipped
    print(msg)


def _find_session_in_use(session_id):
    """Return PIDs of any running claude process that has session_id as an arg.

    Used to prevent resuming a Claude session that another process still owns —
    two writers on the same .jsonl session file will corrupt it.

    Match is strict: session_id must appear as a standalone argv entry (i.e.
    the value side of `--session-id SID` / `--resume SID`), not as a substring.
    This avoids false positives on shell wrappers that source files under
    ~/.claude/ or happen to have the SID embedded in a larger eval string.
    """
    pids = []
    if not session_id:
        return pids
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as f:
                    raw = f.read().decode("utf-8", errors="replace")
            except (OSError, IOError):
                continue
            parts = [p for p in raw.split("\x00") if p]
            if not parts:
                continue
            # Session ID must be a standalone argv entry (the value of
            # --session-id or --resume), not a substring of a larger arg.
            if session_id not in parts:
                continue
            # And argv[0] must actually look like a claude launch — either
            # the binary basename is "claude", or it's a node/python wrapper
            # whose argv[1] ends in "claude".
            exe_base = os.path.basename(parts[0]).lower()
            looks_like_claude = (
                exe_base == "claude"
                or (len(parts) > 1 and os.path.basename(parts[1]).lower().endswith("claude"))
            )
            if not looks_like_claude:
                continue
            try:
                pids.append(int(entry))
            except ValueError:
                pass
    except OSError:
        pass
    return pids


_UUID_RE_STR = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _extract_session_from_cmdline(claude_pid):
    """Layer 1a: read --session-id / --resume argument from Claude's cmdline.

    Most reliable per-process signal — cmdline is set at exec time and cannot
    change. camc always passes one of these flags to Claude, so any Claude
    process we manage will expose its session UUID here.
    Returns UUID string or None.
    """
    import re
    if not claude_pid:
        return None
    try:
        with open("/proc/%s/cmdline" % claude_pid, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
    except (OSError, IOError):
        return None
    parts = [p for p in raw.split("\x00") if p]
    pat = re.compile(r"^" + _UUID_RE_STR + r"$")
    for i, p in enumerate(parts):
        # `--session-id=UUID` / `--resume=UUID`
        if p.startswith("--session-id=") or p.startswith("--resume="):
            val = p.split("=", 1)[1]
            if pat.match(val):
                return val
        # `--session-id UUID` / `--resume UUID`
        if p in ("--session-id", "--resume") and i + 1 < len(parts):
            val = parts[i + 1]
            if pat.match(val):
                return val
    return None


def _extract_session_from_fd(claude_pid):
    """Layer 1b: read Claude's open file descriptors for a session UUID.

    Claude Code v2.1.114+ keeps ~/.claude/tasks/<uuid>/.lock open. Extract the
    uuid from any fd target matching that pattern. Linux-only (/proc/*/fd).
    Returns UUID string or None.
    """
    import re
    if not claude_pid:
        return None
    fd_dir = "/proc/%s/fd" % claude_pid
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return None
    pat = re.compile(r"\.claude/tasks/(" + _UUID_RE_STR + r")")
    for fd in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        m = pat.search(target)
        if m:
            return m.group(1)
    return None


def _project_dirs_for_workdir(workdir):
    """Candidate ~/.claude/projects/<encoded>/ dirs for a given workdir.

    Claude's canonical encoding replaces every '/', '.', and '_' with '-'
    (e.g. /home/scratch.hren_gpu_1/test → -home-scratch-hren-gpu-1-test).
    Older sessions used more conservative encodings that kept dots and/or
    underscores, so we also try those variants so heal/reboot can find
    pre-existing project dirs.
    """
    wd = workdir.strip("/")
    # Ordered from most-aggressive (current) to most-conservative (legacy).
    variants = [
        wd.replace("/", "-").replace(".", "-").replace("_", "-"),  # canonical
        wd.replace("/", "-").replace(".", "-"),                    # dots only
        wd.replace("/", "-"),                                      # dashes only
    ]
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    seen, out = set(), []
    for enc in variants:
        path = os.path.join(base, "-" + enc)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _get_tmux_pane_cwd(session):
    """Return the live working directory of the tmux pane, or None.

    Agents often `cd` during execution, so the pane's current path diverges
    from the context_path recorded when the agent was created. This live
    value is what Claude uses to pick its ~/.claude/projects/<enc>/ dir.
    """
    if not session:
        return None
    sock = _find_tmux_socket(session)
    if not sock:
        return None
    try:
        rc, out = _run(
            ["tmux", "-S", sock, "display-message", "-t", session,
             "-p", "#{pane_current_path}"],
            timeout=5,
        )
    except Exception:
        return None
    if rc != 0:
        return None
    cwd = (out or "").strip()
    return cwd or None


def _extract_session_from_project_dir(workdir):
    """Layer 2: most-recently-modified <uuid>/ subdir under the project dir.

    Each live Claude session creates ~/.claude/projects/<enc>/<uuid>/ (with
    subagents/, tool-results/). Returns newest UUID or None.
    """
    import re
    pat = re.compile(r"^" + _UUID_RE_STR + r"$")
    best = None  # (mtime, uuid)
    for proj_dir in _project_dirs_for_workdir(workdir):
        if not os.path.isdir(proj_dir):
            continue
        try:
            entries = os.listdir(proj_dir)
        except OSError:
            continue
        for entry in entries:
            if entry == "memory" or not pat.match(entry):
                continue
            full = os.path.join(proj_dir, entry)
            if not os.path.isdir(full):
                continue
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, entry)
    return best[1] if best else None


def _extract_session_from_jsonl(workdir):
    """Layer 3: newest .jsonl file under the project dir (legacy fallback)."""
    best = None  # (mtime, uuid)
    for proj_dir in _project_dirs_for_workdir(workdir):
        if not os.path.isdir(proj_dir):
            continue
        try:
            entries = os.listdir(proj_dir)
        except OSError:
            continue
        for f in entries:
            if not f.endswith(".jsonl"):
                continue
            full = os.path.join(proj_dir, f)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            uid = f[:-6]  # strip .jsonl
            if best is None or mtime > best[0]:
                best = (mtime, uid)
    return best[1] if best else None


def _find_session_id_pid(claude_pid):
    """Return a PID-authoritative session UUID, or None.

    Only uses signals that identify *this specific* Claude process — cmdline
    args and open fds. Never falls back to workdir-based guessing (which
    collides when multiple agents share a cwd). Use this when the alternative
    to "no answer" is "potentially wrong answer" — e.g. backfilling session
    IDs for many agents in heal.
    """
    if not claude_pid:
        return None
    return (
        _extract_session_from_cmdline(claude_pid)
        or _extract_session_from_fd(claude_pid)
    )


def _find_session_id(agent_id, claude_pid, workdir=None, tmux_session=None):
    """Find the Claude session UUID for an adopted/resumed agent.

    Layered fallback. PID-based signals come first (authoritative for the
    specific Claude process). Workdir-based signals are attempted last and
    should be treated as best-effort guesses — two agents sharing a workdir
    will appear identical to these layers.

      1a. /proc/<pid>/cmdline — --session-id / --resume arg
      1b. /proc/<pid>/fd — ~/.claude/tasks/<uuid>/.lock
      2.  ~/.claude/projects/<enc>/<uuid>/ subdirectory (newest mtime)
      3.  ~/.claude/projects/<enc>/<uuid>.jsonl (newest mtime)
      4.  synthesize deterministic UUID from agent_id
    """
    sid = _find_session_id_pid(claude_pid)
    if sid:
        return sid

    # Gather candidate workdirs. Pane CWD is tried first because Claude
    # often cd's into a sub-project after launch and writes its session file
    # under the *new* workdir's project dir, not the one we recorded.
    workdirs = []
    pane_cwd = _get_tmux_pane_cwd(tmux_session) if tmux_session else None
    if pane_cwd:
        workdirs.append(pane_cwd)
    if workdir and workdir not in workdirs:
        workdirs.append(workdir)

    # Layer 2: project subdir
    for wd in workdirs:
        sid = _extract_session_from_project_dir(wd)
        if sid:
            return sid

    # Layer 3: jsonl
    for wd in workdirs:
        sid = _extract_session_from_jsonl(wd)
        if sid:
            return sid

    # Layer 4: synthetic last resort
    if agent_id:
        return "%s-0000-0000-0000-000000000000" % agent_id
    return None


def _find_claude_pid(session):
    """Find the Claude process PID inside a tmux session. Returns int or None."""
    sock = _find_tmux_socket(session)
    if not sock:
        return None
    try:
        rc, out = _run(["tmux", "-S", sock, "list-panes", "-t", session, "-F", "#{pane_pid}"])
        if rc != 0 or not out.strip():
            return None
        pane_pid = out.strip()
        result = subprocess.run(["pgrep", "-P", pane_pid], capture_output=True, timeout=3)
        for child in result.stdout.decode().split():
            child = child.strip()
            if not child:
                continue
            try:
                with open("/proc/%s/comm" % child) as f:
                    if "claude" in f.read().lower():
                        return int(child)
            except Exception:
                pass
    except Exception:
        pass
    return None


def graceful_exit(session, timeout=15):
    """Safely exit Claude Code in a tmux session.

    Sequence:
      1. Esc x3 (interrupt sub-agents / tool calls)
      2. Ctrl+C (interrupt generating response)
      3. Wait, verify idle (❯ prompt on screen)
      4. /exit (clean exit)
      5. If still alive → kill Claude process (not tmux)
    Returns True if exited cleanly, False if had to kill process.
    Does NOT kill the tmux session. Does NOT touch agents.json.
    """
    # Step 1: Esc x3 — interrupt any sub-agent / tool call
    for _ in range(3):
        tmux_send_key(session, "Escape")
        time.sleep(0.5)

    # Step 2: Ctrl+C — interrupt generating response
    tmux_send_key(session, "C-c")
    time.sleep(2)

    # Step 3: Verify idle — check for ❯ prompt
    output = capture_tmux(session)
    lines = output.strip().splitlines() if output else []
    last_lines = "\n".join(lines[-5:]) if lines else ""
    if "❯" not in last_lines:
        # Not idle yet, wait a bit more
        time.sleep(3)

    # Step 4: /exit
    tmux_send_input(session, "/exit", send_enter=True)
    for _ in range(10):
        time.sleep(1)
        if not _find_claude_pid(session):
            return True

    # Step 5: Kill Claude process (not tmux)
    pid = _find_claude_pid(session)
    if pid:
        try:
            os.kill(pid, 9)
            time.sleep(1)
        except Exception:
            pass
        if not _find_claude_pid(session):
            return False  # killed, not clean exit

    return False


def cmd_migrate(args):
    """Restart an agent with session resume (preserves conversation history)."""
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id)
        sys.exit(1)

    tool = _tf(a, "tool", "claude")
    if tool != "claude":
        print_error("Reboot with session resume only works with Claude agents (got: %s)" % tool)
        sys.exit(1)

    old_id = a["id"]
    old_session = _sf(a, "tmux_session")
    name = _tf(a, "name") or old_id
    workdir = _sf(a, "context_path", os.getcwd())
    old_session_id = a.get("session_id", "")
    # If session_id is the synthetic deterministic form, treat it as absent so
    # the 4-layer extractor can try to find the real Claude session UUID.
    synthetic = old_session_id == "%s-0000-0000-0000-000000000000" % old_id
    if not old_session_id or synthetic:
        claude_pid = _find_claude_pid(old_session) if old_session else None
        found = _find_session_id(
            old_id, claude_pid, workdir=workdir, tmux_session=old_session,
        )
        # _find_session_id returns the synthetic form as a last resort — accept
        # only if it's a real session (either from fd, or at least a uuid that
        # isn't all zeros after the agent-id prefix).
        if found and found != "%s-0000-0000-0000-000000000000" % old_id:
            old_session_id = found
            log.info("Recovered session_id for %s via 4-layer extract: %s", old_id, old_session_id)
    tags = _tf(a, "tags")
    tags = tags if isinstance(tags, list) else []

    print_info("Rebooting agent %s (%s)..." % (name, old_id))

    # 1. Graceful exit — same as `camc exit`. Claude goes away, tmux stays.
    if not (old_session and tmux_session_exists(old_session)):
        print_error("No tmux session found for agent")
        sys.exit(1)
    print_info("Sending graceful exit sequence...")
    clean = graceful_exit(old_session)
    print_info("Agent exited cleanly" if clean else "Agent killed (exit not clean)")
    # Let the shell prompt settle before typing into it.
    time.sleep(2)

    # 2. Guard against the session file still being held. graceful_exit kills
    # the process, but if anything else somehow is still writing that .jsonl,
    # two writers will corrupt it. Same invariant as `camc run --resume`.
    if old_session_id:
        in_use = _find_session_in_use(old_session_id)
        if in_use:
            print_error("Cannot resume: session %s still in use by PID(s) %s" %
                        (old_session_id, ", ".join(str(p) for p in in_use)))
            sys.exit(1)

    # 3. Kill old monitor (new one will be spawned after relaunch).
    _kill_monitor(a)

    # 4. Relaunch Claude in the SAME tmux session: cd + claude --resume.
    new_session_uuid = "%s-0000-0000-0000-000000000000" % old_id
    config = _load_config(tool)
    launch_cmd = _build_command(config, "", workdir)
    if old_session_id:
        launch_cmd += ["--resume", old_session_id]
    else:
        launch_cmd += ["--session-id", new_session_uuid]

    import shlex
    cmd_str = "cd %s && %s" % (shlex.quote(workdir), " ".join(shlex.quote(arg) for arg in launch_cmd))
    tmux_send_input(old_session, cmd_str, send_enter=True)

    # 5. Update agent record (same ID, preserve session_id).
    if not old_session_id:
        a["session_id"] = new_session_uuid
    a["status"] = "running"
    a["state"] = "initializing"
    a.get("task", {})["prompt"] = "(resumed)"
    store.save(a)

    # 5. Restart monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, _CAMC_SCRIPT, "_monitor", old_id] if _CAMC_SCRIPT else [sys.executable, "-m", "camc_pkg", "_monitor", old_id],
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(LOGS_DIR, "monitor-%s.stderr" % old_id), "a"),
            start_new_session=True)
        store.update(old_id, pid=proc.pid)
    except Exception as e:
        print_warning("Monitor failed to start: %s" % e)

    print_success("Agent rebooted: %s (%s)" % (name, old_id))
    if old_session_id:
        print("  Resumed from: %s" % old_session_id)
    print("  Session ID: %s" % new_session_uuid)
    print()
    print("  Attach: camc attach %s" % old_id)


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
        print("Attaching to most recent: %s (%s)" % (_tf(a, "name") or a["id"], a["id"]))
    else:
        a = store.get(agent_id)
        if not a:
            sys.stderr.write("Error: agent '%s' not found\n" % agent_id); sys.exit(1)
    session = _sf(a, "tmux_session")
    sock = _find_tmux_socket(session)
    # If the session is unreachable — socket missing, or socket present but
    # the tmux server behind it is dead — say so instead of handing a bare
    # `tmux attach` to the default socket (which just prints "no sessions"
    # with no hint why).
    if not tmux_session_exists(session):
        aid = a.get("id", "")
        reason = "stale tmux socket (server not running)" if sock \
            else "tmux session '%s' not found" % session
        sys.stderr.write(
            "Error: agent '%s' — %s.\n"
            "  Run 'camc heal' to clean up, or 'camc rm %s' to remove the record.\n"
            % (aid, reason, aid))
        sys.exit(1)
    if sock:
        os.execvp("tmux", ["tmux", "-u", "-S", sock, "attach", "-t", session])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", session])


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
        prompt = _tf(a, "prompt") or ""
        prompt_display = prompt[:80] + "..." if len(prompt) > 80 else prompt or "(interactive)"
        alive = tmux_session_exists(_sf(a, "tmux_session"))
        # Session ID info
        sid = a.get("session_id", "")
        session_file_info = None
        if sid:
            # Build session file path: ~/.claude/projects/<dir-slug>/<session>.jsonl
            ctx_path = _sf(a, "context_path", "")
            if ctx_path:
                slug = ctx_path.replace("/", "-").lstrip("-")
                sf_path = os.path.expanduser("~/.claude/projects/%s/%s.jsonl" % (slug, sid))
                if os.path.exists(sf_path):
                    sz = os.path.getsize(sf_path)
                    mtime = os.path.getmtime(sf_path)
                    session_file_info = "%s (%s, %s)" % (
                        sf_path, _fmt_size(sz), _time_ago(_iso_from_ts(mtime)))
                else:
                    session_file_info = "%s (not found)" % sf_path

        pairs = [
            ("ID", a.get("id", "?")),
            ("Name", _tf(a, "name")),
            ("Tool", _tf(a, "tool", "?")),
            ("Status", a.get("status", "?")),
            ("State", a.get("state") or "-"),
            ("Path", _sf(a, "context_path", "?")),
            ("Session", _sf(a, "tmux_session", "?")),
            ("Session ID", sid or None),
            ("Session File", session_file_info),
            ("Started", a.get("started_at") or "-"),
            ("Completed", a.get("completed_at")),
            ("Exit", a.get("exit_reason")),
            ("Prompt", prompt_display),
            ("Auto-exit", "ON" if _tf(a, "auto_exit") else None),
            ("Alive", _c("alive", "green") if alive else _c("dead", "red")),
        ]
        print_detail(pairs, title="Agent: %s" % a.get("id", "?"), border_style="green")
        return

    # Multiple agents — JSON dump
    if _want_json(args):
        print(json.dumps([_agent_to_cam_json(a) for a in agents], indent=2))
    else:
        raw = json.dumps(agents, sort_keys=True)
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        print(json.dumps({"agents": agents, "hash": h}))


def _kill_all_monitors():
    """Kill all monitor processes on this machine (old and new)."""
    killed = 0
    try:
        # Find all monitor processes: camc _monitor, cam-client.py _monitor_bg
        out = subprocess.check_output(
            ["ps", "aux"], stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace")
        for line in out.splitlines():
            if ("_monitor" in line or "monitor_bg" in line) and "grep" not in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                        if pid != os.getpid():
                            os.kill(pid, 15)  # SIGTERM
                            killed += 1
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
    except Exception:
        pass
    return killed


def cmd_heal(args):
    """Check running agents and restart dead monitor daemons."""
    upgrade = getattr(args, "upgrade", False)

    if upgrade:
        killed = _kill_all_monitors()
        print("Upgrade: killed %d old monitor process(es)" % killed)
        time.sleep(0.5)  # let processes die

    my_hostname = _sock.gethostname()
    store = AgentStore()
    agents = store.list()

    if upgrade:
        # Fix unnamed agents: use basename of context_path
        # Detect duplicates and append ID prefix to disambiguate
        unnamed = []
        for a in agents:
            t = a.get("task")
            name = t.get("name", "") if isinstance(t, dict) else a.get("name", "")
            if not name:
                unnamed.append(a)
        if unnamed:
            name_counts = {}
            for a in unnamed:
                base = os.path.basename((a.get("context_path") or "").rstrip("/")) or "agent"
                name_counts.setdefault(base, []).append(a["id"])
            fixed = 0
            for a in unnamed:
                base = os.path.basename((a.get("context_path") or "").rstrip("/")) or "agent"
                new_name = "%s-%s" % (base, a["id"][:4]) if len(name_counts[base]) > 1 else base
                t = a.get("task")
                if isinstance(t, dict):
                    t["name"] = new_name
                else:
                    a["name"] = new_name
                store.save(a)
                fixed += 1
            print("Upgrade: named %d unnamed agent(s)" % fixed)
    # --- Phase 1: Resume agents with live sessions but terminal status ---
    terminal = [a for a in agents if a.get("status") in ("completed", "failed", "stopped")]
    resumed = 0
    for a in terminal:
        agent_host = a.get("hostname")
        if agent_host and not _is_same_host(agent_host, my_hostname):
            continue
        session = _sf(a, "tmux_session")
        if not session:
            continue
        transport = a.get("transport_type", "local")
        if transport != "local":
            continue
        if not tmux_session_exists(session):
            continue
        aid = a["id"]
        name = _tf(a, "name") or aid
        # Session still alive but agent marked terminal — resume it
        store.update(aid, status="running", state="idle",
                     completed_at=None, exit_reason=None)
        # Spawn monitor
        try:
            proc = subprocess.Popen(
                [sys.executable, _CAMC_SCRIPT, "_monitor", aid] if _CAMC_SCRIPT else [sys.executable, "-m", "camc_pkg", "_monitor", aid],
                stdout=subprocess.DEVNULL,
                stderr=open(os.path.join(LOGS_DIR, "monitor-%s.stderr" % aid), "a"),
                start_new_session=True)
            store.update(aid, pid=proc.pid)
            print("  %s (%s): resumed (session alive, PID %d)" % (name, aid, proc.pid))
        except Exception as e:
            print("  %s (%s): resume failed: %s" % (name, aid, e))
        resumed += 1
    if resumed:
        print("Resumed %d agent(s) with live sessions" % resumed)
        # Re-read agents list after resume updates
        agents = store.list()

    # --- Phase 2: Check running agents, restart dead monitors ---
    running = [a for a in agents if a.get("status") == "running"]
    if not running and not resumed:
        print("No running agents.")
        return

    healed = 0
    ok = 0
    skipped = 0
    for a in running:
        agent_host = a.get("hostname")
        if not agent_host:
            # Unknown hostname — check if tmux session exists locally.
            # If yes, claim it for this host. If no, skip (don't mark dead).
            session = _sf(a, "tmux_session")
            if session and tmux_session_exists(session):
                store.update(a["id"], hostname=my_hostname)
                a["hostname"] = my_hostname
                agent_host = my_hostname
            else:
                aid = a["id"]
                name = _tf(a, "name") or aid
                print("  %s (%s): skipped (no hostname, session not local)" % (name, aid))
                skipped += 1
                continue
        if not _is_same_host(agent_host, my_hostname):
            skipped += 1
            continue
        aid = a["id"]
        name = _tf(a, "name") or aid
        session = _sf(a, "tmux_session")

        # Skip remote (SSH) agents — their tmux sessions are on the remote machine,
        # not locally checkable. Remote heal is handled by `camc heal` on each machine.
        transport = a.get("transport_type", "local")
        if transport != "local":
            print("  %s (%s): skipped (remote/%s)" % (name, aid, transport))
            skipped += 1
            continue

        # Check if tmux session is alive — double-check to avoid false negatives
        # (cron environment may cause transient tmux failures)
        session_alive = tmux_session_exists(session)
        if not session_alive:
            # Retry once with a small delay to handle race conditions
            time.sleep(0.5)
            session_alive = tmux_session_exists(session)
        if not session_alive:
            state = a.get("state") or "initializing"
            status = "completed" if state not in ("initializing",) else "failed"
            store.update(aid, status=status, exit_reason="Session gone (heal)", completed_at=_now_iso())
            # Unlink the dead session's socket too. Otherwise `camc attach`
            # on this agent would find the stale .sock, hand it to tmux,
            # and tmux would silently say "no sessions". The agent record
            # stays around so the user can see history / run `camc rm`;
            # only the dangling socket is removed.
            sock_path = _find_tmux_socket(session)
            if sock_path:
                try:
                    os.unlink(sock_path)
                except OSError:
                    pass
            print("  %s (%s): session dead, marked %s" % (name, aid, status))
            continue

        # Check if monitor process is alive
        monitor_alive = False
        pid = _sf(a, "pid", None)
        if pid:
            try:
                os.kill(pid, 0)
                monitor_alive = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if not monitor_alive:
            from camc_pkg import PIDS_DIR
            for pid_path in (os.path.join(PIDS_DIR, "%s.pid" % aid), "/tmp/camc-%s.pid" % aid):
                if os.path.exists(pid_path):
                    try:
                        with open(pid_path) as f:
                            fpid = int(f.read().strip())
                        os.kill(fpid, 0)
                        monitor_alive = True
                        break
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
                [sys.executable, _CAMC_SCRIPT, "_monitor", aid] if _CAMC_SCRIPT else [sys.executable, "-m", "camc_pkg", "_monitor", aid],
                stdout=subprocess.DEVNULL,
                stderr=open(os.path.join(LOGS_DIR, "monitor-%s.stderr" % aid), "a"),
                start_new_session=True)
            store.update(aid, pid=proc.pid)
            print("  %s (%s): restarted (PID %d)" % (name, aid, proc.pid))
            healed += 1
        except Exception as e:
            print("  %s (%s): restart failed: %s" % (name, aid, e))

    local_count = len(running) - skipped
    failed = local_count - ok - healed
    msg = "Heal: %d healthy, %d restarted" % (ok, healed)
    if resumed:
        msg += ", %d resumed" % resumed
    if failed:
        msg += ", %d failed" % failed
    if skipped:
        msg += " (%d skipped, other host)" % skipped
    print(msg)

    # --- Phase 2.5: Backfill session_id for running local agents -------------
    # Many agents were adopted/created before session_id tracking existed (or
    # by older camc builds). Their session_id is empty or the synthetic
    # <aid>-0000-... placeholder. Reboot then falls back to guessing and can
    # pick the wrong session when multiple agents share a workdir.
    #
    # For backfill we deliberately use ONLY PID-authoritative signals
    # (cmdline / fd). Workdir-based layers would silently assign the same
    # UUID to multiple agents that happen to live in the same dir — worse
    # than leaving the field empty, because reboot would then confidently
    # resume the wrong session.
    #
    # Strategy: whenever we can read the Claude cmdline (authoritative), we
    # *replace* the stored session_id with the cmdline value — even if the
    # old one looks like a real UUID. This is the only way to repair past
    # workdir-guess collisions (multiple agents in the same cwd all got
    # backfilled to the same newest-subdir UUID). When cmdline is silent
    # (Claude exited), we leave the stored value alone.
    backfilled = 0
    corrected = 0
    bf_skipped_no_claude = 0
    for a in running:
        agent_host = a.get("hostname")
        if agent_host and not _is_same_host(agent_host, my_hostname):
            continue
        # Don't filter by transport_type — on NFS-shared agents.json clusters,
        # the record's transport_type may say "ssh" even though tmux runs
        # locally on this host. The real gate is tmux_session_exists.
        aid = a["id"]
        old_sid = a.get("session_id", "") or ""
        session = _sf(a, "tmux_session")
        if not session or not tmux_session_exists(session):
            continue
        claude_pid = _find_claude_pid(session)
        if not claude_pid:
            synthetic = old_sid == "%s-0000-0000-0000-000000000000" % aid
            if not old_sid or synthetic:
                bf_skipped_no_claude += 1
            continue
        # Cmdline is live truth — prefer it over anything we have stored.
        found = _find_session_id_pid(claude_pid)
        if not found or found == old_sid:
            continue
        store.update(aid, session_id=found)
        name = _tf(a, "name") or aid
        if old_sid:
            log.info("Corrected session_id for %s (%s): %s → %s",
                     name, aid, old_sid, found)
            corrected += 1
        else:
            log.info("Backfilled session_id for %s (%s): %s", name, aid, found)
            backfilled += 1
    if backfilled:
        print("Backfilled session_id for %d agent(s)" % backfilled)
    if corrected:
        print("Corrected wrong session_id on %d agent(s)" % corrected)
    if bf_skipped_no_claude:
        print("Backfill: %d agent(s) skipped (no live Claude process)" %
              bf_skipped_no_claude)

    # --- Session file check (warning only) ---
    # Re-read running list so the check sees the session_ids we just backfilled.
    running_recheck = [r for r in store.list() if r.get("status") == "running"]
    for a in running_recheck:
        sid = a.get("session_id", "")
        if not sid:
            continue
        ctx_path = _sf(a, "context_path", "")
        if not ctx_path:
            continue
        # Project-dir encoding matches _project_dirs_for_workdir: canonical
        # '/ . _ → -' form. Previously used only '/ → -', which gave false
        # "session file missing" warnings for every path containing dots or
        # underscores (e.g. /home/scratch.hren_gpu_1/...).
        name = _tf(a, "name") or a["id"]
        proj_dirs = _project_dirs_for_workdir(ctx_path)
        for proj_dir in proj_dirs:
            sf_path = os.path.join(proj_dir, "%s.jsonl" % sid)
            if os.path.exists(sf_path):
                break
        else:
            log.warning("Session file missing for %s (%s): %s", name, a["id"],
                        os.path.join(proj_dirs[0], "%s.jsonl" % sid))

    # --- Phase 3: Adopt orphan tmux sessions not in agents.json ---
    # Scan socket directories for cam-* sessions that have no agent record.
    # This catches agents created by cam server (SQLite) or other transports
    # whose monitors died — they'd otherwise be invisible to heal.
    #
    # Multiple agents in the same workdir is a legitimate pattern (parallel
    # work on the same project), so we do NOT dedup by workdir — only by
    # tmux session name. What we do enforce is that each agent record gets
    # a unique display name; when basename(cwd) collides, we append the
    # agent ID suffix ("falcon" → "falcon-852159c6").
    known_sessions = set()
    known_names = set()
    for a in store.list():
        s = _sf(a, "tmux_session")
        if s:
            known_sessions.add(s)
        name = _tf(a, "name") or ""
        if name:
            known_names.add(name)

    orphan_dirs = [
        SOCKETS_DIR,  # /tmp/cam-sockets
        "/tmp/cam-agent-sockets",
        os.path.expanduser("~/.local/share/cam/sockets"),
    ]
    adopted = 0
    stale_cleaned = 0
    for sock_dir in orphan_dirs:
        try:
            entries = os.listdir(sock_dir)
        except OSError:
            continue
        for fname in entries:
            if not fname.startswith("cam-") or not fname.endswith(".sock"):
                continue
            session = fname[:-5]  # strip .sock → "cam-abc12345"
            # A socket is stale whenever tmux has no live server behind it,
            # regardless of whether an agent record still points at it.
            # This catches terminal agents (completed/killed/failed) whose
            # record survived but whose tmux died — without cleanup, the next
            # `camc attach` would hand the dead .sock to tmux and get "no
            # sessions" back with no hint why.
            if not tmux_session_exists(session):
                try:
                    os.unlink(os.path.join(sock_dir, fname))
                    stale_cleaned += 1
                except OSError:
                    pass
                continue
            # Live session with an existing agent record → skip orphan adopt.
            if session in known_sessions:
                continue
            # Extract agent ID from session name (cam-{id})
            aid = session[4:]  # strip "cam-"
            if not aid:
                continue
            # Prevent duplicate adoption (same session in multiple socket dirs)
            known_sessions.add(session)
            # Try to figure out the working directory from tmux
            cwd = ""
            try:
                sock_path = os.path.join(sock_dir, fname)
                rc, out = _run(["tmux", "-S", sock_path,
                                "display-message", "-t", session, "-p", "#{pane_current_path}"],
                               timeout=5)
                if rc == 0 and out.strip():
                    cwd = out.strip()
            except Exception:
                pass
            # Default tool to claude (most common agent type)
            tool = "claude"
            # Give the adopted agent a name unique within the store: start
            # with basename(cwd); if that collides with an existing name,
            # append the agent-id suffix (e.g. falcon → falcon-852159c6).
            # Multiple orphans sharing a workdir is OK; they just each get a
            # unique display name.
            base_name = os.path.basename(cwd) if cwd else "orphan-%s" % aid[:4]
            agent_name = base_name
            if agent_name in known_names:
                agent_name = "%s-%s" % (base_name, aid[:8])
                # Extremely unlikely, but if that's ALSO taken, keep the
                # suffixed name regardless — the agent id guarantees uniqueness.
            known_names.add(agent_name)
            # Try to recover the Claude session UUID from the running process.
            # Without this, `camc reboot` on an adopted agent falls back to
            # workdir-based session lookup and picks the wrong session when
            # multiple agents share a cwd (e.g. /home/hren).
            claude_pid = _find_claude_pid(session)
            session_uuid = _find_session_id(
                aid, claude_pid, workdir=cwd, tmux_session=session,
            ) or ""
            store.save({
                "id": aid,
                "session_id": session_uuid,
                "task": {"name": agent_name, "tool": tool, "prompt": "",
                         "auto_confirm": True, "auto_exit": False},
                "context_id": "",
                "context_name": "",
                "context_path": cwd,
                "transport_type": "local",
                "status": "running",
                "state": "idle",
                "tmux_session": session,
                "tmux_socket": "",
                "pid": None,
                "hostname": my_hostname,
                "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
                "retry_count": 0, "cost_estimate": None, "files_changed": [],
            })
            if session_uuid:
                log.info("Adopted %s with session_id=%s", aid, session_uuid)
            # Spawn monitor for the adopted agent
            try:
                proc = subprocess.Popen(
                    [sys.executable, _CAMC_SCRIPT, "_monitor", aid] if _CAMC_SCRIPT else [sys.executable, "-m", "camc_pkg", "_monitor", aid],
                    stdout=subprocess.DEVNULL,
                    stderr=open(os.path.join(LOGS_DIR, "monitor-%s.stderr" % aid), "a"),
                    start_new_session=True)
                store.update(aid, pid=proc.pid)
                print("  %s (%s): adopted orphan session, monitor PID %d" % (agent_name, aid, proc.pid))
            except Exception as e:
                print("  %s (%s): adopted but monitor failed: %s" % (agent_name, aid, e))
            adopted += 1
    if adopted:
        print("Adopted %d orphan session(s)" % adopted)
    if stale_cleaned:
        print("Cleaned %d stale socket(s)" % stale_cleaned)

    # Maintenance: rotate old events (30 days)
    try:
        removed = EventStore().rotate(max_age_days=30)
        if removed:
            print("Events: rotated %d old entries" % removed)
    except Exception:
        pass

    # Cleanup legacy /tmp/camc-*.log files (Phase 2 consolidation)
    try:
        for f in os.listdir("/tmp"):
            if f.startswith("camc-") and f.endswith(".log"):
                p = os.path.join("/tmp", f)
                try:
                    if os.path.getsize(p) == 0:
                        os.unlink(p)
                except OSError:
                    pass
    except OSError:
        pass


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
    _evt_colors = {
        "state_change": "cyan", "auto_confirm": "yellow",
        "completed": "green", "monitor_start": "blue",
    }
    headers = ["AGENT", "TIME", "EVENT", "DETAIL"]
    rows = []
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
        rows.append([
            ev.get("agent_id", "?")[:8], ts,
            _c(etype, _evt_colors.get(etype)), detail_str,
        ])
    print_table(headers, rows, title="Events")


# ===========================================================================
# Machine management
# ===========================================================================

def cmd_machine(args):
    """Machine management subcommands."""
    sub = getattr(args, "machine_cmd", None)
    if sub == "list":
        cmd_machine_list(args)
    elif sub == "add":
        cmd_machine_add(args)
    elif sub == "rm":
        cmd_machine_rm(args)
    elif sub == "edit":
        cmd_machine_edit(args)
    elif sub == "ping":
        cmd_machine_ping(args)
    else:
        print("Usage: camc machine {list|add|rm|edit|ping}", file=sys.stderr)
        sys.exit(1)


def cmd_machine_list(args):
    from camc_pkg.machine_store import MachineStore
    store = MachineStore()
    machines = store.list()
    if _want_json(args):
        print(json.dumps(machines, indent=2))
        return
    if not machines:
        print("No machines configured.")
        return
    _mtype_colors = {"local": "cyan", "ssh": "green"}
    headers = ["NAME", "TYPE", "HOST", "PORT", "USER"]
    rows = []
    for m in machines:
        mtype = m.get("type", "?")
        rows.append([
            m.get("name", "?"),
            _c(mtype, _mtype_colors.get(mtype)),
            m.get("host") or "-",
            m.get("port") or "-",
            m.get("user") or "-",
        ])
    print_table(headers, rows, title="Machines")


def cmd_machine_add(args):
    from camc_pkg.machine_store import MachineStore
    store = MachineStore()
    name = args.name
    if store.get(name) and name != "local":
        print("Error: machine '%s' already exists (use 'camc machine edit' to update)" % name, file=sys.stderr)
        sys.exit(1)
    machine = {"name": name, "type": args.type or "ssh"}
    if args.host:
        machine["host"] = args.host
    if args.user:
        machine["user"] = args.user
    if args.port:
        machine["port"] = int(args.port)
    if getattr(args, "env_setup", None):
        machine["env_setup"] = args.env_setup
    if getattr(args, "key_file", None):
        machine["key_file"] = args.key_file
    # Validate SSH machines
    if machine["type"] == "ssh":
        if not machine.get("host"):
            print("Error: SSH machine requires --host", file=sys.stderr)
            sys.exit(1)
        if not machine.get("user"):
            print("Error: SSH machine requires --user", file=sys.stderr)
            sys.exit(1)
    store.save(machine)
    print("Added machine '%s' (%s)" % (name, machine["type"]))
    if machine.get("host"):
        print("  %s@%s:%s" % (machine.get("user", ""), machine["host"], machine.get("port", 22)))


def cmd_machine_rm(args):
    from camc_pkg.machine_store import MachineStore
    store = MachineStore()
    name = args.name
    if name == "local":
        print("Error: cannot remove the 'local' machine", file=sys.stderr)
        sys.exit(1)
    if not store.remove(name):
        print("Error: machine '%s' not found" % name, file=sys.stderr)
        sys.exit(1)
    print("Removed machine '%s'" % name)


def cmd_machine_edit(args):
    from camc_pkg.machine_store import MachineStore
    store = MachineStore()
    name = args.name
    m = store.get(name)
    if not m:
        print("Error: machine '%s' not found" % name, file=sys.stderr)
        sys.exit(1)
    changed = False
    if args.host is not None:
        m["host"] = args.host; changed = True
    if args.user is not None:
        m["user"] = args.user; changed = True
    if args.port is not None:
        m["port"] = int(args.port); changed = True
    if getattr(args, "env_setup", None) is not None:
        m["env_setup"] = args.env_setup; changed = True
    if getattr(args, "key_file", None) is not None:
        m["key_file"] = args.key_file; changed = True
    if getattr(args, "type", None) is not None:
        m["type"] = args.type; changed = True
    if not changed:
        print("Nothing to change. Use --host, --user, --port, --env-setup, --key-file")
        return
    store.save(m)
    print("Updated machine '%s'" % name)


def cmd_machine_ping(args):
    from camc_pkg.machine_store import MachineStore
    from camc_pkg.remote import ssh_ping
    store = MachineStore()
    if args.name:
        machines = [store.get(args.name)]
        if not machines[0]:
            print("Error: machine '%s' not found" % args.name, file=sys.stderr)
            sys.exit(1)
    else:
        machines = store.list_ssh()
    if not machines:
        print("No SSH machines configured.")
        return
    for m in machines:
        name = m.get("name", "?")
        host = m.get("host", "?")
        ok = ssh_ping(m)
        if ok:
            print_success("%s (%s)" % (name, host))
        else:
            print_error("%s (%s) — unreachable" % (name, host))


# ===========================================================================
# Context management
# ===========================================================================

def cmd_context(args):
    """Context management subcommands."""
    sub = getattr(args, "context_cmd", None)
    if sub == "list":
        cmd_context_list(args)
    elif sub == "add":
        cmd_context_add(args)
    elif sub == "rm":
        cmd_context_rm(args)
    else:
        print("Usage: camc context {list|add|rm}", file=sys.stderr)
        sys.exit(1)


def cmd_context_list(args):
    from camc_pkg.context_store import ContextStore
    store = ContextStore()
    contexts = store.list()
    if _want_json(args):
        print(json.dumps(contexts, indent=2))
        return
    if not contexts:
        print("No contexts configured.")
        return
    headers = ["NAME", "MACHINE", "PATH"]
    rows = [[c.get("name", "?"), c.get("machine", "?"), c.get("path", "?")] for c in contexts]
    print_table(headers, rows, title="Contexts", col_styles={0: "bold cyan", 1: "magenta"})


def cmd_context_add(args):
    from camc_pkg.context_store import ContextStore
    from camc_pkg.machine_store import MachineStore
    ctx_store = ContextStore()
    name = args.name
    if ctx_store.get(name):
        print("Error: context '%s' already exists" % name, file=sys.stderr)
        sys.exit(1)
    machine_name = getattr(args, "machine", None) or "local"
    m_store = MachineStore()
    if not m_store.get(machine_name):
        print("Error: machine '%s' not found (use 'camc machine add' first)" % machine_name, file=sys.stderr)
        sys.exit(1)
    path = os.path.abspath(getattr(args, "path", None) or os.getcwd())
    ctx_store.save({"name": name, "machine": machine_name, "path": path})
    print("Added context '%s' (machine=%s, path=%s)" % (name, machine_name, path))


def cmd_context_rm(args):
    from camc_pkg.context_store import ContextStore
    store = ContextStore()
    if not store.remove(args.name):
        print("Error: context '%s' not found" % args.name, file=sys.stderr)
        sys.exit(1)
    print("Removed context '%s'" % args.name)


# ===========================================================================
# Sync
# ===========================================================================

def cmd_sync(args):
    """Sync camc and configs to remote machines."""
    from camc_pkg.machine_store import MachineStore
    from camc_pkg.remote import sync_camc_to_machine
    store = MachineStore()

    target = getattr(args, "target", None)
    if target:
        m = store.get(target)
        if not m:
            print("Error: machine '%s' not found" % target, file=sys.stderr)
            sys.exit(1)
        if m.get("type") != "ssh":
            print("Error: sync only works with SSH machines" , file=sys.stderr)
            sys.exit(1)
        machines = [m]
    else:
        machines = store.list_ssh()

    if not machines:
        print("No SSH machines to sync.")
        return

    total_ok = 0
    total_fail = 0
    for m in machines:
        name = m.get("name", "?")
        host = m.get("host", "?")
        print("Syncing to %s (%s)..." % (name, host))
        try:
            results = sync_camc_to_machine(m)
        except Exception as e:
            print("  \033[31m✗ Failed: %s\033[0m" % e)
            total_fail += 1
            continue
        for fname, status in results.items():
            if status == "deployed":
                print("  \033[32m%s: deployed\033[0m" % fname)
                total_ok += 1
            elif status == "unchanged":
                print("  %s: unchanged" % fname)
            else:
                print("  \033[31m%s: FAILED\033[0m" % fname)
                total_fail += 1
    print()
    print("Sync complete: %d deployed, %d failed" % (total_ok, total_fail))


def cmd_db_migrate(args):
    """Migrate cam SQLite data to JSON format."""
    from camc_pkg.migrate import run_migrate
    db_path = getattr(args, "db", None)
    dry_run = getattr(args, "dry_run", False)
    run_migrate(db_path=db_path, dry_run=dry_run)


def cmd_version(args):
    build = __build__ if __build__ else "dev"
    print("camc v%s (%s)" % (__version__, build))
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
    if a:
        session = _sf(a, "tmux_session")
    else:
        # Not in agents.json — treat id as a tmux session name directly.
        # This allows cam server to capture sessions it created via SSHTransport.
        session = args.id
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
    if a:
        session = _sf(a, "tmux_session")
    else:
        session = args.id
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
    if a:
        session = _sf(a, "tmux_session")
    else:
        session = args.id
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
  camc run -n myfix "fix lint errors" Launch agent with a name
  camc list                           List all agents
  camc a myfix                        Attach by name (a = attach)
  camc a 3                            Attach by index (1-based)
  camc run                            Launch claude interactively
  camc run -t codex "add tests"       Launch codex with a prompt
  camc stop myfix                     Gracefully stop by name
  camc logs myfix -f                  Follow agent output
  camc kill abc1                      Force kill an agent
  camc status abc1                    Show detailed agent status
  camc add my-session --tool claude   Adopt existing tmux session
  camc rm abc1 --kill                 Remove and kill agent
  camc apply -f tasks.yaml            Run DAG task file
  camc history abc1                   Show event history for agent
  camc capture myfix --lines 50       Capture agent screen output
  camc send myfix --text "hello"      Send text to agent
  camc key abc1 --key C-c             Send special key to agent
  camc heal                           Restart dead monitors
  camc machine list                   List machines
  camc context list                   List contexts
  camc sync                           Sync camc to all remote machines
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
    # Hidden safety arm for --auto-exit. Without this, --auto-exit is a no-op
    # in the monitor. Intentionally undocumented (argparse.SUPPRESS) — the
    # idle detector is heuristic and the false-positive cost is lost work,
    # so only callers who understand the risk should reach for this.
    r.add_argument("--auto-exit-enable", dest="auto_exit_enable",
                   action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--tag", action="append", default=[], help="Add tag (repeatable)")
    r.add_argument("--resume", dest="resume_session", default=None,
                   metavar="SESSION_ID",
                   help="Resume an existing Claude session by ID (adds --resume to claude, skips prompt injection)")
    r.add_argument("--no-inherit-env", action="store_true", help="Legacy mode: wrap command with bash -c and env_setup")

    # list
    ls = sub.add_parser("list", aliases=["ls"], help="List agents")
    ls.add_argument("--status", default=None, help="Filter by status")
    ls.add_argument("--tag", default=None, help="Filter by tag")
    ls.add_argument("--last", "-n", type=int, default=50, help="Show last N agents [default: 50]")

    # logs
    l = sub.add_parser("logs", help="View agent output logs")
    l.add_argument("id", help="Agent ID (prefix match)")
    l.add_argument("-f", "--follow", action="store_true", help="Follow output")
    l.add_argument("--tail", "-n", type=int, default=50, help="Last N lines [default: 50]")

    # exit — graceful /exit, tmux stays alive
    ex = sub.add_parser("exit", help="Gracefully exit Claude (tmux stays alive, can be resumed)")
    ex.add_argument("id", help="Agent ID")

    # stop — kill tool process, tmux stays alive
    s = sub.add_parser("stop", help="Kill the tool process (tmux stays alive)")
    s.add_argument("id", help="Agent ID")

    # kill — nuke tmux session
    k = sub.add_parser("kill", help="Force kill agent and tear down tmux session")
    k.add_argument("id", help="Agent ID")

    # update
    up = sub.add_parser("update", help="Update agent properties")
    up.add_argument("id", help="Agent ID")
    up.add_argument("--name", default=None, help="Set agent name")
    up.add_argument("--auto-confirm", dest="auto_confirm", default=None,
                    type=lambda v: v.lower() in ("true", "1", "yes"), help="Enable/disable auto-confirm")
    up.add_argument("--tag", action="append", default=[], help="Add tag (repeatable)")
    up.add_argument("--untag", action="append", default=[], help="Remove tag (repeatable)")

    # add
    a = sub.add_parser("add", help="Adopt existing tmux session")
    a.add_argument("session", help="tmux session name")
    a.add_argument("--tool", "-t", default="claude", help="Tool type")
    a.add_argument("--name", "-n", default=None, help="Human-readable name")

    # rm
    rm = sub.add_parser("rm", help="Remove a single agent (always kills tmux)")
    rm.add_argument("id", help="Agent ID")
    rm.add_argument("--kill", "-k", action="store_true",
                    help="[deprecated, no-op] tmux is always killed now")

    # prune
    sub.add_parser("prune", help="Remove all non-running agents")

    # reboot / migrate
    rb = sub.add_parser("reboot", help="Restart agent with session resume (alias for migrate)")
    rb.add_argument("id", help="Agent ID or name")
    rb.add_argument("--to", default=None, help="Target machine (host:port). Omit for local reboot.")
    mig_a = sub.add_parser("migrate", help="Restart or move agent with session resume")
    mig_a.add_argument("id", help="Agent ID or name")
    mig_a.add_argument("--to", default=None, help="Target machine (host:port). Omit for local reboot.")

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
    heal_p = sub.add_parser("heal", help="Check running agents and restart dead monitor daemons")
    heal_p.add_argument("--upgrade", action="store_true", help="Kill ALL monitors and restart with current camc binary")

    # machine
    mp = sub.add_parser("machine", help="Manage remote machines")
    msub = mp.add_subparsers(dest="machine_cmd")
    msub.add_parser("list", help="List machines")
    ma = msub.add_parser("add", help="Add a machine")
    ma.add_argument("name", help="Machine name")
    ma.add_argument("--type", default="ssh", help="Machine type (local, ssh) [default: ssh]")
    ma.add_argument("--host", default=None, help="Hostname or IP")
    ma.add_argument("--user", default=None, help="SSH user")
    ma.add_argument("--port", default=None, help="SSH port")
    ma.add_argument("--env-setup", default=None, help="Shell commands to run before agent")
    ma.add_argument("--key-file", default=None, help="SSH key file")
    mrm = msub.add_parser("rm", help="Remove a machine")
    mrm.add_argument("name", help="Machine name")
    me = msub.add_parser("edit", help="Edit a machine")
    me.add_argument("name", help="Machine name")
    me.add_argument("--type", default=None)
    me.add_argument("--host", default=None)
    me.add_argument("--user", default=None)
    me.add_argument("--port", default=None)
    me.add_argument("--env-setup", default=None)
    me.add_argument("--key-file", default=None)
    mping = msub.add_parser("ping", help="Test SSH connectivity")
    mping.add_argument("name", nargs="?", default=None, help="Machine name (omit for all)")

    # context
    cp = sub.add_parser("context", help="Manage project contexts")
    csub = cp.add_subparsers(dest="context_cmd")
    csub.add_parser("list", help="List contexts")
    ca = csub.add_parser("add", help="Add a context")
    ca.add_argument("name", help="Context name")
    ca.add_argument("--machine", "-m", default="local", help="Machine name [default: local]")
    ca.add_argument("--path", "-p", default=None, help="Working directory [default: cwd]")
    crm = csub.add_parser("rm", help="Remove a context")
    crm.add_argument("name", help="Context name")

    # sync
    sy = sub.add_parser("sync", help="Sync camc and configs to remote machines")
    sy.add_argument("target", nargs="?", default=None, help="Machine name (omit for all SSH machines)")

    # migrate
    mig = sub.add_parser("db-migrate", help="Migrate cam SQLite data to JSON")
    mig.add_argument("--db", default=None, help="Path to cam.db [default: ~/.local/share/cam/cam.db]")
    mig.add_argument("--dry-run", action="store_true", help="Show plan without writing files")

    # version
    sub.add_parser("version", help="Show version")

    # Hidden _monitor subcommand
    if len(sys.argv) >= 3 and sys.argv[1] == "_monitor":
        _run_monitor(sys.argv[2])
        return

    # Command aliases (expand before argparse sees them)
    _aliases = {"a": "attach", "ls": "list"}
    if len(sys.argv) > 1 and sys.argv[1] in _aliases:
        sys.argv[1] = _aliases[sys.argv[1]]

    args = p.parse_args()
    # Enable debug logging if --verbose
    if getattr(args, "verbose", False):
        logging.getLogger("camc").setLevel(logging.DEBUG)

    # Auto-move logs to scratch if available (silent, one-time)
    _ensure_logs_on_scratch()

    cmds = {
        "init": cmd_init,
        "run": cmd_run,
        "list": cmd_list, "ls": cmd_list,
        "logs": cmd_logs,
        "exit": cmd_exit,
        "stop": cmd_stop,
        "kill": cmd_kill,
        "update": cmd_update,
        "add": cmd_add,
        "rm": cmd_rm,
        "prune": cmd_prune,
        "reboot": cmd_migrate,
        "migrate": cmd_migrate,
        "attach": cmd_attach, "a": cmd_attach,
        "status": cmd_status,
        "apply": cmd_apply,
        "history": cmd_history,
        "capture": cmd_capture,
        "send": cmd_send,
        "key": cmd_key,
        "heal": cmd_heal,
        "machine": cmd_machine,
        "context": cmd_context,
        "sync": cmd_sync,
        "db-migrate": cmd_db_migrate,
        "version": cmd_version,
    }
    if args.command in cmds:
        cmds[args.command](args)
    else:
        p.print_help()
        sys.exit(1)
