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

from camc_pkg import __build__


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


def _agent_tool(agent):
    task = agent.get("task")
    if isinstance(task, dict) and task.get("tool"):
        return task.get("tool")
    return agent.get("tool", "")


def _tool_prompt_submit_delay(tool):
    if not tool:
        return 0.0
    try:
        return _load_config(tool).prompt_submit_delay
    except Exception:
        return 0.0


def _send_with_submit_delay(session_id, text, send_enter=True, submit_delay=0.0):
    if submit_delay > 0 and send_enter and text:
        if not tmux_send_input(session_id, text, send_enter=False):
            return False
        time.sleep(submit_delay)
        return tmux_send_key(session_id, "Enter")
    return tmux_send_input(session_id, text, send_enter=send_enter)


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
            if config.prompt_submit_delay > 0:
                tmux_send_input(session, prompt, send_enter=False)
                time.sleep(config.prompt_submit_delay)
                tmux_send_key(session, "Enter")
            else:
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


def _jsonl_summary(jsonl_bytes):
    """Compute a full summary from a session.jsonl bytes blob.

    Returns {"turns": [...], "totals": {...}, "tool_calls": [...],
             "files_changed": {...}, "last_assistant_text": str,
             "line_count": N}.

    Each turn:
        {
          "idx": 1-based,
          "line": 1-indexed line number of the user record in jsonl,
          "ts": iso timestamp (first timestamp in the turn),
          "end_ts": iso timestamp of the last assistant message in the turn,
          "prompt": first 400 chars of the user text,
          "reply":  first 300 chars of the first assistant text,
          "tools":  [tool_name, ...] unique, in first-use order,
          "files":  files touched via Edit/Write tool_use (unique),
        }

    tool_calls: a flat list of every tool_use with its target. Each entry:
        {"turn": int, "line": int, "tool": str, "target": str,
         "ok": bool, "error": str|None}
      target is a short, human-readable description of the input (file path,
      bash command, etc.). `ok` + `error` come from the matching tool_result
      record paired by toolUseID.

    files_changed: {file_path: {"edits": int, "writes": int, "turns": [idx,…]}}
      Aggregated across the whole session.

    last_assistant_text: the last assistant message whose content had any
      text (not just tool_use). This is Claude's final narrative reply
      before the session ended — the natural "what did the agent conclude"
      to surface in archive summaries.

    Skips tool_result synthetic "user" records and system-wrapper prompts —
    only real human user turns land in `turns`.
    """
    import json as _json
    turns = []
    current = None
    tool_calls = []
    tool_use_by_id = {}   # toolUseID → tool_calls[] index, for pairing results
    files_changed = {}    # path → {"edits": int, "writes": int, "turns": set}
    last_assistant_text = ""
    # Per-turn "last text block": the final text the assistant produced in the
    # turn, overwritten as new text blocks arrive. Used downstream to extract
    # a summary paragraph.
    current_last_text = ""

    def _last_paragraph(text):
        """Return the last non-empty paragraph (split on \\n\\n)."""
        if not text:
            return ""
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        return paras[-1] if paras else ""

    def _short_target(tool, inp):
        """One-line human-readable summary of a tool_use input."""
        if not isinstance(inp, dict):
            return ""
        if tool == "Bash":
            return (inp.get("command") or "").strip().splitlines()[0][:120]
        if tool in ("Edit", "Write", "Read"):
            return inp.get("file_path") or inp.get("path") or ""
        if tool == "Glob":
            return inp.get("pattern") or ""
        if tool == "Grep":
            return "%s%s" % (
                inp.get("pattern") or "",
                "  [in %s]" % inp.get("path") if inp.get("path") else "",
            )[:120]
        if tool in ("WebFetch", "WebSearch"):
            return inp.get("url") or inp.get("query") or ""
        if tool == "Task":
            return (inp.get("description") or inp.get("prompt") or "")[:120]
        if tool == "TodoWrite":
            todos = inp.get("todos") or []
            return "%d items" % len(todos) if isinstance(todos, list) else ""
        # Fallback: first string value
        for v in inp.values():
            if isinstance(v, str):
                return v[:120]
        return ""

    def _error_from_tool_result(c):
        """Extract first line of error payload from a tool_result. Only
        returns a string when the caller already knows is_error is True —
        text-keyword heuristics falsely flag legitimate Read/Grep output
        that happens to mention 'error' anywhere."""
        res = c.get("content")
        if isinstance(res, str):
            s = res.strip()
            return s.splitlines()[0][:160] if s else None
        if isinstance(res, list):
            for x in res:
                if isinstance(x, dict) and x.get("type") == "text":
                    t = (x.get("text") or "").strip()
                    if t:
                        return t.splitlines()[0][:160]
        return None

    for i, line in enumerate(jsonl_bytes.splitlines(), 1):
        try:
            o = _json.loads(line)
        except Exception:
            continue
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        ts = o.get("timestamp") or (msg.get("timestamp") if isinstance(msg, dict) else None) or ""
        ttype = o.get("type", "")

        if ttype == "user" and not o.get("isMeta") and msg.get("role") == "user":
            content = msg.get("content", "")
            # Tool-result synthetic user turn: pair results back to their calls
            if isinstance(content, list) and all(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
            ):
                for c in content:
                    tuid = c.get("tool_use_id") or c.get("toolUseId")
                    if not tuid:
                        continue
                    idx = tool_use_by_id.get(tuid)
                    if idx is None:
                        continue
                    is_error = bool(c.get("is_error"))
                    err = _error_from_tool_result(c) if is_error else None
                    tool_calls[idx]["ok"] = not is_error
                    tool_calls[idx]["error"] = err
                continue
            # Real user prompt
            text = content if isinstance(content, str) else ""
            if not text and isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text", "")
                        break
            stripped = text.lstrip()
            if stripped.startswith("<local-command-") or stripped.startswith("<command-"):
                continue
            if current is not None:
                # Closing-out previous turn: stamp its summary_paragraph.
                current["summary_paragraph"] = _last_paragraph(current_last_text)
                turns.append(current)
            current = {
                "idx": len(turns) + 1,
                "line": i,
                "ts": ts,
                "end_ts": ts,
                "prompt": text[:400],
                "reply": "",
                "tools": [],
                "files": [],
                "summary_paragraph": "",
            }
            current_last_text = ""
        elif ttype == "assistant" and current is not None:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            turn_idx = current["idx"]
            for c in content:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "tool_use":
                    tool = c.get("name", "?")
                    if tool not in current["tools"]:
                        current["tools"].append(tool)
                    inp = c.get("input") or {}
                    target = _short_target(tool, inp)
                    entry = {
                        "turn": turn_idx,
                        "line": i,
                        "tool": tool,
                        "target": target,
                        "ok": None,
                        "error": None,
                    }
                    tuid = c.get("id")
                    if tuid:
                        tool_use_by_id[tuid] = len(tool_calls)
                    tool_calls.append(entry)
                    if tool in ("Edit", "Write"):
                        fp = inp.get("file_path") or inp.get("path")
                        if fp:
                            if fp not in current["files"]:
                                current["files"].append(fp)
                            rec = files_changed.setdefault(
                                fp, {"edits": 0, "writes": 0, "turns": []}
                            )
                            if tool == "Edit":
                                rec["edits"] += 1
                            else:
                                rec["writes"] += 1
                            if turn_idx not in rec["turns"]:
                                rec["turns"].append(turn_idx)
                elif ct == "text":
                    txt = c.get("text") or ""
                    if txt.strip():
                        if not current["reply"]:
                            current["reply"] = txt[:300]
                        last_assistant_text = txt  # keeps overwriting → last wins (whole session)
                        current_last_text = txt    # same, scoped to current turn
            if ts:
                current["end_ts"] = ts
    if current is not None:
        current["summary_paragraph"] = _last_paragraph(current_last_text)
        turns.append(current)

    totals = {
        "prompts": len(turns),
        "tool_uses": len(tool_calls),
        "first_ts": turns[0]["ts"] if turns else None,
        "last_ts": turns[-1]["end_ts"] if turns else None,
    }
    return {
        "turns": turns,
        "totals": totals,
        "tool_calls": tool_calls,
        "files_changed": files_changed,
        "last_assistant_text": last_assistant_text,
        "line_count": len(jsonl_bytes.splitlines()),
    }


def cmd_archive(args):
    """Bundle one agent's full history into a tar.gz for long-term storage.

    Layout of the archive (schema v1):
        MANIFEST.json         — provenance + what the bundle contains
        agent.json            — the agent record from agents.json
        events.jsonl          — events for this agent, filtered from EventStore
        monitor.log           — monitor log (if present)
        monitor.stderr        — monitor stderr (if present)
        capture.txt           — best-effort tmux capture-pane (only if alive)
        claude/session.jsonl  — authoritative transcript from ~/.claude/projects/
        claude/session-dir/   — per-session subdir (subagents, tool-results)

    For older agents whose session_id was never tracked (or is the synthetic
    <aid>-0000-… placeholder), we run the same 4-layer extractor `camc heal`
    uses (cmdline → /proc/fd → project subdir → jsonl). When a real session_id
    is recovered we persist it back into agents.json so next archive/reboot
    can skip the search. If the extractor falls through to the synthetic form
    we accept it (it's the actual session UUID for camc-run agents).
    """
    import gzip
    import hashlib
    import io
    import json as _json
    import tarfile

    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)

    aid = a["id"]
    name = _tf(a, "name") or aid
    session = _sf(a, "tmux_session", "")
    workdir = _sf(a, "context_path", "")

    # --- Resolve session_id (heal-style extraction for old agents) -----------
    sid = (a.get("session_id") or "").strip()
    synthetic_form = "%s-0000-0000-0000-000000000000" % aid
    override_sid = getattr(args, "session_id", None)
    if override_sid:
        sid = override_sid
        log.info("Archive %s: using --session-id override %s", aid, sid)
    elif not sid or sid == synthetic_form:
        # Live Claude pid if the tmux session is still around
        claude_pid = _find_claude_pid(session) if session and tmux_session_exists(session) else None
        found = _find_session_id(aid, claude_pid, workdir=workdir, tmux_session=session)
        if found:
            sid = found
            if not a.get("session_id"):
                store.update(aid, session_id=sid)
                log.info("Archive %s: backfilled session_id = %s", aid, sid)
        else:
            print_warning("Could not determine session_id; archive will omit Claude transcript")

    # --- Pick output location ------------------------------------------------
    out_dir = getattr(args, "output", None) or os.path.join(CAM_DIR, "archives")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print_error("Cannot create archive dir %s: %s" % (out_dir, e))
        sys.exit(1)

    # Filename: <aid>-<sid[:8]>-<timestamp>-<safe-name>.tar.gz
    # Keep sid + timestamp so we never overwrite an earlier snapshot (each
    # archive is an immutable point-in-time capture); append the name so
    # `camc archive summary <name>` can resolve by filename substring.
    from datetime import datetime, timezone
    import re as _re
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sid_tag = (sid[:8] if sid else "nosid")
    safe_name = _re.sub(r"[^A-Za-z0-9._-]", "-", name) if name else "unnamed"
    out_path = os.path.join(
        out_dir, "%s-%s-%s-%s.tar.gz" % (aid, sid_tag, ts, safe_name)
    )

    # --- Collect artefacts ---------------------------------------------------
    manifest = {
        "schema": "camc-archive/1",
        "agent_id": aid,
        "agent_name": name,
        "session_id": sid or None,
        "hostname": _sock.gethostname(),
        "camc_version": __version__,
        "created_at": _now_iso(),
        "files": [],  # filled below
    }
    members: list[tuple[str, bytes]] = []  # (arcname, bytes)

    def _add(arcname: str, data: bytes) -> None:
        members.append((arcname, data))
        manifest["files"].append({
            "path": arcname,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    # agent.json
    _add("agent.json",
         _json.dumps(a, indent=2, sort_keys=True).encode("utf-8"))

    # events.jsonl (filter)
    try:
        ev_lines: list[str] = []
        for ev in EventStore().read(agent_id=aid, limit=10_000_000):
            ev_lines.append(_json.dumps(ev, separators=(",", ":")))
        if ev_lines:
            _add("events.jsonl", ("\n".join(ev_lines) + "\n").encode("utf-8"))
    except Exception as e:
        log.warning("events read failed: %s", e)

    # monitor logs
    for base in ("monitor-%s.log", "monitor-%s.stderr"):
        p = os.path.join(LOGS_DIR, base % aid)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    _add(base % aid, f.read())
            except OSError:
                pass

    # live pane capture (best-effort)
    if session and tmux_session_exists(session):
        try:
            cap = capture_tmux(session, lines=10_000)
            if cap:
                _add("capture.txt", cap.encode("utf-8", errors="replace"))
        except Exception as e:
            log.warning("capture failed: %s", e)

    # Claude transcript — the main prize
    jsonl_bytes = None
    jsonl_source_path = None
    if sid and workdir:
        claude_dir = None
        for proj in _project_dirs_for_workdir(workdir):
            if os.path.isdir(proj):
                claude_dir = proj
                break
        if claude_dir is None:
            print_warning("No ~/.claude/projects/ dir for %s — skipping Claude transcript" % workdir)
        else:
            jsonl = os.path.join(claude_dir, "%s.jsonl" % sid)
            if os.path.isfile(jsonl):
                try:
                    with open(jsonl, "rb") as f:
                        jsonl_bytes = f.read()
                    jsonl_source_path = jsonl
                    _add("claude/session.jsonl", jsonl_bytes)
                except OSError as e:
                    log.warning("read %s: %s", jsonl, e)
            else:
                print_warning("No transcript at %s — Claude may have never written one" % jsonl)
            # Per-session subdir (subagents, tool-results, etc.)
            subdir = os.path.join(claude_dir, sid)
            if os.path.isdir(subdir):
                for root, _dirs, files in os.walk(subdir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, subdir)
                        try:
                            with open(full, "rb") as f:
                                _add(os.path.join("claude/session-dir", rel), f.read())
                        except OSError:
                            pass

    # summary.json — pre-computed per-prompt view for fast `archive summary`
    # (avoids re-parsing the full jsonl every time the user lists turns).
    if jsonl_bytes:
        try:
            summary = _jsonl_summary(jsonl_bytes)
            # Record the live source path for `archive paths` / `info` later.
            summary["source_jsonl"] = jsonl_source_path
            _add("summary.json",
                 _json.dumps(summary, indent=2).encode("utf-8"))
        except Exception as e:
            log.warning("summary build failed: %s", e)

    # MANIFEST last so it reflects everything
    manifest_bytes = _json.dumps(manifest, indent=2).encode("utf-8")

    # --- Write the tarball ---------------------------------------------------
    tmp_path = out_path + ".partial"
    try:
        with gzip.open(tmp_path, "wb", compresslevel=6) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                def _add_member(arcname: str, data: bytes) -> None:
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    info.mtime = int(time.time())
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
                _add_member("MANIFEST.json", manifest_bytes)
                for arcname, data in members:
                    _add_member(arcname, data)
        os.replace(tmp_path, out_path)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print_error("Failed to write archive: %s" % e)
        sys.exit(1)

    size = os.path.getsize(out_path)
    print_success("Archived agent %s (%s) → %s" % (aid, name, out_path))
    print("  session_id: %s" % (sid or "(not found)"))
    print("  files:      %d" % len(members))
    print("  size:       %s" % _fmt_size(size))


# ---------------------------------------------------------------------------
# Archive read-side: list / info / summary / show / paths / grep
# ---------------------------------------------------------------------------


_ARCHIVES_DIR = os.path.join(CAM_DIR, "archives")


def _archives_dir():
    return os.environ.get("CAMC_ARCHIVES_DIR") or _ARCHIVES_DIR


def _list_archive_files():
    d = _archives_dir()
    if not os.path.isdir(d):
        return []
    return sorted(
        os.path.join(d, f)
        for f in os.listdir(d)
        if f.endswith(".tar.gz")
    )


def _resolve_archive(ref):
    """Accept an agent id prefix, agent name, archive filename, or path.
    Return the newest matching .tar.gz, or None.

    Filename layout is <aid>-<safe-name>.tar.gz, so a substring match on
    the basename catches both id prefixes and name fragments without
    having to open each tarball's MANIFEST.
    """
    # Direct path (absolute or relative)
    if os.path.isfile(ref):
        return ref
    d = _archives_dir()
    direct = os.path.join(d, ref)
    if os.path.isfile(direct):
        return direct
    archives = _list_archive_files()
    # Prefix on basename (matches id-first filenames)
    matches = [p for p in archives if os.path.basename(p).startswith(ref)]
    if not matches:
        # Substring on basename: catches names like `teadev` that come
        # after the id (e.g. 5d4d9f71-teadev.tar.gz).
        matches = [p for p in archives if ref in os.path.basename(p)]
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def _read_archive_member(archive_path, arcname):
    """Extract one member from a tar.gz into bytes, or None if missing."""
    import gzip as _gzip
    import tarfile as _tar
    try:
        with _gzip.open(archive_path, "rb") as gz:
            with _tar.open(fileobj=gz, mode="r") as tar:
                try:
                    m = tar.getmember(arcname)
                except KeyError:
                    return None
                f = tar.extractfile(m)
                return f.read() if f else None
    except (OSError, _tar.TarError):
        return None


def _load_archive_meta(archive_path):
    """Return (manifest_dict, summary_dict) — both may be None if missing."""
    import json as _json
    mf_raw = _read_archive_member(archive_path, "MANIFEST.json")
    sm_raw = _read_archive_member(archive_path, "summary.json")
    mf = _json.loads(mf_raw) if mf_raw else None
    sm = _json.loads(sm_raw) if sm_raw else None
    return mf, sm


def _fmt_dur_iso(a, b):
    """Human-readable duration between two ISO timestamps, tolerant to
    timezone suffixes. Returns '?' if anything's off."""
    if not a or not b:
        return "?"
    try:
        from datetime import datetime
        def _p(s):
            s = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        d = (_p(b) - _p(a)).total_seconds()
        if d < 60:
            return "%ds" % int(d)
        if d < 3600:
            return "%dm" % int(d / 60)
        if d < 86400:
            return "%dh" % int(d / 3600)
        return "%dd" % int(d / 86400)
    except Exception:
        return "?"


def _trunc(s, n):
    if s is None:
        s = ""
    s = s.replace("\n", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def cmd_archive_list(args):
    """List archives in ~/.cam/archives/ as a table."""
    archives = _list_archive_files()
    if not archives:
        print("No archives in %s" % _archives_dir())
        return
    rows = []
    for p in archives:
        mf, sm = _load_archive_meta(p)
        if mf is None:
            continue
        turns = (sm or {}).get("totals", {}).get("prompts", 0) if sm else 0
        tools = (sm or {}).get("totals", {}).get("tool_uses", 0) if sm else 0
        rows.append({
            "file": os.path.basename(p),
            "id": mf.get("agent_id", ""),
            "name": mf.get("agent_name", ""),
            "sid": (mf.get("session_id") or "")[:8],
            "created": (mf.get("created_at") or "")[:19].replace("T", " "),
            "turns": turns,
            "tools": tools,
            "size": os.path.getsize(p),
        })
    rows.sort(key=lambda r: r["created"], reverse=True)

    if getattr(args, "json", False):
        print(_json_dump(rows))
        return

    headers = ["ID", "NAME", "SID", "CREATED", "TURNS", "TOOLS", "SIZE"]
    table = []
    for r in rows:
        table.append([
            r["id"],
            _trunc(r["name"], 16),
            r["sid"],
            r["created"],
            str(r["turns"]),
            str(r["tools"]),
            _fmt_size(r["size"]),
        ])
    print_table(headers, table, title="Archives (%s)" % _archives_dir())
    total = sum(r["size"] for r in rows)
    tt = sum(r["turns"] for r in rows)
    tu = sum(r["tools"] for r in rows)
    print("  total: %d archive(s), %s — %d prompts, %d tool uses"
          % (len(rows), _fmt_size(total), tt, tu))


def cmd_archive_info(args):
    """Print archive header, file list, and (if present) JSONL record-type breakdown."""
    path = _resolve_archive(args.ref)
    if not path:
        sys.stderr.write("Error: archive '%s' not found in %s\n" % (args.ref, _archives_dir()))
        sys.exit(1)
    mf, sm = _load_archive_meta(path)
    if mf is None:
        sys.stderr.write("Error: MANIFEST.json missing inside %s\n" % path)
        sys.exit(1)
    totals = (sm or {}).get("totals", {}) if sm else {}

    print("archive info: %s" % os.path.basename(path))
    print()
    print("  AGENT")
    print("    id         %s" % mf.get("agent_id", ""))
    print("    name       %s" % mf.get("agent_name", ""))
    print("    session    %s" % (mf.get("session_id") or "(none)"))
    print()
    print("  LOCATION")
    print("    archive    %s" % path)
    if sm and sm.get("source_jsonl"):
        src = sm["source_jsonl"]
        alive = os.path.isfile(src)
        print("    source     %s" % src)
        print("               (%s)" % ("still on disk" if alive else "no longer on disk"))
    print()
    print("  PROVENANCE")
    print("    host       %s" % mf.get("hostname", ""))
    print("    created    %s" % mf.get("created_at", ""))
    print("    camc       v%s" % mf.get("camc_version", "?"))
    print("    size       %s" % _fmt_size(os.path.getsize(path)))
    print("    files      %d" % len(mf.get("files", [])))
    print()
    if totals:
        dur = _fmt_dur_iso(totals.get("first_ts"), totals.get("last_ts"))
        print("  SESSION")
        print("    prompts    %d" % totals.get("prompts", 0))
        print("    tool uses  %d" % totals.get("tool_uses", 0))
        print("    started    %s" % (totals.get("first_ts") or "?"))
        print("    last msg   %s   (%s active)" % (totals.get("last_ts") or "?", dur))
        print()
    print("  FILES")
    for x in mf.get("files", []):
        print("    %-48s  %s" % (x["path"], _fmt_size(x["size"])))

    # SESSION SUMMARY: last assistant message with text content. This is the
    # agent's final narrative reply — the natural "what did it conclude" you
    # want to surface alongside the file manifest.
    last_text = (sm or {}).get("last_assistant_text", "") if sm else ""
    if last_text.strip():
        print()
        print("  SESSION SUMMARY  (last assistant text)")
        for para in last_text.splitlines():
            print("    " + para)


def cmd_archive_summary(args):
    """Per-prompt table for one archive (replaces older sectioned view).

    Reads summary.json embedded in the tarball at archive time. Filters:
      --search SUBSTR         match against prompt text
      --tool A,B              only turns whose tools include any of these
      --limit N               last N prompts (0 = all, default 10)
      --json                  full payload as JSON
    """
    path = _resolve_archive(args.ref)
    if not path:
        sys.stderr.write("Error: archive '%s' not found\n" % args.ref)
        sys.exit(1)
    mf, sm = _load_archive_meta(path)
    if not sm:
        sys.stderr.write("Error: %s has no summary.json (probably built by an older camc)\n"
                         % os.path.basename(path))
        sys.exit(1)

    turns = sm.get("turns", [])
    search = getattr(args, "search", None)
    if search:
        needle = search.lower()
        turns = [t for t in turns if needle in (t.get("prompt", "") or "").lower()]
    tool_filter = getattr(args, "tool", None)
    if tool_filter:
        want = set(s.strip() for s in tool_filter.split(",") if s.strip())
        turns = [t for t in turns if want & set(t.get("tools", []))]
    limit = getattr(args, "limit", 10) or 10
    if limit and limit > 0:
        turns = turns[-limit:]

    if getattr(args, "json", False):
        print(_json_dump({"archive": os.path.basename(path), "turns": turns}))
        return

    header = "%s (%s)   sid %s   %d prompts" % (
        (mf or {}).get("agent_name", ""),
        (mf or {}).get("agent_id", ""),
        ((mf or {}).get("session_id") or "")[:8],
        sm.get("totals", {}).get("prompts", 0),
    )
    print(header)
    src = sm.get("source_jsonl", "")
    alive = src and os.path.isfile(src)
    print("  archive:  %s" % path)
    if src:
        print("  source:   %s   (%s)" % (src, "live on disk" if alive else "no longer on disk"))
    print()
    rows = []
    for t in turns:
        when = (t.get("ts") or "")[:16].replace("T", " ")
        # SUMMARY: last paragraph of the final assistant text block in the
        # turn. Capped at 48 chars for table-legibility; empty if the turn
        # had no narrative reply (pure tool-use, or still-running).
        summary = _trunc(t.get("summary_paragraph", "") or "", 48)
        rows.append([
            str(t.get("idx", "")),
            when,
            _trunc(t.get("prompt", ""), 40),
            _trunc(",".join(t.get("tools", [])), 18),
            _fmt_dur_iso(t.get("ts"), t.get("end_ts")),
            "L%d" % t.get("line", 0),
            summary,
        ])
    print_table(
        ["#", "WHEN", "PROMPT", "TOOLS", "DUR", "LINE", "SUMMARY"],
        rows, title=None,
    )
    total = sm.get("totals", {}).get("prompts", 0)
    if len(turns) < total:
        print("  (showing %d of %d \u2014 pass --limit N or --limit 0 for all)"
              % (len(turns), total))


def cmd_archive_show(args):
    """Dump the full conversation in Q0/A0/Q1/A1/… order.

    No filtering or turn selection — this is the "cat the whole transcript
    in reading order" command. Pipe to less, or redirect to a file:
      camc archive show teadev | less
      camc archive show teadev > teadev-conversation.txt
    """
    import json as _json
    path = _resolve_archive(args.ref)
    if not path:
        sys.stderr.write("Error: archive '%s' not found\n" % args.ref)
        sys.exit(1)
    mf, sm = _load_archive_meta(path)
    if not sm:
        sys.stderr.write("Error: %s has no summary.json\n" % os.path.basename(path))
        sys.exit(1)
    jsonl = _read_archive_member(path, "claude/session.jsonl")
    if jsonl is None:
        sys.stderr.write("Error: no claude/session.jsonl in %s\n" % path)
        sys.exit(1)
    lines = jsonl.splitlines()
    turns = sm.get("turns") or []
    if not turns:
        print("(no turns in this archive)")
        return

    if getattr(args, "json", False):
        # Dump every record in the jsonl, one per line. Not filtered — the
        # user can `jq .` it downstream.
        for raw in lines:
            print(raw.decode("utf-8", errors="replace"))
        return

    def _short_target(name, inp):
        """One-line summary of a tool_use input. Matches archive summary."""
        if not isinstance(inp, dict):
            return ""
        if name == "Bash":
            cmd = (inp.get("command") or "").strip()
            return cmd.splitlines()[0] if cmd else ""
        if name in ("Edit", "Write", "Read"):
            return inp.get("file_path") or inp.get("path") or ""
        if name in ("Glob", "Grep"):
            return inp.get("pattern") or ""
        if name in ("WebFetch", "WebSearch"):
            return inp.get("url") or inp.get("query") or ""
        if name == "Task":
            return (inp.get("description") or inp.get("prompt") or "")
        for v in inp.values():
            if isinstance(v, str):
                return v
        return ""

    # Pair tool_result to tool_use by id. Trust ONLY the is_error flag on
    # the tool_result — text-based keyword heuristics are too loose (a
    # Read of a README that mentions "error" anywhere would false-trip).
    tool_status = {}  # tool_use_id → (ok: bool, err: str|None)
    for raw in lines:
        try:
            o = _json.loads(raw)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            tuid = c.get("tool_use_id") or c.get("toolUseId")
            if not tuid:
                continue
            is_err = bool(c.get("is_error"))
            err_text = None
            if is_err:
                # Extract the first meaningful line of the error payload.
                res = c.get("content")
                if isinstance(res, str):
                    err_text = res.strip().splitlines()[0][:200] if res.strip() else None
                elif isinstance(res, list):
                    for x in res:
                        if isinstance(x, dict) and x.get("type") == "text":
                            t = (x.get("text") or "").strip()
                            if t:
                                err_text = t.splitlines()[0][:200]
                                break
            tool_status[tuid] = (not is_err, err_text)

    # Header
    agent_name = (mf or {}).get("agent_name", "")
    agent_id = (mf or {}).get("agent_id", "")
    totals = sm.get("totals") or {}
    first_ts = (totals.get("first_ts") or "")[:10]
    last_ts = (totals.get("last_ts") or "")[:10]
    print("=== %s (%s) · %d turns · %s → %s ===" % (
        agent_name, agent_id, len(turns), first_ts, last_ts,
    ))
    print()

    for t in turns:
        idx = (t.get("idx") or 1) - 1  # 0-based Q0/A0 per user's format
        ts = (t.get("ts") or "")[:16].replace("T", " ")
        line_no = t.get("line") or 0

        # Find range of this turn in the jsonl
        next_line = None
        for nt in turns:
            if nt.get("line", 0) > line_no:
                next_line = nt["line"]
                break
        end = (next_line - 1) if next_line else len(lines)

        # Walk records in source order, label as Q then A
        printed_q = False
        a_header_printed = False
        for i in range(line_no - 1, min(end, len(lines))):
            try:
                o = _json.loads(lines[i])
            except Exception:
                continue
            ttype = o.get("type", "")
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}

            if ttype == "user" and not o.get("isMeta") and msg.get("role") == "user":
                content = msg.get("content")
                # Skip tool_result synthetic user turns inside this span
                if isinstance(content, list) and all(
                    isinstance(c, dict) and c.get("type") == "tool_result"
                    for c in content
                ):
                    continue
                if printed_q:
                    continue  # only one Q per turn
                text = content if isinstance(content, str) else ""
                if not text and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "")
                            break
                stripped = text.lstrip()
                if stripped.startswith("<local-command-") or stripped.startswith("<command-"):
                    continue
                print("## Q%d  %s  L%d" % (idx, ts, i + 1))
                print(text.rstrip())
                print()
                printed_q = True

            elif ttype == "assistant":
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                rec_ts = (o.get("timestamp")
                          or (msg.get("timestamp") if isinstance(msg, dict) else "")
                          or "")[:19].replace("T", " ")
                if not a_header_printed:
                    print("## A%d  %s" % (idx, rec_ts))
                    a_header_printed = True
                # Emit content items in source order so text and tool calls
                # interleave the way Claude actually produced them.
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct == "text":
                        txt = c.get("text") or ""
                        if txt.strip():
                            print(txt.rstrip())
                            print()
                    elif ct == "tool_use":
                        name = c.get("name", "?")
                        inp = c.get("input") or {}
                        target = _short_target(name, inp)
                        tuid = c.get("id")
                        ok, err = tool_status.get(tuid or "", (None, None))
                        marker = "✗" if err else "▸"
                        print("  %s %s(%s)" % (
                            marker, name, _trunc(str(target), 120),
                        ))
                        if err:
                            print("      └─ %s" % _trunc(err, 120))

        # Spacer between turns
        print()


def _json_dump(obj):
    import json as _json
    return _json.dumps(obj, indent=2, default=str)


def cmd_archive_dispatch(args):
    """Entry point for the `camc archive …` multiplexer."""
    sub = getattr(args, "archive_cmd", None)
    if sub == "list":
        cmd_archive_list(args)
    elif sub == "info":
        cmd_archive_info(args)
    elif sub == "summary":
        cmd_archive_summary(args)
    elif sub == "show":
        cmd_archive_show(args)
    elif sub == "create":
        cmd_archive(args)
    else:
        sys.stderr.write(
            "Usage:\n"
            "  camc archive <id|name>             create an archive\n"
            "  camc archive list                  list all archives\n"
            "  camc archive info <id|name>        header + last assistant text\n"
            "  camc archive summary <id|name>     per-prompt table\n"
            "  camc archive show <id|name>        dump full conversation (Q/A order)\n"
        )
        sys.exit(1)


def cmd_rm(args):
    """Remove an agent record, tear down tmux, delete the socket.

    Archive is opt-in via --archive. The default is OFF because workflows
    tend to spawn many short-lived agents and auto-archiving every one
    produces a lot of tarball noise. For agents you care about, either
    pass --archive, or run `camc archive <id>` explicitly before rm.
    --kill is a deprecated no-op: tmux is always killed on rm.
    """
    store = AgentStore()
    a = store.get(args.id)
    if not a:
        sys.stderr.write("Error: agent '%s' not found\n" % args.id); sys.exit(1)
    # Archive first, while the record + logs + (maybe) tmux are still alive.
    # Failing to archive should not block the rm — print a warning and carry on.
    if getattr(args, "archive", False):
        try:
            archive_args = argparse.Namespace(
                id=a["id"], output=None, session_id=None,
            )
            cmd_archive(archive_args)
        except SystemExit:
            print_warning("Archive step failed; proceeding with rm anyway.")
        except Exception as e:
            print_warning("Archive step raised %s; proceeding with rm anyway." % e)
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
    # Default is full scrollback (capture-pane -S -). Pass --lines N to
    # truncate to the last N lines. Tmux's history-limit (default 2000)
    # caps the real amount either way; long runs drop off the top.
    lines = getattr(args, "lines", 0) or 0
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
    tool = _agent_tool(a) if a else ""
    submit_delay = _tool_prompt_submit_delay(tool)
    _send_with_submit_delay(session, args.text, send_enter=send_enter,
                            submit_delay=submit_delay)
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
    rm = sub.add_parser("rm", help="Remove a single agent (kills tmux; archive off by default)")
    rm.add_argument("id", help="Agent ID")
    rm.add_argument("--archive", dest="archive", action="store_true",
                    default=False, help="Archive history before removing")
    rm.add_argument("--kill", "-k", action="store_true",
                    help="[deprecated, no-op] tmux is always killed now")

    # archive — dual-mode: `camc archive <id>` creates a new archive;
    #                       `camc archive <subcommand> …` inspects existing ones.
    ar = sub.add_parser(
        "archive",
        help="Archive one agent's history OR list/inspect existing archives",
    )
    # metavar hides the full choice list (which would otherwise include
    # the internal `create` subcommand) from the usage line.
    ar_sub = ar.add_subparsers(dest="archive_cmd", metavar="{list,info,summary,show}")

    # camc archive list
    ar_list = ar_sub.add_parser("list", help="List all archives as a table")
    ar_list.add_argument("--json", action="store_true", help="JSON output")

    # camc archive info <ref>
    ar_info = ar_sub.add_parser("info", help="Show header + file manifest + last assistant text")
    ar_info.add_argument("ref", help="Agent id prefix, agent name, or archive path")

    # camc archive summary <ref> — per-prompt table with LINE column
    ar_sum = ar_sub.add_parser(
        "summary",
        help="Per-prompt table for one archive (with LINE + SUMMARY columns)",
    )
    ar_sum.add_argument("ref", help="Agent id prefix, agent name, or archive path")
    ar_sum.add_argument("--limit", "-n", type=int, default=10,
                        help="Max prompts to show (0 = all, default 10)")
    ar_sum.add_argument("--search", default=None,
                        help="Substring filter on prompt text")
    ar_sum.add_argument("--tool", default=None,
                        help="Comma-separated tool filter; only show turns using one of them")
    ar_sum.add_argument("--json", action="store_true",
                        help="Emit the full payload as JSON")

    # camc archive show <ref> — dump the whole conversation, Q0/A0/Q1/A1/...
    ar_show = ar_sub.add_parser(
        "show",
        help="Dump the full conversation in Q/A order (pipe to less or redirect)",
    )
    ar_show.add_argument("ref", help="Agent id prefix, agent name, or archive path")
    ar_show.add_argument("--json", action="store_true",
                         help="Emit raw jsonl records instead of pretty-print")

    # camc archive create <id> — the default path when `camc archive <id>`
    # is invoked (argv pre-processor injects `create`). Users only ever see
    # the naked `camc archive <id>` form, so hide `create` from --help.
    # Note: `help=argparse.SUPPRESS` on a subparser still prints the
    # literal "==SUPPRESS==" in current Python (known arg-parse bug), so
    # we also pop it from the subparsers action's choices-list manually.
    ar_create = ar_sub.add_parser("create", help=argparse.SUPPRESS)
    ar_create.add_argument("id", help="Agent ID")
    ar_create.add_argument("--output", "-o", default=None,
                           help="Output directory (default: ~/.cam/archives/)")
    ar_create.add_argument("--session-id", dest="session_id", default=None,
                           help="Override session_id (skip auto-recovery)")
    # Hide 'create' from the parent parser's subcommand listing.
    for _act in list(ar_sub._choices_actions):
        if _act.dest == "create":
            ar_sub._choices_actions.remove(_act)

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
    cap.add_argument("--lines", "-n", type=int, default=0,
                     help="Tail last N lines (default: 0 = full scrollback)")

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

    # Dual-mode `archive`: if no inspect-subcommand is given, inject
    # `create` so `camc archive <id>` routes to the builder while
    # `camc archive list/info/…` route to the readers.
    _archive_subs = {"list", "info", "summary", "show", "create"}
    if (len(sys.argv) > 2 and sys.argv[1] == "archive"
            and sys.argv[2] not in _archive_subs
            and not sys.argv[2].startswith("-")):
        # Naked `camc archive <id>` form — route to the hidden `create`
        # subparser. If argv has no extra token (just `camc archive`) we
        # let argparse's subparser group print its normal usage/help
        # instead of triggering a misleading "create needs id" error.
        sys.argv.insert(2, "create")

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
        "archive": cmd_archive_dispatch,
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
