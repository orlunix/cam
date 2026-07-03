---
name: managing-camc
description: >
  Manage AI coding agents (Claude Code, Codex, Cursor) on a single
  machine with camc — the standalone, stdlib-only Python CLI. Use when
  the user wants to start an agent, send it a prompt, capture its
  output, attach to it, resume / reboot / move it, or clean up records.
  For inter-agent messaging use the camc-messaging skill. For cron/loops use
  camc-cron-loop. For diagnosing stuck agents use camc-diagnose. For
  cross-machine fleet ops use the sibling managing-cam skill.
compatibility: Requires camc binary in PATH or at ~/.cam/camc.
metadata:
  author: hren
  tags: camc, cam, agent, agents, tmux, claude, claude-code, codex,
    cursor, monitor, session, attach, capture, send, key, prune,
    reboot, resume, migrate, heal, single-machine, ai-agent,
    coding-agent, pm2, run, lifecycle
  category: infra
  requires-tools: camc, tmux
disable-model-invocation: false
argument-hint: "[run | list | capture <agent> | send <agent> -t '...' | reboot <agent> | attach <agent> | status <agent>]"
allowed-tools: Bash Read Glob Grep
---

# camc — single-machine agent manager

camc starts AI coding agents (claude / codex / cursor) inside tmux
sessions, lets you send them prompts and read their output, and tracks
each agent's lifecycle. Every agent has its own tmux session, monitor
process, and Claude session-id, so multiple agents in the same
workspace are fully isolated at the conversation level.

> **When in doubt, run `--help` first — never guess.** Flags drift
> between releases; the binary is the source of truth. `camc --help`
> for subcommands; `camc <subcommand> --help` for flags.

Agents are addressable by name, ID prefix, or `#N` (1-based from `camc list`).

## Quick Reference

| Task | Command |
|---|---|
| Start agent | `camc run "task" -n <name>` |
| Start interactively | `camc run -n <name>` |
| Resume Claude session | `camc run -n <name> --resume <session-id>` |
| List agents | `camc list` (or `ls`) |
| Status detail | `camc status <agent>` |
| Capture screen | `camc capture <agent> --lines 200` |
| Send text + Enter | `camc send <agent> --text "msg"` |
| Send text only | `camc send <agent> --text "..." --no-enter` |
| Send special key | `camc key <agent> --key Escape` |
| Attach interactively | `camc attach <agent>` (Ctrl+B D to detach) |
| Logs | `camc logs <agent> -f` |
| Reboot (resume session) | `camc reboot <agent>` |
| Move to other host | `camc reboot <agent> --to host:port` |
| Update name/tag | `camc update <agent> --name x --tag T` |
| Stop (graceful) | `camc stop <agent>` |
| Kill (force) | `camc kill <agent>` |
| Remove | `camc rm <agent>` (always kills tmux; `--archive` to save first) |
| Bulk cleanup | `camc prune --orphans` |
| Start with system prompt | `camc run "task" -n name --system-prompt "..."` or `--system-file <path>` |
| Run via IHUB API | `camc run -t claude --api glm-5.1 -n name "task"` |
| API health + enable flags | `camc api check` |
| List API profiles | `camc api list` |
| Show per-tool defaults | `camc api default show` · `… show --json` |
| Opt in default API | `camc api default set glm-5.1 --tool claude` |
| Skip default for one run | `camc run --no-default-api …` |

**Other skill areas:**

| Area | Skill |
|------|-------|
| Inter-agent messaging (delegation) | camc-messaging |
| Cron jobs & prompt loops | camc-cron-loop |
| Diagnose stuck/failed agents | camc-diagnose |
| Archive, DAG, prune, history, adopt | camc-misc |

## Inference Hub / API mode

Use **`--api NAME`** or an **opt-in per-tool default** to run Claude or Codex against NVIDIA Inference Hub (IHUB) with token auth in `~/.cam/token.env` — not Claude/Codex subscription login.

| Tool | `--api` | No API / no default |
|------|---------|---------------------|
| **claude** | curated IHUB models | OAuth `/login` via `~/.claude/` |
| **codex** | same models | OAuth via `~/.codex/` |
| **cursor** | not supported | normal login |

**Defaults are opt-in:** `camc api default set NAME --tool TOOL` writes `defaults.<tool>` in `~/.cam/api-models.json`. Fresh install and legacy top-level `"default"` do **not** auto-enable API. Empty/missing default → login. **`--api NAME`** wins over default. **`--no-default-api`** skips default for one run.

Before relying on a default, run **`camc api check`** so the model is `enabled: true`. Disabled default → `camc run` fails with guidance (fail closed).

```bash
$EDITOR ~/.cam/token.env              # INFERENCE_HUB_TOKEN=… (once)
camc api check
camc api default set glm-5.1 --tool claude   # optional
camc run -t claude -n ihub-task "fix bug"    # uses default when enabled
```

## 1. Starting an agent

Always pass `--name`. The name is how you and other tools find this
agent later. Use the user's name if given, otherwise generate a short
clear one (`fix-ecc`, `bug5893270`, `regr-fn100`).

```bash
camc run "fix the ECC error" -n fix-ecc                # claude (default)
camc run "add tests" -t codex -n add-tests
camc run -n debug-mem                                   # interactive (no prompt)
camc run "build" -a -n nightly                          # auto-exit on completion
camc run "x" -n x --tag NR10 --tag WORK
camc run "..." -n redo --resume <session-id>            # resume Claude session
camc run "x" -p /path/to/proj -n proj-dev               # explicit path (default: CWD)
camc run "x" -n y --system-prompt "You are a reviewer." # inject system prompt inline
camc run "x" -n y --system-file path/to/SKILL.md        # inject system prompt from file
```

`--system-prompt` / `--system-file` writes the content into a
marker-delimited block inside the tool's auto-loaded config file in the
workdir (`CLAUDE.md` for claude/codex, `AGENTS.md` for codex/cursor).
The tool picks it up on startup via its own auto-load mechanism.

**Naming gotcha:** don't use `camflow-*` for personal dev agents.
camflow's pre-run cleanup matches `name.startswith("camflow-")` and
will kill them. Use `cf-dev`, `flowdev`, etc.

## 2. Send request / get response

Once an agent is running, you talk to it through three commands.

### Send a prompt

```bash
camc send <agent> --text "please refactor src/foo.py to ..."
```

`--text` payload is sent literally to the agent's tmux pane, then a
trailing Enter submits it. Use `--no-enter` to type without submitting.

### Read what the agent wrote

```bash
camc capture <agent>                    # last 100 lines (default)
camc capture <agent> --lines 500        # more
camc capture <agent> --lines 0          # full scrollback (60s timeout)
camc --json capture <agent>             # JSON with content hash
```

Capture reads the tmux pane buffer — exactly what a user attached to
the session would see.

### Special keys

```bash
camc key <agent> --key Escape           # cancel a tool call / interrupt
camc key <agent> --key Enter
camc key <agent> --key C-c              # SIGINT to foreground process
camc key <agent> --key C-d              # EOF
```

### Typical interaction loop

```bash
camc send <agent> --text "summarize the diff"
sleep 5                                  # let the model think
camc capture <agent> --lines 80          # read the answer
# ...iterate
```

When the agent is mid-tool-call and you want to stop it:

```bash
camc key <agent> --key Escape
```

### Attach for interactive work

```bash
camc attach <agent>     # join the tmux session; Ctrl+B D detaches
```

### Inter-agent messaging (delegation)

For asking another agent to do something, use the **camc-messaging** skill:

```bash
camc msg send <to> -t "..."             # block until reply
camc msg send <to> -t "..." --no-wait   # return message id immediately
camc msg reply <msg_id> -t "..."        # reply on same thread
camc msg read [--next] [--mark]         # read inbox
```

Full protocol, wire format, and patterns: see the camc-messaging skill.

## 3. Manage / lifecycle

### Inspect

```bash
camc list                          # all agents on this host
camc status <agent>                # detailed JSON-like state
camc logs <agent> -f               # follow monitor log
```

### Reboot / move

```bash
camc reboot <agent>                # restart locally; resumes Claude session
camc reboot <agent> --to host:port # reboot on a different machine
```

### Update

```bash
camc update <agent> --name new-name
camc update <agent> --tag SMOKE
```

### Stop / kill / remove

```bash
camc stop <agent>           # graceful: sends /exit; tmux stays alive (resumable)
camc kill <agent>           # force: tears down tmux session
camc rm <agent>             # remove record + always kills tmux + unlinks socket
camc rm <agent> --archive   # also tar.gz the history under ~/.cam/archives/
```

### Bulk cleanup

```bash
camc prune                  # drift-fix ONLY (status wrong but tmux alive)
camc prune --orphans        # actually delete dead-tmux records + stale files
camc prune --orphans --dry-run
```

### Adopt an existing tmux session

```bash
camc add my-existing-session --tool claude --name my-agent
```

## Files

```
~/.cam/
├── agents.json                          # agent registry
├── machines.json
├── contexts.json
├── messages.jsonl                       # inter-agent mailbox ledger
├── events.jsonl                         # append-only event log (auto-rotates 30d)
├── logs/monitor-<id>.log                # monitor stdout
├── pids/<id>.pid
├── archives/                            # tar.gz from `camc archive`
├── cron/jobs.d/<job_id>.json            # host cron job files
└── loops/<agent_id>/
    ├── agent.loop.json                  # per-agent prompt loop registry
    └── runs.jsonl                       # loop dispatch event log
/tmp/cam-sockets/                        # tmux sockets (per-machine)
```

## Troubleshooting

| Symptom | First thing to try |
|---|---|
| Status says `running` but tmux is dead | `camc heal` |
| Monitor died after camc upgrade | `camc upgrade` |
| Agent not responding to `send` | `camc capture <agent> --lines 30` to see; then `camc attach` for manual |
| Want to find an agent's Claude JSONL | `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` — see `reference/sessions.md` |

For deeper diagnosis (stuck agents, exit reasons, heal details):
use the **camc-diagnose** skill.

## When NOT to use camc

Use `cam` (sibling skill managing-cam) for cross-machine ops — listing all
agents on the fleet, syncing, releasing, or running on a remote. camc only
sees its own host.

## Reference (deep-dives — load on demand)

| File | When to read |
|---|---|
| `reference/sessions.md` | Find / resume a Claude session JSONL |
| `reference/machines-and-contexts.md` | Add SSH host, debug `env_setup` |

### Other skills

| Skill | Covers |
|-------|--------|
| camc-messaging | Delegation protocol, wire format, mailbox/read API |
| camc-cron-loop | Host cron jobs, per-agent prompt loops |
| camc-diagnose | Heal deep-dive, monitor details, stuck/failed fix workflows |
| camc-misc | Archive, DAG/apply, prune, history, adopt |
| managing-cam | Cross-machine fleet ops via cam server |