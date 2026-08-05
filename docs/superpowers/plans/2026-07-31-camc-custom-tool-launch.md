# CAMC Custom Tool Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `camc run` launch arbitrary tools with an optional executable directory, per-agent environment overrides, and extra argv while preserving an interactive tmux shell when the tool exits.

**Architecture:** Keep the existing strict adapter preflight untouched when no custom launch option is supplied. Parse custom launch inputs into an effective runtime environment and argv without shell evaluation; `tool-dir` is a highest-priority executable lookup. Launch through a small shell wrapper that writes an agent-owned exit-status file after the tool process terminates and leaves the tmux shell usable. `cmd_run` reports an immediate non-zero exit; the monitor consumes later exit status and marks the retained session failed or completed.

**Tech Stack:** Python stdlib, argparse, tmux, existing `AgentStore`/monitor runtime.

## Global Constraints

- Do not change existing no-option Claude, Codex, or Cursor preflight behavior.
- `tool-dir` is an absolute direct-executable directory and wins over normal resolution.
- Environment values and arguments are passed as data, never evaluated as shell source.
- Tool failure never removes a successfully created tmux session.
- Unknown tools stay `others` and never receive automation.

---

### Task 1: Define custom launch inputs and tool resolution

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Modify: `src/camc_pkg/runtime_env.py`
- Test: `tests/test_arbitrary_tool.py`

- [x] Add failing tests for an absolute `--tool-dir` executable winning over PATH, `--tool-env NAME=VALUE` changing only the launch runtime, and literal extra argv reaching the launch command.
- [x] Run the focused tests and capture RED because the parser and resolver do not expose these inputs.
- [x] Add `--tool-dir`, repeatable `--tool-env`, and `--tool-args` handling. Reject malformed env names and non-absolute tool directories, expand only `$NAME`/`${NAME}` from the inherited runtime, and use the supplied directory before configured/golden/PATH lookup.
- [x] Run the focused tests to GREEN.

### Task 2: Preserve tmux and surface tool exit status

**Files:**
- Modify: `src/camc_pkg/transport.py`
- Modify: `src/camc_pkg/cli.py`
- Modify: `src/camc_pkg/monitor.py`
- Test: `tests/test_arbitrary_tool.py`

- [x] Add failing tests that a non-zero arbitrary-tool exit leaves the tmux creation result intact, writes a per-agent startup status, reports immediate failure, and lets monitor state become failed without killing tmux.
- [x] Run focused tests and capture RED because no status wrapper exists.
- [x] Run each launch under a shell wrapper that executes the already-quoted argv, writes only exit code/time to the per-agent status file, emits an in-pane diagnostic on non-zero exit, and returns to an interactive shell. Read an immediate status in `cmd_run`; let `monitor.py` consume later status once and update `status`, `state`, and `exit_reason` without changing tmux lifecycle.
- [x] Run focused tests to GREEN.

### Task 3: Regression verification and bundle

**Files:**
- Modify only generated: `dist/camc`, `dist/BUILD_LOG.md`
- Test: existing runtime, arbitrary-tool, and monitor tests.

- [x] Run existing known-tool strict-preflight and arbitrary-tool tests to prove the no-option path is unchanged.
- [x] Run a real disposable custom tool that exits non-zero; verify the reported error and that its tmux session remains attachable, then remove the disposable session.
- [x] Build with `PYTHONPATH=src python3 build_camc.py`, run bundle help/version smoke checks, and run `git diff --check`.
