---
name: workflow-check
description: Check cam-flow workflow health. Use with /loop for periodic monitoring. Detects stuck workflows, infinite loops, and state corruption. Trigger on "check workflow", "workflow status", or via /loop 5m /workflow-check.
---

# Workflow Health Check

Quick health check for a running cam-flow workflow.

## Check Procedure

### 1. Read State
```bash
cat .claude/state/workflow.json 2>/dev/null
```

- File missing → "No workflow state found"
- status == "done" → "Workflow completed successfully"
- status == "failed" or "aborted" → report reason
- status == "waiting" → "Workflow waiting for input"
- status == "running" → continue checks

### 2. Staleness Check

Check how long since state.json was last modified:
```bash
stat -c %Y .claude/state/workflow.json
```

- Over 15 minutes → WARN: possibly stuck
- 5-15 minutes → OK (some nodes take a while)
- Under 5 minutes → actively running

### 3. Loop Detection

Read last 10 trace entries:
```bash
tail -10 .claude/state/trace.log
```

- Same node appearing 3+ consecutive times with status=fail → STUCK IN LOOP
- Two nodes alternating (A→B→A→B) all failing → PING-PONG FAILURE

### 4. Report

One line summary:

```
OK: node=test, last update 2m ago
WARN: node=build stuck for 18m — may need restart
ERROR: node=fix failed 4 times in a row — needs human intervention
DONE: workflow completed successfully
```

### 5. Auto-fix (only for clear issues)

**Loop detected**: Set state.status to "waiting" to stop the loop.
**State corruption**: Reset to last known good state from trace.log.

For anything else, just report — don't try to fix automatically.
