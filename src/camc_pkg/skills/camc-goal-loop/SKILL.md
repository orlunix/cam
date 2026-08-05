---
name: camc-goal-loop
description: Use when an autonomous coding or debugging agent needs a durable Goal, Checklist, and Verify state with periodic continuation prompts.
compatibility: Requires ~/.cam/camc deployed by the camc release.
metadata:
  author: hren
  tags: camc, goal, checklist, verify, loop, cron, autonomous
  category: infra
  requires-tools: camc
disable-model-invocation: false
argument-hint: "[create a goal prompt | register a prompt loop]"
allowed-tools: Bash Read Glob Grep Write
---

# camc-goal-loop

Use a small, durable task file plus a periodic prompt. CAMC delivers the
prompt only when the owner is idle; the agent keeps the task state current.
This is a continuation mechanism, not an external verifier or a task DSL.

## Create exactly two task artifacts

1. Render `reference/prompt.md` into the project auto-load file:
   `CLAUDE.md` for Claude, `AGENTS.md` for Codex or Cursor. Fill its Goal,
   Checklist, and Verify sections with concrete project facts.
2. Render `reference/loop.md` into a short continuation message. Replace the
   owner ID, loop name, and auto-load filename with their actual values.

Do not hand-create `~/.cam/loops/*.json`; CAMC owns that registry.

## Register the continuation

Resolve the stable owner ID first. Prefer the eight-character CAMC ID over a
display name, then register the rendered continuation file:

```bash
~/.cam/camc status <agent>
~/.cam/camc cron add --loop --owner <agent-id> --name <loop-name> \
  --every 5m --prompt-file /absolute/path/to/continuation.md
~/.cam/camc cron list --loop --owner <agent-id>
```

The owner must be running and idle before CAMC delivers a due prompt. Busy
owners are deferred; messages do not interrupt work.

## Per-turn rule

On every continuation, re-read the task file, run the most relevant
deterministic project scripts for the selected Checklist item, then perform
only one bounded item. Prefer existing test, lint, build, reproduction, or
project check scripts. If a reusable check is missing, add a small
deterministic project script as a Checklist item; it must report useful
evidence and fail when its checked condition is unmet.

Update Checklist status only for real work or real evidence. Script output is
evidence for the agent, not an automatic CAMC completion signal. Never claim
Goal success without the concrete checks in Verify.

## Stop or adjust

- Goal achieved with its Verify evidence: remove the loop:

  ```bash
  ~/.cam/camc cron rm --loop --owner <agent-id> <loop-name>
  ```

- Blocked or ambiguous: leave the loop registered, record the blocker in the
  task file, and ask a human to revise Goal, Checklist, or Verify.
- Human stop: use the same `cron rm --loop` command.

For scheduler, interval, and troubleshooting details, use `camc-cron-loop`.
The two templates are the source of truth for task state and continuation
wording; keep this guide short rather than copying them here.
