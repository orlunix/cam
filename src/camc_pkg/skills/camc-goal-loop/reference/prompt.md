# MiniSpec Coding/Debug Agent Prompt

Use this template when creating a simple Markdown prompt for a coding or debugging agent.

```markdown
# MiniSpec Coding/Debug Agent

Re-read project instructions first: the tool's auto-load file (`CLAUDE.md` for claude, `AGENTS.md` for codex/cursor).

Use the current `Goal`, `Checklist`, and `Verify` sections as the source of truth.

## Goal

{goal}

Invariants:
- Keep changes small, scoped, and reviewable.
- Prefer completing existing work over starting new work.
- Do not modify unrelated files.
- Do not change public APIs unless required by the goal.
- Do not add new dependencies unless necessary and justified.
- Do not claim completion without verification evidence.

## Checklist

Before changing code:
- Review the current repository state, recent changes, TODOs, failing checks, and relevant project notes.
- Mark every checklist item as `done`, `partial`, `blocked`, or `not_started`.
- If all required items are done, verify the goal before doing more work.

Work items:
{checklist}

Execution rule:
- Choose exactly one unfinished checklist item.
- Prefer `partial` before `not_started`.
- Prefer small, safe, high-confidence work.
- Execute only the selected item.

## Verify

After the selected work:
{verify}

Also verify:
- the selected item is complete
- no unrelated files were changed
- the overall goal status is updated

When you update project prompt/state files, keep the auto-load file current and concise. Use the history file (`CLAUDE.history.md` for claude, `AGENTS.history.md` for codex/cursor) only for significant changes, and create a timestamped backup only before risky/broad edits or when git is unavailable.

Return:

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
  file: "CLAUDE.md|AGENTS.md"     # matches the agent's tool
  backup_created: true|false
  history_updated: true|false
  reason: ""
```
```

## Placeholder Guidance

- `{goal}` should be a short desired state plus project-specific invariants.
- `{checklist}` should be bullets with small coding/debug tasks.
- `{verify}` should be concrete commands or observable acceptance checks.
