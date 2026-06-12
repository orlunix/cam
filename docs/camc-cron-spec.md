# Feature Spec: camc Cron Jobs

## Overview

Add a small, host-local cron facility to camc for scheduled agent work.
The design goal is simple and stable: jobs are validated by camc, stored
as individual JSON files, scheduled by one tick per host, and executed by
a separate run worker.

Agent-owned monitor loops are a separate feature. They reuse schedule and
policy concepts, but live in per-agent `agent.loop.json` files and are
executed by the owner's monitor process, not by `camc cron tick`. See
`docs/camc-agent-loop-spec.md`.

User-facing commands stay intentionally small:

```bash
camc cron add --name NAME (--every DUR | --daily HH:MM | --at TIME | --in DUR) [options] -- COMMAND...
camc cron rm <job-id-or-name>
camc cron list [--json]
```

Internal commands:

```bash
camc cron tick
camc cron run <run_id>
```

`tick` is the scheduler entrypoint. It is called by the OS cron daemon,
or in the future by the camc monitor loop. `tick` must stay lightweight:
it scans jobs, claims due work, records a run, starts `camc cron run
<run_id>`, and exits. `cron run` owns actual command execution.

## Design Rules

- One host has one cron executor.
- One host installs one camc cron tick block.
- Any human or agent on that host may create jobs through `camc cron add`.
- A job defaults to `host = current hostname`.
- Only the same host's tick may execute that job.
- On shared NFS, other hosts may see the job file, but must skip it.
- `tick` schedules only; it must not block on long command execution or
  agent replies.
- `cron run <run_id>` executes one already-recorded run.
- Jobs are one-file-per-job. Do not use one global `jobs.json` registry.
- Action semantics are opaque to cron. `camc msg`, `camc run`, shell
  commands, and other commands are all just executable actions.

## Goals

- Keep the public API small: add, remove, list.
- Make storage robust on shared filesystems by using one job per file.
- Keep cron idempotent with run records and locks.
- Avoid missed due work by storing `schedule.next_due_at`.
- Avoid long scheduler latency by dispatching `cron run` workers.
- Auto-recycle expired jobs and jobs with too many failures.
- Preserve logs and run history for postmortem.

## Non-Goals

- No host:port or CPU executor identity in P0.
- No `host:any` cluster-wide execution in P0.
- No raw cron expression UI in P0.
- No direct user editing of canonical job JSON in the normal path.
- No separate crontab line per job.
- No requirement for an always-running camc daemon.
- No parsing of action-specific semantics inside cron.

### Agent Loop Integration (V0, 2026-06-12, CAM-DESK-CRON-010)

`camc cron tick` now also services agent loops under
`~/.cam/loops/<owner>/agent.loop.json` after the host-job pass.
The CLI surface is `camc cron {add,list,rm} --loop --owner <agent>`;
see `docs/camc-agent-loop-spec.md` for the loop file shape and
the JSON list envelope Desktop consumes. Loops cannot execute
shell commands — the only allowed action is sending prompt text
to the owner agent via `camc msg send`, and dispatch defers when
the owner is not currently `status=running, state=idle`.

This integration is additive: the host job surface
(`jobs.d/<job_id>.json`, `cron run <run_id>`, the crontab block
spec below) is unchanged. Loop dispatch never reads `jobs.d/` and
host-job dispatch never reads `loops/`.

## Files

```text
~/.cam/cron/config.json           global cron settings
~/.cam/cron/jobs.d/<job_id>.json  one active job per file
~/.cam/cron/runs.jsonl            append-only run/event log
~/.cam/cron/state.json            tick heartbeat / last status
~/.cam/cron/tick.lock             non-blocking scheduler lock
~/.cam/cron/run.lock              short lock for run state updates
~/.cam/cron/logs/<run_id>.log     stdout/stderr for one run worker
~/.cam/cron/archive/              archived removed/recycled jobs
~/.cam/cron/cron.log              human-readable tick stdout/stderr
```

A corrupt job file affects only that job. camc must not overwrite it or
silently ignore it in mutating commands. `list`, `tick`, and `heal`
should fail closed or warn clearly when corrupt job files are found.

## User Commands

### `camc cron add ... -- COMMAND...`

Adds one job from a small set of scheduling presets. Users and agents use
this command; they do not write `jobs.d/*.json` by hand.

Examples:

```bash
camc cron add --name review --every 30m -- camc msg send cam-dev -t "review" --no-wait --expect-reply
camc cron add --name heartbeat --every 2h -- camc list
camc cron add --name daily --daily 09:30 -- camc msg send cam-dev -t "daily check" --no-wait
camc cron add --name once --at "2026-05-12T09:30:00-07:00" -- camc msg send cam-dev -t "one shot" --no-wait
camc cron add --name later --in 45m -- camc msg send cam-dev -t "check this later" --no-wait
```

Options:

```text
--ttl-days N          recycle job after N days [default: config default, 7]
--expires-at TIME     recycle job at an absolute time
--no-expire           job never expires automatically
--max-attempts N      recycle after N failed command attempts [default: 3]
--timeout SECONDS     child command timeout [default: 60]
--cwd PATH            command working directory [default: current directory]
--host HOST           execution host [default: current hostname]
--shell "COMMAND"     explicit shell command mode; mutually exclusive with -- COMMAND...
```

`--host any` is not supported in P0. On shared NFS, all nodes may see all
job files, but each node tick only schedules jobs whose `host` equals the
current hostname.

Behavior:

1. Parse and validate exactly one schedule preset.
2. Load or create `config.json`.
3. Scan `jobs.d/*.json`.
4. Reject duplicate active job names on the same host.
5. Build the normalized job record.
6. Write `jobs.d/<job_id>.json` atomically.
7. Ensure the single system cron tick block exists.
8. Print the job id and tick state.

Output:

```text
added cron job daily-review (a1b2c3d4)
tick: installed
```

If the crontab update fails, the job remains saved and the command exits
non-zero with a repair hint:

```text
added cron job daily-review (a1b2c3d4)
ERROR: failed to install system cron tick: <reason>
Run `camc heal` after fixing crontab access.
```

### `camc cron rm <job-id-or-name>`

Removes one active job. Removal is archival, not destructive.

Behavior:

1. Resolve by exact id, exact name, or unique id prefix.
2. Move the job file from `jobs.d/` to `archive/`.
3. Append `job_removed` to `runs.jsonl`.
4. If no enabled jobs remain on this host, remove the camc crontab block.

Ambiguous prefixes are rejected without changing files.

### `camc cron list [--json]`

Read-only listing of active jobs.

Behavior:

1. Scan `jobs.d/*.json`.
2. Fail closed on corrupt job files: exit non-zero, stderr explains the
   bad file, and `--json` emits no partial JSON.
3. Print one row per active job or emit a stable JSON payload.
4. Do not install/remove crontab, run tick, mutate jobs, append runs, or
   archive anything.

Human output:

```text
ID         NAME                  SCHED               EN   HOST             EXPIRES              ATT/MAX  LAST
a1b2c3d4   daily-review          daily 09:00         y    pd05             2026-05-17T09:00:00  0/3      -
0d1e2f34   ping                  every 30m           y    pd05             2026-05-17T09:00:00  0/3      -
```

Empty output:

```text
No cron jobs.
```

JSON output:

```json
{
  "count": 1,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily-review",
      "enabled": true,
      "kind": "daily",
      "schedule": {"type": "daily", "time": "09:00", "timezone": "local", "next_due_at": "2026-05-11T09:00:00-07:00"},
      "host": "pd05",
      "expires_at": "2026-05-17T09:00:00-07:00",
      "max_attempts": 3,
      "attempts": 0,
      "last_status": null,
      "last_due_at": null,
      "last_run_id": null
    }
  ]
}
```

Empty JSON output is `{"count": 0, "jobs": []}`.

## Internal Commands

### `camc cron tick`

This command is installed into the user's crontab and is not the primary
human workflow.

Behavior:

1. Take a non-blocking exclusive lock on `tick.lock`.
2. If lock is held, append `tick_skipped_locked` and exit 0.
3. Load config and scan `jobs.d/*.json`.
4. Update `state.json` with `last_tick_started_at`.
5. Recycle expired jobs before scheduling.
6. For each enabled job whose `host` matches current hostname:
   - If `now < schedule.next_due_at`, skip.
   - If a queued/running/terminal run already exists for `job_id + due_at`, skip.
   - Create a new `run_id`.
   - Append `run_queued` to `runs.jsonl`.
   - Update job state: `last_due_at`, `last_run_id`, `updated_at`.
   - Advance `schedule.next_due_at`.
   - Start detached worker: `camc cron run <run_id>`.
7. Update `state.json` with `last_tick_completed_at` and status.
8. Exit quickly.

`tick` must not execute the saved action inline. Its output and errors go
to `cron.log` through the system crontab block.

### `camc cron run <run_id>`

This command is internal. `tick` starts it in detached/nohup-style mode
for a previously recorded run.

Behavior:

1. Look up `run_id` in `runs.jsonl`.
2. Refuse to run if missing, already terminal, or if the job file is missing.
3. Append `run_started` with pid and log path.
4. Execute the opaque action:
   - `command.argv`: `subprocess.run(argv, cwd=..., timeout=...)`
   - `command.shell`: explicit shell mode only.
5. Write stdout/stderr to `logs/<run_id>.log`.
6. Append `run_succeeded`, `run_failed`, or `run_timed_out`.
7. Update job runtime state: attempts, last_run_id, last_status, updated_at.
8. Recycle the job when appropriate.

The worker only evaluates process result. Exit code 0 is success; nonzero
exit or timeout is failure. It does not inspect agent state, message
replies, or command output semantics.

Agent jobs should normally be async so the worker does not wait for model
completion:

```bash
camc msg send <agent> -t "..." --no-wait --expect-reply
```

## System Crontab Block

camc owns exactly one marked block in the current user's crontab.

```cron
# camc cron begin
* * * * HOME=/home/hren PATH=/home/prgn_share/bin:/home/hren/.cam:/usr/local/bin:/usr/bin:/bin /home/prgn_share/bin/camc cron tick >> /home/hren/.cam/cron/cron.log 2>&1
# camc cron end
```

Rules:

- Only modify text between `# camc cron begin` and `# camc cron end`.
- Never rewrite unrelated user crontab lines.
- If the block exists with an old camc path, replace the block.
- If active enabled jobs exist for this host and the block is missing,
  install it.
- If no enabled jobs remain for this host after `cron rm`, remove it.
- Use a short stable PATH in the crontab line. Do not copy an arbitrarily
  long login-shell PATH into crontab.

## Heal Integration

`camc heal` keeps existing agent monitor repair behavior and adds one
cron repair check:

```text
if jobs.d/ has at least one enabled active job for current host:
    ensure the system cron tick block exists and is current
else:
    do not install tick
```

Repair output:

```text
Cron: installed missing tick entry (3 enabled job(s))
```

Failure output:

```text
Cron: repair failed: crontab command not available
```

No other camc command should auto-install cron except `cron add` and
`heal`.

## Job Schema

One job is stored in one file:

```text
~/.cam/cron/jobs.d/a1b2c3d4.json
```

Example:

```json
{
  "version": 1,
  "id": "a1b2c3d4",
  "name": "daily-review",
  "enabled": true,
  "kind": "daily",
  "host": "pd05",
  "created_by": {
    "type": "agent",
    "agent_id": "f1a1a661",
    "agent_name": "cam-dev",
    "tmux_session": "cam-f1a1a661"
  },
  "schedule": {
    "type": "daily",
    "time": "09:00",
    "timezone": "local",
    "next_due_at": "2026-05-11T09:00:00-07:00"
  },
  "policy": {
    "ttl_days": 7,
    "expires_at": "2026-05-17T09:00:00-07:00",
    "max_attempts": 3
  },
  "command": {
    "argv": [
      "camc",
      "msg",
      "send",
      "cam-dev",
      "-t",
      "Review latest changes and reply with blockers.",
      "--no-wait",
      "--expect-reply"
    ],
    "cwd": "/current/dir",
    "timeout_seconds": 60
  },
  "state": {
    "attempts": 0,
    "created_at": "2026-05-10T09:00:00-07:00",
    "updated_at": "2026-05-10T09:00:00-07:00",
    "last_due_at": null,
    "last_run_id": null,
    "last_status": null
  }
}
```

Creation identity:

- If `cron add` runs inside a known camc agent tmux session, stamp
  `created_by.type = "agent"` plus agent id/name/session.
- If `cron add` runs from a normal shell, stamp `created_by.type =
  "human"`, current user, and best-effort tty.
- `created_by` is metadata. Execution ownership is still the `host` field.

Host behavior:

- If input omits `--host`, camc writes current hostname.
- `tick` only schedules jobs whose `host` equals current hostname.
- Other nodes on shared NFS skip the job.
- P0 does not support `host:any` or host:port executor identity.

## Schedule Presets

P0 supports simple user-facing schedule forms only:

```text
--every 30m      interval job, every 30 minutes
--every 2h       interval job, every 2 hours
--daily 09:30    daily job at local 09:30
--at TIME        one-time job at absolute timestamp
--in 45m         one-time job after a delay
```

Persisted schedule examples:

```json
{"type": "interval", "every_seconds": 1800, "next_due_at": "2026-05-10T09:30:00-07:00"}
```

```json
{"type": "daily", "time": "09:30", "timezone": "local", "next_due_at": "2026-05-11T09:30:00-07:00"}
```

```json
{"type": "once", "run_at": "2026-05-12T09:30:00-07:00", "next_due_at": "2026-05-12T09:30:00-07:00"}
```

`--in 45m` is converted to a concrete `run_at` and `next_due_at` when
the job is added.

### Due Semantics

`next_due_at` is the source of truth. A job is due when:

```text
now >= schedule.next_due_at
```

After `tick` queues a run, it advances `next_due_at` before dispatching
the worker. This avoids missing a job when the tick is delayed or busy.

Idempotency key:

```text
job_id + due_at
```

If repeated ticks evaluate the same due time, only one run should be
queued.

### Advancing `next_due_at`

- Interval: add `every_seconds` until the value is greater than `now`.
- Daily: choose the next local day/time greater than `now`.
- Once: after queuing its run, set `next_due_at = null`; recycle on
  success, or retry on later ticks after failure until `max_attempts` is
  exhausted.

## Command Forms

Preferred command form is argv. Everything after `--` is stored as
`command.argv` without shell parsing:

```bash
camc cron add --name nightly --daily 23:30 --timeout 120 --cwd /repo \
  -- camc run -t codex -p /repo -n cron-nightly "Run nightly checks."
```

```json
{
  "command": {
    "argv": ["camc", "run", "-t", "codex", "-p", "/repo", "-n", "cron-nightly", "Run nightly checks."],
    "cwd": "/repo",
    "timeout_seconds": 120
  }
}
```

Shell mode is explicit only:

```bash
camc cron add --name shell-example --every 30m --shell "camc list >/tmp/camc-list.txt"
```

```json
{"shell": "camc list >/tmp/camc-list.txt", "cwd": "/current/dir", "timeout_seconds": 60}
```

Validation rule: exactly one of `command.argv` or `command.shell` is
present.

## Run Records

`runs.jsonl` is append-only. It records scheduler decisions and worker
results. A run has one `run_id` and belongs to one `job_id` and one
`due_at`.

Queued:

```json
{
  "ts": "2026-05-10T09:00:00-07:00",
  "event": "run_queued",
  "run_id": "r9f31a2c0",
  "job_id": "a1b2c3d4",
  "job_name": "daily-review",
  "due_at": "2026-05-10T09:00:00-07:00",
  "host": "pd05"
}
```

Started:

```json
{
  "ts": "2026-05-10T09:00:01-07:00",
  "event": "run_started",
  "run_id": "r9f31a2c0",
  "job_id": "a1b2c3d4",
  "pid": 12345,
  "log_path": "/home/hren/.cam/cron/logs/r9f31a2c0.log"
}
```

Succeeded:

```json
{
  "ts": "2026-05-10T09:00:02-07:00",
  "event": "run_succeeded",
  "run_id": "r9f31a2c0",
  "job_id": "a1b2c3d4",
  "exit_code": 0,
  "duration_seconds": 1.2
}
```

Failed:

```json
{
  "ts": "2026-05-10T09:00:02-07:00",
  "event": "run_failed",
  "run_id": "r9f31a2c0",
  "job_id": "a1b2c3d4",
  "exit_code": 1,
  "attempts": 2
}
```

Recycled:

```json
{
  "ts": "2026-05-10T09:00:02-07:00",
  "event": "job_recycled",
  "job_id": "a1b2c3d4",
  "job_name": "daily-review",
  "reason": "too_many_failures",
  "archive_path": "/home/hren/.cam/cron/archive/20260510-090002-daily-review-a1b2c3d4.json"
}
```

## Global Config

`config.json` is auto-created on first `cron add`.

```json
{
  "version": 1,
  "enabled": true,
  "default_ttl_days": 7,
  "max_attempts": 3,
  "misfire": "run_late",
  "logs": {"retention": "forever"},
  "tick": {
    "schedule": "* * * * *",
    "lock_timeout_seconds": 0,
    "max_runtime_seconds": 50,
    "max_jobs_per_tick": 20
  }
}
```

Config rules:

- `enabled=false` makes `tick` exit without scheduling runs.
- `default_ttl_days` applies when a job omits `ttl_days`.
- `ttl_days` is converted to concrete `policy.expires_at` at add time.
- `ttl_days: null` and `expires_at: null` mean no automatic expiration.
- Per-job `policy.max_attempts` overrides the global default.
- `misfire="run_late"` means if `next_due_at` is in the past, tick queues
  one run and advances to the next future due time. It does not backfill
  every missed interval.
- Logs are permanent in P0.
- Invalid config makes `tick` fail closed.

## Recycle Policy

Recycle means move the job file out of `jobs.d/`, write an archived copy,
and append a structured run event. It does not silently delete evidence.

### TTL Recycle

On `tick`, `run`, and `heal`:

```text
if job.policy.expires_at is not null and now >= expires_at:
    recycle reason = expired
```

Default TTL is 7 days from job creation.

### Failure Recycle

On every failed run:

```text
job.state.attempts += 1
if job.state.attempts >= job.policy.max_attempts:
    recycle reason = too_many_failures
```

On success:

```text
job.state.attempts = 0
```

### Once Job Recycle

A once job is recycled after success with:

```text
reason = once_completed
```

If it fails, it is retried on later ticks until `max_attempts` is
exhausted.

### Manual Remove

`camc cron rm` archives with:

```text
reason = manual_remove
```

## Heartbeat

`state.json` is overwritten on each tick.

```json
{
  "version": 1,
  "last_tick_started_at": "2026-05-10T09:00:00-07:00",
  "last_tick_completed_at": "2026-05-10T09:00:02-07:00",
  "last_tick_status": "ok",
  "last_tick_host": "pd05",
  "last_tick_pid": 12345,
  "last_error": null
}
```

This is the cron health signal. A stale heartbeat means the system cron
entry is missing, cron daemon is not running, the camc path is bad, or
`tick` fails before updating state.

## Failure Handling

- Invalid `cron add` options: exits non-zero and writes nothing.
- Missing command after `--`: exits non-zero and writes nothing.
- Corrupt `config.json`: tick fails closed.
- Corrupt job file: mutating commands refuse to overwrite it; list fails
  closed; tick warns/skips and records the corrupt path.
- Crontab install failure: job remains saved, add exits non-zero.
- Tick lock held: tick exits 0 and writes `tick_skipped_locked`.
- Duplicate `job_id + due_at`: no duplicate run is queued.
- Worker command exits non-zero: write `run_failed`; update attempts.
- Worker timeout: terminate child, write `run_timed_out`; update attempts.
- Three failed attempts by default: recycle job.
- Message target missing is just a command failure.
- Agent reply timeout is not interpreted by cron unless the supplied
  command itself exits non-zero.

## Implementation Plan

### Module

```text
src/camc_pkg/cron.py
```

Responsibilities:

- `CronConfig`: read/write config with defaults.
- `CronJobStore`: scan/read/write one job per file under `jobs.d/`.
- Schedule parser, `next_due_at` calculation, and due computation.
- Crontab block install/remove/ensure.
- `tick`: scheduler, run queueing, detached worker spawn.
- `run`: execute one recorded run, write logs, update job state.
- Archive/recycle.
- Structured run logging.

### CLI

`src/camc_pkg/cli.py`:

- `camc cron add`
- `camc cron rm`
- `camc cron list [--json]`
- internal `camc cron tick`
- internal `camc cron run <run_id>`
- call `cron.ensure_tick_if_needed()` from `cmd_heal`

### Build Sync

After implementation:

```text
src/camc_pkg/cron.py
src/camc_pkg/cli.py
src/camc
dist/camc
dist/BUILD_LOG.md
tests/test_cron.py
```

## Acceptance Tests

1. `cron add --every 30m -- ...` writes `jobs.d/<job_id>.json`, creates
   config, and installs exactly one crontab block.
2. Duplicate active job name on the same host fails without changing job
   files.
3. `cron rm` archives the job file and removes the tick block when no
   enabled jobs remain for this host.
4. `heal` repairs a missing tick block when enabled current-host jobs exist.
5. `heal` does not install tick when no enabled current-host jobs exist.
6. `cron list` is read-only and emits stable human/JSON output.
7. Corrupt job file makes `list` fail closed and does not overwrite data.
8. `tick` takes a non-blocking lock; concurrent tick exits without
   duplicate queueing.
9. `job_id + due_at` prevents duplicate run queueing.
10. `tick` queues due jobs and starts `camc cron run <run_id>` instead of
    executing the user command inline.
11. `cron run <run_id>` executes `command.argv` without shell parsing and
    records exit code.
12. `command.shell` is accepted only in explicit shell mode.
13. `cron run` writes `logs/<run_id>.log`.
14. Interval jobs advance `next_due_at` after queueing.
15. Daily jobs advance to the next local daily time.
16. Once jobs run after `run_at`, recycle after success, and retry after
    failure until `max_attempts`.
17. Expired jobs are archived using the default 7-day TTL.
18. Jobs are recycled after three failed attempts by default.
19. Host mismatch skips scheduling and records `job_skipped_host` or an
    equivalent structured event.
20. On shared NFS, a different host seeing the job file does not execute it.
21. `runs.jsonl`, `state.json`, `cron.log`, and `logs/<run_id>.log` are
    written in expected locations.
22. The crontab PATH is short and stable; long login-shell PATH values do
    not create an overlong crontab command.
23. `src/camc` and `dist/camc` are rebuilt and byte-identical.

## Open Questions

- Whether P1 should support `camc cron show <job>` and `camc cron runs <job>`.
- Whether P1 should support a safe interactive editor for job updates.
- Whether P1 should support cluster-wide `host:any` with atomic claims.
- Whether P1 should use the monitor loop as an alternate tick backend.
