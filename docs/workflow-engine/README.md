# Workflow Engine Project

## Goal

Build a stateful graph workflow engine for CAM that can orchestrate `skill`, `cmd`, `agent`, `goto`, `memory`, `handoff`, `wait`, and `resume` in a way that is short for humans, easy for AI to generate, and stable enough for long-running industrial workflows.

This design targets loops, retries, human handoff, compact/resume, and agent-to-agent execution. It is intentionally **not** a pure DAG engine. The top-level model is a **stateful graph workflow** with structured transitions and append-only execution trace.

## Why this exists

CAM already has DAG scheduling for straightforward dependencies. What is missing is a richer execution model for:

- loopable agent workflows
- recovery after failure
- wait/human approval/resume
- structured handoff/checkpoint generation
- traceable execution history
- deterministic runtime control around LLM-driven steps

## Core design decisions

### 1. Top-level abstraction

Use a **stateful graph workflow**, not a pure DAG.

- DAG is good for one-way dependency graphs.
- This project needs loops, previous-node jumps, waiting, and handoff.
- The runtime acts more like a small virtual controller than a batch scheduler.

### 2. Minimal DSL

Keep the language small:

- `do`
- `if`
- `goto`
- `set`
- `fail`

And `do` can run:

- `skill`
- `cmd`
- `agent`

This keeps workflows short, AI-friendly, and runtime-friendly.

### 3. Three-way state separation

Split runtime state into:

- **trace**: append-only execution history
- **memory**: small current working state for decisions
- **artifacts/logs**: large outputs, raw logs, reports, agent raw responses

### 4. Handoff is a first-class pattern

A key workflow capability is a structured **handoff/checkpoint** node. It is used for:

- loop boundary compression
- resume checkpoints
- agent-to-agent transfer
- human review packets
- pre-compact summaries

### 5. Runtime owns workflow status

Executors return normalized node results. Only the runtime decides workflow state transitions.

That means:

- node fail does **not** automatically mean workflow fail
- waiting is a normal top-level workflow state
- resume uses `resume_pc`, not trace replay

## Architecture summary

### Core pieces

- **DSL**: minimal YAML-like node definitions
- **Executors**: implementations of `skill`, `cmd`, `agent`
- **Node Output Contract**: normalized result bus from executors to runtime
- **Transition Resolver**: deterministic next-step decision engine
- **Runtime State Machine**: top-level workflow lifecycle
- **Trace**: append-only execution timeline
- **Log/Artifact Store**: raw data and large outputs
- **Handoff/Checkpoint**: compressed transfer package between phases/actors

## Runtime state machine

Top-level workflow states:

- `ready`
- `running`
- `waiting`
- `paused`
- `done`
- `failed`
- `aborted`

These are specified in `spec/runtime-state-machine.md`.

## Current specification set

### Core specs

- `spec/runtime-state-machine.md`
- `spec/node-output-contract.md`
- `spec/transition-resolution.md`
- `spec/dsl-node-schema.md`
- `spec/trace-schema.md`
- `spec/python-runtime-skeleton.md`

## What is decided now

### Chosen baseline

- stateful graph workflow
- small DSL
- append-only trace
- memory/trace/artifact separation
- structured handoff/checkpoint
- deterministic transition resolver
- runtime-controlled top-level state

### Explicitly not chosen as v0.1 baseline

- BPMN first
- large visual editor first
- heavy type system
- pure DAG-only scheduling model
- complex expression language
- parallel execution in v0.1
- distributed orchestration in v0.1

## v0.1 scope

### Must have

- parse workflow node definitions
- run `cmd`, `skill`, `agent`
- normalized node output contract
- deterministic transition resolution
- runtime state machine
- append-only trace
- logs/artifacts with refs
- handoff/checkpoint as a standard node pattern
- waiting + resume
- basic loop support

### Nice to have but still optional in v0.1

- YAML loader
- simple CLI inspect/status view
- artifact directory structure
- minimal Claude executor adapter

## v0.1 implementation plan

### Step 1: lock specs

Done in this doc set:

- runtime state machine
- node output contract
- transition rules
- DSL node schema
- trace schema
- runtime skeleton

### Step 2: build a tiny runnable engine

Implement a minimal local engine that can:

- run a demo workflow
- write runtime state
- append trace
- write logs/artifacts refs
- wait/resume

### Step 3: connect a real Claude executor

Swap the stub agent executor with a real Claude-backed executor while preserving the same node result contract.

### Step 4: integrate into CAM

Possible integration paths:

- standalone workflow module under `src/cam/core/`
- optional advanced workflow mode next to existing DAG scheduler
- use handoff/checkpoint around Claude compact/session boundaries

## To-do list

### Immediate

- [ ] implement minimal runtime loop
- [ ] implement node result normalization helpers
- [ ] implement transition resolver from spec
- [ ] implement trace writer
- [ ] implement logs/artifacts path allocator
- [ ] implement wait/resume support
- [ ] create minimal YAML loader
- [ ] add a tiny demo workflow

### Next

- [ ] real Claude executor adapter
- [ ] handoff/checkpoint helpers and templates
- [ ] CLI commands for inspect/status/resume/override
- [ ] branch/target validation and workflow linting
- [ ] retry budget and backoff policy
- [ ] side-effect/commit boundary model

### Later

- [ ] parallel nodes / join semantics
- [ ] subflow `call/return`
- [ ] policy layer
- [ ] distributed workers
- [ ] UI/visual graph editor
- [ ] reusable workflow library
- [ ] richer artifact retention policy

## Open design items

These are recognized but not yet frozen in v0.1:

- retry/backoff schema
- policy representation
- commit/apply nodes for side-effect control
- idempotency/dedup semantics for effectful nodes
- subflow stack model
- parallel execution model

## Recommended repository layout

```text
src/cam/
  core/
    workflow/
      engine.py
      resolver.py
      state.py
      trace.py
      logsink.py
      executors/
        base.py
        cmd.py
        skill.py
        agent.py
      loader.py
      validators.py

docs/workflow-engine/
  README.md
  spec/
    runtime-state-machine.md
    node-output-contract.md
    transition-resolution.md
    dsl-node-schema.md
    trace-schema.md
    python-runtime-skeleton.md
```

## Integration options considered

### Option A: keep as standalone workflow module inside CAM

Recommended first.

Pros:

- small blast radius
- easier iteration
- can coexist with current DAG scheduler

### Option B: replace DAG scheduler directly

Not recommended for first version.

Pros:

- unified scheduling model

Cons:

- too disruptive
- DAG and stateful graph are not the same thing

### Option C: keep DAG for simple jobs, add stateful graph workflow for agent flows

Strong long-term direction.

This likely matches CAM best:

- DAG for simple dependency orchestration
- stateful graph workflow for complex agent loops and handoffs

## First release target

A first release is successful if CAM can run a small workflow like:

```text
start -> test -> analyze -> handoff -> approve(wait) -> resume -> fix -> test -> done
```

with:

- runtime state persisted
- trace appended every step
- logs/artifacts written by refs
- human wait/resume working
- handoff artifact generated

## Long-term direction

Over time this can become CAM's advanced orchestration layer for:

- long-running coding agents
- human-in-the-loop development loops
- cross-machine agent execution
- resilient resume after compact, crash, or disconnect
- structured checkpointing around Claude or other agent context boundaries

This is the intended direction, but v0.1 should stay small and prove the runtime shape first.
