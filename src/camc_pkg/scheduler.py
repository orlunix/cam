"""DAG task scheduler: YAML parser, dependency graph, parallel execution."""

import json
import os
import sys
import time

from camc_pkg import log
from camc_pkg.utils import _now_iso, _load_default_context, _build_command
from camc_pkg.adapters import _load_config
from camc_pkg.storage import AgentStore, EventStore
from camc_pkg.transport import (
    capture_tmux, tmux_session_exists, tmux_send_input,
    tmux_kill_session, create_tmux_session,
)
from camc_pkg.detection import should_auto_confirm, is_ready_for_input


class SchedulerError(Exception):
    """Error raised by the task scheduler."""


# ---------------------------------------------------------------------------
# Task graph (DAG) — topological sort, cycle detection
# ---------------------------------------------------------------------------

class TaskGraph(object):
    """Directed Acyclic Graph of task dependencies."""

    def __init__(self, tasks):
        self._tasks = {}       # name -> task dict
        self._edges = {}       # name -> [dep names]

        for task in tasks:
            name = task.get("name")
            if not name:
                raise SchedulerError("All tasks must have a 'name' field")
            if name in self._tasks:
                raise SchedulerError("Duplicate task name: '%s'" % name)
            self._tasks[name] = task
            deps = task.get("depends_on", [])
            if isinstance(deps, str):
                deps = [deps]
            self._edges[name] = deps

        self._validate()

    def _validate(self):
        # Check all dependencies reference existing tasks
        for task_name, deps in self._edges.items():
            for dep in deps:
                if dep not in self._tasks:
                    raise SchedulerError(
                        "Task '%s' depends on '%s' which is not defined" % (task_name, dep))

        # Detect cycles using DFS
        visited = set()
        in_stack = set()

        def dfs(node):
            if node in in_stack:
                raise SchedulerError("Circular dependency detected involving task '%s'" % node)
            if node in visited:
                return
            in_stack.add(node)
            for dep in self._edges.get(node, []):
                dfs(dep)
            in_stack.discard(node)
            visited.add(node)

        for task_name in self._tasks:
            dfs(task_name)

    def execution_order(self):
        """Return tasks grouped by execution level (list of lists of names)."""
        in_degree = {name: len(self._edges.get(name, [])) for name in self._tasks}
        levels = []
        ready = sorted([name for name, deg in in_degree.items() if deg == 0])

        while ready:
            levels.append(ready)
            next_ready = []
            for completed_name in ready:
                for task_name, deps in self._edges.items():
                    if completed_name in deps:
                        in_degree[task_name] -= 1
                        if in_degree[task_name] == 0:
                            next_ready.append(task_name)
            ready = sorted(next_ready)

        return levels

    def get_task(self, name):
        return self._tasks[name]

    @property
    def task_names(self):
        return list(self._tasks.keys())

    def __len__(self):
        return len(self._tasks)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

def load_task_file(path):
    """Load a task YAML file and return (tasks, metadata).

    Each task is a dict with: name, tool, prompt, context, timeout,
    depends_on, env.
    """
    try:
        import yaml
    except ImportError:
        raise SchedulerError(
            "PyYAML is required for task files. Install with: pip install pyyaml")

    if not os.path.exists(path):
        raise SchedulerError("Task file not found: %s" % path)

    with open(path, "r") as f:
        try:
            data = yaml.safe_load(f)
        except Exception as e:
            raise SchedulerError("Invalid YAML in %s: %s" % (path, e))

    if not isinstance(data, dict):
        raise SchedulerError("Task file must be a YAML mapping")

    defaults = data.get("defaults", {})
    default_tool = defaults.get("tool", "claude")
    default_timeout = defaults.get("timeout")

    raw_tasks = data.get("tasks", [])
    if not raw_tasks:
        raise SchedulerError("No tasks defined in %s" % path)

    tasks = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise SchedulerError("Each task must be a mapping")
        name = raw.get("name")
        if not name:
            raise SchedulerError("Each task must have a 'name' field")
        prompt = raw.get("prompt")
        if not prompt:
            raise SchedulerError("Task '%s' is missing required 'prompt' field" % name)

        depends_on = raw.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]

        tasks.append({
            "name": name,
            "tool": raw.get("tool", default_tool),
            "prompt": prompt,
            "context": raw.get("context"),
            "timeout": raw.get("timeout", default_timeout),
            "depends_on": depends_on,
            "env": raw.get("env", {}),
        })

    metadata = {
        "version": data.get("version", "1"),
        "defaults": defaults,
        "task_count": len(tasks),
    }
    return tasks, metadata


# ---------------------------------------------------------------------------
# Scheduler — synchronous, level-by-level execution
# ---------------------------------------------------------------------------

def _launch_agent(task, workdir):
    """Launch a single agent for a task. Returns agent dict or None."""
    from uuid import uuid4
    import subprocess

    tool = task.get("tool", "claude")
    prompt = task.get("prompt", "")
    name = task.get("name")
    config = _load_config(tool)

    agent_id = uuid4().hex[:8]
    session = "cam-%s" % agent_id
    launch_cmd = _build_command(config, prompt, workdir)

    context = _load_default_context()
    env_setup = context.get("env_setup") or None

    if not create_tmux_session(session, launch_cmd, workdir, env_setup=env_setup, inherit_env=True):
        return None

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
            if config.prompt_submit_delay > 0:
                tmux_send_input(session, prompt, send_enter=False)
                time.sleep(config.prompt_submit_delay)
                tmux_send_key(session, "Enter")
            else:
                tmux_send_input(session, prompt, send_enter=True)

    store = AgentStore()
    import socket as _sock
    ctx_name = context.get("name", "") if isinstance(context, dict) else ""
    ctx_host = context.get("host") if isinstance(context, dict) else None
    transport = "ssh" if ctx_host and ctx_host not in ("localhost", "127.0.0.1") else "local"
    agent = {
        "id": agent_id,
        "task": {"name": name or "", "tool": tool, "prompt": prompt,
                 "auto_confirm": True, "auto_exit": False},
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
    }
    store.save(agent)

    # Spawn background monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(sys.argv[0]), "_monitor", agent_id] if os.path.isfile(sys.argv[0]) else [sys.executable, "-m", "camc_pkg", "_monitor", agent_id],
            stdout=subprocess.DEVNULL,
            stderr=open("/tmp/camc-%s.log" % agent_id, "a"),
            start_new_session=True)
        store.update(agent_id, pid=proc.pid)
    except Exception:
        pass

    return agent


def run_dag(graph, workdir=None, dry_run=False, poll_interval=5, timeout=None):
    """Execute a TaskGraph synchronously, level by level.

    Args:
        graph: Validated TaskGraph.
        workdir: Working directory for agents (default: cwd).
        dry_run: If True, just print plan without executing.
        poll_interval: Seconds between status polls.
        timeout: Max seconds to wait per level (None = no limit).

    Returns:
        Dict mapping task name -> final status string.
    """
    workdir = workdir or os.getcwd()
    levels = graph.execution_order()
    results = {}  # task_name -> {"status": ..., "agent_id": ...}

    # Print plan
    print("Task graph: %d tasks, %d levels" % (len(graph), len(levels)))
    for level_idx, level in enumerate(levels):
        print("  Level %d:" % (level_idx + 1))
        for task_name in level:
            task = graph.get_task(task_name)
            deps = " (after: %s)" % ", ".join(task.get("depends_on", [])) if task.get("depends_on") else ""
            print("    %s: [%s] %s%s" % (task_name, task.get("tool", "claude"),
                  (task.get("prompt", ""))[:50], deps))
    print()

    if dry_run:
        print("Dry run complete — no tasks were executed.")
        return results

    store = AgentStore()
    events = EventStore()

    for level_idx, level in enumerate(levels):
        print("--- Level %d/%d ---" % (level_idx + 1, len(levels)))

        # Check dependencies succeeded
        for task_name in level:
            task = graph.get_task(task_name)
            for dep in task.get("depends_on", []):
                dep_result = results.get(dep, {})
                if dep_result.get("status") != "completed":
                    msg = "Task '%s' skipped: dependency '%s' %s" % (
                        task_name, dep, dep_result.get("status", "not run"))
                    print("  ✗ %s" % msg)
                    results[task_name] = {"status": "skipped", "agent_id": None}
                    events.append(task_name, "skipped", {"reason": msg})
                    continue

        # Launch agents for this level
        level_agents = {}  # task_name -> agent_id
        for task_name in level:
            if task_name in results:
                continue  # Already handled (skipped)
            task = graph.get_task(task_name)
            print("  Starting: %s [%s]" % (task_name, task.get("tool", "claude")))
            agent = _launch_agent(task, workdir)
            if agent:
                level_agents[task_name] = agent["id"]
                print("    Agent %s session %s" % (agent["id"], agent.get("tmux_session", "")))
                events.append(agent["id"], "dag_task_start", {"task": task_name, "level": level_idx + 1})
            else:
                print("    ✗ Failed to launch")
                results[task_name] = {"status": "failed", "agent_id": None}

        # Wait for all agents in this level to complete
        if level_agents:
            start = time.time()
            pending = set(level_agents.keys())
            while pending:
                if timeout and (time.time() - start) > timeout:
                    for tn in list(pending):
                        results[tn] = {"status": "timeout", "agent_id": level_agents[tn]}
                    print("  ⚠ Level timeout reached")
                    break
                time.sleep(poll_interval)
                for task_name in list(pending):
                    agent_id = level_agents[task_name]
                    a = store.get(agent_id)
                    if not a:
                        continue
                    status = a.get("status", "running")
                    if status != "running":
                        results[task_name] = {"status": status, "agent_id": agent_id}
                        pending.discard(task_name)
                        icon = "✓" if status == "completed" else "✗"
                        print("  %s %s: %s [%s]" % (icon, task_name, status, agent_id))

    # Summary
    print()
    completed = sum(1 for r in results.values() if r.get("status") == "completed")
    failed = sum(1 for r in results.values() if r.get("status") in ("failed", "timeout", "skipped"))
    print("Results: %d completed, %d failed/skipped out of %d" % (completed, failed, len(results)))

    return results
