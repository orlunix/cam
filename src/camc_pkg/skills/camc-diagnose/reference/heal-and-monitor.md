# `camc heal` and the monitor loop

## What the monitor is

Every running agent has a background **monitor** Python subprocess that:

- captures the tmux pane every ~1s
- detects state (`planning`, `editing`, `testing`, `committing`, `idle`)
- auto-confirms permission dialogs (`1` for menu, `y` for y/n, etc.)
- detects completion (Claude prompt count, busy/done patterns from TOML)
- updates `~/.cam/agents.json` and emits events to `~/.cam/events.jsonl`
- self-restarts on crash (5 retries with exponential backoff)

Logs land in `~/.cam/logs/monitor-<id>.log` (stdout) and
`monitor-<id>.stderr`. PID file: `~/.cam/pids/<id>.pid`.

## `camc heal`

Walks every running agent on the current host:

```bash
camc heal               # restart any agent whose monitor PID is dead
camc heal --upgrade     # kill ALL monitors, restart each with the current
                        # camc binary — use after deploying a new version
```

Heal also cleans up:
- stale socket files at `/tmp/cam-sockets/` whose tmux server is gone
- orphan tmux sessions adoptable into agents.json (Phase 3)

Heal **filters by hostname** — on NFS-shared `~/.cam/agents.json` clusters
(PDX containers), `camc heal` on machine X only touches agents whose
`hostname` field matches X. Other machines' agents are untouched.

## When to run heal

| Symptom | Action |
|---|---|
| `camc list` shows running but nothing's happening | `camc heal` |
| Just deployed new camc version | `camc heal --upgrade` |
| Many agents stopped responding to send/key | `camc heal` |
| sockets piling up in `/tmp/cam-sockets/` | `camc heal` (auto-cleans) |

## Cron suggestion

```bash
# Every 30 minutes
*/30 * * * * /home/hren/.cam/camc heal >/dev/null 2>&1
```

`cam sync` deploys this cron entry automatically (best-effort — tolerated
to fail on hosts without `crontab`). Server side, `cam heal` runs hourly
and SSHes into each unique host to call `camc heal` once per host.

## Self-healing details

The monitor wraps its main loop in `try/except` with restart-on-error.
Common exceptions handled:

- `database is locked` (SQLite contention with many concurrent agents)
- `OSError` from a flaky NFS read on `agents.json`
- `tmux server died` mid-capture

After 5 consecutive failures the monitor exits and `camc heal` is
expected to restart it on the next sweep.
