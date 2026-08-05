# CAMC Loop Feedback Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, local feedback script to an agent prompt loop so each eligible tick can send real diagnostic output to the owner agent, while preserving the current prompt-only loop contract when no script is configured.

**Architecture:** `camc cron --loop` remains a persistent, idle-gated prompt delivery mechanism. A future optional `--feedback-script` is run by the scheduler on the owner node and its bounded result is appended to the continuation message; the agent still owns task judgment and may remove its own loop. This phase deliberately does not treat an exit code as proof of Goal completion, does not add an external terminal-state controller, and does not change ordinary prompt loops.

**Tech Stack:** Python standard library, existing `camc_pkg.cron_loop`, `camc_pkg.cli`, pytest.

## Current Contract (Do Not Change In This Task)

- A loop entry stores prompt text in `~/.cam/loops/<owner-id>/agent.loop.json`.
- `camc cron tick` dispatches only when the owner is local, `running`, and `idle`.
- Dispatch is `camc msg send <owner-id> --text <prompt> --no-wait`.
- `state.max_attempts` concerns failed delivery only; it is not task-progress accounting.
- A normal `camc cron add --loop` must remain prompt-only and must not run shell commands.
- CAMC does not currently infer `SUCCESS`, `STALLED`, `BLOCKED`, or `EXHAUSTED` from an agent's work. The goal-loop skill's self-removal instruction remains a prompt convention.

## Explicitly Deferred

- No required verifier for every loop.
- No automatic completion from a script exit code.
- No parsing of checklist text, model YAML, or agent prose.
- No round acknowledgement/reply protocol.
- No automatic `STALLED` decision from repeated output.
- No remote execution: a feedback script belongs on the owner node.
- No mutation of existing loops, built-in skills, or Desktop UI in this phase.

## Proposed Future Command

```bash
camc cron add --loop --owner <agent-id> --name <name> --every 5m \
  --prompt-file /absolute/path/continue.md \
  --feedback-script /absolute/path/diagnose.sh \
  --feedback-timeout 60
```

`--feedback-script` is optional. Its output is evidence for the next agent turn, not a success/failure authority. The command must keep rejecting `--shell` and free-form command argv in loop mode.

The script must be an existing absolute regular executable. CAMC runs it with `shell=False`, in the resolved owner workdir, with a bounded timeout and bounded combined stdout/stderr capture. It receives no generated shell text and no agent-controlled argv.

The rendered message is:

```text
<configured continuation prompt>

[CAMC feedback script: exit <code>]
<bounded stdout/stderr>
```

If the script cannot be started or times out, CAMC must append a concise execution error as feedback; it must not mark the Goal complete, remove the loop, or silently send an unchanged prompt.

---

### Task 1: Persist and validate optional feedback-script configuration

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Modify: `src/camc_pkg/cron_loop.py`
- Test: `tests/test_cron_loop.py`

**Consumes:** Existing `_cmd_cron_add_loop`, `build_loop`, and owner resolution.

**Produces:** An additive `action.feedback_script` object containing an absolute path and timeout, or no object for legacy prompt-only loops.

- [ ] **Step 1: Add focused failing CLI tests**

```python
def test_add_loop_persists_optional_feedback_script(...):
    args = self._add_args(feedback_script="/tmp/check.sh", feedback_timeout=60)
    ...
    assert loop["action"]["feedback_script"] == {
        "path": "/tmp/check.sh", "timeout_seconds": 60,
    }

def test_add_loop_rejects_relative_or_missing_feedback_script(...):
    ...
    assert "feedback-script" in capsys.readouterr().err
```

- [ ] **Step 2: Run the focused tests and capture RED**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cron_loop.py -k feedback_script`

Expected: FAIL because parser arguments and persisted feedback-script configuration do not exist.

- [ ] **Step 3: Add parser and registration validation**

Add `--feedback-script PATH` and `--feedback-timeout SECONDS` only to `camc cron add`. In `_cmd_cron_add_loop`, require an absolute existing regular executable path and a positive bounded timeout before storing the additive object. Leave the object absent when the option is omitted.

- [ ] **Step 4: Extend `build_loop` without changing legacy records**

Add an optional `feedback_script=None` parameter. When present, emit:

```python
"feedback_script": {
    "path": feedback_script["path"],
    "timeout_seconds": feedback_script["timeout_seconds"],
}
```

When absent, do not add the key. Existing files must load and dispatch exactly as before.

- [ ] **Step 5: Run focused tests and capture GREEN**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cron_loop.py -k feedback_script`

Expected: PASS.

### Task 2: Execute feedback scripts safely and append their results

**Files:**
- Modify: `src/camc_pkg/cron_loop.py`
- Test: `tests/test_cron_loop.py`

**Consumes:** Additive `action.feedback_script`, owner record workdir, existing `dispatch_loop`.

**Produces:** A test-injectable feedback runner and a rendered one-turn message containing bounded script evidence.

- [ ] **Step 1: Add focused failing dispatch tests**

```python
def test_due_feedback_script_output_is_appended_before_dispatch(...):
    loop = _loop.build_loop(..., feedback_script={...})
    calls = []
    _loop.tick_loops(dispatch=lambda L: calls.append(L) or (True, "msg1"), ...)
    assert "[CAMC feedback script: exit 1]" in calls[0]["action"]["text"]
    assert "test_failure" in calls[0]["action"]["text"]

def test_feedback_timeout_is_sent_as_error_not_success(...):
    ...
    assert "timed out" in calls[0]["action"]["text"]
```

- [ ] **Step 2: Run the focused tests and capture RED**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cron_loop.py -k feedback`

Expected: FAIL because `tick_loops` currently sends only the configured prompt.

- [ ] **Step 3: Add a bounded runner with no shell interpolation**

Implement `_run_feedback_script(loop, owner_rec, runner=subprocess.run)` in `cron_loop.py` using a single-element executable argv, `cwd=workdir`, `shell=False`, `stdout=PIPE`, `stderr=STDOUT`, and the configured timeout. Truncate captured output to a documented byte/character limit and return a concise start/timeout failure string instead of raising.

- [ ] **Step 4: Render a copy of the loop for dispatch**

Do not mutate stored `action.text`. Immediately before `dispatch_fn`, construct a shallow copy whose message is the configured prompt plus the feedback block. Persist only the existing dispatch state fields, so repeated ticks do not accumulate feedback into the loop file.

- [ ] **Step 5: Run focused tests and regression tests**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cron_loop.py tests/test_cron.py`

Expected: PASS, including existing prompt-only, busy-deferral, retry-budget, and host-filter tests.

### Task 3: Document the optional evidence-only semantics

**Files:**
- Modify: `src/camc_pkg/skills/camc-cron-loop/SKILL.md`
- Modify: `src/camc_pkg/skills/camc-goal-loop/SKILL.md`
- Modify: `docs/camc-agent-loop-spec.md`
- Test: `tests/test_cron_loop.py` or an existing documentation/source assertion test if one exists

**Consumes:** Implemented optional command and current goal-loop convention.

**Produces:** Accurate instructions that feedback script output informs the agent but does not constitute automatic completion.

- [ ] **Step 1: Add a failing source assertion**

```python
def test_goal_loop_docs_do_not_claim_feedback_script_controls_completion():
    text = Path(... / "camc-goal-loop" / "SKILL.md").read_text()
    assert "evidence for the next agent turn" in text
    assert "exit 0 automatically completes" not in text
```

- [ ] **Step 2: Run the assertion and capture RED**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cron_loop.py -k feedback_docs`

Expected: FAIL until the built-in skill is updated.

- [ ] **Step 3: Update all three documents**

Document the optional flags, local-owner requirement, bounded output, and the distinction between feedback and an authoritative verifier. Preserve the existing self-removal wording for Goal completion.

- [ ] **Step 4: Run documentation and full focused checks**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cron_loop.py tests/test_cron.py`

Expected: PASS.

### Task 4: Bundle and verify backward compatibility

**Files:**
- Modify: `dist/camc`
- Modify: `dist/BUILD_LOG.md`

**Consumes:** Source implementation and built-in skill changes.

**Produces:** A distributable CAMC bundle with optional feedback-script support.

- [ ] **Step 1: Build the standalone bundle**

Run: `PYTHONPATH=src python3 build_camc.py`

Expected: Generated `dist/camc` includes the new CLI help and built-in skill text.

- [ ] **Step 2: Smoke-test both command shapes**

Run:

```bash
./dist/camc cron add --help
./dist/camc cron list --help
./dist/camc skills list
```

Expected: Help shows optional feedback-script flags only on add; existing loop/list commands remain available.

- [ ] **Step 3: Check generated artifact and diff hygiene**

Run:

```bash
git diff --check
git diff -- src/camc_pkg/cron_loop.py src/camc_pkg/cli.py \
  src/camc_pkg/skills/camc-cron-loop/SKILL.md \
  src/camc_pkg/skills/camc-goal-loop/SKILL.md
```

Expected: No whitespace errors; prompt-only loop behavior is unchanged by the diff.

## Later Controlled-Loop Extension

Only after feedback-script usage proves useful, consider a separate design for an authoritative verifier with terminal states, round acknowledgement, stable progress tokens, and `--max-stalled`. It must be a distinct opt-in mode; it must not reinterpret ordinary script exit codes or change existing `cron --loop` behavior.
