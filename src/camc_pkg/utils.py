"""Utility functions: ANSI stripping, text processing, subprocess helpers."""

import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone

from camc_pkg import CAM_DIR, CONTEXT_FILE, _DEFAULT_CONTEXT, log

# ---------------------------------------------------------------------------
# ANSI stripping & text processing
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"\x1B\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1B\][^\x07]*\x07"
    r"|\x1B[()][AB012]"
    r"|\x1B[@-_]"
)


def strip_ansi(text):
    return _ANSI_RE.sub("", text)


_BOX_CHARS = u"\u2500\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u256d\u256e\u2570\u256f \t"


def clean_for_confirm(text):
    lines = [line.strip(_BOX_CHARS) for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).rstrip()


_RE_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}


def compile_pattern(pattern, flags=None):
    f = 0
    for name in (flags or []):
        upper = name.upper()
        if upper in _RE_FLAGS:
            f |= _RE_FLAGS[upper]
    return re.compile(pattern, f)


# ---------------------------------------------------------------------------
# Subprocess helper (3.6 compat)
# ---------------------------------------------------------------------------

def _run(args, timeout=5, check=False):
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
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_ago(iso_str):
    if not iso_str:
        return ""
    try:
        clean = iso_str.replace("Z", "").replace("+00:00", "")
        dt = datetime.strptime(clean[:19], "%Y-%m-%dT%H:%M:%S")
        diff = (datetime.now(timezone.utc).replace(tzinfo=None) - dt).total_seconds()
        if diff < 60: return "%ds ago" % int(diff)
        if diff < 3600: return "%dm ago" % int(diff / 60)
        if diff < 86400: return "%dh ago" % int(diff / 3600)
        return "%dd ago" % int(diff / 86400)
    except Exception:
        return iso_str[:16]


def _load_default_context():
    if not os.path.exists(CONTEXT_FILE):
        try:
            os.makedirs(CAM_DIR, exist_ok=True)
        except OSError:
            pass
        with open(CONTEXT_FILE, "w") as f:
            json.dump(_DEFAULT_CONTEXT, f, indent=2)
            f.write("\n")
        return dict(_DEFAULT_CONTEXT)
    try:
        with open(CONTEXT_FILE) as f:
            ctx = json.load(f)
        result = dict(_DEFAULT_CONTEXT)
        result.update(ctx)
        return result
    except (ValueError, IOError):
        return dict(_DEFAULT_CONTEXT)


def _build_command(config, prompt, path):
    replacements = {"{prompt}": prompt, "{path}": path}
    result = []
    for part in config.command:
        for key, value in replacements.items():
            if key in part:
                part = part.replace(key, value)
                break
        result.append(part)
    # Inject --permission-mode auto if enabled in config
    if getattr(config, "auto_permission_mode", False):
        # Insert after the executable name (index 0)
        result[1:1] = ["--permission-mode", "auto"]
    return result


def _kill_monitor(agent):
    pid = agent.get("pid") or agent.get("monitor_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    from camc_pkg import PIDS_DIR
    pid_path = os.path.join(PIDS_DIR, "%s.pid" % agent["id"])
    # Also check legacy path
    legacy_pid_path = "/tmp/camc-%s.pid" % agent["id"]
    for p in (pid_path, legacy_pid_path):
        if os.path.exists(p):
            try:
                with open(p) as f:
                    os.kill(int(f.read().strip()), signal.SIGTERM)
            except (ValueError, OSError):
                pass
            try:
                os.unlink(p)
            except OSError:
                pass
