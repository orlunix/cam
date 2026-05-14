# Feature Spec: camc Cron Jobs

## Overview

Add a small cron facility to camc for scheduled agent work.

The design keeps the user-facing command surface intentionally small:

```bash
camc cron add --name NAME (--every DUR | --daily HH:MM | --at TIME | --in DUR) [options] -- COMMAND...
camc cron rm <job-id-or-name>
```

Internally, camc installs one system crontab entry that calls:

```bash
camc cron tick
```

`tick` is the scheduler entrypoint. It is called by the OS cron daemon,
not by a long-running camc daemon. `camc heal` also repairs the crontab
entry when active cron jobs exist and the tick entry is missing.

## Goals

- Keep the public API simple: add and remove jobs.
- Store jobs in a camc-owned JSON registry.
- Automatically install the system cron tick entry when the first job is
  added.
- Automatically repair the tick entry from `camc heal` when cron jobs
  exist.
- Keep execution idempotent with locks and per-due-time records.
- Auto-recycle jobs after a configured TTL or repeated failures.
- Provide structured logs for diagnosis and postmortem.

## Non-Goals

- Do not make every camc command inspect or mutate crontab.
- Do not store every job as a separate crontab line.
- Do not parse action-specific business semantics such as `msg` or `run`
  in the scheduler. The user supplies a runnable command and owns its
  correctness.
- Do not depend on an always-running camc daemon.
- Do not make per-agent monitor loops the only scheduler backend.

## Files

```text
~/.cam/cron.json             active job registry
~/.cam/cron-config.json      global cron settings
~/.cam/cron-runs.jsonl       structured execution/recycle log
~/.cam/cron.state.json       heartbeat and last tick status
~/.cam/cron.lock             tick lock
~/.cam/cron-archive/         archived removed/recycled jobs
~/.cam/logs/cron.log         human-readable tick stdout/stderr
```

`cron.json` and `cron-config.json` are ordinary JSON files with
fcntl-based write locking, following the same defensive style as
`AgentStore`.

## User Commands

### `camc cron add ... -- COMMAND...`

Adds one job from a small set of scheduling presets. Users do not edit
`~/.cam/cron.json` directly. `cron add` validates the CLI options and
fills a generated job record from defaults.

Supported schedule forms:

```bash
camc cron add --name review --every 30m -- camc msg send cam-dev -t "review" --no-wait --expect-reply
camc cron add --name heartbeat --every 2h -- camc list
camc cron add --name daily --daily 09:30 -- camc msg send cam-dev -t "daily check" --no-wait
camc cron add --name once --at "2026-05-12T09:30:00-07:00" -- camc msg send cam-dev -t "one shot" --no-wait
camc cron add --name later --in 45m -- camc msg send cam-dev -t "check this later" --no-wait
```

Common options:

```text
--ttl-days N          recycle job after N days [default: config default, 7]
--expires-at TIME     recycle job at an absolute time
--no-expire           job never expires automatically
--max-attempts N      recycle after N failed command attempts [default: 3]
--timeout SECONDS     child command timeout [default: 60]
--cwd PATH            command working directory [default: current directory]
--host HOST|any       execution host [default: current hostname]
--shell "COMMAND"     explicit shell command mode; mutually exclusive with -- COMMAND...
```

Behavior:

1. Parse and validate one schedule preset.
2. Load or create `~/.cam/cron-config.json`.
3. Load or create `~/.cam/cron.json`.
4. Reject duplicate active job names.
5. Build a normalized job record from the default template.
6. Save the job into `~/.cam/cron.json`.
7. Ensure the system cron tick block exists.
8. Print the job id and tick state.

Example:

```bash
camc cron add --name daily-review --daily 09:00 \
  -- camc msg send cam-dev -t "Review latest changes." --no-wait --expect-reply
```

Output:

```text
added cron job daily-review (a1b2c3d4)
tick: installed
```

If the crontab update fails, the job remains saved and the command exits
non-zero with a clear repair hint:

```text
added cron job daily-review (a1b2c3d4)
ERROR: failed to install system cron tick: <reason>
Run `camc heal` after fixing crontab access.
```

### `camc cron rm <job-id-or-name>`

Removes one active job. Removal is archival, not destructive.

Behavior:

1. Resolve by exact id, id prefix, or exact name.
2. Remove the job from active `cron.json`.
3. Write a copy to `~/.cam/cron-archive/<timestamp>-<name>-<id>.json`.
4. Append a `job_removed` record to `cron-runs.jsonl`.
5. If no enabled jobs remain, remove the camc system crontab block.

Example:

```bash
camc cron rm daily-review
```

Output:

```text
removed cron job daily-review (a1b2c3d4)
tick: removed (no enabled jobs remain)
```

`rm` resolves the argument in order: exact id, exact name, then a
unique id prefix. An ambiguous prefix (more than one match) is
rejected without changing the registry — the operator is asked to
re-run with a longer prefix or the exact name.

### `camc cron list [--json]`

Read-only listing of active jobs.

Behavior:

1. Read `~/.cam/cron.json`.
2. If the file exists but is corrupt: print a clear error to stderr
   and exit non-zero. Do not silently emit an empty listing — silent
   "empty" would mask the failure.
3. Otherwise print one row per active job in a stable table, or with
   `--json` emit a machine-readable payload.

`list` does NOT install or remove the system crontab block, does NOT
run `tick`, and does NOT mutate `cron.json`, `cron-runs.jsonl`, or
`cron-archive/`. Failing closed on corrupt registry is what
distinguishes it from a no-op.

Human (default) output columns:

```text
ID         NAME                  SCHED               EN   HOST             EXPIRES              ATT/MAX  LAST
a1b2c3d4   daily-review          daily 09:00         y    pd05             2026-05-17T09:00:00  0/3      -
0d1e2f34   ping                  every 30m           y    pd05             2026-05-17T09:00:00  0/3      -
```

If the registry is empty (or has no jobs after recycle), `list` prints:

```text
No cron jobs.
```

`--json` shape:

```json
{
  "count": 2,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily-review",
      "enabled": true,
      "kind": "daily",
      "schedule": {"type": "daily", "time": "09:00", "timezone": "local"},
      "host": "pd05",
      "expires_at": "2026-05-17T09:00:00-07:00",
      "max_attempts": 3,
      "attempts": 0,
      "last_status": null,
      "last_due_at": null
    }
  ]
}
```

Empty registry under `--json` returns `{"count": 0, "jobs": []}`.
Corrupt registry under `--json` still fails closed: exit 1, error on
stderr, empty stdout (no partial JSON object).

`list` is the only cron command safe to wire into scripts or
dashboards — `add`/`rm` mutate registry and crontab; `tick` is for the
OS cron daemon only.

## Internal Command

### `camc cron tick`

This command is installed into the user's crontab and is not the primary
human workflow.

Behavior:

1. Take non-blocking exclusive lock on `~/.cam/cron.lock`.
2. If lock is held, append `tick_skipped_locked` and exit 0.
3. Load config and jobs.
4. Update `cron.state.json` heartbeat with `last_tick_started_at`.
5. Recycle expired jobs before scheduling.
6. Compute due jobs for the current minute.
7. For each due job:
   - Skip if disabled.
   - Skip if host does not match current host.
   - Skip if `job_id + due_at` already has a terminal run record.
   - Append `job_started`.
   - Execute the job command.
   - Append `job_succeeded` or `job_failed`.
   - Update job counters and last-run fields.
   - Recycle if failure policy threshold is reached.
8. Recycle jobs that crossed their configured expiration boundary.
9. Update `cron.state.json` with `last_tick_completed_at` and status.

`tick` only evaluates the command's process result: exit code 0 means
success; non-zero exit or timeout means failure. It does not inspect
agent state, message replies, or command output semantics.

## System Crontab Block

camc owns exactly one marked block in the current user's crontab.

```cron
# camc cron begin
* * * * HOME=/home/hren PATH=/home/prgn_share/bin:/home/hren/.cam:/usr/local/bin:/usr/bin:/bin /home/prgn_share/bin/camc cron tick >> /home/hren/.cam/logs/cron.log 2>&1
# camc cron end
```

Rules:

- Only modify text between `# camc cron begin` and `# camc cron end`.
- Never rewrite unrelated user crontab lines.
- If the block exists with an old camc path, replace the block.
- If active enabled jobs exist and the block is missing, install it.
- If no enabled jobs remain after `cron rm`, remove the block.

## Heal Integration

`camc heal` keeps its existing agent monitor repair behavior and adds one
cron repair check:

```text
if ~/.cam/cron.json has at least one enabled active job:
    ensure the system cron tick block exists and is current
else:
    do not install tick
```

`heal` should print a short line when it repairs cron:

```text
Cron: installed missing tick entry (3 enabled jobs)
```

If crontab access fails, `heal` should warn but continue with normal
agent healing:

```text
Cron: repair failed: crontab command not available
```

No other camc command should auto-install cron.

## Generated Job Template

`cron add` writes normalized JSON into `~/.cam/cron.json`. Users should
not edit this registry by hand. The command remains opaque to the
scheduler: camc does not parse whether it is a message, run, apply, or
any other operation. The user provides a command that is directly
executable, and cron only evaluates exit code and timeout.

The default generated template is:

```json
{
  "name": "daily-review",
  "kind": "daily",
  "schedule": {
    "type": "daily",
    "time": "09:00",
    "timezone": "local"
  },
  "enabled": true,
  "ttl_days": 7,
  "expires_at": "2026-05-18T09:00:00-07:00",
  "max_attempts": 3,
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
  }
}
```

This example is generated from:

```bash
camc cron add --name daily-review --daily 09:00 \
  -- camc msg send cam-dev -t "Review latest changes and reply with blockers." --no-wait --expect-reply
```

### Schedule Presets

P0 supports these simple user-facing schedule forms:

```text
--every 30m      interval job, every 30 minutes
--every 2h       interval job, every 2 hours
--daily 09:30    daily job at local 09:30
--at TIME        one-time job at absolute timestamp
--in 45m         one-time job after a delay
```

Generated schedule records:

```json
{"type": "interval", "every_seconds": 1800, "anchor": "created_at"}
```

```json
{"type": "daily", "time": "09:30", "timezone": "local"}
```

```json
{"type": "once", "run_at": "2026-05-12T09:30:00-07:00"}
```

`--in 45m` is converted to a concrete `run_at` when the job is added.

### Command Forms

The preferred command form is argv. Everything after `--` is stored as
`command.argv` without shell parsing:

```bash
camc cron add --name nightly --daily 23:30 --timeout 120 --cwd /repo \
  -- camc run -t codex -p /repo -n cron-nightly "Run nightly checks."
```

```json
{
  "command": {
    "argv": [
      "camc",
      "run",
      "-t",
      "codex",
      "-p",
      "/repo",
      "-n",
      "cron-nightly",
      "Run nightly checks."
    ],
    "cwd": "/repo",
    "timeout_seconds": 120
  }
}
```

Shell mode is supported only through explicit `--shell`:

```bash
camc cron add --name shell-example --every 30m --shell "camc list >/tmp/camc-list.txt"
```

Generated command:

```json
{"shell": "camc list >/tmp/camc-list.txt", "cwd": "/current/dir", "timeout_seconds": 60}
```

Validation rule: exactly one of `command.argv` or `command.shell` is
present.

### One-Time Jobs

One-time jobs are created by `--at` or `--in`. They are recycled after
success with `reason = once_completed`. If a one-time job fails, it is
retried on later ticks until `max_attempts` is exhausted, then recycled
with `reason = too_many_failures`.

## Persisted Job Schema

camc stores the generated fields in `~/.cam/cron.json`.

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily-review",
      "enabled": true,
      "kind": "daily",
      "schedule": {
        "type": "daily",
        "time": "09:00",
        "timezone": "local"
      },
      "host": "pd05",
      "ttl_days": 7,
      "expires_at": "2026-05-17T09:00:00-07:00",
      "max_attempts": 3,
      "attempts": 0,
      "created_at": "2026-05-10T09:00:00-07:00",
      "updated_at": "2026-05-10T09:00:00-07:00",
      "last_due_at": null,
      "last_run_id": null,
      "last_status": null,
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
      }
    }
  ]
}
```

Host behavior:

- If input omits `host`, camc writes the current hostname.
- `"host": "any"` is allowed but not recommended on shared home
  directories.
- `tick` only executes jobs whose host is current hostname or `any`.

## Global Config

`~/.cam/cron-config.json` is auto-created on first `cron add`.

```json
{
  "version": 1,
  "enabled": true,
  "default_ttl_days": 7,
  "max_attempts": 3,
  "misfire": "skip",
  "logs": {
    "retention": "forever"
  },
  "tick": {
    "schedule": "* * * * *",
    "lock_timeout_seconds": 0,
    "max_runtime_seconds": 50
  }
}
```

Config rules:

- `default_ttl_days` applies when a job omits `ttl_days`.
- `ttl_days` is converted to a concrete per-job `expires_at` when the
  job is added.
- A job may provide `expires_at` directly instead of `ttl_days`.
- `ttl_days: null` and `expires_at: null` mean the job does not expire
  automatically.
- Per-job `max_attempts` overrides the global default.
- `misfire: "skip"` means missed runs are not backfilled after downtime.
- Logs are not time-pruned in P0. `cron-runs.jsonl` is append-only, and
  `logs.retention` is fixed to `"forever"` for clarity.
- P0 does not need a `camc cron config` command. Users may edit the
  JSON file directly; invalid config makes `tick` fail closed.

## Scheduling Semantics

P0 does not expose raw cron expressions to users. `cron add` supports
only simple presets and stores normalized schedule objects.

### Interval

`--every DUR` supports minutes and hours:

```text
--every 5m
--every 30m
--every 2h
```

`DUR` grammar:

```text
<positive integer><m|h>
```

The persisted schedule is:

```json
{"type": "interval", "every_seconds": 1800, "anchor": "created_at"}
```

Interval jobs are due when `now >= last_due_at + every_seconds`. If
`last_due_at` is null, the first due time is `created_at +
every_seconds`.

### Daily

`--daily HH:MM` runs once per local day at the requested time.

```bash
camc cron add --name daily --daily 09:30 -- camc list
```

The persisted schedule is:

```json
{"type": "daily", "time": "09:30", "timezone": "local"}
```

### Once

`--at TIME` and `--in DUR` create one-time jobs.

```bash
camc cron add --name once --at "2026-05-12T09:30:00-07:00" -- camc list
camc cron add --name later --in 45m -- camc list
```

The persisted schedule is:

```json
{"type": "once", "run_at": "2026-05-12T09:30:00-07:00"}
```

### Due Identity

Due time is rounded to minute precision. The idempotency key is:

```text
job_id + due_at
```

If repeated `tick` processes evaluate the same minute, only one may
execute the job.

## Recycle Policy

Recycle means: remove from active registry, write archived copy, append
a structured log record. It does not silently delete evidence.

### TTL Recycle

On every `tick` and `heal`, camc checks active jobs:

```text
if job.expires_at is not null and now >= expires_at:
    recycle reason = expired
```

Default TTL is 7 days from job creation.

### Failure Recycle

On every job failure:

```text
job.attempts += 1
if job.attempts >= max_attempts:
    recycle reason = too_many_failures
```

On success:

```text
job.attempts = 0
```

Default max attempts is 3. The scheduler does not try to prove the
command is correct; the user owns that guarantee. A command that exits
non-zero three times is removed from the active registry and archived
for inspection.

### Manual Remove

`camc cron rm` uses the same archive path with:

```text
recycle reason = manual_remove
```

## Log System

### Human Log

`~/.cam/logs/cron.log` receives stdout/stderr from the system crontab
entry. It is for quick debugging.

### Structured Log

`~/.cam/cron-runs.jsonl` is append-only JSONL.

Required event shapes:

```json
{
  "ts": "2026-05-10T09:00:00-07:00",
  "event": "tick_started",
  "source": "system-cron",
  "host": "pd05",
  "pid": 12345
}
```

```json
{
  "ts": "2026-05-10T09:00:00-07:00",
  "event": "job_started",
  "run_id": "20260510-090000-a1b2c3d4",
  "job_id": "a1b2c3d4",
  "job_name": "daily-review",
  "due_at": "2026-05-10T09:00:00-07:00",
  "command": ["camc", "msg", "send", "cam-dev", "-t", "...", "--no-wait", "--expect-reply"]
}
```

```json
{
  "ts": "2026-05-10T09:00:02-07:00",
  "event": "job_succeeded",
  "run_id": "20260510-090000-a1b2c3d4",
  "job_id": "a1b2c3d4",
  "job_name": "daily-review",
  "status": "exit_0",
  "exit_code": 0,
  "duration_seconds": 1.2
}
```

```json
{
  "ts": "2026-05-10T09:00:02-07:00",
  "event": "job_failed",
  "run_id": "20260510-090000-a1b2c3d4",
  "job_id": "a1b2c3d4",
  "job_name": "daily-review",
  "error": "command exited 1",
  "exit_code": 1,
  "attempts": 2
}
```

```json
{
  "ts": "2026-05-10T09:00:02-07:00",
  "event": "job_recycled",
  "job_id": "a1b2c3d4",
  "job_name": "daily-review",
  "reason": "too_many_failures",
  "archive_path": "/home/hren/.cam/cron-archive/20260510-090002-daily-review-a1b2c3d4.json"
}
```

`EventStore` should also receive concise events:

```text
cron_tick
cron_job_started
cron_job_succeeded
cron_job_failed
cron_job_recycled
cron_tick_installed
```

Use `agent_id=null` or `agent_id="cron"` for global cron events.

## Heartbeat

`~/.cam/cron.state.json` is overwritten on each tick.

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

This file is the best health signal. A stale heartbeat means the system
cron entry is missing, cron daemon is not running, the camc path is bad,
or tick is failing before it can update state.

## Failure Handling

- Invalid `cron add` options: exits non-zero and writes nothing.
- Missing command after `--`: exits non-zero and writes nothing.
- Corrupt `cron.json`: refuse to overwrite; print repair guidance.
- Crontab install failure: job remains saved, add exits non-zero.
- Lock held: tick exits 0 and writes `tick_skipped_locked`.
- Command exits non-zero: write `job_failed`; update attempt counters.
- Command timeout: terminate the child process, write `job_failed`,
  update attempt counters.
- Three failed attempts by default: recycle job.
- Message target missing is just a command failure.
- Run preflight failure is just a command failure.
- Agent reply timeout is not interpreted by cron unless the supplied
  command itself exits non-zero.

## Implementation Plan

### New module

```text
src/camc_pkg/cron.py
```

Responsibilities:

- CronStore read/write with locking.
- Config read/write with defaults.
- Schedule parser and due computation.
- Crontab block install/remove/ensure.
- Tick execution and idempotency.
- Archive/recycle.
- Structured logging.

### CLI changes

`src/camc_pkg/cli.py`:

- Add `cmd_cron`.
- Add parser for `cron add`, `cron rm`, `cron list`, `cron tick`.
- Hide or de-emphasize `tick` in help because it is system-facing.
- Call `cron.ensure_tick_if_needed()` from `cmd_heal`.

### Build sync

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

1. `cron add --every 30m -- ...` writes a normalized interval job,
   creates config, and installs exactly one crontab block.
2. Running `cron add` for a duplicate name fails without changing the
   registry.
3. `cron rm` archives the job and removes the tick block when no enabled
   jobs remain.
4. `heal` repairs a missing tick block when `cron.json` has enabled jobs.
5. `heal` does not install tick when there are no enabled jobs.
6. `tick` takes a non-blocking lock; concurrent tick exits without
   duplicate execution.
7. `job_id + due_at` prevents duplicate execution for the same minute.
8. Expired jobs are recycled using the default 7-day TTL.
9. A job is recycled after three failed command attempts by default.
10. `--daily HH:MM` writes a normalized daily job.
11. `--at TIME` and `--in DUR` write normalized once jobs.
12. `command.argv` executes without shell parsing and records exit code.
13. `command.shell` is accepted only when explicitly configured and
    records exit code.
14. Once jobs run after `run_at` and recycle after success.
15. `cron-runs.jsonl`, `cron.state.json`, and `logs/cron.log` are
    written in expected locations.
16. Corrupt `cron.json` is not overwritten by add, rm, heal, or tick.
17. Host mismatch skips execution and logs `job_skipped_host`.

## Open Questions

- Whether `cron rm` should support a future hard-delete/purge mode.
- Whether P1 should support `systemd timer` or monitor-hook triggering
  as alternative tick backends.
- Whether P1 should add command templates such as timestamps in argv.
