"""camc cron — small scheduler facility (P0).

The user surface is intentionally tiny:

    camc cron add --name NAME (--every DUR | --daily HH:MM
                              | --at TIME | --in DUR) [...] -- COMMAND...
    camc cron rm <id-or-name>

Internally camc installs a single marked block in the current user's
crontab that calls `camc cron tick` every minute. tick is the
scheduler entrypoint — it is *not* a long-running daemon. Concurrent
ticks are guarded by a non-blocking fcntl lock; idempotency is by
(job_id, due_at_minute) against the structured runs log.

Files under ~/.cam/ (see spec):
    cron.json             active job registry
    cron-config.json      global cron settings
    cron-runs.jsonl       structured execution / recycle log
    cron.state.json       heartbeat + last tick status
    cron.lock             tick lock
    cron-archive/         archived removed/recycled jobs
    logs/cron.log         human-readable tick stdout/stderr

The scheduler is opaque to command semantics: success = exit 0,
failure = nonzero or timeout. It does not parse msg/run/apply.
"""

import json
import os
import re
import shutil
import socket as _socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from camc_pkg import CAM_DIR, LOGS_DIR, log
from camc_pkg.utils import _now_iso

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — non-POSIX
    _fcntl = None


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

CRON_FILE = os.path.join(CAM_DIR, "cron.json")
CRON_CONFIG_FILE = os.path.join(CAM_DIR, "cron-config.json")
CRON_RUNS_FILE = os.path.join(CAM_DIR, "cron-runs.jsonl")
CRON_STATE_FILE = os.path.join(CAM_DIR, "cron.state.json")
CRON_LOCK_FILE = os.path.join(CAM_DIR, "cron.lock")
CRON_ARCHIVE_DIR = os.path.join(CAM_DIR, "cron-archive")
CRON_HUMAN_LOG = os.path.join(LOGS_DIR, "cron.log")

CRON_BEGIN = "# camc cron begin"
CRON_END = "# camc cron end"

# Defaults match the spec.
DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CONFIG = {
    "version": 1,
    "enabled": True,
    "default_ttl_days": DEFAULT_TTL_DAYS,
    "max_attempts": DEFAULT_MAX_ATTEMPTS,
    "misfire": "skip",
    "logs": {"retention": "forever"},
    "tick": {
        "schedule": "* * * * *",
        "lock_timeout_seconds": 0,
        "max_runtime_seconds": 50,
    },
}


# ---------------------------------------------------------------------------
# Helpers (time, ids, paths)
# ---------------------------------------------------------------------------

def _ensure_dir(path):
    try:
        os.makedirs(path)
    except OSError:
        pass


def _now_utc():
    return datetime.now(timezone.utc)


def _now_local():
    return datetime.now().astimezone()  # local tz with explicit offset


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _new_job_id():
    return uuid4().hex[:8]


def _hostname():
    return _socket.gethostname()


def _short_host(h):
    return (h or "").split(".", 1)[0]


# ---------------------------------------------------------------------------
# Schedule preset parser
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"^(\d+)(m|h)$")


def parse_duration(s):
    """`30m` → 1800, `2h` → 7200. Returns int seconds; raises ValueError."""
    if s is None:
        raise ValueError("duration is required")
    m = _DUR_RE.match(s.strip())
    if not m:
        raise ValueError("bad duration %r — use Nm or Nh" % s)
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError("duration must be positive")
    return n * (60 if unit == "m" else 3600)


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_daily(s):
    """`HH:MM` → `{"type": "daily", "time": "HH:MM", "timezone": "local"}`."""
    if not s or not _HHMM_RE.match(s.strip()):
        raise ValueError("bad daily time %r — use HH:MM" % s)
    hh, mm = s.strip().split(":")
    return {"type": "daily", "time": "%02d:%s" % (int(hh), mm), "timezone": "local"}


def parse_at(s):
    """Absolute ISO timestamp → `{"type": "once", "run_at": "<iso>"}`."""
    try:
        dt = datetime.fromisoformat(s.strip())
    except (ValueError, TypeError):
        raise ValueError("bad --at %r — use ISO 8601 (e.g. 2026-05-12T09:30:00-07:00)" % s)
    if dt.tzinfo is None:
        # Assume local tz if naive.
        dt = dt.replace(tzinfo=_now_local().tzinfo)
    return {"type": "once", "run_at": _iso(dt)}


def parse_in(s):
    """Relative duration → `{"type": "once", "run_at": now + dur}` (local tz)."""
    sec = parse_duration(s)
    dt = _now_local() + timedelta(seconds=sec)
    return {"type": "once", "run_at": _iso(dt)}


def parse_every(s):
    """Interval preset → `{"type": "interval", "every_seconds": N, "anchor": "created_at"}`."""
    sec = parse_duration(s)
    return {"type": "interval", "every_seconds": sec, "anchor": "created_at"}


# ---------------------------------------------------------------------------
# Config + store
# ---------------------------------------------------------------------------

class CronConfig(object):
    """Read/write ~/.cam/cron-config.json, defaulting to DEFAULT_CONFIG.

    `load()` raises `CorruptCronConfig` when the file EXISTS but fails to
    parse — tick fails closed in that case per spec. A missing file is
    not an error: we return a copy of DEFAULT_CONFIG.
    """

    def __init__(self, path=None):
        self._path = path or CRON_CONFIG_FILE

    def load(self):
        if not os.path.exists(self._path):
            return dict(DEFAULT_CONFIG)
        try:
            with open(self._path, "r") as f:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_SH)
                try:
                    cfg = json.load(f)
                finally:
                    if _fcntl:
                        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except (ValueError, OSError) as e:
            raise CorruptCronConfig("%s: %s" % (self._path, e))
        if not isinstance(cfg, dict):
            raise CorruptCronConfig("%s: not a JSON object" % self._path)
        # Shallow-merge missing top-level keys from defaults.
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg

    def load_or_default(self):
        """Best-effort: return defaults instead of raising on corrupt.

        Used by build_job during `cron add` so users can recover with a
        good config later; tick uses the strict load() instead.
        """
        try:
            return self.load()
        except CorruptCronConfig:
            return dict(DEFAULT_CONFIG)

    def ensure(self):
        """Create cron-config.json with defaults if missing. Idempotent."""
        if os.path.exists(self._path):
            return
        _ensure_dir(os.path.dirname(self._path))
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        os.replace(tmp, self._path)


class CronStore(object):
    """fcntl-locked read/write of ~/.cam/cron.json.

    Same defensive style as AgentStore: refuses to overwrite a non-empty
    file that fails to parse (likely corrupt / NFS stale read), and
    refuses to shrink a non-empty file to an empty list via a no-op
    update. The on-disk shape is `{"version": 1, "jobs": [...]}`.
    """

    def __init__(self, path=None):
        self._path = path or CRON_FILE

    def _read_raw(self):
        if not os.path.exists(self._path):
            return {"version": 1, "jobs": []}, False
        try:
            sz = os.path.getsize(self._path)
            had_data = sz > 4
            with open(self._path, "r") as f:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    if _fcntl:
                        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
            if not isinstance(data, dict) or "jobs" not in data:
                raise ValueError("schema mismatch")
            return data, had_data
        except (ValueError, OSError):
            return None, True  # corrupt or unreadable

    def load(self):
        data, _had = self._read_raw()
        if data is None:
            # Corrupt: surface empty for read-only callers; modify() refuses.
            return {"version": 1, "jobs": []}
        return data

    def is_corrupt(self):
        """True iff cron.json EXISTS but cannot be parsed.

        Callers that mutate side-state based on jobs() (e.g.
        ensure_tick_if_needed deciding to remove the crontab block)
        MUST check this and bail out — silently treating corrupt as
        empty can disable scheduled jobs at exactly the moment the
        registry needs manual inspection.
        """
        data, _had = self._read_raw()
        return data is None

    def jobs(self):
        return self.load().get("jobs", [])

    def find(self, key):
        """Resolve key → job dict, or None.

        Order: exact id, exact name, unique id prefix. An ambiguous
        prefix raises AmbiguousJobKey so that destructive operations
        (cron rm) can't silently target the wrong job.
        """
        if not key:
            return None
        jobs = self.jobs()
        for j in jobs:
            if j.get("id") == key or j.get("name") == key:
                return j
        prefix_matches = [j for j in jobs
                          if (j.get("id") or "").startswith(key)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            raise AmbiguousJobKey(
                key, [j.get("id") or "" for j in prefix_matches])
        return None

    def _modify(self, fn):
        """Lock + read + transform + atomic write. Refuses on corrupt parse."""
        _ensure_dir(os.path.dirname(self._path))
        lock_path = self._path + ".lock"
        with open(lock_path, "w") as lf:
            if _fcntl:
                _fcntl.flock(lf.fileno(), _fcntl.LOCK_EX)
            try:
                data, had_data = self._read_raw()
                if data is None:
                    # Corrupt registry — do not touch.
                    raise CorruptCronJSON(self._path)
                new = fn(data)
                if new is None:
                    return data
                if not new.get("jobs") and had_data and data.get("jobs"):
                    # Refuse to wipe non-empty registry via a no-op.
                    return data
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(new, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
                return new
            finally:
                if _fcntl:
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)

    def add(self, job):
        def _fn(data):
            for j in data.get("jobs", []):
                if j.get("name") == job["name"]:
                    raise DuplicateJobName(job["name"])
            data.setdefault("jobs", []).append(job)
            return data
        self._modify(_fn)

    def remove(self, key):
        """Remove a job by exact id, exact name, or UNIQUE id prefix.

        Returns the removed job dict, or None when no match. Raises
        AmbiguousJobKey when a prefix matches multiple ids — never
        silently target the first match.
        """
        # Resolve to a single target id under the same lock as the
        # subsequent mutation. We re-walk inside _modify so the read
        # and write share one fcntl region.
        removed_ref = {"job": None}

        def _fn(data):
            jobs = data.get("jobs", [])
            # Exact id / name first.
            for j in jobs:
                if j.get("id") == key or j.get("name") == key:
                    target = j
                    break
            else:
                # Unique prefix only.
                prefix_matches = [j for j in jobs
                                  if (j.get("id") or "").startswith(key)]
                if len(prefix_matches) > 1:
                    raise AmbiguousJobKey(
                        key, [j.get("id") or "" for j in prefix_matches])
                if len(prefix_matches) != 1:
                    data["jobs"] = jobs  # unchanged
                    return data
                target = prefix_matches[0]
            removed_ref["job"] = target
            data["jobs"] = [j for j in jobs if j is not target]
            return data
        self._modify(_fn)
        return removed_ref["job"]

    def replace_jobs(self, jobs):
        def _fn(data):
            data["jobs"] = jobs
            return data
        self._modify(_fn)


class DuplicateJobName(Exception):
    pass


class CorruptCronJSON(Exception):
    pass


class CorruptCronConfig(Exception):
    pass


class AmbiguousJobKey(Exception):
    def __init__(self, key, matches):
        super(AmbiguousJobKey, self).__init__(
            "ambiguous prefix %r matches %d ids: %s"
            % (key, len(matches), ", ".join(matches)))
        self.key = key
        self.matches = matches


# ---------------------------------------------------------------------------
# Job builder (cron add)
# ---------------------------------------------------------------------------

def build_job(name, schedule, command, *,
              ttl_days=None, expires_at=None, no_expire=False,
              max_attempts=None, host=None, created_at=None):
    """Normalize a job record for ~/.cam/cron.json.

    Caller is responsible for validating `command` (exactly one of argv
    or shell) and `schedule` (already a normalized dict from parse_*).
    """
    cfg = CronConfig().load_or_default()
    now = created_at or _iso(_now_local())

    # ttl_days → concrete expires_at unless --no-expire or explicit
    # expires_at was passed.
    if no_expire:
        expires = None
    elif expires_at:
        try:
            datetime.fromisoformat(expires_at)
        except (ValueError, TypeError):
            raise ValueError("bad --expires-at %r — use ISO 8601" % expires_at)
        expires = expires_at
    else:
        days = ttl_days if ttl_days is not None else cfg.get(
            "default_ttl_days", DEFAULT_TTL_DAYS)
        if days is None:
            expires = None
        else:
            base_dt = (_now_local()
                       if not created_at else datetime.fromisoformat(created_at))
            expires = _iso(base_dt + timedelta(days=int(days)))

    kind = {"interval": "interval", "daily": "daily",
            "once": "once"}.get(schedule["type"], schedule["type"])

    return {
        "id": _new_job_id(),
        "name": name,
        "enabled": True,
        "kind": kind,
        "schedule": schedule,
        "host": host or _hostname(),
        "ttl_days": ttl_days,
        "expires_at": expires,
        "max_attempts": max_attempts if max_attempts is not None else cfg.get(
            "max_attempts", DEFAULT_MAX_ATTEMPTS),
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
        "last_due_at": None,
        "last_run_id": None,
        "last_status": None,
        "command": command,
    }


# ---------------------------------------------------------------------------
# Structured + human logging
# ---------------------------------------------------------------------------

def _append_runs(record):
    """Append a record to ~/.cam/cron-runs.jsonl (best-effort)."""
    record.setdefault("ts", _iso(_now_local()))
    _ensure_dir(os.path.dirname(CRON_RUNS_FILE))
    try:
        with open(CRON_RUNS_FILE, "a") as f:
            if _fcntl:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
            try:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            finally:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
    except OSError as e:
        log.warning("cron: failed to append run record: %s", e)


def _ensure_human_log():
    _ensure_dir(os.path.dirname(CRON_HUMAN_LOG))
    if not os.path.exists(CRON_HUMAN_LOG):
        try:
            open(CRON_HUMAN_LOG, "a").close()
        except OSError:
            pass


def _emit_event(event, agent_id="cron", **detail):
    """Best-effort EventStore mirror so `camc history` can surface cron events."""
    try:
        from camc_pkg.storage import EventStore
        EventStore().append(agent_id, event, detail)
    except Exception:
        pass


def _write_state(status, started_at, completed_at=None, error=None):
    _ensure_dir(os.path.dirname(CRON_STATE_FILE))
    state = {
        "version": 1,
        "last_tick_started_at": started_at,
        "last_tick_completed_at": completed_at,
        "last_tick_status": status,
        "last_tick_host": _hostname(),
        "last_tick_pid": os.getpid(),
        "last_error": error,
    }
    tmp = CRON_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, CRON_STATE_FILE)
    except OSError as e:
        log.warning("cron: failed to write state: %s", e)


# ---------------------------------------------------------------------------
# Crontab block management
# ---------------------------------------------------------------------------

def _camc_path():
    """Detect the camc executable path for the crontab line."""
    # 1. Caller's argv[0] if it resolves to an executable.
    if sys.argv and sys.argv[0]:
        p = os.path.realpath(sys.argv[0])
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # 2. ~/.cam/camc (deployed) if present.
    deployed = os.path.expanduser("~/.cam/camc")
    if os.path.isfile(deployed) and os.access(deployed, os.X_OK):
        return deployed
    # 3. PATH lookup.
    found = shutil.which("camc")
    if found:
        return found
    return "camc"  # last resort; cron daemon's PATH will decide


def _build_tick_block(camc_path=None):
    home = os.path.expanduser("~")
    path = os.environ.get(
        "PATH",
        "/home/prgn_share/bin:%s/.cam:/usr/local/bin:/usr/bin:/bin" % home)
    binp = camc_path or _camc_path()
    line = "* * * * * HOME=%s PATH=%s %s cron tick >> %s 2>&1" % (
        home, path, binp, CRON_HUMAN_LOG)
    return "%s\n%s\n%s" % (CRON_BEGIN, line, CRON_END)


def _read_user_crontab(runner=None):
    """Return current crontab text (or '' if empty/missing). `runner` is
    an injectable subprocess.run for tests."""
    runner = runner or subprocess.run
    try:
        r = runner(["crontab", "-l"], stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, universal_newlines=True,
                   timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise CrontabUnavailable(str(e))
    if r.returncode != 0 and "no crontab" not in (r.stderr or "").lower():
        # Some `crontab -l` impls exit 1 with "no crontab for ...". Treat
        # that as empty.
        if r.returncode != 1 or r.stdout:
            raise CrontabUnavailable(r.stderr.strip() or "exit %d" % r.returncode)
    return r.stdout or ""


def _write_user_crontab(text, runner=None):
    runner = runner or subprocess.run
    try:
        r = runner(["crontab", "-"], input=text, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, universal_newlines=True,
                   timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise CrontabUnavailable(str(e))
    if r.returncode != 0:
        raise CrontabUnavailable(r.stderr.strip() or "exit %d" % r.returncode)


def _strip_block(text):
    """Remove a `# camc cron begin ... # camc cron end` block (or any
    fragments). Always returns text without the block."""
    if CRON_BEGIN not in text and CRON_END not in text:
        return text
    out_lines = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == CRON_BEGIN:
            in_block = True
            continue
        if stripped == CRON_END:
            in_block = False
            continue
        if not in_block:
            out_lines.append(line)
    # Collapse trailing blank lines but keep a single trailing newline.
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def install_tick(camc_path=None, runner=None):
    """Replace (or insert) the camc cron block in the user's crontab."""
    block = _build_tick_block(camc_path)
    current = _read_user_crontab(runner)
    stripped = _strip_block(current)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    new_text = stripped + block + "\n"
    _write_user_crontab(new_text, runner)
    _append_runs({"event": "tick_installed", "host": _hostname()})
    _emit_event("cron_tick_installed")


def remove_tick(runner=None):
    """Remove the camc cron block. No-op if absent."""
    current = _read_user_crontab(runner)
    if CRON_BEGIN not in current and CRON_END not in current:
        return False
    stripped = _strip_block(current)
    _write_user_crontab(stripped, runner)
    return True


def _block_in_crontab(text, camc_path=None):
    """True iff the current crontab block matches the expected line shape."""
    if CRON_BEGIN not in text:
        return False
    expected = _build_tick_block(camc_path).splitlines()[1].strip()
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if s == CRON_BEGIN:
            in_block = True
            continue
        if s == CRON_END:
            in_block = False
            continue
        if in_block and s == expected:
            return True
    return False


def ensure_tick_if_needed(runner=None, camc_path=None):
    """heal helper: install/repair the tick block iff active jobs exist;
    remove it iff no enabled jobs remain. Returns one of:
        "installed", "repaired", "removed", "ok", "noop"
        ("crontab_unavailable", reason) on crontab IO failure.
        ("cron_json_corrupt", path) when cron.json exists but can't be
        parsed — we refuse to install OR remove in that case to avoid
        silently disabling scheduled jobs when the registry needs
        manual inspection.
    """
    store = CronStore()
    if store.is_corrupt():
        return ("cron_json_corrupt", store._path)
    enabled = [j for j in store.jobs() if j.get("enabled", True)]
    try:
        current = _read_user_crontab(runner)
    except CrontabUnavailable as e:
        return ("crontab_unavailable", str(e))
    has_block = CRON_BEGIN in current
    if not enabled:
        if has_block:
            try:
                remove_tick(runner=runner)
            except CrontabUnavailable as e:
                return ("crontab_unavailable", str(e))
            return "removed"
        return "noop"
    # enabled jobs exist
    if not has_block:
        try:
            install_tick(camc_path=camc_path, runner=runner)
        except CrontabUnavailable as e:
            return ("crontab_unavailable", str(e))
        return "installed"
    if not _block_in_crontab(current, camc_path):
        try:
            install_tick(camc_path=camc_path, runner=runner)
        except CrontabUnavailable as e:
            return ("crontab_unavailable", str(e))
        return "repaired"
    return "ok"


class CrontabUnavailable(Exception):
    pass


# ---------------------------------------------------------------------------
# Recycle / archive
# ---------------------------------------------------------------------------

def _archive_job(job, reason):
    _ensure_dir(CRON_ARCHIVE_DIR)
    ts = _now_local().strftime("%Y%m%d-%H%M%S")
    fname = "%s-%s-%s.json" % (ts, job.get("name") or "anon",
                                job.get("id") or "noid")
    path = os.path.join(CRON_ARCHIVE_DIR, fname)
    payload = dict(job)
    payload["recycled_at"] = _iso(_now_local())
    payload["recycle_reason"] = reason
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        log.warning("cron: failed to write archive: %s", e)
        return None
    return path


def recycle_job(job, reason):
    """Remove `job` from active registry, archive it, log it. Best-effort."""
    archive_path = _archive_job(job, reason)
    try:
        CronStore().remove(job.get("id") or job.get("name"))
    except CorruptCronJSON:
        log.warning("cron: cron.json corrupt; cannot remove %s", job.get("id"))
    _append_runs({
        "event": "job_recycled",
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "reason": reason,
        "archive_path": archive_path or "",
    })
    _emit_event("cron_job_recycled", job_id=job.get("id"), reason=reason)


# ---------------------------------------------------------------------------
# Due computation
# ---------------------------------------------------------------------------

def _round_minute(dt):
    return dt.replace(second=0, microsecond=0)


def _next_daily_due(time_str, now_local, last_due_at=None):
    """Compute the most recent daily due time at or before `now_local`."""
    hh, mm = time_str.split(":")
    candidate = now_local.replace(hour=int(hh), minute=int(mm), second=0,
                                   microsecond=0)
    if candidate > now_local:
        candidate -= timedelta(days=1)
    return candidate


def is_due(job, now=None):
    """Return (due, due_at_iso) for this job at `now` (defaults to local now)."""
    now = now or _now_local()
    sched = job.get("schedule") or {}
    stype = sched.get("type")
    if not job.get("enabled", True):
        return False, None
    last_due = job.get("last_due_at")
    last_due_dt = None
    if last_due:
        try:
            last_due_dt = datetime.fromisoformat(last_due)
        except (ValueError, TypeError):
            last_due_dt = None

    if stype == "interval":
        every = int(sched.get("every_seconds") or 0)
        if every <= 0:
            return False, None
        anchor = last_due_dt
        if anchor is None:
            anchor_iso = job.get("created_at")
            try:
                anchor = datetime.fromisoformat(anchor_iso) if anchor_iso else now
            except (ValueError, TypeError):
                anchor = now
        if now >= anchor + timedelta(seconds=every):
            due_at = _round_minute(now)
            return True, _iso(due_at)
        return False, None

    if stype == "daily":
        time_str = sched.get("time") or "00:00"
        due_at = _next_daily_due(time_str, now)
        if last_due_dt and last_due_dt >= due_at:
            return False, None
        # only "due" if we're within the same minute as the daily slot —
        # missed runs are not backfilled per misfire=skip.
        if due_at <= now < due_at + timedelta(seconds=60):
            return True, _iso(due_at)
        return False, None

    if stype == "once":
        run_at = sched.get("run_at")
        if not run_at:
            return False, None
        try:
            run_dt = datetime.fromisoformat(run_at)
        except (ValueError, TypeError):
            return False, None
        if now < run_dt:
            return False, None
        # Use the CURRENT minute as due_at so successive ticks get a
        # distinct (job_id, due_at) idempotency key. This lets a failed
        # once-job retry on later ticks until max_attempts (which is
        # what the recycle policy assumes); the prior implementation
        # used run_at as the key and permanently suppressed retries.
        return True, _iso(_round_minute(now))

    return False, None


# ---------------------------------------------------------------------------
# Tick — the cron entry point
# ---------------------------------------------------------------------------

def _acquire_lock(path=None):
    """Return an open file handle holding an exclusive non-blocking lock,
    or None if the lock is held elsewhere."""
    path = path or CRON_LOCK_FILE
    _ensure_dir(os.path.dirname(path))
    try:
        f = open(path, "w")
    except OSError:
        return None
    if _fcntl is None:
        return f  # cannot enforce; better than nothing
    try:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    try:
        f.write(str(os.getpid()))
        f.flush()
    except OSError:
        pass
    return f


def _release_lock(handle):
    if handle is None:
        return
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _already_ran(job_id, due_at):
    """True iff a job_started record exists for (job_id, due_at) in cron-runs.jsonl."""
    if not os.path.exists(CRON_RUNS_FILE):
        return False
    try:
        with open(CRON_RUNS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if (r.get("event") == "job_started"
                        and r.get("job_id") == job_id
                        and r.get("due_at") == due_at):
                    return True
    except OSError:
        return False
    return False


def _run_command(cmd, *, timeout, cwd, runner=None):
    """Execute `cmd` (argv list or shell str). Returns (exit_code, duration,
    error_str)."""
    runner = runner or subprocess.run
    start = time.time()
    try:
        if isinstance(cmd, list):
            r = runner(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True,
                       timeout=timeout)
        else:
            r = runner(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True,
                       timeout=timeout, shell=True)
        return r.returncode, time.time() - start, None
    except subprocess.TimeoutExpired:
        return 124, time.time() - start, "timeout after %ss" % timeout
    except (OSError, subprocess.SubprocessError) as e:
        return 127, time.time() - start, str(e)


def _job_attempt(job, due_at, runner=None):
    """Run one job attempt; update job counters; recycle on failure threshold."""
    run_id = "%s-%s" % (_now_local().strftime("%Y%m%d-%H%M%S"),
                        job.get("id") or "noid")
    cmd = job.get("command") or {}
    argv = cmd.get("argv")
    shell = cmd.get("shell")
    cwd = cmd.get("cwd") or os.getcwd()
    timeout = int(cmd.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    cmd_for_log = argv if argv else shell
    _append_runs({
        "event": "job_started",
        "run_id": run_id,
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "due_at": due_at,
        "command": cmd_for_log,
    })
    _emit_event("cron_job_started", job_id=job.get("id"))
    exit_code, duration, err = _run_command(
        argv if argv else shell, timeout=timeout, cwd=cwd, runner=runner)
    if exit_code == 0:
        _append_runs({
            "event": "job_succeeded",
            "run_id": run_id,
            "job_id": job.get("id"),
            "job_name": job.get("name"),
            "status": "exit_0",
            "exit_code": 0,
            "duration_seconds": round(duration, 3),
        })
        _emit_event("cron_job_succeeded", job_id=job.get("id"))
        return True, run_id
    _append_runs({
        "event": "job_failed",
        "run_id": run_id,
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "error": err or "command exited %d" % exit_code,
        "exit_code": exit_code,
        "attempts": int(job.get("attempts") or 0) + 1,
    })
    _emit_event("cron_job_failed", job_id=job.get("id"), exit_code=exit_code)
    return False, run_id


def _bump_job(store, job_id, *, due_at, run_id, success):
    def _fn(data):
        for j in data.get("jobs", []):
            if j.get("id") != job_id:
                continue
            j["last_due_at"] = due_at
            j["last_run_id"] = run_id
            j["last_status"] = "exit_0" if success else "failed"
            j["updated_at"] = _iso(_now_local())
            j["attempts"] = 0 if success else int(j.get("attempts") or 0) + 1
            break
        return data
    store._modify(_fn)


def tick(now=None, runner=None):
    """Scheduler entrypoint — called by the system crontab once a minute.

    Idempotency: a non-blocking fcntl lock prevents overlapping ticks;
    per-job dedup uses (job_id, due_at_minute) against the runs log.
    Returns a dict summarizing the run (handy for tests).
    """
    started_at = _iso(_now_local())
    lock = _acquire_lock()
    if lock is None:
        _append_runs({"event": "tick_skipped_locked", "host": _hostname(),
                      "pid": os.getpid()})
        return {"status": "skipped_locked", "ran": 0}
    try:
        _ensure_human_log()
        _append_runs({"event": "tick_started", "source": "system-cron",
                      "host": _hostname(), "pid": os.getpid()})
        _emit_event("cron_tick", host=_hostname())

        # Fail-closed gates per spec:
        #   - corrupt cron-config.json → tick records error and exits;
        #     do NOT silently treat it as defaults.
        #   - corrupt cron.json → same. Both registry files must be
        #     valid before any job runs.
        #   - global enabled=false → skip execution entirely.
        try:
            cfg = CronConfig().load()
        except CorruptCronConfig as e:
            _append_runs({"event": "tick_aborted", "reason": "corrupt_config",
                          "error": str(e)})
            _write_state("error", started_at, _iso(_now_local()), str(e))
            return {"status": "error", "error": "corrupt config: %s" % e,
                    "ran": 0}
        if not bool(cfg.get("enabled", True)):
            _append_runs({"event": "tick_disabled",
                          "reason": "config_enabled_false"})
            _write_state("disabled", started_at, _iso(_now_local()))
            return {"status": "disabled", "ran": 0}

        store = CronStore()
        if store.is_corrupt():
            err = "%s: corrupt registry" % store._path
            _append_runs({"event": "tick_aborted", "reason": "corrupt_registry",
                          "error": err})
            _write_state("error", started_at, _iso(_now_local()), err)
            return {"status": "error", "error": err, "ran": 0}
        try:
            jobs = store.jobs()
        except Exception as e:
            _write_state("error", started_at, _iso(_now_local()), str(e))
            return {"status": "error", "error": str(e), "ran": 0}

        # -------- Recycle expired jobs first --------
        now_local = now or _now_local()
        for j in list(jobs):
            exp = j.get("expires_at")
            if not exp:
                continue
            try:
                exp_dt = datetime.fromisoformat(exp)
            except (ValueError, TypeError):
                continue
            if now_local >= exp_dt:
                recycle_job(j, "expired")
        jobs = store.jobs()  # re-read after possible recycles

        my_host = _hostname()
        ran = 0
        for j in jobs:
            if not j.get("enabled", True):
                continue
            jhost = j.get("host") or "any"
            if jhost != "any" and not _short_host(jhost) == _short_host(my_host):
                _append_runs({"event": "job_skipped_host",
                              "job_id": j.get("id"), "job_name": j.get("name"),
                              "job_host": jhost, "current_host": my_host})
                continue
            due, due_at = is_due(j, now=now_local)
            if not due:
                continue
            if _already_ran(j.get("id"), due_at):
                continue
            success, run_id = _job_attempt(j, due_at, runner=runner)
            _bump_job(store, j["id"], due_at=due_at, run_id=run_id, success=success)
            ran += 1
            # Failure recycle threshold
            if not success:
                updated = store.find(j["id"])
                if updated and int(updated.get("attempts") or 0) >= int(
                        updated.get("max_attempts") or DEFAULT_MAX_ATTEMPTS):
                    recycle_job(updated, "too_many_failures")
                    continue
            # Once-job success: recycle after success.
            if success and (j.get("schedule") or {}).get("type") == "once":
                recycle_job(store.find(j["id"]) or j, "once_completed")

        completed_at = _iso(_now_local())
        _write_state("ok", started_at, completed_at)
        return {"status": "ok", "ran": ran}
    finally:
        _release_lock(lock)
