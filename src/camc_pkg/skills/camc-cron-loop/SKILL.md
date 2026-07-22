---
name: camc-cron-loop
description: >
  Schedule recurring shell commands (host cron jobs) and per-agent
  prompt loops via ~/.cam/camc cron. Use when the user wants to schedule
  a recurring prompt to an agent, set up a periodic shell command,
  or manage existing cron jobs and loops.
compatibility: Requires ~/.cam/camc deployed by the camc release.
metadata:
  author: hren
  tags: camc, cam, cron, loop, schedule, scheduled, periodic,
    recurring, agent-loop, prompt-loop
  category: infra
  requires-tools: camc
disable-model-invocation: false
argument-hint: "[add | list | rm | tick]"
allowed-tools: Bash Read Glob Grep
---

# camc-cron-loop — Scheduled Jobs & Agent Loops

Two kinds of scheduled work, both managed via `~/.cam/camc cron`:

- **Host cron jobs** — shell commands on a schedule, persistent across reboots
- **Per-agent prompt loops** — recurring prompts delivered to one agent via mailbox

## Host cron jobs

Run any shell/argv command on a schedule. Job files at `~/.cam/cron/jobs.d/`;
a single marked block in the user's crontab calls `~/.cam/camc cron tick` every minute.

```bash
~/.cam/camc cron add --name daily-rev --daily 09:00 \
  -- ~/.cam/camc msg send cam-dev -t "Review latest changes." --no-wait
~/.cam/camc cron add --name ping --every 30m -- ~/.cam/camc list
~/.cam/camc cron add --name later --in 45m -- ~/.cam/camc msg send cam-dev -t "check this" --no-wait
~/.cam/camc cron add --name shell-example --every 30m \
  --shell "~/.cam/camc list > /tmp/camc-list.txt"
```

```bash
~/.cam/camc cron list                          # active jobs table
~/.cam/camc cron list --json                   # stable JSON (count + jobs[])
~/.cam/camc cron rm <id|name|prefix>           # archive + remove
```

Schedule presets (exactly one): `--every Nm|Nh`, `--daily HH:MM` (local),
`--at <ISO-8601>`, `--in Nm|Nh`. Defaults: `--ttl-days 7`, `--max-attempts 3`,
`--timeout 60`.

## Per-agent prompt loops (`--loop`)

Deliver a recurring prompt to **one specific agent** via mailbox.
Stored in `~/.cam/loops/<owner_id>/agent.loop.json` (separate from
host cron jobs). Dispatched by `~/.cam/camc cron tick`, but **only when the
owner agent is `status=running` AND `state=idle`** — if busy, the tick
is silently deferred (no message sent, schedule not advanced).
This means the agent is never interrupted mid-task, and prompts
never pile up.

```bash
# Register a loop on agent "cam-dev"
~/.cam/camc cron add --loop --owner cam-dev \
  --name skill-check --every 30m \
  --prompt "Use the managing-camc skill and check whether any agents are stuck."

~/.cam/camc cron add --loop --owner cam-dev \
  --name daily-status --daily 09:00 \
  --prompt-file path/to/daily-prompt.md \
  --no-expire

# List / remove
~/.cam/camc cron list --loop --owner cam-dev       # shows this agent's loops
~/.cam/camc cron rm --loop --owner cam-dev <name>  # archives + removes the loop entry
```

**Delivery mechanism:** each fire = `~/.cam/camc msg send <owner_id> -t <prompt> --no-wait`.
The prompt lands in the agent's mailbox ledger, and the agent reads it
via `~/.cam/camc msg read --next` on its next idle turn. One message at a time —
no pileup because of the idle-gate.

**Loop file:** `~/.cam/loops/<agent_id>/agent.loop.json` — one file
per agent, `loops:[]` array inside. Multiple loops per agent supported.
`runs.jsonl` in the same dir records dispatch events.

**Lifecycle:**
- Busy owner → `loop_deferred` event, schedule NOT advanced, attempts NOT incremented
- Failed dispatch → attempts incremented; disabled at `max_attempts`
- One-time loops (`--at`, `--in`) → archived after success
- `--no-expire` → loop persists indefinitely

## When to use loops vs host cron

| Use loop | Use host cron |
|----------|--------------|
| Recurring prompts to an agent (status checks, skill invocations) | Arbitrary shell/argv commands |
| Action = text only | Not tied to one agent |
| Gated by agent idle state | Runs unconditionally on schedule |

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Loop not firing | `~/.cam/camc status <owner>` — must be `running` + `idle` |
| Loop disabled | `runs.jsonl` — check attempt count vs `max_attempts` |
| Host cron not running | `~/.cam/camc cron list` — verify job exists; check crontab has the `~/.cam/camc cron tick` block |
| `~/.cam/camc cron tick` errors | `~/.cam/logs/cron-tick.log` |