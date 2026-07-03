---
name: camc-misc
description: >
  Miscellaneous camc operations: archive agent history, run DAG
  workflows (camc apply), prune dead records, view agent event
  history, adopt existing tmux sessions. Use when these specific
  non-core tasks come up — they're kept separate to keep the main
  managing-camc skill focused on the agent lifecycle.
compatibility: Requires camc binary in PATH.
metadata:
  author: hren
  tags: camc, cam, archive, dag, apply, prune, history, adopt,
    batch, workflow, cleanup, tar, tgz
  category: infra
  requires-tools: camc, tmux
disable-model-invocation: false
argument-hint: "[archive <agent> | apply -f tasks.yaml | prune | history <agent> | add <session>]"
allowed-tools: Bash Read Glob Grep
---

# camc-misc — Archive, DAG, Prune, History, Adopt

## 1. Archive

`camc archive` packages a single agent's tmux scrollback + Claude session
JSONL + monitor logs into a tar.gz, plus subcommands to inspect archives.

### Create

```bash
camc archive <agent>                   # tar.gz under ~/.cam/archives/
camc archive <agent> -o /tmp/foo.tgz   # custom output path
camc archive <agent> --session-id <sid> # specific session jsonl
```

Filename: `<agent-id>-<session-id>-<YYYYMMDDHHMMSS>[-<name>].tar.gz`.
`camc rm <agent> --archive` archives before removing (default `rm` does NOT).

### Inspect

```bash
camc archive list                      # all archives as table
camc archive info <archive-name>       # header + manifest + last assistant text
camc archive summary <archive-name>    # per-prompt table (line + summary)
camc archive show <archive-name>       # full conversation Q/A (pipe to less)
```

### When to archive

- Before `camc rm` if the conversation is worth keeping
- End of a long debugging session
- Before cross-machine reboot (context survives if transfer fails)

Archive is **opt-in** — short-lived workflow agents shouldn't auto-archive.

## 2. DAG workflows (`camc apply`)

Run multiple agents with dependencies from a YAML file.

```bash
camc apply -f tasks.yaml
camc apply -f tasks.yaml --dry-run
camc apply -f tasks.yaml -p /path/to/dir
```

### YAML schema

```yaml
version: 1
defaults:
  tool: claude
  timeout: 30m

tasks:
  - name: research
    prompt: "Research the ECC error pattern"

  - name: fix
    prompt: "Fix the ECC error based on research findings"
    depends_on: [research]

  - name: test
    prompt: "Run regression to verify the fix"
    depends_on: [fix]
    timeout: 1h
```

### Rules

- No cycles in `depends_on`; all deps must exist in `tasks`
- Each level (independent siblings) runs in parallel
- **Detach is NOT supported** — detached agents don't complete before next level

### Patterns

**Fan-out / fan-in:**
```yaml
tasks:
  - { name: split, prompt: "split work into N shards" }
  - { name: w1, depends_on: [split], prompt: "shard 1" }
  - { name: w2, depends_on: [split], prompt: "shard 2" }
  - { name: merge, depends_on: [w1, w2], prompt: "merge results" }
```

## 3. Prune

```bash
camc prune                  # drift-fix ONLY (status wrong but tmux alive)
camc prune --orphans        # actually delete dead-tmux records + stale files
camc prune --orphans --dry-run
```

Plain `camc prune` does NOT delete records — it only fixes status drift.
Pass `--orphans` to clean dead-tmux entries and stale logs/sockets/PIDs.

## 4. History

```bash
camc history <agent>        # event log: starts, confirms, errors
```

Events stored append-only in `~/.cam/events.jsonl` (auto-rotates 30 days).

## 5. Adopt an existing tmux session

```bash
camc add my-existing-session --tool claude --name my-agent
```

Registers an unmanaged tmux session as a camc-tracked agent.

## Reference

| File | When to read |
|---|---|
| `reference/archive.md` | Inspect/extract an archive in detail |
| `reference/dag.md` | Full DAG spec — validation, failure handling, fan-out patterns |