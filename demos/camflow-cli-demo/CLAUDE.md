# cam-flow CLI Demo

Workflow-driven bug fixing demo powered by cam-flow DSL.

## Project

- `calculator.py` — Python calculator with 4 bugs
- `test_calculator.py` — Tests that expose the bugs (do NOT modify these)
- `workflow.yaml` — Workflow: analyze → fix → test (loop until pass) → done

## How to Run

1. Start the workflow: `/workflow-run`
2. Set up the loop: `/loop 1m /workflow-run`
3. The loop drives execution — one node per minute until done

## State

- `.claude/state/workflow.json` — current position and status
- `.claude/state/trace.log` — execution history

To resume after interruption: just run `/workflow-run` again.
