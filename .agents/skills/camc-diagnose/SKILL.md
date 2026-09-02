---
name: camc-diagnose
description: >
  Diagnose and fix stuck, exited, failed, or unresponsive camc agents.
  Use when an agent's status shows running but it's not responding,
  the monitor died, capture is empty, or you need to resume a dead
  agent's Claude session. Covers heal, monitor self-healing, and
  common failure modes with step-by-step fixes.
compatibility: Requires ~/.cam/camc deployed by the camc release.
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
~/.cam/camc list                              # all agents on this host
~/.cam/camc status <agent>                    # detailed state: status, state, exit_reason, pid, session
camc logs <agent> -f                   # follow monitor log for live errors
~/.cam/camc capture <agent> --lines 30        # see what's on screen
```

## Common failure modes

| Symptom | Fix |
|---------|-----|
| Status says `running` but tmux is dead | `~/.cam/camc heal` |
| Need to replace a healthy local monitor too | `~/.cam/camc heal --restart` |
| tmux settings need a refresh | `~/.cam/camc heal --tmux` |
| Agent stuck (idle but not responding) | `~/.cam/camc key <agent> --key Escape`, then `~/.cam/camc send <agent> --text "..."` |
| Agent `exited` or `failed` | Check `~/.cam/camc status <agent>` for `exit_reason`; `camc reboot <agent>` to retry with same session |
| Capture empty / attach hangs | `~/.cam/camc capture <agent> --lines 0` to verify; if still blank check `~/.cam/camc status <agent>` for `tmux_session` alive |
| Agent `state = error` | `camc logs <agent>` for tracebacks; `camc reboot <agent>` to restart |
| Many agents stopped responding | `~/.cam/camc heal` restarts all dead monitors |
| Stale sockets in /tmp/cam-sockets/ | `~/.cam/camc heal` auto-cleans |

## `~/.cam/camc heal` — what it does

Walks every running agent on the current host:

- Restarts agents whose monitor PID is dead
- Cleans stale socket files at `/tmp/cam-sockets/` whose tmux server is gone
- Adopts orphan tmux sessions into agents.json
- Filters by hostname — on NFS-shared `~/.cam/agents.json` clusters,
  only touches agents from the current machine

```bash
~/.cam/camc heal               # default: recover dead monitors; do not restart healthy ones
~/.cam/camc heal --monitor     # explicit spelling of the same default monitor heal
~/.cam/camc heal --restart     # one verified local monitor at a time, then normal heal
~/.cam/camc heal --tmux        # rewrite CAMC tmux.conf, source it in each local agent socket
~/.cam/camc heal --agents      # migrate only verified legacy agents.json records
```

`heal --upgrade` is retained only as a hidden, deprecated compatibility
command. Do not use it for routine monitor recovery; use `heal --restart`
when a deliberate local monitor restart is needed.

### When to run heal

| Trigger | Command |
|---------|---------|
| `~/.cam/camc list` shows running but nothing's happening | `~/.cam/camc heal` |
| Need to deliberately restart healthy local monitors | `~/.cam/camc heal --restart` |
| Need the managed tmux template on active local servers | `~/.cam/camc heal --tmux` |
| Many agents stopped responding to send/key | `~/.cam/camc heal` |
| Sockets piling up in `/tmp/cam-sockets/` | `~/.cam/camc heal` (auto-cleans) |

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

After 5 consecutive failures the monitor exits and `~/.cam/camc heal` is
expected to restart it on the next sweep.

Logs: `~/.cam/logs/monitor-<id>.log` (stdout), `monitor-<id>.stderr`.
PID file: `~/.cam/pids/<id>.pid`.

## Resuming a dead agent's Claude session

If an agent is gone but the Claude session JSONL still exists, you can
resume the conversation in a fresh agent:

```bash
# Find the session id from the old agent record
camc --json list | python3 -c "
for a in json.load(sys.stdin):
    sid = a.get('session_id','')
    if sid: print(a['id'][:8], a.get('status'), sid)
"

# Resume it
~/.cam/camc run --tool claude \
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
