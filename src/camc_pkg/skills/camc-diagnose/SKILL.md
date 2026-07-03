---
name: camc-diagnose
description: >
  Diagnose and fix stuck, exited, failed, or unresponsive camc agents.
  Use when an agent's status shows running but it's not responding,
  the monitor died, capture is empty, or you need to resume a dead
  agent's Claude session. Covers heal, monitor self-healing, and
  common failure modes with step-by-step fixes.
compatibility: Requires camc binary in PATH.
metadata:
  author: hren
  tags: camc, cam, diagnose, diagnosis, heal, stuck, error, failed,
    exited, stopped, resume, monitor, fix, troubleshoot, debugging
  category: infra
  requires-tools: camc, tmux
disable-model-invocation: false
argument-hint: "[list | status <agent> | heal | logs <agent> | reboot <agent>]"
allowed-tools: Bash Read Glob Grep
---

# camc-diagnose — Fix Stuck & Failed Agents

## Quick diagnosis workflow

```bash
camc list                              # all agents on this host
camc status <agent>                    # detailed state: status, state, exit_reason, pid, session
camc logs <agent> -f                   # follow monitor log for live errors
camc capture <agent> --lines 30        # see what's on screen
```

## Common failure modes

| Symptom | Fix |
|---------|-----|
| Status says `running` but tmux is dead | `camc heal` |
| Monitor died after camc upgrade | `camc upgrade` (or `camc heal --upgrade`) |
| Agent stuck (idle but not responding) | `camc key <agent> --key Escape`, then `camc send <agent> --text "..."` |
| Agent `exited` or `failed` | Check `camc status <agent>` for `exit_reason`; `camc reboot <agent>` to retry with same session |
| Capture empty / attach hangs | `camc capture <agent> --lines 0` to verify; if still blank check `camc status <agent>` for `tmux_session` alive |
| Agent `state = error` | `camc logs <agent>` for tracebacks; `camc reboot <agent>` to restart |
| Many agents stopped responding | `camc heal` restarts all dead monitors |
| Stale sockets in /tmp/cam-sockets/ | `camc heal` auto-cleans |

## `camc heal` — what it does

Walks every running agent on the current host:

- Restarts agents whose monitor PID is dead
- Cleans stale socket files at `/tmp/cam-sockets/` whose tmux server is gone
- Adopts orphan tmux sessions into agents.json
- Filters by hostname — on NFS-shared `~/.cam/agents.json` clusters,
  only touches agents from the current machine

```bash
camc heal               # restart dead monitors
camc heal --upgrade     # kill ALL monitors, restart with current binary
```

### When to run heal

| Trigger | Command |
|---------|---------|
| `camc list` shows running but nothing's happening | `camc heal` |
| Just deployed new camc version | `camc upgrade` |
| Many agents stopped responding to send/key | `camc heal` |
| Sockets piling up in `/tmp/cam-sockets/` | `camc heal` (auto-cleans) |

### Cron suggestion

```bash
# Every 30 minutes
*/30 * * * * /home/hren/.cam/camc heal >/dev/null 2>&1
```

`cam sync` deploys this cron entry automatically (best-effort).

## Monitor self-healing

Every running agent has a background **monitor** that:

- Captures the tmux pane every ~1s
- Detects state (planning/editing/testing/committing/idle)
- Auto-confirms permission dialogs
- Detects completion
- Updates `~/.cam/agents.json` and `~/.cam/events.jsonl`

The monitor wraps its main loop in `try/except` with restart-on-error.
Common exceptions handled:

- `database is locked` (SQLite contention)
- `OSError` from flaky NFS read on `agents.json`
- `tmux server died` mid-capture

After 5 consecutive failures the monitor exits and `camc heal` is
expected to restart it on the next sweep.

Logs: `~/.cam/logs/monitor-<id>.log` (stdout), `monitor-<id>.stderr`.
PID file: `~/.cam/pids/<id>.pid`.

## Resuming a dead agent's Claude session

If an agent is gone but the Claude session JSONL still exists, you can
resume the conversation in a fresh agent:

```bash
# Find the session id from the old agent record
camc --json list | python3 -c "
import json, sys
for a in json.load(sys.stdin):
    sid = a.get('session_id','')
    if sid: print(a['id'][:8], a.get('status'), sid)
"

# Resume it
camc run --tool claude \
  --name <new-name> \
  --resume <session-id>
```

`--resume` passes through to `claude --resume`, which reads the JSONL and
rebuilds the agent's full memory. The new agent gets a new agent ID and
tmux session, but inherits the old conversation history.

For session JSONL paths and inspection, see managing-camc's
`reference/sessions.md`.

## Reference

| File | When to read |
|---|---|
| `reference/heal-and-monitor.md` | Monitor internals, self-healing details, NFS hostname filtering |