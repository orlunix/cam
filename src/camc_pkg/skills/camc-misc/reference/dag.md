# `camc apply` — DAG workflows

Run multiple agents with dependencies from a YAML file.

```bash
camc apply -f tasks.yaml
camc apply -f tasks.yaml --dry-run
camc apply -f tasks.yaml -p /path/to/dir
```

## YAML schema

```yaml
version: 1
defaults:
  tool: claude
  timeout: 30m
  retry: 0

tasks:
  - name: research
    prompt: "Research the ECC error pattern in bug 5893270"

  - name: fix
    prompt: "Fix the ECC error based on research findings"
    depends_on: [research]

  - name: test
    prompt: "Run regression to verify the fix"
    depends_on: [fix]
    timeout: 1h
```

## Validation rules

- No cycles in `depends_on`
- All `depends_on` names must exist in `tasks`
- Topological sort defines run order
- Each "level" (independent siblings) runs in parallel via `asyncio.gather`

## Detach is NOT supported

`--detach` doesn't work with DAGs. Detached agents return immediately
with status=running, breaking dependency checks (next level fires
before the current one finishes). Always use **follow mode** with
`apply` — the call blocks until the graph completes or a task fails.

## Failure handling

By default a failed task fails the DAG. Override per-task or globally:

```yaml
defaults:
  retry: 1                    # one retry on failure
  on_failure: continue        # don't abort the DAG

tasks:
  - name: flaky
    on_failure: continue       # task-level override
    prompt: "..."
```

## Patterns

### Fan-out / fan-in

```yaml
tasks:
  - { name: split, prompt: "split work into N shards" }
  - { name: w1, depends_on: [split], prompt: "shard 1" }
  - { name: w2, depends_on: [split], prompt: "shard 2" }
  - { name: w3, depends_on: [split], prompt: "shard 3" }
  - { name: merge, depends_on: [w1, w2, w3], prompt: "merge results" }
```

### Conditional path

camc DAG has no `if` primitive — express via dependency on a "decision"
task whose output is a marker file the downstream tasks check.
