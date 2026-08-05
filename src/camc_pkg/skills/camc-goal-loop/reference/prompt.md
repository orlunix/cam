# MiniSpec Task File

Render this file as `CLAUDE.md` for Claude or `AGENTS.md` for Codex and
Cursor. It is the durable state read at startup and on every continuation.

```markdown
# Goal: {short goal}

## Goal

{goal}

Invariants:
{invariants}

## Checklist

{checklist}

Statuses are `not_started`, `partial`, `blocked`, or `done`.

Rules:
- Re-read this file before choosing work.
- Choose exactly one unfinished item; finish `partial` work before new work.
- Update an item only for real work or real evidence.

## Verify

{verify}

Use the most relevant deterministic project script as evidence: an existing
test, lint, build, reproduction, or project check. If a reusable check is
missing, add a small deterministic project script as a Checklist item; it
must print useful diagnostics and fail while its checked condition is unmet.

## Report

After one item, report: selected item, evidence, Checklist changes, and next
step. Do not claim Goal success until every required Verify check passes.
```

Keep the rendered task file concise. It is the source of truth; do not create
a separate history, backup, or loop JSON protocol for normal progress.
