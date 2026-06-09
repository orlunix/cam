"""camc cron — small scheduler facility (P0).

Storage model (per docs/camc-cron-spec.md):

    ~/.cam/cron/config.json           global cron settings
    ~/.cam/cron/jobs.d/<job_id>.json  one active job per file
    ~/.cam/cron/runs.jsonl            append-only run/event log
    ~/.cam/cron/state.json            tick heartbeat / last status
    ~/.cam/cron/tick.lock             non-blocking scheduler lock
    ~/.cam/cron/run.lock              short lock for run state updates
    ~/.cam/cron/logs/<run_id>.log     stdout/stderr for one run worker
    ~/.cam/cron/archive/              archived removed/recycled jobs
    ~/.cam/cron/cron.log              human-readable tick stdout/stderr

Execution model:

    crond → camc cron tick           (scheduler only; never blocks)
              ├─ scan jobs.d/
              ├─ for each due job:
              │    append `run_queued` to runs.jsonl
              │    advance schedule.next_due_at, save job
              │    spawn detached `camc cron run <run_id>`
              └─ exit

    detached → camc cron run <run_id>  (one-shot worker; opaque action)
              ├─ resolve job from jobs.d/<job_id>.json
              ├─ append `run_started`
              ├─ subprocess.run(argv or shell), redirect to logs/<run_id>.log
              ├─ append `run_succeeded` / `run_failed` / `run_timed_out`
              ├─ update job.state.attempts/last_run_id/last_status
              └─ recycle if appropriate

Action semantics are OPAQUE to cron: success = exit 0; anything else is
failure. cron does not parse msg/run/apply replies.
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

from camc_pkg import CAM_DIR, log
from camc_pkg.utils import _now_iso  # noqa: F401  (kept for downstream imports)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — non-POSIX
    _fcntl = None


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

CRON_DIR = os.path.join(CAM_DIR, "cron")
CRON_JOBS_DIR = os.path.join(CRON_DIR, "jobs.d")
CRON_CONFIG_FILE = os.path.join(CRON_DIR, "config.json")
CRON_RUNS_FILE = os.path.join(CRON_DIR, "runs.jsonl")
CRON_STATE_FILE = os.path.join(CRON_DIR, "state.json")
CRON_LOCK_FILE = os.path.join(CRON_DIR, "tick.lock")
CRON_RUN_LOCK_FILE = os.path.join(CRON_DIR, "run.lock")
CRON_ARCHIVE_DIR = os.path.join(CRON_DIR, "archive")
CRON_LOGS_DIR = os.path.join(CRON_DIR, "logs")
CRON_HUMAN_LOG = os.path.join(CRON_DIR, "cron.log")

# Legacy single-file registry — only consulted at migration time.
CRON_LEGACY_FILE = os.path.join(CRON_DIR, "jobs.json")
# Back-compat alias for test fixtures that monkeypatched `_cron.CRON_FILE`.
CRON_FILE = CRON_LEGACY_FILE

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
    "misfire": "run_late",
    "logs": {"retention": "forever"},
    "tick": {
        "schedule": "* * * * *",
        "lock_timeout_seconds": 0,
        "max_runtime_seconds": 50,
        "max_jobs_per_tick": 20,
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
    return datetime.now().astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse_iso(s):
    """ISO 8601 → tz-aware datetime; naive inputs assumed local tz."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now_local().tzinfo)
    return dt


def _new_job_id():
    return uuid4().hex[:8]


def _new_run_id():
    # `r` prefix per spec example; 9 hex chars after = ~36 bits of entropy
    return "r" + uuid4().hex[:9]


def _hostname():
    return _socket.gethostname()


def _short_host(h):
    return (h or "").split(".", 1)[0]


def _same_host(a, b):
    return _short_host(a) == _short_host(b)


def _detect_creator():
    """Best-effort creator stamp for `created_by`.

    Inside a camc agent tmux session, ``TMUX_PANE`` is set and the session
    name follows ``cam-<8hex>``. We stamp agent identity from the session;
    otherwise we fall back to the running user."""
    try:
        sess = os.environ.get("TMUX", "")
        # Allow tests to inject an explicit session via CAMC_TMUX_SESSION.
        sess_name = os.environ.get("CAMC_TMUX_SESSION")
        if not sess_name and sess:
            # `TMUX` is ",<socket>,<id>" — no session name. Best effort:
            # fall through to env hint.
            sess_name = os.environ.get("CAMC_AGENT_ID")
        if sess_name and sess_name.startswith("cam-"):
            agent_id = sess_name.split("-", 1)[1][:8]
            return {
                "type": "agent",
                "agent_id": agent_id,
                "agent_name": os.environ.get("CAMC_AGENT_NAME") or "",
                "tmux_session": sess_name,
            }
    except Exception:
        pass
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    except Exception:
        user = ""
    tty = ""
    try:
        if sys.stdin and sys.stdin.isatty():
            tty = os.ttyname(sys.stdin.fileno())
    except (OSError, ValueError):
        pass
    return {"type": "human", "user": user, "tty": tty}


# ---------------------------------------------------------------------------
# Schedule preset parsers (unchanged)
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
    """`HH:MM` → daily schedule dict (no next_due_at yet — added by build_job)."""
    if not s or not _HHMM_RE.match(s.strip()):
        raise ValueError("bad daily time %r — use HH:MM" % s)
    hh, mm = s.strip().split(":")
    return {"type": "daily", "time": "%02d:%s" % (int(hh), mm),
            "timezone": "local"}


def parse_at(s):
    """Absolute ISO timestamp → once schedule dict."""
    try:
        dt = datetime.fromisoformat(s.strip())
    except (ValueError, TypeError):
        raise ValueError(
            "bad --at %r — use ISO 8601 (e.g. 2026-05-12T09:30:00-07:00)" % s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now_local().tzinfo)
    return {"type": "once", "run_at": _iso(dt)}


def parse_in(s):
    sec = parse_duration(s)
    dt = _now_local() + timedelta(seconds=sec)
    return {"type": "once", "run_at": _iso(dt)}


def parse_every(s):
    sec = parse_duration(s)
    return {"type": "interval", "every_seconds": sec}


# ---------------------------------------------------------------------------
# next_due_at computation
# ---------------------------------------------------------------------------

def _initial_next_due_at(schedule, now_dt):
    """Compute the initial next_due_at for a freshly-created job."""
    stype = schedule.get("type")
    if stype == "interval":
        every = int(schedule.get("every_seconds") or 0)
        if every <= 0:
            return _iso(now_dt)
        return _iso(now_dt + timedelta(seconds=every))
    if stype == "daily":
        hh, mm = (schedule.get("time") or "00:00").split(":")
        cand = now_dt.replace(hour=int(hh), minute=int(mm),
                              second=0, microsecond=0)
        if cand <= now_dt:
            cand += timedelta(days=1)
        return _iso(cand)
    if stype == "once":
        run_at = schedule.get("run_at")
        return run_at
    return None


def advance_next_due_at(schedule, now_dt):
    """Return a NEW schedule dict with next_due_at advanced past `now_dt`.

    interval: add every_seconds until > now.
    daily:    next local HH:MM > now.
    once:     None (once jobs are not periodic; the scheduler will not
              re-fire them after queuing the single run).
    """
    new = dict(schedule)
    stype = schedule.get("type")
    if stype == "interval":
        every = int(schedule.get("every_seconds") or 0)
        if every <= 0:
            new["next_due_at"] = None
            return new
        cur = _parse_iso(schedule.get("next_due_at")) or now_dt
        if cur <= now_dt:
            # advance by whole intervals so we don't backfill missed slots
            cur = cur + timedelta(seconds=every)
            while cur <= now_dt:
                cur = cur + timedelta(seconds=every)
        new["next_due_at"] = _iso(cur)
        return new
    if stype == "daily":
        hh, mm = (schedule.get("time") or "00:00").split(":")
        cand = now_dt.replace(hour=int(hh), minute=int(mm),
                              second=0, microsecond=0)
        while cand <= now_dt:
            cand += timedelta(days=1)
        new["next_due_at"] = _iso(cand)
        return new
    if stype == "once":
        new["next_due_at"] = None
        return new
    return new


# ---------------------------------------------------------------------------
# Global config (unchanged interface)
# ---------------------------------------------------------------------------

class CronConfig(object):
    """Read/write ~/.cam/cron/config.json with defaults."""

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
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg

    def load_or_default(self):
        try:
            return self.load()
        except CorruptCronConfig:
            return dict(DEFAULT_CONFIG)

    def ensure(self):
        if os.path.exists(self._path):
            return
        _ensure_dir(os.path.dirname(self._path))
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# Per-file job store
# ---------------------------------------------------------------------------

def _job_file(job_id):
    return os.path.join(CRON_JOBS_DIR, "%s.json" % job_id)


class CronJobStore(object):
    """One-file-per-job storage under ~/.cam/cron/jobs.d/.

    A corrupt job file affects only that job — mutating commands refuse
    to touch corrupt files, and `is_corrupt()` returns the list of bad
    paths so `list`/`heal` can fail closed per spec.
    """

    def __init__(self, root=None):
        self._root = root or CRON_JOBS_DIR

    # -- migration -------------------------------------------------------

    def migrate_legacy_if_present(self):
        """One-shot import of an old ~/.cam/cron/jobs.json into jobs.d/.

        Idempotent: only runs when the legacy file exists AND jobs.d/
        is empty/missing. Bad legacy files are left in place and an
        error is returned; we never delete the old file."""
        legacy = CRON_LEGACY_FILE
        if not os.path.exists(legacy):
            return None
        _ensure_dir(self._root)
        try:
            existing = [n for n in os.listdir(self._root)
                        if n.endswith(".json")]
        except OSError:
            existing = []
        if existing:
            return None  # already migrated
        try:
            with open(legacy, "r") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("cron: legacy jobs.json present but unreadable: %s", e)
            return None
        jobs = (data or {}).get("jobs") or []
        migrated = 0
        for j in jobs:
            try:
                norm = _normalize_legacy_record(j)
                self.save(norm)
                migrated += 1
            except Exception as e:
                log.warning("cron: failed to migrate legacy job %s: %s",
                            j.get("id"), e)
        if migrated:
            # Rename the legacy file out of the way so we don't migrate
            # again on the next call. Keep evidence on disk.
            try:
                os.rename(legacy, legacy + ".migrated")
            except OSError:
                pass
        return migrated

    # -- read ------------------------------------------------------------

    def _scan(self):
        """Yield (job_id, path) for every *.json in jobs.d/."""
        if not os.path.isdir(self._root):
            return
        for name in sorted(os.listdir(self._root)):
            if not name.endswith(".json"):
                continue
            yield name[:-5], os.path.join(self._root, name)

    def load_one(self, path):
        with open(path, "r") as f:
            if _fcntl:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                if _fcntl:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        if not isinstance(data, dict) or "id" not in data:
            raise ValueError("missing required fields")
        return data

    def jobs(self):
        """Return all valid jobs (corrupt files are silently skipped for
        read-only callers — `is_corrupt()` is the authoritative check)."""
        out = []
        for _jid, path in self._scan():
            try:
                out.append(self.load_one(path))
            except (OSError, ValueError):
                continue
        return out

    def corrupt_files(self):
        """Return list of paths for unreadable / malformed job files."""
        bad = []
        for _jid, path in self._scan():
            try:
                self.load_one(path)
            except (OSError, ValueError):
                bad.append(path)
        return bad

    def is_corrupt(self):
        return bool(self.corrupt_files())

    def find(self, key):
        """Resolve key → job dict, or None. Exact id, exact name, or
        unique id prefix. Raises AmbiguousJobKey on multi-match prefix."""
        if not key:
            return None
        # Exact id is a one-file lookup.
        exact = os.path.join(self._root, "%s.json" % key)
        if os.path.exists(exact):
            try:
                return self.load_one(exact)
            except (OSError, ValueError):
                pass
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

    # -- write -----------------------------------------------------------

    def save(self, job):
        """Atomically write one job file (overwrite ok)."""
        jid = job.get("id")
        if not jid:
            raise ValueError("job missing id")
        _ensure_dir(self._root)
        path = _job_file(jid)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    def add(self, job):
        """Reject duplicate active names on the same host, then save."""
        name = job.get("name")
        host = job.get("host") or _hostname()
        for other in self.jobs():
            if other.get("name") == name and _same_host(
                    other.get("host") or "", host):
                raise DuplicateJobName(name)
        self.save(job)

    def remove(self, key):
        """Remove (delete) one job file. Returns the removed dict or None.

        Resolution: exact id, exact name, or UNIQUE id prefix. Ambiguous
        prefixes raise AmbiguousJobKey without touching files."""
        target = self.find(key)
        if target is None:
            return None
        path = _job_file(target["id"])
        try:
            os.remove(path)
        except OSError:
            pass
        return target

    # -- legacy alias used by recycle_job --------------------------------

    def _modify(self, fn):  # pragma: no cover — legacy shim
        """Compat shim for old code that mutated via a callback. Unused
        in the new path; kept to avoid breaking external callers."""
        raise NotImplementedError(
            "CronJobStore is per-file; use save() / remove() directly")


# Back-compat alias for callers that import CronStore.
CronStore = CronJobStore


def _normalize_legacy_record(j):
    """Convert an old flat job record into the new nested shape."""
    sched = dict(j.get("schedule") or {})
    if "next_due_at" not in sched:
        sched["next_due_at"] = _initial_next_due_at(sched, _now_local())
    created_at = j.get("created_at") or _iso(_now_local())
    return {
        "version": 1,
        "id": j.get("id") or _new_job_id(),
        "name": j.get("name") or "",
        "enabled": bool(j.get("enabled", True)),
        "kind": j.get("kind") or sched.get("type"),
        "host": j.get("host") or _hostname(),
        "created_by": j.get("created_by") or {"type": "human",
                                              "user": "", "tty": ""},
        "schedule": sched,
        "policy": {
            "ttl_days": j.get("ttl_days"),
            "expires_at": j.get("expires_at"),
            "max_attempts": j.get("max_attempts") or DEFAULT_MAX_ATTEMPTS,
        },
        "command": j.get("command") or {},
        "state": {
            "attempts": int(j.get("attempts") or 0),
            "created_at": created_at,
            "updated_at": j.get("updated_at") or created_at,
            "last_due_at": j.get("last_due_at"),
            "last_run_id": j.get("last_run_id"),
            "last_status": j.get("last_status"),
        },
    }


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
# Job builder
# ---------------------------------------------------------------------------

def build_job(name, schedule, command, *,
              ttl_days=None, expires_at=None, no_expire=False,
              max_attempts=None, host=None, created_at=None,
              created_by=None):
    """Normalize a job record matching the spec schema."""
    cfg = CronConfig().load_or_default()
    now_dt = _parse_iso(created_at) if created_at else _now_local()
    now_iso = created_at or _iso(now_dt)

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
        expires = (_iso(now_dt + timedelta(days=int(days)))
                   if days is not None else None)

    sched = dict(schedule)
    if "next_due_at" not in sched:
        sched["next_due_at"] = _initial_next_due_at(sched, now_dt)

    return {
        "version": 1,
        "id": _new_job_id(),
        "name": name,
        "enabled": True,
        "kind": sched.get("type"),
        "host": host or _hostname(),
        "created_by": created_by or _detect_creator(),
        "schedule": sched,
        "policy": {
            "ttl_days": ttl_days if ttl_days is not None
                        else cfg.get("default_ttl_days", DEFAULT_TTL_DAYS),
            "expires_at": expires,
            "max_attempts": max_attempts if max_attempts is not None
                            else cfg.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
        },
        "command": command,
        "state": {
            "attempts": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_due_at": None,
            "last_run_id": None,
            "last_status": None,
        },
    }


# ---------------------------------------------------------------------------
# Run / event logging
# ---------------------------------------------------------------------------

def _append_runs(record):
    """Append a record to ~/.cam/cron/runs.jsonl (best-effort)."""
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
    """Best-effort EventStore mirror so `camc history` can see cron events."""
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


def _scan_runs():
    """Yield parsed records from runs.jsonl (newest last)."""
    if not os.path.exists(CRON_RUNS_FILE):
        return
    try:
        with open(CRON_RUNS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _already_queued(job_id, due_at):
    """True iff a run_queued event exists for (job_id, due_at)."""
    for r in _scan_runs():
        if (r.get("event") == "run_queued"
                and r.get("job_id") == job_id
                and r.get("due_at") == due_at):
            return True
    return False


def _find_queued_run(run_id):
    """Walk runs.jsonl in reverse; return the run_queued record + terminal
    state ('queued' | 'started' | 'terminal' | 'missing')."""
    queued = None
    terminal = False
    started = False
    for r in _scan_runs():
        if r.get("run_id") != run_id:
            continue
        ev = r.get("event")
        if ev == "run_queued":
            queued = r
        elif ev == "run_started":
            started = True
        elif ev in ("run_succeeded", "run_failed", "run_timed_out"):
            terminal = True
    if queued is None:
        return None, "missing"
    if terminal:
        return queued, "terminal"
    if started:
        return queued, "started"
    return queued, "queued"


# ---------------------------------------------------------------------------
# Crontab block management
# ---------------------------------------------------------------------------

def _camc_path():
    if sys.argv and sys.argv[0]:
        p = os.path.realpath(sys.argv[0])
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    deployed = os.path.expanduser("~/.cam/camc")
    if os.path.isfile(deployed) and os.access(deployed, os.X_OK):
        return deployed
    found = shutil.which("camc")
    if found:
        return found
    return "camc"


# Spec: "Use a short stable PATH in the crontab line. Do not copy an
# arbitrarily long login-shell PATH into crontab." So we hard-code a
# short stable set and refuse to inherit $PATH.
_CRON_STABLE_PATH = "/home/prgn_share/bin:%s/.cam:/usr/local/bin:/usr/bin:/bin"


def _build_tick_block(camc_path=None):
    home = os.path.expanduser("~")
    path = _CRON_STABLE_PATH % home
    binp = camc_path or _camc_path()
    line = "* * * * * HOME=%s PATH=%s %s cron tick >> %s 2>&1" % (
        home, path, binp, CRON_HUMAN_LOG)
    return "%s\n%s\n%s" % (CRON_BEGIN, line, CRON_END)


def _read_user_crontab(runner=None):
    runner = runner or subprocess.run
    try:
        r = runner(["crontab", "-l"], stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, universal_newlines=True,
                   timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise CrontabUnavailable(str(e))
    if r.returncode != 0 and "no crontab" not in (r.stderr or "").lower():
        if r.returncode != 1 or r.stdout:
            raise CrontabUnavailable(r.stderr.strip()
                                     or "exit %d" % r.returncode)
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
    if CRON_BEGIN not in text and CRON_END not in text:
        return text
    out_lines = []
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if s == CRON_BEGIN:
            in_block = True
            continue
        if s == CRON_END:
            in_block = False
            continue
        if not in_block:
            out_lines.append(line)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def install_tick(camc_path=None, runner=None):
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
    current = _read_user_crontab(runner)
    if CRON_BEGIN not in current and CRON_END not in current:
        return False
    stripped = _strip_block(current)
    _write_user_crontab(stripped, runner)
    return True


def _block_in_crontab(text, camc_path=None):
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
    remove it iff no enabled jobs remain."""
    store = CronJobStore()
    store.migrate_legacy_if_present()
    if store.is_corrupt():
        return ("cron_json_corrupt", store.corrupt_files()[0])
    enabled = [j for j in store.jobs()
               if j.get("enabled", True)
               and _same_host(j.get("host") or _hostname(), _hostname())]
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
    """Archive a job + delete from jobs.d/ + append job_recycled."""
    archive_path = _archive_job(job, reason)
    try:
        CronJobStore().remove(job.get("id") or job.get("name"))
    except AmbiguousJobKey:
        pass
    _append_runs({
        "event": "job_recycled",
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "reason": reason,
        "archive_path": archive_path or "",
    })
    _emit_event("cron_job_recycled", job_id=job.get("id"), reason=reason)


# ---------------------------------------------------------------------------
# is_due — new contract using schedule.next_due_at as source of truth
# ---------------------------------------------------------------------------

def is_due(job, now=None):
    """Return (due, due_at_iso). Due iff now >= schedule.next_due_at."""
    if not job.get("enabled", True):
        return False, None
    sched = job.get("schedule") or {}
    nda = sched.get("next_due_at")
    if not nda:
        return False, None
    nda_dt = _parse_iso(nda)
    if nda_dt is None:
        return False, None
    now = now or _now_local()
    if now >= nda_dt:
        return True, nda
    return False, None


# ---------------------------------------------------------------------------
# Locking helpers
# ---------------------------------------------------------------------------

def _acquire_lock(path):
    _ensure_dir(os.path.dirname(path))
    try:
        f = open(path, "w")
    except OSError:
        return None
    if _fcntl is None:
        return f
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


# ---------------------------------------------------------------------------
# Worker spawn helper (separate so tests can monkeypatch)
# ---------------------------------------------------------------------------

def _spawn_worker(run_id):
    """Start `camc cron run <run_id>` detached. Returns spawned pid or None."""
    binp = _camc_path()
    _ensure_dir(CRON_LOGS_DIR)
    log_path = os.path.join(CRON_LOGS_DIR, "%s.log" % run_id)
    try:
        out = open(log_path, "ab")
    except OSError as e:
        log.warning("cron: failed to open worker log %s: %s", log_path, e)
        out = subprocess.DEVNULL
    try:
        p = subprocess.Popen(
            [binp, "cron", "run", run_id],
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return p.pid
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("cron: failed to spawn worker %s: %s", run_id, e)
        return None
    finally:
        try:
            if hasattr(out, "close"):
                out.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# tick — schedule + queue + spawn (NEVER executes user command inline)
# ---------------------------------------------------------------------------

def tick(now=None, runner=None, spawn=None):
    """Scheduler entrypoint. ``spawn`` overridable for tests."""
    spawn_fn = spawn or _spawn_worker
    started_at = _iso(_now_local())
    lock = _acquire_lock(CRON_LOCK_FILE)
    if lock is None:
        _append_runs({"event": "tick_skipped_locked",
                      "host": _hostname(), "pid": os.getpid()})
        return {"status": "skipped_locked", "queued": 0}
    try:
        _ensure_human_log()
        _append_runs({"event": "tick_started", "source": "system-cron",
                      "host": _hostname(), "pid": os.getpid()})
        _emit_event("cron_tick", host=_hostname())

        try:
            cfg = CronConfig().load()
        except CorruptCronConfig as e:
            _append_runs({"event": "tick_aborted",
                          "reason": "corrupt_config", "error": str(e)})
            _write_state("error", started_at, _iso(_now_local()), str(e))
            return {"status": "error",
                    "error": "corrupt config: %s" % e, "queued": 0}
        if not bool(cfg.get("enabled", True)):
            _append_runs({"event": "tick_disabled",
                          "reason": "config_enabled_false"})
            _write_state("disabled", started_at, _iso(_now_local()))
            return {"status": "disabled", "queued": 0}

        store = CronJobStore()
        store.migrate_legacy_if_present()
        if store.is_corrupt():
            bad = store.corrupt_files()
            err = "corrupt job files: %s" % ", ".join(bad)
            _append_runs({"event": "tick_aborted",
                          "reason": "corrupt_job_files",
                          "files": bad})
            _write_state("error", started_at, _iso(_now_local()), err)
            return {"status": "error", "error": err, "queued": 0}

        now_local = now or _now_local()

        # -- Recycle expired jobs first --
        for j in list(store.jobs()):
            exp = (j.get("policy") or {}).get("expires_at")
            if not exp:
                continue
            exp_dt = _parse_iso(exp)
            if exp_dt is None:
                continue
            if now_local >= exp_dt:
                recycle_job(j, "expired")

        my_host = _hostname()
        max_jobs = int((cfg.get("tick") or {}).get("max_jobs_per_tick", 20))
        queued = 0
        for j in store.jobs():
            if queued >= max_jobs:
                break
            if not j.get("enabled", True):
                continue
            jhost = j.get("host") or my_host
            if not _same_host(jhost, my_host):
                _append_runs({"event": "job_skipped_host",
                              "job_id": j.get("id"), "job_name": j.get("name"),
                              "job_host": jhost, "current_host": my_host})
                continue
            due, due_at = is_due(j, now=now_local)
            if not due:
                continue
            if _already_queued(j.get("id"), due_at):
                continue

            run_id = _new_run_id()
            _append_runs({
                "event": "run_queued",
                "run_id": run_id,
                "job_id": j.get("id"),
                "job_name": j.get("name"),
                "due_at": due_at,
                "host": my_host,
            })

            # Advance schedule + persist job state BEFORE dispatching the
            # worker so a delayed/crashing worker can't cause us to
            # re-queue the same due_at.
            j["schedule"] = advance_next_due_at(j["schedule"], now_local)
            st = j.setdefault("state", {})
            st["last_due_at"] = due_at
            st["last_run_id"] = run_id
            st["updated_at"] = _iso(_now_local())
            try:
                store.save(j)
            except OSError as e:
                log.warning("cron: failed to persist job after queueing: %s", e)

            # Dispatch (best-effort, async).
            spawn_fn(run_id)
            queued += 1

            # once jobs with next_due_at advanced to None do NOT recycle
            # here — recycle happens in the worker after success or in
            # subsequent ticks once max_attempts is exhausted.

        completed_at = _iso(_now_local())
        _write_state("ok", started_at, completed_at)
        return {"status": "ok", "queued": queued}
    finally:
        _release_lock(lock)


# ---------------------------------------------------------------------------
# run worker — execute one previously-queued run
# ---------------------------------------------------------------------------

def _run_command(cmd, *, timeout, cwd, log_path, runner=None):
    """Execute `cmd` (argv or shell str); pipe stdout/stderr to log_path.

    Returns (exit_code, duration_seconds, error_str, timed_out)."""
    runner = runner or subprocess.run
    start = time.time()
    _ensure_dir(os.path.dirname(log_path))
    try:
        logf = open(log_path, "ab")
    except OSError:
        logf = None
    try:
        kwargs = dict(cwd=cwd, stdout=logf or subprocess.DEVNULL,
                      stderr=subprocess.STDOUT, timeout=timeout)
        if isinstance(cmd, list):
            r = runner(cmd, **kwargs)
        else:
            r = runner(cmd, shell=True, **kwargs)
        return r.returncode, time.time() - start, None, False
    except subprocess.TimeoutExpired:
        return 124, time.time() - start, "timeout after %ss" % timeout, True
    except (OSError, subprocess.SubprocessError) as e:
        return 127, time.time() - start, str(e), False
    finally:
        try:
            if logf:
                logf.close()
        except OSError:
            pass


def cron_run(run_id, runner=None):
    """Execute one queued run. Idempotent: refuses re-execution.

    Returns a dict summary. Stdout/stderr of the user command go to
    ~/.cam/cron/logs/<run_id>.log. Structured events go to runs.jsonl.
    """
    queued, status = _find_queued_run(run_id)
    if status == "missing":
        msg = "no run_queued record for %s" % run_id
        log.warning("cron run: %s", msg)
        return {"status": "missing", "error": msg}
    if status in ("started", "terminal"):
        msg = "run %s already %s" % (run_id, status)
        log.info("cron run: %s", msg)
        return {"status": status, "error": msg}

    job_id = queued.get("job_id")
    due_at = queued.get("due_at")
    job_host = queued.get("host")
    if job_host and not _same_host(job_host, _hostname()):
        # Not ours — refuse to execute someone else's queued run.
        _append_runs({"event": "run_skipped_host",
                      "run_id": run_id, "job_id": job_id,
                      "job_host": job_host, "current_host": _hostname()})
        return {"status": "skipped_host"}

    store = CronJobStore()
    job = None
    try:
        job = store.find(job_id) if job_id else None
    except AmbiguousJobKey:
        job = None
    if job is None:
        _append_runs({"event": "run_failed", "run_id": run_id,
                      "job_id": job_id, "due_at": due_at,
                      "error": "job file missing",
                      "exit_code": 127, "attempts": 0})
        return {"status": "no_job"}

    cmd = job.get("command") or {}
    argv = cmd.get("argv")
    shell = cmd.get("shell")
    cwd = cmd.get("cwd") or os.getcwd()
    timeout = int(cmd.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    if not argv and not shell:
        _append_runs({"event": "run_failed", "run_id": run_id,
                      "job_id": job_id, "due_at": due_at,
                      "error": "job has no command",
                      "exit_code": 127, "attempts": 0})
        return {"status": "no_command"}

    log_path = os.path.join(CRON_LOGS_DIR, "%s.log" % run_id)
    _append_runs({
        "event": "run_started",
        "run_id": run_id,
        "job_id": job_id,
        "due_at": due_at,
        "pid": os.getpid(),
        "log_path": log_path,
    })
    _emit_event("cron_run_started", job_id=job_id, run_id=run_id)

    exit_code, duration, err, timed_out = _run_command(
        argv if argv else shell,
        timeout=timeout, cwd=cwd, log_path=log_path, runner=runner)

    success = (exit_code == 0)

    # Bump attempts / status on the job and persist (under run lock).
    rl = _acquire_lock(CRON_RUN_LOCK_FILE)
    try:
        # Re-read in case another worker mutated it.
        fresh = None
        try:
            fresh = store.find(job_id)
        except AmbiguousJobKey:
            fresh = None
        target = fresh or job
        st = target.setdefault("state", {})
        prev_attempts = int(st.get("attempts") or 0)
        if success:
            st["attempts"] = 0
            st["last_status"] = "exit_0"
        else:
            st["attempts"] = prev_attempts + 1
            st["last_status"] = "timeout" if timed_out else "failed"
        st["last_run_id"] = run_id
        st["last_due_at"] = due_at
        st["updated_at"] = _iso(_now_local())
        try:
            store.save(target)
        except OSError as e:
            log.warning("cron run: failed to persist job state: %s", e)
    finally:
        _release_lock(rl)

    if success:
        _append_runs({
            "event": "run_succeeded",
            "run_id": run_id, "job_id": job_id, "due_at": due_at,
            "exit_code": 0, "duration_seconds": round(duration, 3),
        })
        _emit_event("cron_run_succeeded", job_id=job_id, run_id=run_id)
        # once jobs succeed → recycle
        if (job.get("schedule") or {}).get("type") == "once":
            recycle_job(target, "once_completed")
        return {"status": "succeeded", "exit_code": 0,
                "duration_seconds": duration}

    event = "run_timed_out" if timed_out else "run_failed"
    _append_runs({
        "event": event,
        "run_id": run_id, "job_id": job_id, "due_at": due_at,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 3),
        "attempts": int(target.get("state", {}).get("attempts") or 0),
        "error": err or "command exited %d" % exit_code,
    })
    _emit_event("cron_run_failed" if not timed_out else "cron_run_timed_out",
                job_id=job_id, run_id=run_id, exit_code=exit_code)

    # Failure recycle on max_attempts.
    max_att = int((target.get("policy") or {}).get(
        "max_attempts") or DEFAULT_MAX_ATTEMPTS)
    if int(target.get("state", {}).get("attempts") or 0) >= max_att:
        recycle_job(target, "too_many_failures")
    return {"status": event, "exit_code": exit_code,
            "duration_seconds": duration, "error": err}
