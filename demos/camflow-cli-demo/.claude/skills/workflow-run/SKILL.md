---
name: workflow-run
description: Execute ONE step of the cam-flow workflow, then return. Designed to be called by /loop for continuous execution. Reads workflow.yaml, executes the current node, updates state, and exits. Trigger when user says "run workflow", "start", "execute", "continue", "next step", or via /loop.
---

# Workflow Runner (single-step)

Execute exactly ONE node of the workflow, update state, then stop.
The /loop scheduler will call you again for the next node.

## Procedure

### 1. Read state

Read `.claude/state/workflow.json`.

- If file missing → initialize: `{"pc": "start", "status": "running"}` and write it.
- If `status` is `done` → reply "Workflow completed." and stop.
- If `status` is `failed` → reply "Workflow failed at node {pc}." and stop.
- If `status` is `waiting` → reply "Workflow waiting." and stop.
- If `status` is `running` → continue to step 2.

### 2. Read current node

Read `workflow.yaml`. Find the node matching `pc`.
Replace `{{state.xxx}}` in the `with` field with values from state.

### 3. Execute the node

**`cmd <command>`** — Run the shell command. Exit 0 = success, non-zero = fail.

**`agent claude`** — The `with` field is your task. Do it: read files, write code, run commands, whatever is needed.

**`skill <name>`** — Invoke the skill using the Skill tool: call `Skill("<name>")`. Pass the `with` field content as the task context before invoking. This ensures the skill is loaded through Claude's native skill system with proper priority and isolation, not just read as a text file.

### 4. Build result

Determine:
- `status`: "success" or "fail"
- `summary`: one sentence describing what happened
- `state_updates`: key-value pairs to merge into state
  - On failure: include `{"error": "description of what went wrong"}`
  - On success: include useful info for downstream nodes

### 5. Resolve transition (first match wins)

1. Node has `transitions` with `if: fail` and result is fail → goto that target
2. Node has `transitions` with `if: success` and result is success → goto that target
3. Node has `transitions` with `if: output.xxx` / `if: state.xxx` → check condition
4. Node has `next` → go to next
5. Nothing matched + success → status = "done"
6. Nothing matched + fail → status = "failed"

### 6. Update files

Merge `state_updates` into state, then write `.claude/state/workflow.json`:
```json
{"pc": "<next node or null>", "status": "<running|done|failed>", ...merged state}
```

Append to `.claude/state/trace.log`:
```json
{"pc": "node_id", "next_pc": "next_id", "status": "success|fail", "summary": "...", "reason": "..."}
```

### 7. Report and exit

Print a one-line status: `[node_id] status → next_id (reason)`
Then STOP. Do not continue to the next node. /loop will call you again.

## Loop detection

Before executing, check trace.log. If the current node has failed 3+ times consecutively, set status to "failed" and report:
```
Workflow stuck: node '{pc}' failed {count} times. Stopping.
```

## Example trace

```
[start] success → fix (next)
[fix] success → test (next)
[test] fail → fix (transition: if fail goto fix)
[fix] success → test (next)
[test] success → done (transition: if success goto done)
[done] success → null (workflow done)
```
