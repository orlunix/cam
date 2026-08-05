# Node Cron Scheduler Design

## Goal

Run CAMC cron jobs and agent loops without a system crontab by maintaining one
lightweight scheduler process per local host and user.

## Design

Persistent jobs, loop definitions, workers, retry policy, and history remain
under `~/.cam`. Runtime coordination is node-local under
`/tmp/camc-<uid>/cron/`: `start.lock` serializes recovery, `tick.lock`
serializes each scheduling pass, and `state.json` stores the scheduler PID,
start time, and random generation token.

`cron add` and `cron list` check the PID and asynchronously start a missing
scheduler. `heal`, `heal --monitor`, and `heal --restart` use the same helper
synchronously. Restart only terminates a PID whose command line contains the
recorded generation. The scheduler runs the deployed `~/.cam/camc cron tick`
on minute boundaries so later ticks use the newest bundle.

Normal cron jobs already record a host. Loops will record their owner host and
only dispatch there. Legacy hostless jobs and loops are skipped unless a loop
host can be safely resolved from its owner record. Normal `camc list` never
checks scheduler state. New paths do not invoke or inspect `crontab`.

## Constraints

- No commit or push under this task.
- Keep legacy crontab helpers for compatibility, but do not call them from the
  new add/list/heal flow.
- Scheduler launch is best-effort for add/list and verified for heal.
- No user cron job is deleted while recovering runtime state.
