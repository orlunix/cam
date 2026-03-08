#!/usr/bin/env python3
"""cam-client: Local agent monitor and shared library for CAM target machines.

Runs on the target machine alongside the tmux session. Performs auto-confirm,
state detection, and completion detection locally (<1ms), then syncs state
to the CAM server via HTTP (~2-10ms local, ~200ms remote).

Also serves as the shared library for camc (standalone CLI).

Requirements: Python 3.6+, stdlib only (no pip dependencies).

Usage (standalone monitor):
    python3 cam-client.py \\
        --agent-id <uuid> \\
        --session <tmux-session-name> \\
        --server http://cam-server:8420 \\
        --token <auth-token> \\
        --tool claude
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time

try:
    import urllib.error
    import urllib.request
except ImportError:
    pass  # Not needed when used as library by camc

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cam-client] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("cam-client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOCKETS_DIR = "/tmp/cam-sockets"

# ---------------------------------------------------------------------------
# Minimal TOML parser (subset: strings, bools, numbers, arrays, tables)
# ---------------------------------------------------------------------------


def _parse_toml(text):
    """Parse TOML text into a dict. Handles the adapter config subset."""
    root = {}
    current = root

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Array of tables: [[foo.bar]]
        m = re.match(r"^\[\[([^\]]+)\]\]$", line)
        if m:
            key_path = m.group(1).strip().split(".")
            target = root
            for k in key_path[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]
            last = key_path[-1]
            if last not in target:
                target[last] = []
            new_item = {}
            target[last].append(new_item)
            current = new_item
            continue

        # Table: [foo] or [foo.bar]
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            key_path = m.group(1).strip().split(".")
            target = root
            for k in key_path:
                if k not in target:
                    target[k] = {}
                target = target[k]
            current = target
            continue

        # Key = value
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if m:
            current[m.group(1)] = _parse_toml_value(m.group(2).strip())

    return root


def _parse_toml_value(s):
    if s.startswith('"'):
        i, result = 1, []
        while i < len(s):
            c = s[i]
            if c == "\\":
                i += 1
                if i < len(s):
                    esc = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(s[i], "\\" + s[i])
                    result.append(esc)
            elif c == '"':
                break
            else:
                result.append(c)
            i += 1
        return "".join(result)
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith("["):
        inner = s[1:].rstrip()
        if inner.endswith("]"):
            inner = inner[:-1]
        return [_parse_toml_value(p.strip()) for p in inner.split(",") if p.strip()]
    val = s.split("#")[0].strip()
    try:
        return float(val) if "." in val else int(val)
    except ValueError:
        return val


def load_toml(path):
    """Load a TOML file. Uses stdlib tomllib (3.11+), tomli, or embedded parser."""
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, ModuleNotFoundError):
        pass
    try:
        import tomli
        with open(path, "rb") as f:
            return tomli.load(f)
    except (ImportError, ModuleNotFoundError):
        pass
    with open(path, "r") as f:
        return _parse_toml(f.read())


# ---------------------------------------------------------------------------
# ANSI strip
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"\x1B\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1B\][^\x07]*\x07"
    r"|\x1B[()][AB012]"
    r"|\x1B[@-_]"
)


def strip_ansi(text):
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Box-drawing cleanup
# ---------------------------------------------------------------------------

_BOX_CHARS = u"\u2500\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u256d\u256e\u2570\u256f \t"


def clean_for_confirm(text):
    lines = [line.strip(_BOX_CHARS) for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Pattern compiler
# ---------------------------------------------------------------------------

_RE_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}


def compile_pattern(pattern, flags=None):
    f = 0
    for name in (flags or []):
        upper = name.upper()
        if upper not in _RE_FLAGS:
            raise ValueError("Unknown regex flag: %r (valid: %s)" % (name, list(_RE_FLAGS)))
        f |= _RE_FLAGS[upper]
    return re.compile(pattern, f)


# ---------------------------------------------------------------------------
# Adapter config parser
# ---------------------------------------------------------------------------

class AdapterConfig(object):
    """Parsed adapter config from a dict (TOML or JSON)."""

    def __init__(self, config):
        launch = config.get("launch", {})
        self.strip_ansi = launch.get("strip_ansi", False)
        self.command = launch.get("command", [])

        rp = launch.get("ready_pattern")
        self.ready_pattern = compile_pattern(rp, launch.get("ready_flags")) if rp else None
        self.prompt_after_launch = launch.get("prompt_after_launch", False)
        self.startup_wait = float(launch.get("startup_wait", 2.0))

        state_cfg = config.get("state", {})
        self.state_strategy = state_cfg.get("strategy", "first")
        self.state_recent_chars = state_cfg.get("recent_chars", 2000)
        self.state_patterns = []
        for entry in state_cfg.get("patterns", []):
            self.state_patterns.append((
                entry["state"],
                compile_pattern(entry["pattern"], entry.get("flags")),
            ))

        comp = config.get("completion", {})
        self.completion_strategy = comp.get("strategy", "process_exit")
        self.completion_recent_chars = comp.get("recent_chars", 500)
        self.min_output_length = comp.get("min_output_length", 100)

        cp = comp.get("completion_pattern")
        self.completion_pattern = compile_pattern(cp, comp.get("completion_flags")) if cp else None
        ep = comp.get("error_pattern")
        self.error_pattern = compile_pattern(ep, comp.get("error_flags")) if ep else None
        self.error_search_full = comp.get("error_search_full", True)

        sp = comp.get("shell_prompt_pattern")
        self.shell_prompt_pattern = compile_pattern(sp, comp.get("shell_prompt_flags")) if sp else None

        pp = comp.get("prompt_pattern")
        self.prompt_pattern = compile_pattern(pp, comp.get("prompt_flags")) if pp else None
        self.prompt_count_threshold = comp.get("prompt_count_threshold", 2)
        fp = comp.get("fallback_summary_pattern")
        self.fallback_summary_pattern = compile_pattern(fp, comp.get("fallback_summary_flags")) if fp else None

        self.confirm_rules = []
        for rule in config.get("confirm", []):
            self.confirm_rules.append((
                compile_pattern(rule["pattern"], rule.get("flags")),
                rule.get("response", ""),
                rule.get("send_enter", True),
            ))

        probe_cfg = config.get("probe", {})
        self.probe_char = probe_cfg.get("char", "Z")
        self.probe_confirm_response = probe_cfg.get("confirm_response", "")
        self.probe_confirm_send_enter = probe_cfg.get("confirm_send_enter", True)
        self.probe_wait = float(probe_cfg.get("wait", 0.3))
        self.probe_idle_threshold = int(probe_cfg.get("idle_threshold", 2))

        mon_cfg = config.get("monitor", {})
        self.confirm_cooldown = float(mon_cfg.get("confirm_cooldown", 5.0))
        self.confirm_sleep = float(mon_cfg.get("confirm_sleep", 0.5))
        self.completion_stable = float(mon_cfg.get("completion_stable", 3.0))
        self.health_check_interval = float(mon_cfg.get("health_check_interval", 15))
        self.empty_threshold = int(mon_cfg.get("empty_threshold", 3))
        self.auto_exit = bool(mon_cfg.get("auto_exit", False))
        self.exit_action = mon_cfg.get("exit_action", "kill_session")
        self.exit_command = mon_cfg.get("exit_command", "/exit")


# ---------------------------------------------------------------------------
# Subprocess helper (3.6 compat: no capture_output, no text)
# ---------------------------------------------------------------------------

def _run(args, timeout=5, check=False):
    """Run command, return (returncode, stdout_str)."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, _ = proc.communicate(timeout=timeout)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, args)
        return proc.returncode, stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return -1, ""
    except Exception:
        if check:
            raise
        return -1, ""


# ---------------------------------------------------------------------------
# TMUX interaction
# ---------------------------------------------------------------------------

def _find_tmux_socket(session_id):
    for sock_dir in (SOCKETS_DIR, "/tmp/cam-agent-sockets"):
        sock = "%s/%s.sock" % (sock_dir, session_id)
        if os.path.exists(sock):
            return sock
    return None


def _tmux_base(session_id):
    socket = _find_tmux_socket(session_id)
    return ["tmux", "-u", "-S", socket] if socket else ["tmux"]


def capture_tmux(session_id, lines=100):
    socket = _find_tmux_socket(session_id)
    target = "%s:0.0" % session_id
    if socket:
        args = ["tmux", "-u", "-S", socket, "capture-pane", "-p", "-J",
                "-t", target, "-S", "-%d" % lines]
    else:
        args = ["tmux", "capture-pane", "-p", "-J",
                "-t", target, "-S", "-%d" % lines]
    try:
        rc, output = _run(args)
        if len(output.strip()) < 20:
            alt = list(args)
            alt.insert(alt.index("capture-pane") + 1, "-a")
            rc2, alt_out = _run(alt)
            if len(alt_out.strip()) > len(output.strip()):
                output = alt_out
        return strip_ansi(output).rstrip()
    except Exception as e:
        log.debug("capture_tmux failed: %s", e)
        return ""


def tmux_session_exists(session_id):
    socket = _find_tmux_socket(session_id)
    if socket:
        args = ["tmux", "-u", "-S", socket, "has-session", "-t", session_id]
    else:
        args = ["tmux", "has-session", "-t", session_id]
    rc, _ = _run(args)
    return rc == 0


def tmux_send_input(session_id, text, send_enter=True):
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    try:
        if text:
            _run(base + ["send-keys", "-t", target, "-l", "--", text], check=True)
        if send_enter:
            _run(base + ["send-keys", "-t", target, "Enter"], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_input failed: %s", e)
        return False


def tmux_send_key(session_id, key):
    base = _tmux_base(session_id)
    target = "%s:0.0" % session_id
    try:
        _run(base + ["send-keys", "-t", target, key], check=True)
        return True
    except Exception as e:
        log.warning("tmux_send_key failed: %s", e)
        return False


def tmux_kill_session(session_id):
    socket = _find_tmux_socket(session_id)
    if socket:
        args = ["tmux", "-u", "-S", socket, "kill-session", "-t", session_id]
    else:
        args = ["tmux", "kill-session", "-t", session_id]
    rc, _ = _run(args)
    return rc == 0


def create_tmux_session(session_id, command, workdir, env_setup=None):
    """Create a detached tmux session with a per-session socket."""
    try:
        os.makedirs(SOCKETS_DIR)
    except OSError:
        pass
    socket = "%s/%s.sock" % (SOCKETS_DIR, session_id)

    inner_cmd = " ".join(shlex.quote(arg) for arg in command)
    if env_setup:
        command_str = "env -u CLAUDECODE bash -l -c %s" % shlex.quote(env_setup + " && exec " + inner_cmd)
    else:
        command_str = "env -u CLAUDECODE bash -l -c %s" % shlex.quote("exec " + inner_cmd)

    try:
        proc = subprocess.Popen(
            ["tmux", "-u", "-S", socket, "new-session",
             "-d", "-x", "220", "-y", "50",
             "-s", session_id, "-c", workdir, command_str],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=10)
        if proc.returncode != 0:
            return False
        _run(["tmux", "-u", "-S", socket, "set-option", "-t", session_id,
              "history-limit", "50000"])
        return True
    except Exception as e:
        log.error("Failed to create session %s: %s", session_id, e)
        return False


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def detect_state(output, config):
    recent = output[-config.state_recent_chars:]
    if config.strip_ansi:
        recent = strip_ansi(recent)
    if config.state_strategy == "last":
        last_pos, last_state = -1, None
        for state_name, pattern in config.state_patterns:
            for m in pattern.finditer(recent):
                if m.start() > last_pos:
                    last_pos = m.start()
                    last_state = state_name
        return last_state
    else:
        for state_name, pattern in config.state_patterns:
            if pattern.search(recent):
                return state_name
        return None


def should_auto_confirm(output, config):
    if config.strip_ansi:
        output = strip_ansi(output)
    clean = clean_for_confirm(output)
    recent = clean[-500:] if len(clean) > 500 else clean
    for pattern, response, send_enter in config.confirm_rules:
        if pattern.search(recent):
            return (response, send_enter)
    return None


def detect_completion(output, config):
    if config.completion_strategy == "process_exit":
        return None
    if config.completion_strategy == "prompt_count":
        return _detect_prompt_count(output, config)
    return _detect_pattern(output, config)


def _detect_pattern(output, config):
    if config.strip_ansi:
        output = strip_ansi(output)
    if config.error_pattern:
        search_text = output if config.error_search_full else output[-config.completion_recent_chars:]
        if config.error_pattern.search(search_text):
            return "failed"
    recent = output[-config.completion_recent_chars:]
    if config.completion_pattern and config.completion_pattern.search(recent):
        return "completed"
    if (config.shell_prompt_pattern
            and config.shell_prompt_pattern.search(recent)
            and len(output) > config.min_output_length):
        return "completed"
    return None


def _detect_prompt_count(output, config):
    if not config.prompt_pattern:
        return None
    clean = strip_ansi(output) if config.strip_ansi else output
    count = len(config.prompt_pattern.findall(clean))
    if count >= config.prompt_count_threshold:
        # Guard against false completion when a confirm/permission dialog is
        # active. The prompt char (e.g. ❯) may appear as part of an Ink select
        # menu ("❯ 1. Yes"), inflating the count. If the output matches any
        # confirm pattern, the tool is waiting for approval — not idle.
        if config.confirm_rules:
            for cp, _resp, _enter in config.confirm_rules:
                if cp.search(clean):
                    return None
        return "completed"
    if (count == 1
            and config.fallback_summary_pattern
            and config.fallback_summary_pattern.search(clean)):
        return "completed"
    return None


def is_ready_for_input(output, config):
    if not config.ready_pattern:
        return True
    clean = strip_ansi(output) if config.strip_ansi else output
    return bool(config.ready_pattern.search(clean))


# ---------------------------------------------------------------------------
# HTTP sync (used by ClientMonitor, not by camc)
# ---------------------------------------------------------------------------

def sync_with_server(server_url, agent_id, token, **kwargs):
    """POST sync data to server and return response."""
    url = "%s/api/client/%s/sync" % (server_url, agent_id)
    body = {
        "output": kwargs.get("output"),
        "output_hash": kwargs.get("output_hash"),
        "state": kwargs.get("state"),
        "status": kwargs.get("status"),
        "exit_reason": kwargs.get("exit_reason"),
        "events": kwargs.get("events", []),
        "cost_estimate": kwargs.get("cost_estimate"),
        "files_changed": kwargs.get("files_changed"),
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("Sync failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------

def write_pid(agent_id):
    pid_path = "/tmp/cam-client-%s.pid" % agent_id
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))
    return pid_path


def remove_pid(agent_id):
    try:
        os.unlink("/tmp/cam-client-%s.pid" % agent_id)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ClientMonitor (server-syncing monitor loop)
# ---------------------------------------------------------------------------

class ClientMonitor(object):
    def __init__(self, agent_id, session_id, server_url, token,
                 adapter_config, auto_confirm=True, poll_interval=1.0,
                 prompt=""):
        self.agent_id = agent_id
        self.session_id = session_id
        self.server_url = server_url
        self.token = token
        self.config = adapter_config
        self.auto_confirm = auto_confirm
        self.poll_interval = poll_interval

        self._previous_output = ""
        self._previous_hash = ""
        self._current_state = None
        self._last_confirm_time = 0.0
        self._has_worked = False
        self._running = True
        self._last_change_time = time.time()
        self._completion_detected = None

    def stop(self):
        self._running = False

    def run(self):
        log.info("Starting monitor for agent %s session %s", self.agent_id, self.session_id)
        write_pid(self.agent_id)
        try:
            self._loop()
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            remove_pid(self.agent_id)
            log.info("Monitor stopped for agent %s", self.agent_id)

    def _loop(self):
        last_health_check = time.time()

        while self._running:
            now = time.time()
            events = []

            if now - last_health_check >= self.config.health_check_interval:
                last_health_check = now
                if not tmux_session_exists(self.session_id):
                    log.info("Session %s gone", self.session_id)
                    completion = detect_completion(self._previous_output, self.config)
                    status = completion or ("completed" if self._has_worked else "failed")
                    reason = "Session exited" if self._has_worked else "Session exited before agent started working"
                    sync_with_server(
                        self.server_url, self.agent_id, self.token,
                        output=self._previous_output, output_hash=self._previous_hash,
                        status=status, exit_reason=reason,
                    )
                    return

            output = capture_tmux(self.session_id)
            output_hash = hashlib.md5(output.encode()).hexdigest()[:8]
            output_changed = output_hash != self._previous_hash
            if output_changed:
                self._last_change_time = now
            self._previous_output = output
            self._previous_hash = output_hash

            if not output.strip():
                time.sleep(self.poll_interval)
                continue

            if self.auto_confirm and now - self._last_confirm_time >= self.config.confirm_cooldown:
                confirm = should_auto_confirm(output, self.config)
                if confirm is not None:
                    response, send_enter = confirm
                    self._last_confirm_time = now
                    tmux_send_input(self.session_id, response, send_enter=send_enter)
                    events.append({"type": "auto_confirm",
                                   "detail": {"response": response, "send_enter": send_enter},
                                   "ts": now})
                    log.info("Auto-confirmed: response=%r send_enter=%s", response, send_enter)
                    time.sleep(self.config.confirm_sleep)
                    continue

            new_state = detect_state(output, self.config)
            state_to_send = None
            if new_state and new_state != self._current_state:
                if new_state != "initializing":
                    self._has_worked = True
                old_state = self._current_state
                self._current_state = new_state
                state_to_send = new_state
                events.append({"type": "state_change",
                               "detail": {"from": old_state or "initializing", "to": new_state},
                               "ts": now})

            # Detect completion
            status_to_send = None
            exit_reason = None
            idle_for = now - self._last_change_time

            if not output_changed and idle_for >= self.config.completion_stable:
                completion = detect_completion(output, self.config)
                if completion:
                    if not self._completion_detected:
                        self._completion_detected = completion
                        events.append({"type": "completion_detected",
                                       "detail": {"status": completion},
                                       "ts": now})

            # Reset completion on real output change
            if output_changed:
                self._completion_detected = None

            # Auto-exit: completion confirmed + idle stable
            if (
                self.config.auto_exit
                and self._has_worked
                and self._completion_detected == "completed"
                and idle_for >= self.config.completion_stable * 2
            ):
                exit_action = self.config.exit_action
                log.info("Auto-exit: action=%s", exit_action)
                if exit_action == "kill_session":
                    tmux_kill_session(self.session_id)
                elif exit_action == "send_exit":
                    tmux_send_input(self.session_id, self.config.exit_command, send_enter=True)
                    for _ in range(10):
                        time.sleep(1)
                        if not tmux_session_exists(self.session_id):
                            break
                    else:
                        tmux_kill_session(self.session_id)
                status_to_send = "completed"
                exit_reason = "Task completed (auto-exit)"

            sync_output = output if output_changed else None
            response = sync_with_server(
                self.server_url, self.agent_id, self.token,
                output=sync_output, output_hash=output_hash,
                state=state_to_send, status=status_to_send,
                exit_reason=exit_reason, events=events,
            )

            for cmd in response.get("commands", []):
                self._execute_command(cmd)
            server_ac = response.get("auto_confirm")
            if server_ac is not None:
                self.auto_confirm = server_ac
            server_interval = response.get("interval")
            if server_interval:
                self.poll_interval = max(0.5, min(10.0, server_interval))

            if status_to_send in ("completed", "failed"):
                log.info("Agent %s: %s (%s)", self.agent_id, status_to_send, exit_reason)
                return

            time.sleep(self.poll_interval)

    def _execute_command(self, cmd):
        cmd_type = cmd.get("type")
        if cmd_type == "input":
            tmux_send_input(self.session_id, cmd.get("text", ""), send_enter=cmd.get("send_enter", True))
        elif cmd_type == "key":
            tmux_send_key(self.session_id, cmd.get("key", ""))
        elif cmd_type == "stop":
            if cmd.get("graceful", True):
                tmux_send_key(self.session_id, "C-c")
                time.sleep(2)
            tmux_kill_session(self.session_id)
            self.stop()


# ---------------------------------------------------------------------------
# AgentStore — JSON file with fcntl locking (shared with camc)
# ---------------------------------------------------------------------------

CAM_DIR = os.path.expanduser("~/.cam")
CONFIGS_DIR = os.path.join(CAM_DIR, "configs")
AGENTS_FILE = os.path.join(CAM_DIR, "agents.json")

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


class AgentStore(object):
    def __init__(self, path=None):
        self._path = path or AGENTS_FILE

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
                agents = []
                if os.path.exists(self._path):
                    try:
                        with open(self._path, "r") as f:
                            agents = json.load(f)
                    except (ValueError, OSError):
                        agents = []
                agents = fn(agents)
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(agents, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            finally:
                if _fcntl:
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)

    def list(self):
        return self._read()

    def get(self, agent_id):
        agents = self._read()
        for a in agents:
            if a["id"] == agent_id:
                return a
        matches = [a for a in agents if a["id"].startswith(agent_id)]
        return matches[0] if len(matches) == 1 else None

    def save(self, agent):
        def _do(agents):
            for i, a in enumerate(agents):
                if a["id"] == agent["id"]:
                    agents[i] = agent
                    return agents
            agents.append(agent)
            return agents
        self._modify(_do)

    def update(self, agent_id, **fields):
        result = [None]
        def _do(agents):
            for a in agents:
                if a["id"] == agent_id:
                    a.update(fields)
                    result[0] = a
                    break
            return agents
        self._modify(_do)
        return result[0]

    def remove(self, agent_id):
        found = [False]
        def _do(agents):
            new = [a for a in agents if a["id"] != agent_id]
            found[0] = len(new) < len(agents)
            return new
        self._modify(_do)
        return found[0]


# ---------------------------------------------------------------------------
# Subcommand helpers
# ---------------------------------------------------------------------------

_VERSION = "0.2.0"


def _json_out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _get_arg(flag, default=None):
    try:
        idx = sys.argv.index(flag)
        return sys.argv[idx + 1]
    except (ValueError, IndexError):
        return default


def _has_flag(flag):
    return flag in sys.argv


def _get_args_after_separator():
    try:
        idx = sys.argv.index("--")
        return sys.argv[idx + 1:]
    except ValueError:
        return []


def _now_iso():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Subcommand: ping
# ---------------------------------------------------------------------------

def _cmd_ping():
    import platform
    _json_out({"ok": True, "version": _VERSION, "platform": platform.system()})


# ---------------------------------------------------------------------------
# Subcommand: status [--hash H]
# ---------------------------------------------------------------------------

def _cmd_status():
    store = AgentStore()
    agents = store.list()
    raw = json.dumps(agents, sort_keys=True)
    h = hashlib.md5(raw.encode()).hexdigest()[:8]
    req_hash = _get_arg("--hash")
    if req_hash and req_hash == h:
        _json_out({"unchanged": True, "hash": h})
    else:
        _json_out({"agents": agents, "hash": h})


# ---------------------------------------------------------------------------
# Subcommand: session create --id SID --workdir DIR [--tool TOOL] [--env-setup CMD] -- CMD...
# ---------------------------------------------------------------------------

def _cmd_session_create():
    sid = _get_arg("--id")
    workdir = _get_arg("--workdir")
    tool = _get_arg("--tool", "generic")
    env_setup = _get_arg("--env-setup")
    command = _get_args_after_separator()

    if not sid or not workdir or not command:
        _json_out({"ok": False, "error": "Missing --id, --workdir, or command after --"})
        sys.exit(1)

    ok = create_tmux_session(sid, command, workdir, env_setup=env_setup)
    if not ok:
        _json_out({"ok": False, "error": "Failed to create tmux session"})
        sys.exit(1)

    # Register agent in store
    store = AgentStore()
    store.save({
        "id": sid, "tool": tool, "session": sid, "status": "running",
        "state": "initializing", "prompt": "", "path": workdir,
        "started_at": _now_iso(), "completed_at": None, "exit_reason": None,
        "monitor_pid": None,
    })

    # Spawn background monitor
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(sys.argv[0]), "_monitor_bg", sid, tool],
            stdout=subprocess.DEVNULL,
            stderr=open("/tmp/cam-client-monitor-%s.log" % sid, "a"),
            start_new_session=True,
        )
        store.update(sid, monitor_pid=proc.pid)
    except Exception as e:
        log.warning("Failed to spawn monitor for %s: %s", sid, e)

    _json_out({"ok": True})


# ---------------------------------------------------------------------------
# Subcommand: session exists --id SID
# ---------------------------------------------------------------------------

def _cmd_session_exists():
    sid = _get_arg("--id")
    if not sid:
        sys.exit(1)
    sys.exit(0 if tmux_session_exists(sid) else 1)


# ---------------------------------------------------------------------------
# Subcommand: session capture --id SID [--lines N] [--hash H]
# ---------------------------------------------------------------------------

def _cmd_session_capture():
    sid = _get_arg("--id")
    lines = int(_get_arg("--lines", "100"))
    req_hash = _get_arg("--hash")

    if not sid:
        sys.exit(1)

    output = capture_tmux(sid, lines=lines)
    h = hashlib.md5(output.encode()).hexdigest()[:8]

    if req_hash and req_hash == h:
        sys.stdout.write("hash:%s\n" % h)
    else:
        sys.stdout.write("hash:%s\n%s" % (h, output))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Subcommand: session send --id SID --text TEXT [--no-enter]
# ---------------------------------------------------------------------------

def _cmd_session_send():
    sid = _get_arg("--id")
    text = _get_arg("--text", "")
    no_enter = _has_flag("--no-enter")

    if not sid:
        _json_out({"ok": False, "error": "Missing --id"})
        sys.exit(1)

    ok = tmux_send_input(sid, text, send_enter=not no_enter)
    _json_out({"ok": ok})


# ---------------------------------------------------------------------------
# Subcommand: session key --id SID --key KEY
# ---------------------------------------------------------------------------

def _cmd_session_key():
    sid = _get_arg("--id")
    key = _get_arg("--key")

    if not sid or not key:
        _json_out({"ok": False, "error": "Missing --id or --key"})
        sys.exit(1)

    ok = tmux_send_key(sid, key)
    _json_out({"ok": ok})


# ---------------------------------------------------------------------------
# Subcommand: session kill --id SID
# ---------------------------------------------------------------------------

def _cmd_session_kill():
    sid = _get_arg("--id")
    if not sid:
        _json_out({"ok": False, "error": "Missing --id"})
        sys.exit(1)

    # Kill monitor process if tracked
    store = AgentStore()
    agent = store.get(sid)
    if agent:
        pid = agent.get("monitor_pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        store.update(sid, status="killed", exit_reason="Killed via protocol",
                     completed_at=_now_iso())

    ok = tmux_kill_session(sid)
    _json_out({"ok": ok})


# ---------------------------------------------------------------------------
# Subcommand: file list --path PATH
# ---------------------------------------------------------------------------

def _cmd_file_list():
    path = _get_arg("--path")
    if not path:
        _json_out({"entries": []})
        return

    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.lstat(full)
                if os.path.islink(full):
                    ftype = "symlink"
                elif os.path.isdir(full):
                    ftype = "dir"
                else:
                    ftype = "file"
                entries.append({
                    "name": name, "type": ftype,
                    "size": st.st_size if ftype == "file" else 0,
                    "mtime": int(st.st_mtime),
                })
            except OSError:
                continue
    except OSError:
        pass
    _json_out({"entries": entries})


# ---------------------------------------------------------------------------
# Subcommand: file read --path PATH [--max-bytes N]
# ---------------------------------------------------------------------------

def _cmd_file_read():
    path = _get_arg("--path")
    max_bytes = int(_get_arg("--max-bytes", "512000"))
    if not path or not os.path.isfile(path):
        sys.exit(1)

    if hasattr(sys.stdout, "buffer"):
        with open(path, "rb") as f:
            sys.stdout.buffer.write(f.read(max_bytes))
    else:
        with open(path, "rb") as f:
            sys.stdout.write(f.read(max_bytes))


# ---------------------------------------------------------------------------
# Subcommand: file write --path PATH (data on stdin)
# ---------------------------------------------------------------------------

def _cmd_file_write():
    path = _get_arg("--path")
    if not path:
        _json_out({"ok": False, "error": "Missing --path"})
        sys.exit(1)

    try:
        d = os.path.dirname(path)
        if d:
            try:
                os.makedirs(d)
            except OSError:
                pass
        if hasattr(sys.stdin, "buffer"):
            data = sys.stdin.buffer.read()
        else:
            data = sys.stdin.read()
        with open(path, "wb") as f:
            if isinstance(data, str):
                data = data.encode("utf-8")
            f.write(data)
        _json_out({"ok": True, "bytes": len(data)})
    except Exception as e:
        _json_out({"ok": False, "error": str(e)})
        sys.exit(1)


# ---------------------------------------------------------------------------
# Background monitor (hidden _monitor_bg subcommand)
# ---------------------------------------------------------------------------

def _run_local_monitor(session_id, tool):
    """Background monitor loop — same logic as camc's _run_monitor."""
    store = AgentStore()
    agent = store.get(session_id)
    if not agent:
        sys.exit(1)

    toml_path = os.path.join(CONFIGS_DIR, "%s.toml" % tool)
    if not os.path.exists(toml_path):
        log.error("Config not found: %s", toml_path)
        sys.exit(1)
    config = AdapterConfig(load_toml(toml_path))
    session = agent.get("session", session_id)

    pid_path = "/tmp/cam-client-monitor-%s.pid" % session_id
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))

    running = [True]
    signal.signal(signal.SIGTERM, lambda s, f: running.__setitem__(0, False))

    prev_hash = ""
    last_change = last_health = time.time()
    last_confirm = 0.0
    current_state = None
    has_worked = False
    empty_count = 0

    try:
        while running[0]:
            now = time.time()

            if now - last_health >= 15:
                last_health = now
                if not tmux_session_exists(session):
                    status = "completed" if has_worked else "failed"
                    store.update(session_id, status=status,
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
                    store.update(session_id, status=status,
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
                store.update(session_id, state=ns)

            # Detect completion for status reporting (but never auto-exit)
            if not changed and now - last_change >= 3.0:
                done = detect_completion(output, config)
                if done:
                    store.update(session_id, state="idle")

            time.sleep(1)
    finally:
        try:
            os.unlink(pid_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Agent management subcommands
# ---------------------------------------------------------------------------

def _cmd_agent_rm():
    """Remove an agent from ~/.cam/agents.json (and optionally kill its session)."""
    aid = _get_arg("--id")
    if not aid:
        _json_out({"ok": False, "error": "Missing --id"})
        sys.exit(1)

    kill = "--kill" in sys.argv
    store = AgentStore()
    agent = store.get(aid)
    if not agent:
        _json_out({"ok": False, "error": "Agent not found: %s" % aid})
        sys.exit(1)

    # Kill session + monitor if requested
    if kill:
        pid = agent.get("monitor_pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        session = agent.get("session", "")
        if session and tmux_session_exists(session):
            tmux_kill_session(session)

    removed = store.remove(aid)
    _json_out({"ok": removed, "id": agent["id"]})


_AGENT_COMMANDS = {
    "rm": _cmd_agent_rm,
}


# ---------------------------------------------------------------------------
# Subcommand dispatcher
# ---------------------------------------------------------------------------

_SESSION_COMMANDS = {
    "create": _cmd_session_create,
    "exists": _cmd_session_exists,
    "capture": _cmd_session_capture,
    "send": _cmd_session_send,
    "key": _cmd_session_key,
    "kill": _cmd_session_kill,
}

_FILE_COMMANDS = {
    "list": _cmd_file_list,
    "read": _cmd_file_read,
    "write": _cmd_file_write,
}


def _dispatch_subcommand():
    cmd = sys.argv[1]
    if cmd == "ping":
        return _cmd_ping()
    elif cmd == "status":
        return _cmd_status()
    elif cmd == "session":
        sub = sys.argv[2] if len(sys.argv) >= 3 else ""
        handler = _SESSION_COMMANDS.get(sub)
        if handler:
            return handler()
        sys.stderr.write("Unknown session subcommand: %s\n" % sub)
        sys.exit(1)
    elif cmd == "file":
        sub = sys.argv[2] if len(sys.argv) >= 3 else ""
        handler = _FILE_COMMANDS.get(sub)
        if handler:
            return handler()
        sys.stderr.write("Unknown file subcommand: %s\n" % sub)
        sys.exit(1)
    elif cmd == "agent":
        sub = sys.argv[2] if len(sys.argv) >= 3 else ""
        handler = _AGENT_COMMANDS.get(sub)
        if handler:
            return handler()
        sys.stderr.write("Unknown agent subcommand: %s\n" % sub)
        sys.exit(1)
    else:
        sys.stderr.write("Unknown command: %s\n" % cmd)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Subcommand protocol dispatch (new path B)
    if len(sys.argv) >= 2 and sys.argv[1] in ("ping", "status", "session", "file", "agent"):
        return _dispatch_subcommand()

    # Hidden background monitor subcommand
    if len(sys.argv) >= 4 and sys.argv[1] == "_monitor_bg":
        return _run_local_monitor(sys.argv[2], sys.argv[3])

    # Legacy HTTP monitor mode (original path B)
    parser = argparse.ArgumentParser(description="CAM client monitor")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--adapter-config", default=None)
    parser.add_argument("--tool", default=None)
    parser.add_argument("--auto-confirm", type=bool, default=True)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--prompt", default="")
    args = parser.parse_args()

    config_dict = None
    if args.adapter_config:
        try:
            config_dict = json.loads(args.adapter_config)
        except (ValueError, TypeError) as e:
            log.error("Invalid adapter config JSON: %s", e)
            sys.exit(1)
    elif args.tool:
        toml_path = os.path.expanduser("~/.cam/configs/%s.toml" % args.tool)
        if not os.path.exists(toml_path):
            log.error("TOML config not found: %s", toml_path)
            sys.exit(1)
        config_dict = load_toml(toml_path)
        log.info("Loaded adapter config from %s", toml_path)
    else:
        log.error("Either --adapter-config or --tool is required")
        sys.exit(1)

    adapter_config = AdapterConfig(config_dict)
    monitor = ClientMonitor(
        agent_id=args.agent_id, session_id=args.session,
        server_url=args.server.rstrip("/"), token=args.token,
        adapter_config=adapter_config,
        auto_confirm=args.auto_confirm, poll_interval=args.poll_interval,
        prompt=args.prompt,
    )

    signal.signal(signal.SIGTERM, lambda s, f: monitor.stop())
    monitor.run()


if __name__ == "__main__":
    main()
