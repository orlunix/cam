# cam-flow CLI Phase Research Handoff

**Date**: 2026-04-05
**Researcher**: Claude (multiple sessions, 2026-04-03 → 2026-04-05)
**Demo directory**: `demos/camflow-cli-demo/`

---

## 1. Background: What is cam-flow?

cam-flow is a workflow runner for AI coding agents. Think of it as "Makefile for agents" — a simple YAML DSL that describes a sequence of nodes (analyze → fix → test → done), where each node can be a shell command, a Claude agent task, a skill invocation, or an isolated subagent.

The key insight: existing workflow tools (Temporal, Airflow) are built for microservices, not for LLM agents. Agents need different primitives — they share a conversation context, they can learn from previous iterations, and their "output" is natural language + file changes rather than structured data.

### The three-phase plan

| Phase | Engine | How nodes run | Status |
|-------|--------|---------------|--------|
| **CLI** | Claude skill + /loop | Same Claude session | **Validated** |
| **CAM** | camc run --wait | Each node = new agent | Planned |
| **SDK** | Anthropic API | Direct API calls | Planned |

This document covers the **CLI phase** research.

---

## 2. DSL Design

```yaml
# workflow.yaml — the full syntax
start:
  do: agent claude          # node type: cmd, agent, skill, subagent
  with: |                   # instructions (template vars: {{state.xxx}})
    Analyze the problem...
    Previous lessons: {{state.lessons}}
  next: fix                 # unconditional next node

test:
  do: cmd pytest -v         # shell command, exit code = success/fail
  transitions:              # conditional branching
    - if: fail
      goto: fix
    - if: success
      goto: done
```

### Four node types

| Type | Syntax | Execution | Context | Use case |
|------|--------|-----------|---------|----------|
| `cmd` | `do: cmd pytest -v` | Shell command | — | Tests, builds, scripts |
| `agent` | `do: agent claude` | Current session does it | Shared (growing) | Analysis, code fixes |
| `skill` | `do: skill greet` | Skill() tool invocation | Shared | Reusable templates |
| `subagent` | `do: subagent claude` | Agent() tool, new process | **Isolated** | Heavy tasks, parallel |

### State and template substitution

State lives in `.claude/state/workflow.json`:
```json
{
  "pc": "fix",
  "status": "running",
  "error": "divide() has no zero-check, factorial() off-by-one",
  "lessons": ["Check ALL test cases for a function, not just the first failure"]
}
```

The `with` field supports `{{state.xxx}}` template variables. Before executing a node, the runner replaces these with current state values. This is how information flows between nodes.

---

## 3. Execution Model

The CLI phase uses a "tick" model:

```
User starts: /workflow-run           ← executes ONE node
User sets up: /loop 1m /workflow-run ← calls /workflow-run every minute
```

Each tick:
1. Read `workflow.json` → get current `pc` (program counter)
2. Read `workflow.yaml` → find the node
3. Execute the node (run command, do agent task, invoke skill)
4. Determine result (success/fail)
5. Resolve transition → update `pc`
6. Write updated state back to `workflow.json`
7. Append to `trace.log`
8. **Stop.** Wait for next /loop tick.

This is intentionally simple — no daemon, no background process, just a skill that gets called periodically. State is persisted to disk, so it survives crashes, session restarts, and even machine reboots.

---

## 4. Experiments and Results

### Experiment 1: Calculator Bug-Fix Loop

**Goal**: Validate the core loop: analyze → fix (one bug) → test → if fail, fix again → if pass, done.

**Setup** (`demos/camflow-cli-demo/`):
- `calculator.py` with 4 intentional bugs:
  - `divide()` — no zero-division check
  - `average()` — no empty-list check
  - `factorial()` — off-by-one (`range(1, n)` instead of `range(1, n+1)`) + no negative check
  - `power()` — manual loop can't handle negative exponents
- `test_calculator.py` — 11 tests that expose these bugs
- `workflow.yaml` — start(analyze) → fix(one bug) → test → loop or done

**Execution trace**:
```
[start]  success → fix    (analyzed 4 bugs, put summary in state.error)
[fix]    success → test   (fixed divide zero-check)
[test]   fail    → fix    (3 bugs remain)
[fix]    success → test   (fixed average empty-list)
[test]   fail    → fix    (2 bugs remain)
[fix]    success → test   (fixed factorial off-by-one + negative)
[test]   fail    → fix    (1 bug remains)
[fix]    success → test   (fixed power negative exponent)
[test]   success → done   (all 11 tests pass)
[done]   success → null   (workflow completed)
```

**Findings**:
- ✅ Conditional transitions work (fail→fix, success→done)
- ✅ State passing works (error info flows from start→fix, updated each round)
- ✅ Loop detection works (would abort after 3 consecutive failures at same node)
- ✅ "One fix per pass" instruction was mostly followed (agent sometimes tried to fix 2)
- ⚠️ The `with` field length matters — too much context and the agent ignores parts of it

### Experiment 2: Skill Invocation

**Goal**: Verify that `do: skill <name>` correctly uses the Skill() tool (not just reading SKILL.md as text).

**Setup** (`/tmp/camflow-skill-test/`):
- Workflow: agent → skill greet → cmd echo → agent
- Custom "greet" skill that outputs a greeting

**Result**:
- ✅ Skill() tool properly invoked — confirmed via screen capture showing `Skill(greet) → Successfully loaded skill`
- ❌ Earlier attempts failed because the runner was doing `Read(SKILL.md)` + following instructions, which doesn't give the skill proper loading priority

**Key learning**: Skills MUST be invoked via `Skill()` tool, not by reading the file. This is now documented in the runner and in the feedback memory.

### Experiment 3: Subagent Isolation

**Goal**: Verify that `do: subagent claude` spawns an independent Agent() process with no parent memory.

**Setup** (`/tmp/camflow-subagent-test/`):
- Workflow: agent(write secret) → subagent(read secret from state) → cmd(verify) → agent(summarize)
- Parent writes a random number to `state.secret_number`
- Subagent reads it via `{{state.secret_number}}` — the ONLY way it can get the data

**Result**:
- ✅ Agent() tool spawned independent process (12K tokens, 10s)
- ✅ Subagent had no parent conversation memory — it only knew what was in `{{state.xxx}}`
- ✅ File system shared, context isolated — correct behavior

### Experiment 4: Lessons Mechanism

**Goal**: Design a way for agents to accumulate knowledge across workflow iterations, so they don't repeat mistakes.

**Approaches considered**:

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| Write lessons to CLAUDE.md | Always in context | File grows unbounded, merge conflicts | ❌ Rejected |
| Separate lessons.md file | Clean separation | Agent may not read it | ❌ Rejected |
| **state.json `lessons` array** | Already in state flow, template-injected | Limited to 10 entries | ✅ Chosen |

**Design**:
1. `state.json` has a `lessons` array (max 10 entries, drop oldest)
2. `CLAUDE.md` has a pointer: "Check `.claude/state/workflow.json` → `lessons` array"
3. `workflow.yaml` injects lessons via `{{state.lessons}}` in the `with` field
4. Agents can add lessons via `state_updates.new_lesson` — the runner appends to the array

**Validation** (simulation, not live agent run):
- Simulated a workflow run in Python: confirmed `{{state.lessons}}` replacement works
- Verified lesson accumulation: state starts empty, after 2 fixes, lessons array has 2 entries
- Current state.json shows real lessons from a prior run:
  ```json
  {
    "lessons": [
      "When fixing a function, check ALL test cases for that function, not just the first failure.",
      "Prefer using Python builtins (base**exp) over manual loops for math operations."
    ]
  }
  ```

**Not yet validated**: A real Claude agent reading and applying lessons from `{{state.lessons}}` in a live workflow run. The template substitution is confirmed working, but whether the agent actually changes behavior based on injected lessons needs a live test.

---

## 5. Key Design Decisions and Rationale

### Why "one node per tick" instead of running the whole workflow?

The CLI phase runs inside a Claude Code session. If we ran the whole workflow in one call, the context window would fill up with all nodes' work. By running one node per tick:
- Context stays manageable
- Each node starts relatively fresh
- /loop provides the heartbeat
- State persists to disk between ticks

### Why not use `claude -p` (headless mode)?

- `claude -p` runs a single prompt and exits — no interactive tools, no Skill() invocation
- Agent nodes need full interactive Claude (Bash, Read, Write, Edit, Skill tools)
- CAM phase will use `camc run` which gives full interactive mode + monitor

### Why YAML and not Python/JS for the DSL?

- Target users are agent operators, not developers
- YAML is readable, versionable, diffable
- The DSL is intentionally limited — complex orchestration should use Temporal

### Why store lessons in state.json instead of CLAUDE.md?

- CLAUDE.md is loaded at session start and doesn't change during execution
- state.json is read fresh each tick
- Template substitution `{{state.lessons}}` injects lessons directly into the agent's prompt
- Max 10 entries prevents unbounded growth
- CLAUDE.md just has a pointer for agents that read it manually

---

## 6. Known Limitations (CLI Phase, by design)

| Limitation | Why | Solved in |
|------------|-----|-----------|
| /loop minimum 1 minute | Claude Code /loop constraint | CAM phase |
| Context window grows for `agent` nodes | Same session accumulates history | Use `subagent` for isolation |
| `--auto-exit` kills /loop | Monitor sees idle during loop sleep, triggers exit | Never use auto-exit with /loop |
| `cmd` nodes waste LLM tokens | Claude interprets "run pytest" instead of just running it | CAM phase: native subprocess |
| No parallel execution | Single Claude session is sequential | CAM phase: parallel agents |
| Lessons not tested live | Template substitution verified, agent behavior not | Needs live test |

---

## 7. File Inventory

### Demo project: `demos/camflow-cli-demo/`

```
demos/camflow-cli-demo/
├── workflow.yaml                         # Workflow definition (start→fix→test→done loop)
├── CLAUDE.md                             # Agent instructions + lessons pointer
├── calculator.py                         # Buggy calculator (4 bugs, reset to buggy state)
├── test_calculator.py                    # 11 tests (DO NOT MODIFY)
└── .claude/
    ├── skills/
    │   ├── workflow-run/SKILL.md          # Single-step workflow executor
    │   └── workflow-check/SKILL.md        # Health check skill (staleness, loop detection)
    └── state/
        ├── workflow.json                  # Current state (pc, status, lessons, error)
        └── trace.log                      # Execution history (JSONL)
```

### Global skills (installed on dev machine)

```
~/.claude/skills/
├── workflow-run/SKILL.md                 # Same as demo copy, master version
└── workflow-creator/SKILL.md             # Interactive project generator
```

### Other test projects (may be cleaned up)

```
/tmp/camflow-skill-test/                  # Skill() invocation test
/tmp/camflow-subagent-test/               # Subagent isolation test
```

---

## 8. How to Run the Demo

```bash
# 1. Reset to buggy state (calculator.py should already have bugs)
cd demos/camflow-cli-demo
cat calculator.py  # verify bugs are present

# 2. Reset state
echo '{"pc": "start", "status": "running", "lessons": []}' > .claude/state/workflow.json
> .claude/state/trace.log

# 3. Start Claude in the demo directory
claude

# 4. Inside Claude, start the workflow
/workflow-run

# 5. Set up the loop for continuous execution
/loop 1m /workflow-run

# 6. Watch it fix bugs one by one (4 rounds of test→fix→test)
# Check progress: cat .claude/state/workflow.json
# Check trace: cat .claude/state/trace.log
```

**Important**: Do NOT use `--auto-exit`. The monitor will kill the session during /loop idle periods.

---

## 9. What's Next (CAM Phase)

The CLI phase proved the DSL, state model, and execution flow work. The CAM phase addresses CLI limitations:

1. **Each node = a separate `camc run --wait` agent** — no context window accumulation
2. **Native subprocess for `cmd` nodes** — no LLM waste
3. **Parallel execution** — multiple agents via CAM's existing infrastructure
4. **No /loop needed** — cam-flow engine runs as a persistent process
5. **Monitor integration** — auto-confirm, completion detection, heal all work

The DSL stays the same. The state model stays the same. Only the execution backend changes.

---

## 10. Open Questions

1. **Lesson effectiveness**: Do agents actually change behavior when `{{state.lessons}}` injects prior lessons? Needs live testing with deliberate repeat-failure scenarios.

2. **Subagent cost**: Each subagent node spawns a new Claude session (12K+ tokens overhead). For a 10-node workflow, that's 120K tokens just in overhead. Is this acceptable?

3. **State size**: What happens when state.json grows large (many keys from many nodes)? Should we have a cleanup/archival mechanism?

4. **Multi-file workflows**: Current demo has one workflow.yaml. Real projects may need workflow composition (import/include).

5. **CAM backend design**: Should `camc run --wait` block until completion, or should cam-flow poll? Blocking is simpler but ties up a process. Polling matches the existing CamcPoller pattern.
