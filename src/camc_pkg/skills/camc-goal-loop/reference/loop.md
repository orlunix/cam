# Goal-Loop Continuation Message

Render this as a plain text file for `~/.cam/camc cron add --loop --prompt-file`.
Replace `{TASK_FILE}`, `{AGENT_ID}`, and `{LOOP_NAME}` before registration.

```text
Continue the current task from {TASK_FILE}.

1. Re-read {TASK_FILE}; Goal, Checklist, and Verify are the source of truth.
2. If Goal is achieved with all required Verify evidence, run:
   ~/.cam/camc cron rm --loop --owner {AGENT_ID} {LOOP_NAME}
   Then report completion and make no further task changes.
3. Otherwise choose exactly one unfinished Checklist item. Prefer `partial`
   before `not_started`.
4. Run the most relevant deterministic project script for that item. If a
   reusable check is missing, create the small deterministic project script as
   this item's work; it must print useful diagnostics and fail while its
   checked condition is unmet.
5. Make only the selected bounded change, run its Verify checks, and update
   {TASK_FILE} only with real status or evidence.
6. Report selected item, evidence, Checklist changes, blockers, and next step.

If blocked or the goal is ambiguous, record the blocker in {TASK_FILE}; leave
the loop active for a human to adjust the task.
```

For Codex or Cursor use `AGENTS.md`; for Claude use `CLAUDE.md`.
