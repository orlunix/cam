---
name: camc-goal-loop
description: >
  Create goal-driven coding/debugging prompts and set up camc cron
  --loop periodic continuations. Use when the user wants to set up an
  autonomous agent that works through a checklist one item at a time,
  with idle-gated delivery via camc mailbox. Covers MiniSpec prompt
  creation (goal/checklist/verify), camc loop registration, and
  structured progress reporting for resumable agent workflows.
compatibility: Requires camc binary in PATH.
metadata:
  author: hren
  tags: camc, goal, loop, minispec, checklist, verify, autonomous,
    cron-loop, agent-loop, periodic, idle, prompt-loop, continuation
  category: infra
  requires-tools: camc
disable-model-invocation: false
argument-hint: "[create a goal-loop prompt | register a camc cron --loop]"
allowed-tools: Bash Read Glob Grep Write
---

# camc-goal-loop — Goal-Driven Agent Loops

Set up autonomous coding agents that work through a checklist one small
item at a time, verify before claiming completion, and resume safely on
each idle tick via `camc cron --loop`.

This is a **per-agent prompt loop** — it uses the standard `camc cron add
--loop` mechanism from camc-cron-loop. Delivery is via `camc msg send`
to the agent's mailbox, idle-gated (never interrupts busy agents).
Loop dispatch is handled by camc's cron tick infrastructure (managed
by `camc heal`); the agent does not set up or manage crontab.

**Two files produced:**
1. **Auto-load file** in project root (`CLAUDE.md` for claude, `AGENTS.md` for codex/cursor) — MiniSpec goal/checklist/verify prompt
2. **`~/.cam/loops/<name>.json`** — loop continuation message (for reproducibility)

Then one `camc cron add --loop` command registers it with default **5m** interval.

See `camc-cron-loop` skill for the full per-agent prompt loop mechanism
(idle gate, delivery, busy deferral, lifecycle).

## Quick Reference

| Task | Action |
|---|---|
| Create MiniSpec prompt | Write `CLAUDE.md` (claude) or `AGENTS.md` (codex/cursor) in project root |
| Create loop JSON | Write `~/.cam/loops/<name>.json` with continuation message |
| Register loop | `camc cron add --loop --owner <agent> --name <name> --every 5m --prompt "<msg>"` |
| List loops | `camc cron list --loop --owner <agent>` |
| Graceful stop (auto) | Agent archives loop JSON → `camc cron rm` on goal achieved |
| Manual stop | `camc cron rm --loop --owner <agent> <name>` |
| Reclaim loop | Read `~/.cam/loops/<name>.json`, re-register with same `--prompt` |

## How it works

```
agent starts → reads auto-load file (CLAUDE.md / AGENTS.md) → gets MiniSpec
per-agent prompt loop (camc cron add --loop):
  → camc msg send <agent> -t "<continuation message>" --no-wait
  → idle gate: only fires when agent status=running AND state=idle
  → agent reads via camc msg read --next on next idle turn
  → agent re-reads auto-load file to pick up current state
  → agent does ONE checklist item → verifies → reports YAML
  → agent updates auto-load file (mark item done/partial/blocked)
  → next fire: agent picks up next item
  → goal achieved: archive loop+goal → camc cron rm → stop
```

The idle gate means the agent is **never interrupted mid-task** — if busy,
the tick is silently deferred. One message at a time; no pileup.

The **auto-load file** is read by the agent on startup and every tick —
it's **not** sent by the loop. The loop sends only a short continuation
message telling the agent to pick up where it left off.

## Tool → auto-load file

| Tool | Auto-load file | History file | Backup pattern |
|------|---------------|--------------|----------------|
| **claude** | `CLAUDE.md` | `CLAUDE.history.md` | `CLAUDE.md.bak-YYYYMMDD-HHMMSS` |
| **codex** | `AGENTS.md` | `AGENTS.history.md` | `AGENTS.md.bak-YYYYMMDD-HHMMSS` |
| **cursor** | `AGENTS.md` | `AGENTS.history.md` | `AGENTS.md.bak-YYYYMMDD-HHMMSS` |

Check via `camc status <agent>` → `task.tool`.

## 1. File 1 — Auto-load prompt (`CLAUDE.md` / `AGENTS.md`)

Write a MiniSpec prompt into the project root. Template: `reference/prompt.md`.

Three sections: **Goal**, **Checklist**, **Verify**.

**Goal** — desired outcome and stable invariants. Define the end state,
include safety boundaries (minimal changes, API compatibility). Avoid
step-by-step execution details.

**Checklist** — small executable items, each bounded and independently
verifiable. Prefer finishing partial work before starting new work.
Statuses: `done`, `partial`, `blocked`, `not_started`.

**Verify** — objective evidence. Prefer external checks: tests pass,
lint/typecheck, bug repro no longer fails. No subjective "looks correct".

### Coding defaults

Unless the user says otherwise:
- Keep changes small and reviewable
- Do not modify unrelated files
- Do not change public APIs unless required
- Do not add dependencies unless necessary and justified
- Do not claim completion without verification evidence

## 2. File 2 — Loop JSON (`~/.cam/loops/<name>.json`)

Write the continuation message as JSON so it's inspectable and
reproducible:

```json
{
  "name": "goal-loop",
  "every": "5m",
  "owner": "<agent-id-or-name>",
  "prompt": "Continue from the current project state using the MiniSpec protocol.\n\nFirst re-read the auto-load file: CLAUDE.md.\n\nThen:\n1. Check whether the Goal is already achieved.\n2. Review every Checklist item and mark it as done, partial, blocked, or not_started.\n3. If the Goal is achieved and all required Checklist items are done: archive the loop and goal together — copy ~/.cam/loops/<name>.json to ~/.cam/loops/archive/<name>.json, copy the auto-load file (CLAUDE.md or AGENTS.md) to ~/.cam/loops/archive/<name>-goal.md, then run `camc cron rm --loop --owner <agent> <loop-name>` to stop the loop, do not delete the loop JSON file — it stays at `~/.cam/loops/<name>.json` for reclaim, set continue_recommended to false, make no code changes, and report completion.\n4. If work remains, choose exactly ONE unfinished Checklist item.\n5. Prefer partial before not_started, small before broad, safe before risky.\n6. Execute only the selected item.\n7. Run the most relevant available checks.\n8. If the selected work changes MiniSpec state, update CLAUDE.md (or AGENTS.md).\n9. Add a short CLAUDE.history.md entry only for significant changes.\n10. Create a timestamped backup before risky or broad prompt edits.\n11. Do not claim success without verification evidence.\n\nReturn the required MiniSpec YAML report."
}
```

Full template: `reference/loop.md`. Pick the variant matching the tool.

## 3. Register the loop

```bash
camc cron add --loop --owner <agent> \
  --name goal-loop --every 5m \
  --prompt "<continuation message from ~/.cam/loops/<name>.json>"
```

**Interval guidance** (default 5m):
- `5m` — **default**, small incremental steps
- `15m` — quick tasks (lint fixes)
- `30m` — longer items
- `1h`–`2h` — large items, debugging
- `--daily 09:00` — daily check-in

## 4. Expected agent behavior on each tick

When the continuation message arrives:

1. **Re-read the auto-load file** — CLAUDE.md or AGENTS.md
2. **Check goal** — achieved? Archive loop JSON, run `camc cron rm`, keep the loop JSON at `~/.cam/loops/<name>.json` for reclaim, set `continue_recommended: false`, stop.
3. **Review checklist** — mark each item done/partial/blocked/not_started
4. **Select one item** — partial before not_started, small over broad
5. **Execute only that item**
6. **Verify** — run tests/lint/reproduction checks
7. **Update the state file** — mark item done/partial/blocked; keep concise
8. **Output YAML report**

### Required output YAML

```yaml
goal_status:
  achieved: true|false
  evidence: ""

checklist_status:
  - item: ""
    status: done|partial|blocked|not_started
    evidence: ""

selected_item: ""

work_done:
  - ""

verification:
  result: passed|failed|not_run
  evidence: ""

remaining_items:
  - ""

next_step: ""

continue_recommended: true|false

minispec_update:
  updated: true|false
  file: "CLAUDE.md|AGENTS.md"
  backup_created: true|false
  history_updated: true|false
  reason: ""
```

Set `continue_recommended: false` when goal achieved (agent self-cleans:
archive loop+goal, `camc cron rm`) or when all items are blocked/impossible
(agent stops doing work, loop stays for human to adjust).

## 5. Project state maintenance

- **Update only** for normal progress: mark items, refine verify commands
- **Append history file** (`CLAUDE.history.md` or `AGENTS.history.md`)
  for significant changes: goal change, new scope, blocked, rollback
- **Backup** (`CLAUDE.md.bak-...` or `AGENTS.md.bak-...`) only before
  risky/broad edits or when git is unavailable

Full guidelines: `reference/state-maintenance.md`

## 6. Common patterns

### New task from scratch (claude)

```bash
# 1. File 1 — Create CLAUDE.md with goal/checklist/verify
cat > /path/to/project/CLAUDE.md << 'EOF'
# MiniSpec: Fix ECC errors in data pipeline

## Goal
ECC errors in the data pipeline are handled gracefully with retry
and fallback instead of crashing the job.

Invariants:
- Keep changes scoped to pipeline error handling
- Do not change data format or output schema
- Do not add new dependencies

## Checklist
- [ ] not_started: Map error paths in pipeline code
- [ ] not_started: Add ECC-specific error classification
- [ ] not_started: Implement retry with exponential backoff
- [ ] not_started: Add fallback path for persistent ECC errors
- [ ] not_started: Add tests for each error path
- [ ] not_started: Run integration test suite

## Verify
- pytest tests/test_ecc_pipeline.py passes
- Integration test with injected ECC errors passes
- No regression in normal-path throughput
EOF

# 2. File 2 — Create ~/.cam/loops/ecc-goal-loop.json
cat > ~/.cam/loops/ecc-goal-loop.json << 'EOFJSON'
{
  "name": "goal-loop",
  "every": "5m",
  "owner": "ecc-fix",
  "prompt": "Continue from the current project state using the MiniSpec protocol.\n\nFirst re-read the auto-load file: CLAUDE.md.\n\nThen:\n1. Check whether the Goal is already achieved.\n2. Review every Checklist item and mark it as done, partial, blocked, or not_started.\n3. If the Goal is achieved and all required Checklist items are done: archive the loop and goal together — copy ~/.cam/loops/<name>.json to ~/.cam/loops/archive/<name>.json, copy the auto-load file (CLAUDE.md or AGENTS.md) to ~/.cam/loops/archive/<name>-goal.md, then run `camc cron rm --loop --owner <agent> <loop-name>` to stop the loop, do not delete the loop JSON file — it stays at `~/.cam/loops/<name>.json` for reclaim, set continue_recommended to false, make no code changes, and report completion.\n4. If work remains, choose exactly ONE unfinished Checklist item.\n5. Prefer partial before not_started, small before broad, safe before risky.\n6. Execute only the selected item.\n7. Run the most relevant available checks.\n8. If the selected work changes MiniSpec state, update CLAUDE.md.\n9. Add a short CLAUDE.history.md entry only for significant changes.\n10. Create a timestamped backup before risky or broad prompt edits.\n11. Do not claim success without verification evidence.\n\nReturn the required MiniSpec YAML report."
}
EOFJSON

# 3. Start a claude agent
camc run -t claude "fix ECC errors per CLAUDE.md" -n ecc-fix -p /path/to/project

# 4. Register the loop (default 5m)
camc cron add --loop --owner ecc-fix \
  --name goal-loop --every 5m \
  --prompt "Continue from the current project state using the MiniSpec protocol.\n\nFirst re-read the auto-load file: CLAUDE.md.\n\nThen:\n1. Check whether the Goal is already achieved.\n2. Review every Checklist item and mark it as done, partial, blocked, or not_started.\n3. If the Goal is achieved and all required Checklist items are done: archive the loop and goal together — copy ~/.cam/loops/<name>.json to ~/.cam/loops/archive/<name>.json, copy the auto-load file (CLAUDE.md or AGENTS.md) to ~/.cam/loops/archive/<name>-goal.md, then run `camc cron rm --loop --owner <agent> <loop-name>` to stop the loop, do not delete the loop JSON file — it stays at `~/.cam/loops/<name>.json` for reclaim, set continue_recommended to false, make no code changes, and report completion.\n4. If work remains, choose exactly ONE unfinished Checklist item.\n5. Prefer partial before not_started, small before broad, safe before risky.\n6. Execute only the selected item.\n7. Run the most relevant available checks.\n8. If the selected work changes MiniSpec state, update CLAUDE.md.\n9. Add a short CLAUDE.history.md entry only for significant changes.\n10. Create a timestamped backup before risky or broad prompt edits.\n11. Do not claim success without verification evidence.\n\nReturn the required MiniSpec YAML report."
```

### New task from scratch (codex / cursor)

Same pattern, `AGENTS.md` and AGENTS.md variant of continuation:

```bash
cat > /path/to/project/AGENTS.md << 'EOF'
# MiniSpec: Fix ECC errors in data pipeline
## Goal ... (same structure)
EOF

cat > ~/.cam/loops/ecc-goal-loop.json << 'EOFJSON'
{
  "name": "goal-loop",
  "every": "5m",
  "owner": "ecc-fix",
  "prompt": "Continue from the current project state...\n\nFirst re-read the auto-load file: AGENTS.md.\n..."
}
EOFJSON

camc run -t codex "fix ECC errors per AGENTS.md" -n ecc-fix -p /path/to/project

camc cron add --loop --owner ecc-fix \
  --name goal-loop --every 5m \
  --prompt "Continue from the current project state...\n\nFirst re-read the auto-load file: AGENTS.md.\n..."
```

### Continue existing work

```bash
# Agent already running, auto-load file has partial items
camc status my-agent           # task.tool: claude → CLAUDE.md, codex → AGENTS.md

# Create ~/.cam/loops/continue.json with continuation message, then:
camc cron add --loop --owner my-agent \
  --name continue --every 5m \
  --prompt "Continue from the current project state..."
```

### Graceful stop (goal achieved)

When the goal is achieved, the agent archives both the loop and the goal,
then self-cleans the cron job:

1. Archive the loop: `mkdir -p ~/.cam/loops/archive && cp ~/.cam/loops/<name>.json ~/.cam/loops/archive/<name>.json`
2. Archive the goal: `cp CLAUDE.md ~/.cam/loops/archive/<name>-goal.md` (or AGENTS.md for codex/cursor)
3. Remove the cron job: `camc cron rm --loop --owner <agent> <loop-name>` (keeps the JSON file)
4. Keep the loop JSON: `~/.cam/loops/<name>.json` stays for reclaim/reproducibility
5. Set `continue_recommended: false` and report completion

### Manual stop (interrupt)

```bash
camc cron rm --loop --owner <agent> <loop-name>
```

Agent also sets `continue_recommended: false` without archiving when all
items are blocked or goal is impossible — loop stays for human to adjust
and resume.

### Reclaim / restore a loop

The `~/.cam/loops/<name>.json` file is the persistent source of truth.
If the cron job was removed or lost, reclaim it from the JSON:

```bash
# Read the saved JSON
cat ~/.cam/loops/goal-loop.json

# Extract the --prompt value and re-register
camc cron add --loop --owner <agent> \
  --name goal-loop --every 5m \
  --prompt "<prompt value from the JSON>"
```

## Reference

| File | When to read |
|---|---|
| `reference/prompt.md` | Full MiniSpec prompt template with YAML output block |
| `reference/loop.md` | Continuation message template + Ralph-style principle |
| `reference/state-maintenance.md` | When to update/history/backup the auto-load file |