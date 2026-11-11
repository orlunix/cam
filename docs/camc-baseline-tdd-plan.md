# CamC baseline TDD plan

Status: implementation plan for review

## Goal

Protect the five CamC baseline capabilities with a small, deterministic,
automated test suite:

1. `run`
2. `rm`
3. `msg`
4. `api`
5. auto-confirm

The tests define observable behavior. They must not duplicate production
implementation, inspect source text, call a real model/provider, or depend on
wall-clock sleeps.

## Current-state findings

- The authoritative development repository is the GitLab `cam` repository on
  branch `camui-desktop-v2`; the shared `tools/cam` GitHub clone is older.
- The development worktree currently contains unrelated, uncommitted API/proxy
  work. Baseline TDD development therefore uses an isolated branch/workspace.
- Clean `69038fd` cannot collect the existing suite because `cli.py` imports
  `tmux_submit_input`, while the helper exists only in the current uncommitted
  transport change. That dependency must be committed or carried as an
  explicitly excluded prerequisite before the TDD commit can be integrated.
- Existing coverage is broad but fragmented. Some tests correctly exercise
  public behavior; others mirror inline implementation or repeat the same
  pattern at several layers. A green test count alone does not establish the
  baseline contract.
- The current dirty worktree collects 1,168 tests, but an available Python
  3.12 environment stalls at the first legacy `cam` server API health test
  after 99 passes (more than 75 seconds). That server test is outside the
  standalone CamC baseline and must be reported separately rather than hidden
  inside the baseline gate.
- CamC production modules and the generated single-file artifact remain
  Python 3.6+, standard-library-only. Tests may run on the project test Python,
  but every candidate must also pass a real Python 3.6 compile/import gate.

## Test architecture

### Layer 1: public command contracts

Create one focused file per baseline capability under `tests/baseline/`.
Invoke the argparse/public command boundary with an isolated `CAM_HOME`, fake
tool executable, private tmux/socket facade, deterministic clock, and local
HTTP stub where needed.

| Test file | Required behavior |
|---|---|
| `test_run_contract.py` | default/explicit tool selection; preflight before mutation; one session and one monitor; prompt delivered once; durable record created only after successful launch; launch failure cleans owned resources; API selection is bound into the record |
| `test_rm_contract.py` | exact target resolution; one target only; owned monitor/tmux/socket cleanup; record removal; archive only when requested; missing target is a clear nonzero result; sibling agents untouched |
| `test_msg_contract.py` | ledger write precedes best-effort pane injection; one prompt submission; no-wait behavior; same-thread reply sequencing; read/mark semantics; delivery failure remains traceable; retry does not duplicate a turn |
| `test_api_contract.py` | list/check/default; alias resolves to canonical model; tool/endpoint compatibility; disabled/invalid profile rejection; secret precedence without secret output; selected profile reaches `run`; no real provider/network access |
| `test_autoconfirm_contract.py` | supported confirmation dialogs; boot-only trust prompts; recent-tail/cursor guards; cooldown/debounce; exactly one response; normal prose and stale dialogs never confirm; monitor reloads updated policy |

### Layer 2: focused implementation tests

Retain small unit tests for parsers, state machines, stores, endpoint
resolution, and adapter pattern matching when they add failure localization.
These tests support Layer 1; they do not replace it.

### Layer 3: built-artifact smoke

Build `dist/camc`, verify it matches the modular source behavior, and run an
offline lifecycle smoke with fake Claude/Codex/Cursor executables and a private
tmux socket:

```text
run -> prompt observed -> auto-confirm observed -> msg round trip -> rm
```

No network, login, provider token, existing `~/.cam`, or user tmux server may
be used. A real-provider smoke remains an explicit release test, not a unit or
CI gate.

### Layer 4: explicit real-provider regression

Run the built candidate against a real coding agent and the inexpensive
internal `kimi-k3` profile:

```bash
python3 scripts/camc_live_api_regression.py --camc /path/to/candidate/camc
```

This command validates the real lifecycle end to end:

```text
run --api -> trust auto-confirm -> task and tests -> msg -> rm --archive
```

It uses a fresh temporary workspace, requires exactly one `auto_confirm` and
one prompt-sent `boot_ready`, independently verifies the generated program,
and cleans up the owned agent. It is intentionally not collected by `pytest`;
the command reaches a real provider and the user's live CamC registry. Its
default can be overridden with `--api` or `CAMC_LIVE_API`.

## Determinism rules

- Inject/fake time; no test should wait for production 5/60/120-second timers.
- Use temporary `CAM_HOME`, workdir, socket, logs, and API config per test.
- Never inspect or modify the user's live agent registry.
- Assert durable facts: process/session calls, ledger rows, records, exit code,
  stdout/stderr, and cleanup. Do not assert prose that is not part of the CLI
  contract.
- Each failure path proves cleanup and absence of duplicate side effects.
- Run the baseline suite repeatedly (10 consecutive runs) to catch order and
  timing dependence.

## Old-test cleanup policy

Do not delete a test because it is old or long. Delete or consolidate it only
when all of the following are recorded:

1. The invariant it claims to protect is identified.
2. A new baseline or focused test objectively covers that invariant.
3. Removing it does not reduce Python 3.6, security, cleanup, or compatibility
   coverage.
4. The old test is duplicate, implementation-mirroring, source-text-based,
   or tied to a removed feature.

Initial review candidates, not pre-approved deletions:

- `tests/test_cmd_run_wrapper_default.py`: explicitly mirrors a block from
  `cmd_run` instead of exercising the command.
- source-reading assertions in `tests/test_camc_hardening_site-a.py` and
  `tests/test_camc_activity_timestamps.py`.
- repeated auto-confirm examples spread across `test_camc.py`,
  `test_adapters.py`, `test_configurable_adapter.py`, and
  `test_monitor_loop.py`; retain one contract vector plus genuinely distinct
  unit/state-machine cases.
- legacy `msg` pane-scrape tests after ledger-first behavior has equivalent
  contract coverage; preserve explicitly supported compatibility behavior.

The implementation change must include an `old test -> replacement test ->
preserved invariant` table. Unmapped deletion is rejected.

## Acceptance gates

1. Five baseline contract files exist and pass independently.
2. The combined baseline suite passes 10 consecutive runs without retry.
3. The full retained standalone-CamC suite passes once. The wider `cam`
   project suite is also run and reported, but an unrelated optional server/UI
   dependency failure is not silently reclassified as a CamC baseline failure.
4. `src/camc_pkg/*.py` compiles with the project's normal test Python and an
   actual Python 3.6 interpreter.
5. The generated `dist/camc` passes the offline built-artifact smoke.
6. Test cleanup has a complete replacement map and does not change production
   behavior.
7. No baseline test reaches a real API, real user agent, live registry, or
   shared tmux server.

## Non-goals

This slice does not redesign `machine`, `sync`, `context`, cron/loop, remote
fleet management, the proxy architecture, or production command semantics.
Defects exposed by the new tests are fixed only with the smallest reviewed
testability/correctness change.
