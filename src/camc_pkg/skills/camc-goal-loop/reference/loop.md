# MiniSpec Continuation Prompt for camc cron --loop

Delivered by `camc cron add --loop --prompt "..."`. Each tick sends this
message to the agent's mailbox. The agent reads it via `camc msg read --next`
on its next idle turn, does one checklist item, verifies, and reports.

## The Loop

```
camc cron tick → idle gate check (agent must be running + idle)
  → camc msg send <agent> -t "<continuation prompt>" --no-wait
  → agent reads via camc msg read --next
  → agent re-reads auto-load file (CLAUDE.md / AGENTS.md)
  → agent picks one unfinished item → executes → verifies
  → agent updates auto-load file (mark progress)
  → agent outputs YAML report
  → if continue_recommended = true: next tick repeats
  → if continue_recommended = false (goal achieved): archive loop+goal, rm cron, stop
  → if continue_recommended = false (blocked/impossible): stop doing work, loop stays
```

Tick only fires when the owner agent is `status=running` AND `state=idle`.
Busy → silently deferred. One message at a time — no pileup.

## Registration

```bash
# Default 5m interval
camc cron add --loop --owner <agent> \
  --name goal-loop --every 5m \
  --prompt "CONTINUATION_PROMPT_HERE"
```

Use `--every` to tune: `5m` (default, small steps), `15m`, `30m`, `1h`,
`--daily 09:00`.

## Continuation prompt template

Use this as the `--prompt` value. Replace `CLAUDE.md` with `AGENTS.md`
for codex/cursor agents.

```
Continue from the current project state using the MiniSpec protocol.

First re-read the auto-load file: CLAUDE.md (for claude) or AGENTS.md (for codex/cursor).

Then:
1. Check whether the Goal is already achieved based on the evidence required.
2. Review every Checklist item and mark each as done, partial, blocked, or not_started.
3. If the Goal is achieved and all required Checklist items are done: archive the loop and goal together — copy ~/.cam/loops/<name>.json to ~/.cam/loops/archive/<name>.json, copy the auto-load file (CLAUDE.md or AGENTS.md) to ~/.cam/loops/archive/<name>-goal.md, then run `camc cron rm --loop --owner <agent> <loop-name>` to stop the loop, do not delete the loop JSON file — it stays at `~/.cam/loops/<name>.json` for reclaim, set continue_recommended to false, make no code changes, and report completion.
4. If work remains, choose exactly ONE unfinished Checklist item.
5. Prefer partial before not_started, small before broad, safe before risky, and finishing existing work before starting new work.
6. Execute only the selected item — do not do extra work.
7. Run the most relevant available checks from the Verify section.
8. If the selected work changes MiniSpec state, update CLAUDE.md (for claude) or AGENTS.md (for codex/cursor). Keep it current and concise.
9. Add a short CLAUDE.history.md (for claude) or AGENTS.history.md (for codex/cursor) entry only for significant goal, invariant, verification, blocked, rollback, or scope decisions.
10. Create a timestamped backup (CLAUDE.md.bak-YYYYMMDD-HHMMSS or AGENTS.md.bak-YYYYMMDD-HHMMSS) only before risky or broad prompt edits, or when git is unavailable.
11. Do not claim success without verification evidence.

Return the required MiniSpec YAML report:
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
```

## Tool-aware variants

Replace `CLAUDE.md` with `AGENTS.md` (and `CLAUDE.history.md` with
`AGENTS.history.md`) throughout when the agent uses codex or cursor.

## Ralph-style principle

External verification beats agent judgment. `goal_status.achieved: true`
requires the verification evidence from the Verify section — subjective
"looks correct" is never sufficient.

## When to stop (graceful self-cleanup)

When the goal is achieved and all required items are verified done, the
agent performs a self-cleaning graceful stop:

1. **Archive the loop**: `mkdir -p ~/.cam/loops/archive && cp ~/.cam/loops/<name>.json ~/.cam/loops/archive/<name>.json`
2. **Archive the goal**: copy the auto-load file (CLAUDE.md or AGENTS.md) to `~/.cam/loops/archive/<name>-goal.md`
3. **Remove the cron job**: `camc cron rm --loop --owner <agent> <loop-name>` (does NOT delete the JSON file)
4. **Keep the loop JSON**: `~/.cam/loops/<name>.json` stays for reclaim/reproducibility
5. **Report completion**: set `continue_recommended: false`

The agent also sets `continue_recommended: false` (without archiving) when:
- All remaining items are `blocked` (needs human decision)
- The goal is impossible with current constraints

In blocked/impossible cases the loop stays registered so a human can
adjust the MiniSpec and resume. The archived files in `~/.cam/loops/archive/`
(`<name>.json` and `<name>-goal.md`) serve as a permanent completion record.

## When to add the loop

- **After the agent is running and has read the auto-load file** — the
  agent needs the MiniSpec loaded before the loop fires
- **If the agent is mid-work**, the idle gate defers the first tick
  until `state=idle`
- The prompt tells the agent to re-read the auto-load file each tick,
  so state changes from manual `camc send` interactions are picked up