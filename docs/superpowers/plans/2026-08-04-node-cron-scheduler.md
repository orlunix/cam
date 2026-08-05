# Node Cron Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CAMC's crontab dependency with one self-healing local cron scheduler per host and user.

**Architecture:** A hidden cron scheduler loop invokes the deployed CAMC bundle at minute boundaries. A local PID state file and short startup lock coordinate recovery; the existing per-tick lock remains, but becomes node-local. Shared NFS registries retain host ownership filtering.

**Tech Stack:** Python standard library, existing CAMC cron/loop stores, pytest.

## Global Constraints

- Do not commit or push.
- Do not invoke or inspect system crontab from cron add/list/heal.
- Ordinary `camc list` must not inspect scheduler state.
- Runtime files use an owner-checked `0700` node-local `/tmp/camc-<uid>/cron` directory.

---

### Task 1: Scheduler runtime coordination

**Files:**
- Modify: `src/camc_pkg/cron.py`
- Modify: `src/camc_pkg/cli.py`
- Test: `tests/test_cron.py`

**Produces:** Runtime-path helpers, a hidden scheduler loop, and
`ensure_cron_scheduler(wait=False, restart=False)`.

- [ ] Write failing tests for dead-PID asynchronous recreation, concurrent
  startup de-duplication, synchronous heal verification, verified restart,
  and node-local tick locking.
- [ ] Run those tests and confirm they fail because the helper/entrypoint is absent.
- [ ] Add minimal runtime directory, state, startup-lock, PID verification,
  scheduler loop, and node-local tick-lock implementation.
- [ ] Run focused tests until green.

### Task 2: CLI integration without crontab

**Files:**
- Modify: `src/camc_pkg/cli.py`
- Test: `tests/test_cron.py`, `tests/test_cron_loop.py`

**Produces:** cron add/list asynchronous repair and heal monitor/restart
synchronous repair, without system crontab calls.

- [ ] Write failing tests that stub crontab and prove add/list do not call it,
  while normal top-level `camc list` does not call scheduler repair.
- [ ] Run RED tests.
- [ ] Replace add/remove/heal crontab management calls with the helper, preserving
  job persistence and existing error handling.
- [ ] Run focused tests until green.

### Task 3: Shared-NFS host-safe loops

**Files:**
- Modify: `src/camc_pkg/cron.py`, `src/camc_pkg/cron_loop.py`
- Test: `tests/test_cron.py`, `tests/test_cron_loop.py`

**Produces:** host-bearing loop records and safe host filtering for legacy data.

- [ ] Write failing tests showing a remote-host job/loop is skipped and a
  legacy loop resolves its owner hostname only when unambiguous.
- [ ] Run RED tests.
- [ ] Add host persistence/filtering, safely skip unassigned legacy work, and
  retain local dispatch behavior.
- [ ] Run all cron tests, build `dist/camc`, smoke the bundle help/list paths,
  and run `git diff --check`.
