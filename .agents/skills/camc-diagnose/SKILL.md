---
name: camc-diagnose
description: >
  Diagnose and fix stuck, exited, failed, or unresponsive camc agents.
  Use when an agent's status shows alive but it's not responding,
  the monitor died, capture is empty, or a recorded terminal agent may
  still be alive. Covers heal, monitor self-healing, and
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
~/.cam/camc logs <agent> -f                   # follow monitor log for live errors
~/.cam/camc capture <agent> --lines 30        # see what's on screen
```

## Common failure modes

| Symptom | Fix |
|---------|-----|
| Status says `alive` but tmux is dead | `~/.cam/camc heal` |
| Need to replace a healthy local monitor too | `~/.cam/camc heal --restart` |
| tmux settings need a refresh | `~/.cam/camc heal --tmux` |
| Agent stuck (idle but not responding) | `~/.cam/camc key <agent> --key Escape`, then `~/.cam/camc send <agent> --text "..."` |
| Agent `dead/failed` | Check `~/.cam/camc status <agent>` for `exit_reason`; `~/.cam/camc reboot <agent>` to launch a fresh tool with the same agent ID |
| Capture empty / attach hangs | `~/.cam/camc capture <agent> --lines 0` to verify; if still blank check `~/.cam/camc status <agent>` for `tmux_session` alive |
| Agent `state = error` | `~/.cam/camc logs <agent>` for tracebacks; `~/.cam/camc reboot <agent>` to restart |
| Many agents stopped responding | `~/.cam/camc heal` restarts all dead monitors |
| Stale sockets in /tmp/cam-sockets/ | `~/.cam/camc heal` auto-cleans |

## `~/.cam/camc heal` — what it does

Inspects local agents, including records marked dead:

- Repairs stale terminal/activity states only when pane 0 has a verified live tool
- Restarts missing monitors; does not reboot tools as part of status repair
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
| `~/.cam/camc list` shows alive but nothing's happening | `~/.cam/camc heal` |
| Need to deliberately restart healthy local monitors | `~/.cam/camc heal --restart` |
| Need the managed tmux template on active local servers | `~/.cam/camc heal --tmux` |
| Many agents stopped responding to send/key | `~/.cam/camc heal` |
| Sockets piling up in `/tmp/cam-sockets/` | `~/.cam/camc heal` (auto-cleans) |

### Startup stalled in initializing

Follow the startup supervision procedure in **managing-camc**: capture the
pane, handle workspace trust and routine onboarding choices yourself, and
continue until `alive` with `phase=started` or a concrete failure/blocker.
Do not treat a returned run command, a live tmux shell, or a submitted LSF job
as proof that the tool is ready. Use `status` to read `exit_reason`; preserve
failed panes for inspection. Reboot is a fresh conversation, not resume.

## Monitor self-healing

Every running agent has a background **monitor** that:

- Captures the tmux pane every ~1s
- Tracks alive agents as idle/running and startup as initializing/started
- Auto-confirms permission dialogs
- Records process exit as dead/stopped, dead/failed, or dead/killed
- Updates `~/.cam/agents.json` and `~/.cam/events.jsonl`

The monitor wraps its main loop in `try/except` with restart-on-error.
Common exceptions handled:

- `OSError` from flaky NFS read on `agents.json`
- `tmux server died` mid-capture

The monitor retries up to five times after its initial failure, then records
`monitor_error`. That alone does not mark the tool dead; `heal` can restart
the monitor. Old monitor generations must not overwrite rebooted agents.

Logs: `~/.cam/logs/monitor-<id>.log` (stdout), `monitor-<id>.stderr`.
PID file: `~/.cam/pids/<id>.pid`.

## Resuming a dead agent's Claude session

If an agent is gone but the Claude session JSONL still exists, you can
resume the conversation in a fresh agent:

```bash
# Find the session id from the old agent record
~/.cam/camc --json list | python3 -c "
import json, sys
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

For session JSONL paths and inspection, see [managing-camc session reference](../managing-camc/reference/sessions.md).

## Reference

| File | When to read |
|---|---|
| `reference/heal-and-monitor.md` | Monitor internals, self-healing details, NFS hostname filtering |
