# CamFlow — Complete Handoff Document

**Date**: 2026-04-12
**From**: aicli agent (main development session)
**To**: camflow agent (639f86d2, DC workflow context)

---

## 1. What is CamFlow?

"Makefile for AI agents" — a YAML DSL that defines multi-step workflows where each node can be a shell command, Claude agent task, skill invocation, or isolated subagent. State persists to disk, agents can accumulate lessons across iterations.

**Repo**: `~/.openclaw/workspace/cam/` (cam project, camflow is a component)
**Demo**: `~/.openclaw/workspace/cam/demos/camflow-cli-demo/`
**Research doc**: `~/.openclaw/workspace/cam/docs/camflow-cli-research-handoff.md` (328 lines, read this first)
**Auto-confirm design**: `~/.openclaw/workspace/cam/docs/auto-confirm-flow.md`

---

## 2. Three-Phase Plan

| Phase | Engine | How nodes run | Status |
|-------|--------|---------------|--------|
| **CLI** | Claude skill + /loop | Same Claude session | **DONE — validated** |
| **CAM** | camc run --wait | Each node = new agent | **Next** |
| **SDK** | Anthropic API | Direct API calls | Future |

---

## 3. What's Been Done (CLI Phase)

### DSL Design — Complete

```yaml
start:
  do: agent claude          # 4 types: cmd, agent, skill, subagent
  with: |                   # instructions with {{state.xxx}} template vars
    Analyze the problem...
    Previous lessons: {{state.lessons}}
  next: fix                 # unconditional next

test:
  do: cmd pytest -v         # shell command
  transitions:              # conditional branching
    - if: fail
      goto: fix
    - if: success
      goto: done
```

### Node Types — All Validated

| Type | Syntax | Test | Result |
|------|--------|------|--------|
| `cmd` | `do: cmd pytest -v` | Calculator demo | ✅ Exit code → success/fail |
| `agent` | `do: agent claude` | Calculator demo | ✅ Shared context, does task |
| `skill` | `do: skill greet` | `/tmp/camflow-skill-test/` | ✅ Must use Skill() tool, not Read |
| `subagent` | `do: subagent claude` | `/tmp/camflow-subagent-test/` | ✅ Isolated context, file system shared |

### Experiments Run — 4 total

**Experiment 1: Calculator Bug-Fix Loop** (main validation)
- 4 intentional bugs in calculator.py, 11 tests
- Flow: start(analyze) → fix(one bug) → test → loop or done
- **Result**: 10 nodes executed, 4 bugs fixed in 4 fix→test loops, all 11 tests pass
- Trace log preserved at `demos/camflow-cli-demo/.claude/state/trace.log`

**Experiment 2: Skill Invocation**
- `do: skill greet` must use `Skill()` tool, NOT `Read(SKILL.md)`
- **Key learning**: Earlier attempts failed because runner read SKILL.md as text

**Experiment 3: Subagent Isolation**
- Parent writes secret to state, subagent reads via `{{state.secret_number}}`
- **Result**: Confirmed — no parent memory leakage, only state template vars pass data

**Experiment 4: Lessons Mechanism**
- `state.lessons` array (max 10 entries), injected via `{{state.lessons}}`
- Template substitution verified in Python simulation
- **Not yet validated**: Live agent actually applying lessons to change behavior

### Skills Created — 2 global, 1 project-local

| Skill | Location | Purpose |
|-------|----------|---------|
| `workflow-run` | `~/.claude/skills/workflow-run/SKILL.md` | Single-step executor (called by /loop) |
| `workflow-creator` | `~/.claude/skills/workflow-creator/SKILL.md` | Interactive project generator |
| `workflow-check` | `demos/camflow-cli-demo/.claude/skills/workflow-check/SKILL.md` | Health check (staleness, loops) |

### How CLI Phase Runs

```bash
cd demos/camflow-cli-demo

# Reset state
echo '{"pc": "start", "status": "running", "lessons": []}' > .claude/state/workflow.json
> .claude/state/trace.log

# Start Claude
claude

# Inside Claude:
/workflow-run              # executes ONE node
/loop 1m /workflow-run     # continuous: one node per minute
```

State file: `.claude/state/workflow.json`
Trace log: `.claude/state/trace.log` (JSONL, one entry per node execution)

---

## 4. CAM / CAMC — How They Work

### cam (Coding Agent Manager)
- **Central orchestrator**, runs on your main machine
- Manages agents across multiple machines (local + SSH remotes)
- Commands: `cam run`, `cam list`, `cam stop`, `cam attach`, `cam capture`, `cam send`
- Data: `~/.cam/` (agent registry, contexts, machines)
- `cam --json list` for machine-readable output (table output truncates names)

### camc (CAM Client)
- **Single-file standalone**, runs on each remote machine
- Same API as cam but local only
- Commands: `camc run`, `camc list`, `camc stop`, `camc attach`, `camc capture`, `camc send`
- Key flag: `camc run --name <name> --path <dir> "prompt"` or `camc run --name <name> --path <dir>` (interactive)
- Auto-confirm: camc monitor auto-confirms tool permission dialogs
- `camc capture <id>` — get agent's tmux screen content
- `camc send <id> "text"` — send input to agent

### Key commands for camflow CAM phase

```bash
# Start agent with task, wait for completion
camc run --name "fix-node" --path /project --auto-exit "Fix the divide bug in calculator.py"

# Check status
camc list                   # table view
camc --json list            # JSON (no truncation)
camc status <id>            # detailed single agent

# Interact
camc capture <id>           # read screen
camc send <id> "yes"        # send input
camc attach <id>            # interactive tmux attach

# Lifecycle
camc stop <id>              # graceful stop
camc kill <id>              # force kill
camc rm <id> --force        # remove from registry
```

### Auto-confirm flow
- camc monitor runs every ~1s
- Detects permission dialogs → auto-sends "y"
- Idle detection: hash stable 60s + prompt visible → idle
- Fast-track: "ed for Xs" pattern → idle in 5s
- Stuck fallback: hash stable 120s + no prompt → sends "1"
- See `docs/auto-confirm-flow.md` for full flowchart

---

## 5. What Needs to Be Done (CAM Phase)

### Core: cam-flow engine

A persistent process (or camc plugin) that:
1. Reads `workflow.yaml` and `workflow.json`
2. For `cmd` nodes: run subprocess directly (no LLM)
3. For `agent`/`subagent` nodes: `camc run --name "node-xxx" --path <dir> --auto-exit "<with text>"`
4. Wait for agent completion (poll `camc status` or use `--auto-exit`)
5. Determine success/fail from agent output or exit code
6. Resolve transition, update state, continue to next node

### Design decisions needed

1. **Blocking vs polling**: `camc run` + poll `camc status` every few seconds? Or add `camc run --wait` that blocks until completion?
2. **Agent output extraction**: How does cam-flow get the result from an agent node? Options:
   - Parse `camc capture` output
   - Agent writes result to a file (e.g., `.claude/state/node-result.json`)
   - Agent updates state.json directly
3. **Parallel nodes**: Should cam-flow support `parallel: [node-a, node-b]`? Or keep it sequential for now?
4. **Error handling**: Agent crashes mid-node → retry? skip? fail workflow?
5. **Integration point**: Standalone script? camc subcommand? cam plugin?

### Suggested implementation order

1. Minimal engine: read YAML → execute cmd/agent nodes sequentially → update state
2. Add transition logic (if/goto)
3. Add template substitution (`{{state.xxx}}`)
4. Add lessons mechanism
5. Add subagent support
6. Add parallel execution (stretch)

---

## 6. Open Questions from Research

1. **Lesson effectiveness**: Do agents actually change behavior when lessons are injected? Needs deliberate repeat-failure test.
2. **Subagent cost**: Each subagent = 12K+ tokens overhead. 10-node workflow = 120K tokens just in setup.
3. **State size**: Need cleanup/archival when state.json grows large.
4. **Multi-file workflows**: Import/include for workflow composition.
5. **CAM backend**: Block vs poll for agent completion detection.

---

## 7. Files You Should Read

Priority order:
1. `docs/camflow-cli-research-handoff.md` — full research details (328 lines)
2. `docs/auto-confirm-flow.md` — how camc monitor works
3. `demos/camflow-cli-demo/workflow.yaml` — the validated DSL
4. `demos/camflow-cli-demo/.claude/state/trace.log` — successful execution trace
5. `~/.claude/skills/workflow-run/SKILL.md` — the CLI phase executor skill
6. `~/.claude/skills/workflow-creator/SKILL.md` — project generator skill

---

## 8. Your Agent Info

- **Agent ID**: 639f86d2
- **Name**: camflow
- **Context**: workflow (DC machine)
- **Path**: /home/scratch.hren_gpu_1/test/workflow
- **Transport**: SSH
- **Status**: running, idle 14 days

Resume work by reading this doc + the research handoff, then start designing the CAM phase engine.
