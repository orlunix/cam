# MiniSpec Project State Maintenance

Use this when the user asks whether `AGENTS.md`, `CLAUDE.md`, or another project prompt/state file should be updated, backed up, or versioned during a coding/debug agent workflow.

## Core Model

- The auto-load file (`CLAUDE.md` for claude, `AGENTS.md` for codex/cursor) is the current source of truth.
- The history file (`CLAUDE.history.md` for claude, `AGENTS.history.md` for codex/cursor) records important decisions and structural changes.
- Git history records the complete edit history.

Keep the auto-load file short, current, and actionable. Do not turn it into a changelog.

## Update Only

Update only the auto-load file when the change reflects normal progress:

- Mark checklist items `done`, `partial`, `blocked`, or `not_started`.
- Add a newly discovered small task that is required to achieve the existing goal.
- Remove or rewrite obsolete checklist items after they are no longer useful.
- Refine verification commands or acceptance checks without changing the goal.
- Add brief current notes that help the next run continue.

Default rule: if the edit does not change project intent, risk boundaries, or execution strategy, update only the current MiniSpec.

## Add History Entry

Append to the history file when the change is significant enough that a future agent or reviewer needs to know why it happened:

- Goal changed or was substantially refined.
- Invariants changed, especially API, security, dependency, data, or compatibility boundaries.
- Verification strategy changed materially.
- A checklist item was added because new scope was discovered.
- A task was marked blocked and requires human/product/architecture decision.
- Work was rolled back, abandoned, or redirected.
- The agent repeated the same failure and changed approach.

History entries should be short:

```markdown
## YYYY-MM-DD
- Goal refined: added API compatibility invariant because integration tests showed public API behavior was affected.
- Checklist updated: split "add tests" into unit tests and integration tests.
```

## Create Backup

Create a backup copy before editing the project prompt/state file only when the edit is risky or broad:

- Rewriting most of the auto-load file.
- Changing the goal.
- Removing multiple checklist items.
- Changing invariants or safety boundaries.
- Migrating from another prompt format into MiniSpec.
- The repository is not under git or git status cannot be checked.

Preferred backup name:

```text
CLAUDE.md.bak-YYYYMMDD-HHMMSS | AGENTS.md.bak-YYYYMMDD-HHMMSS
```

If git is available and the edit is small, do not create backup files; rely on git diff/history.

## Never Do

- Do not append long run logs to the auto-load file.
- Do not preserve stale checklist items just for history.
- Do not change the goal merely because one checklist item is difficult.
- Do not delete blocked items unless the reason is captured in history or the item is truly obsolete.
- Do not claim the MiniSpec is updated unless the file was actually edited or the report clearly says no update was needed.

## Required Report Additions

When project prompt/state was considered, include:

```yaml
minispec_update:
  updated: true|false
  file: "CLAUDE.md"|"AGENTS.md"
  backup_created: true|false
  history_updated: true|false
  reason: ""
```
