---
name: managing-cam
description: >
  Manage AI coding agents (Claude Code, Codex, Cursor) with CAM
  (Coding Agent Manager). Use when the user asks to list agents,
  start/stop agents, check agent status, capture output, send input,
  manage contexts and nodes, or run batch tasks. For inter-agent
  messaging use the camc-messaging skill; for single-machine agent lifecycle
  use managing-camc.
compatibility: Requires cam CLI.
metadata:
  author: hren
  tags: cam, camc, agent, management, claude, coding, codex, cursor,
    fleet, cross-machine, context, node, sync, release, batch, dag
  category: infra
  requires-tools: cam, camc
disable-model-invocation: false
argument-hint: "[list | status <agent> | run <prompt> | capture <agent> | send <agent> -t '...' | attach <agent> | context {list,add,rm} | node list]"
allowed-tools: Bash Read Glob Grep
---

# CAM — Coding Agent Manager

CAM manages AI coding agents (Claude Code, Codex, Cursor) across local and
remote machines. It's like PM2 for AI agents — start, stop, monitor, interact,
orchestrate, and deploy. Every agent op is delegated to `camc` on the right
machine over SSH; per-machine `camc agents.json` is the source of truth.

> **When in doubt, run `--help` first — never guess.** Flags drift between
> releases. Run `cam --help` for subcommands and `cam <subcommand> --help`
> for flags before using them.

## Quick Reference

| Task | Command |
|---|---|
| List all agents | `cam list` |
| Agent details | `cam status <id-or-name>` |
| Start agent | `cam run "task" --ctx <context>` |
| Stop agent | `cam stop <id-or-name>` |
| Kill agent | `cam kill <id-or-name>` |
| Retry failed | `cam retry <id-or-name>` |
| Capture screen | `cam capture <id-or-name> -n 200` |
| Send text | `cam send <id-or-name> -t "message"` |
| Send key | `cam key <id-or-name> -k Escape` |
| Ask another agent | `camc msg send <to> -t "..."` — see **camc-messaging** skill |
| Attach tmux | `cam attach <id-or-name>` |
| View logs | `cam logs <id-or-name> -n 50` |
| List contexts | `cam context list` |
| List nodes | `cam node list` |

**Remote camc API mode:** agents run on each machine via `camc`. On the host, use `camc run -t claude|codex --api NAME`, opt-in `camc api default set`, and `camc api check` — same as `managing-camc`. `cam run` does not expose `--api`; SSH to the machine or use `cam sync` + local `camc` there. Cursor `--api` is not supported.

Agent IDs can be short prefixes (e.g. `dfac` matches `dfac113f`), full IDs, or names.

## Agent Lifecycle

### Start a new agent

```bash
cam run "investigate bug 5893270" --ctx l1tcm --name bug-5893270
cam run "build fn100 variant" --ctx fn211-man --name fn100-build --timeout 2h
cam run "research peregrine ECC issues" --ctx pdxbbs --name ecc-research
```

Options:
- `--ctx <name>` — work context (required for remote, optional for local)
- `--name <name>` — human-readable name
- `--tool <tool>` — agent tool, default: `claude`
- `--timeout <duration>` — e.g. `30m`, `2h`, `1d`
- `--retry <n>` — auto-retry on failure
- `--follow` — follow output instead of detaching
- `--no-auto-confirm` — require manual confirmation for tool use
- `--dry-run` — show plan without executing

### Monitor agents

```bash
cam list                          # all agents, all nodes
cam list --json                   # JSON output for scripting
cam status <agent>                # detailed status
cam logs <agent> -n 100           # last 100 lines of log
cam logs <agent> -f               # follow log output
```

### Stop / kill / remove

```bash
cam stop <agent>                  # graceful stop (sends /exit)
cam kill <agent>                  # force kill
cam rm <agent>                    # remove record (delegates to camc rm)
cam rm <agent> --kill -f          # also kill the session, no confirmation
```

### Retry failed agents

```bash
cam retry <agent>                 # re-run with same config
cam retry <agent> --follow        # re-run and follow output
```

## Agent Interaction

### Capture screen output

```bash
cam capture <agent>               # default 100 lines
cam capture <agent> -n 500        # 500 lines
cam capture <agent> -n 2000       # full scrollback
cam capture <agent> --json        # with content hash
```

### Send text input

```bash
cam send <agent> -t "hello"                    # send + Enter
cam send <agent> -t "partial text" --no-enter   # send without Enter
```

### Send special keys

```bash
cam key <agent> -k Escape         # Escape key
cam key <agent> -k Enter          # Enter key
cam key <agent> -k C-c            # Ctrl+C
cam key <agent> -k C-d            # Ctrl+D (EOF)
```

### Interactive attach

```bash
cam attach <agent>                # attach to tmux session
# Detach with: Ctrl+B, D
```

### Ask another agent (delegation)

For inter-agent messaging, use the **camc-messaging** skill. From inside an
agent's tmux session:

```bash
camc msg send <to> -t "..."             # block until reply (default 600s)
camc msg send <to> -t "..." --no-wait   # return msg_id immediately
camc msg reply <msg_id> -t "..."        # reply on same thread
camc msg read [--next] [--mark]         # read inbox
camc msg read <msg_id>                  # replay full thread
```

Full protocol (wire format, mailbox/ledger, `--expect-reply`,
receiving messages): see the **camc-messaging** skill.
Protocol record shapes: see `camc-messaging/reference/messaging.md`.

## Agent Management

### Update agent properties

```bash
cam update <agent> --name "better-name"
cam update <agent> --tag NR10                # add tag(s) (comma-sep)
cam update <agent> --untag SMOKE             # remove tag(s)
cam update <agent> --auto-confirm            # toggle auto-confirm
```

### Clean up

```bash
cam prune --dry-run                      # preview
cam prune --all                          # remove ALL terminal agents
cam prune --status killed,failed         # filter by status
cam prune --before 7d                    # older than 7 days
cam prune --orphans                      # cascade orphan cleanup to every machine
```

`--orphans` is what removes dead-tmux records on remote camcs. The poller
also auto-deletes a cam DB row that's been missing from camc for 3 polls
in a row, so transient ghosts heal themselves.

## Contexts

Contexts define where agents run — local directories or remote machines via SSH.

### List and inspect

```bash
cam context list                  # all contexts
cam context show <name>           # context details
cam context test <name>           # test SSH connectivity
```

### Create contexts

```bash
# Local
cam context add myproject /path/to/project

# Remote via SSH
cam context add pdx-work /home/scratch/project \
  --host pdx-container-xterm-098.prd.it.nvidia.com \
  --user hren --port 3422

# With environment setup
cam context add pdx-work /path \
  --host server --user dev \
  --env-setup "source /home/hren/.bashrc"
```

### Manage contexts

```bash
cam context update <name> --env-setup "source ~/.bashrc"
cam context copy <name> <new-name>
cam context remove <name>
```

## Nodes

Nodes are machines running agents. A node can have multiple contexts and agents.

```bash
cam node list                     # all nodes with agent counts
cam node status <node>            # agents on specific node
```

## Batch Tasks (DAG Workflows)

Run multiple tasks with dependencies from a YAML file:

```bash
cam apply -f tasks.yaml
cam apply -f tasks.yaml --ctx my-project
cam apply -f tasks.yaml --dry-run         # preview only
```

Example `tasks.yaml`:
```yaml
tasks:
  - name: research
    prompt: "Research the ECC error pattern in bug 5893270"
    ctx: l1tcm

  - name: fix
    prompt: "Fix the ECC error based on the research findings"
    ctx: l1tcm
    depends_on: [research]

  - name: test
    prompt: "Run regression tests to verify the fix"
    ctx: l1tcm
    depends_on: [fix]
```

## History and stats

```bash
cam history                               # last 30 days
cam history --ctx l1tcm --last 7d         # specific context, last week
cam history --status failed               # only failures
cam stats                                 # aggregated stats
cam stats --ctx l1tcm --last 7d           # context-specific stats
```

## System

```bash
cam version                       # version and installed adapters
cam doctor                        # check dependencies
cam heal                          # restart dead monitor daemons
cam sync <context>                # sync camc/configs to remote
cam sync                          # sync to all remote contexts
cam release                       # build → test → deploy camc to every machine
cam release --only pdx-098        # canary one host
```

`cam release` archives each successful build to
`/home/prgn_share/tools/camc/releases/camc-vX.Y.Z-<hash>` and creates
a `deploy-YYYYMMDDHHMMSS` git tag for rollback.

## Common Workflows

### Delegate a task to a remote agent

```bash
cam run "analyze regression failures for fn100" \
  --ctx fn211-man --name fn100-regr --timeout 1h
# Later, check progress:
cam capture fn100-regr -n 50
```

### Monitor all agents across machines

```bash
cam node list                     # overview: which machines, how many agents
cam list                          # detailed agent list
cam capture <agent> -n 20         # quick peek at specific agent
```

### Interact with a running agent

```bash
cam capture <agent> -n 50         # see current state
cam send <agent> -t "try a different approach"
cam capture <agent> -n 50         # check response
```

### Clean up after a sprint

```bash
cam prune --all --before 3d --dry-run    # preview
cam prune --all --before 3d -f           # execute
```
