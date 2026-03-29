# Transition Resolution Spec v0.1

This defines how the runtime decides the next step after a node finishes execution.

## Inputs

- runtime state (pc, memory, status)
- node definition
- node output (normalized contract)

## Node output contract (summary)

```
status: success | fail | wait | abort
summary: string
output: dict
memory_updates: dict
control.action: continue | goto | wait | fail | abort
control.target: optional node
```

## Resolution priority

1. `control.action == abort` → workflow `aborted`
2. `control.action == wait` → workflow `waiting`, set `resume_pc`
3. `status == fail` and DSL has `if fail` → goto target
4. other DSL conditions (in order)
5. `control.action == goto`
6. `else`
7. default: success → `done`, fail → `failed`

## Waiting semantics

- workflow.status = waiting
- runtime.pc = current node
- runtime.resume_pc = control.target or current

## Resume

- if waiting: pc = resume_pc
- status → running

## Key principles

- node fail != workflow fail
- trace is history only
- runtime owns workflow state
- DSL order defines condition priority
