"""Shared proxy lifecycle: ensure / start / stop / status."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from camc_pkg import CAM_DIR, LOGS_DIR
from camc_pkg.api_store import PROXY_RUNS_FILE

ROUTE_DEFAULTS = {
    "completions_to_messages": {
        "port": 18324,
        "from_proto": "anthropic_messages",
        "to_proto": "openai_chat_completions",
    },
}


def _load_runs():
    if not os.path.isfile(PROXY_RUNS_FILE):
        return {}
    try:
        with open(PROXY_RUNS_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (IOError, ValueError):
        return {}


def _save_runs(data):
    os.makedirs(CAM_DIR, exist_ok=True)
    tmp = PROXY_RUNS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, PROXY_RUNS_FILE)


def _run_key(route, upstream_url):
    raw = "%s|%s" % (route, upstream_url or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _health_ok(port, timeout=1.0):
    url = "http://127.0.0.1:%d/health" % int(port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return False


def _camc_argv():
    script = os.path.abspath(sys.argv[0])
    if os.path.isfile(script):
        return [sys.executable, script, "_proxy"]
    return [sys.executable, "-m", "camc_pkg", "_proxy"]


def _start_proxy(route, port, model_alias, upstream_model, upstream_url,
                 proxy_debug=False, token=""):
    os.makedirs(LOGS_DIR, exist_ok=True)
    ready_file = os.path.join(CAM_DIR, "proxy-ready-%s.tmp" % route)
    log_path = os.path.join(LOGS_DIR, "proxy-%s.log" % route)
    argv = _camc_argv() + [
        route,
        "--port", str(port),
        "--model-alias", model_alias,
        "--upstream-model", upstream_model or model_alias,
        "--upstream-url", upstream_url,
        "--ready-file", ready_file,
    ]
    if proxy_debug:
        argv.append("--debug")

    env = os.environ.copy()
    if token:
        env["INFERENCE_HUB_API_KEY"] = token
        env["INFERENCE_HUB_TOKEN"] = token

    with open(log_path, "a") as logf:
        proc = subprocess.Popen(
            argv,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    deadline = time.time() + 15.0
    while time.time() < deadline:
        if os.path.isfile(ready_file):
            break
        if proc.poll() is not None:
            raise RuntimeError("proxy %s exited early (see %s)" % (route, log_path))
        time.sleep(0.2)

    if not _health_ok(port, timeout=0.5):
        time.sleep(0.5)
    if not _health_ok(port):
        try:
            proc.terminate()
        except OSError:
            pass
        raise RuntimeError("proxy %s failed health check on port %d" % (route, port))

    try:
        os.remove(ready_file)
    except OSError:
        pass

    return proc.pid, log_path


def ensure_proxy(plan, token):
    """Start or reuse proxy for plan; return (port, run_record)."""
    if plan.get("mode") != "proxy":
        return None, None

    route = plan.get("route")
    port = int(plan.get("proxy_port") or ROUTE_DEFAULTS.get(route, {}).get("port") or 18324)
    upstream_url = plan.get("upstream_url") or ""
    key = _run_key(route, upstream_url)
    runs = _load_runs()
    rec = runs.get(key)

    if rec and _pid_alive(rec.get("pid")) and _health_ok(rec.get("port") or port):
        return int(rec.get("port") or port), rec

    if rec and rec.get("pid") and not _pid_alive(rec.get("pid")):
        runs.pop(key, None)

    # Reuse an already-listening proxy (e.g. manual start or stale proxy-runs.json).
    if _health_ok(port):
        if not rec:
            rec = {
                "route": route,
                "port": port,
                "pid": None,
                "upstream_url": upstream_url,
                "model": plan.get("model"),
                "api": plan.get("name"),
                "log": os.path.join(LOGS_DIR, "proxy-%s.log" % route),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reused": True,
            }
            runs[key] = rec
            _save_runs(runs)
        return port, rec

    pid, log_path = _start_proxy(
        route=route,
        port=port,
        model_alias=plan.get("name") or "api",
        upstream_model=plan.get("model") or "",
        upstream_url=upstream_url,
        proxy_debug=bool(plan.get("proxy_debug")),
        token=token,
    )
    rec = {
        "route": route,
        "port": port,
        "pid": pid,
        "upstream_url": upstream_url,
        "model": plan.get("model"),
        "api": plan.get("name"),
        "log": log_path,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    runs[key] = rec
    _save_runs(runs)
    return port, rec


def proxy_status():
    runs = _load_runs()
    rows = []
    for key, rec in sorted(runs.items()):
        alive = _pid_alive(rec.get("pid"))
        healthy = _health_ok(rec.get("port") or 0) if alive else False
        rows.append({
            "key": key,
            "route": rec.get("route"),
            "port": rec.get("port"),
            "pid": rec.get("pid"),
            "alive": alive,
            "healthy": healthy,
            "api": rec.get("api"),
            "model": rec.get("model"),
            "upstream_url": rec.get("upstream_url"),
            "log": rec.get("log"),
        })
    return rows


def proxy_stop(route=None):
    runs = _load_runs()
    stopped = []
    for key, rec in list(runs.items()):
        if route and rec.get("route") != route:
            continue
        pid = rec.get("pid")
        if pid and _pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        stopped.append(rec)
        runs.pop(key, None)
    _save_runs(runs)
    return stopped
