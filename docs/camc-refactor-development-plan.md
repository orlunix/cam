# camc Refactor Development Plan

> Status: active development plan / draft for implementation sequencing.
> Date: 2026-06-26.
> Scope: `camc` first. This plan is about the standalone single-machine CLI/runtime (`src/camc_pkg/`, built to `src/camc` / `dist/camc`). It does not require redesigning the higher-level `cam` server first.

## 1. Problem Statement

`camc` has grown into a single-file deployment artifact of roughly 18k lines. That size is not itself the bug: `src/camc` and `dist/camc` are generated bundles, and most of the lines come from real source modules plus embedded adapter/proxy/runtime logic.

The real architectural issue is that `camc` currently mixes three different roles behind one process entrypoint:

| Role | Examples | Runtime shape | Current issue |
|---|---|---|---|
| Control plane | `run`, `rm`, `heal`, `cron add`, `api`, `machine`, `context` | Full Python CLI is acceptable | `cli.py` still owns too much feature logic |
| Data plane / hot path | `capture`, simple `send --text`, possible future status-lite/mailbox-lite | Should be near raw tmux / file-index cost | Full Python startup is too expensive for tiny repeated operations |
| Long-running agents | monitor loop, cron tick/run workers, API proxy | Should run in-process or as owned subprocesses | Lifecycle and ownership need clear boundaries |

The current fast-path work is correct in direction: `capture <exact-id>` and simple `send <exact-id> --text ...` are tiny data-plane operations and should not pay the full Python startup/import cost when the tmux metadata is already available in `agents.json`.

## 2. Current State Snapshot

Approximate current source/build size from the working tree:

| Component | Approx LOC | Notes |
|---|---:|---|
| `src/camc` / `dist/camc` | 17,491 each | Generated single-file artifact; do not edit directly |
| `src/camc_pkg/cli.py` | 6,840 | Main command handlers + parser; biggest maintainability issue |
| `src/camc_pkg/cron.py` + `cron_loop.py` | 2,005 | Host cron + per-agent loop support |
| `src/camc_pkg/proxy/*` + API modules | ~2,800 | Custom API/proxy support for Claude/Codex |
| `src/camc_pkg/monitor.py` + `monitor_features.py` | ~1,030 | Long-running observer loop |
| `transport.py` + `runtime_env.py` | ~1,300 | tmux/tool/env hardening; important for PDX/DC stability |
| `fast_capture.py` + prelude hooks | ~600 | Data-plane fast path; reads `agents.json` directly; new and release-sensitive |

Important distinction: feature/core modules have already been partially split (`cron.py`, `cron_loop.py`, `proxy/*`, `monitor*`, `transport.py`, `runtime_env.py`). The CLI adapter layer is not split yet: `cli.py` still contains many feature command implementations (`cmd_msg_*`, `cmd_cron_*`, `cmd_api_*`, `cmd_archive_*`, etc.) plus all parser registration.

## 3. Fast Data-Plane Experiment Update

### E-01: Use `agents.json` as the fast-path source of truth

The first fast-capture experiment introduced `~/.cam/capture.idx` as a small
side index. Review showed that this adds management cost and creates a new
release-sensitive consistency problem: generated single-file builds must keep
the side index in sync on every agent save/update/remove.

The updated experiment removes the side index. The prelude hook now performs a
very narrow best-effort lookup in the existing `~/.cam/agents.json` file. This
keeps one source of truth and avoids adding a new runtime data structure.

Contract:

- Support `camc capture <exact-8hex-id>` with simple `--lines/-n` and
  `--format plain|ansi` flags.
- Support `camc send <exact-8hex-id> --text/-t TEXT [--no-enter]` for
  short single-line text only. `--file`, `--stdin`, multiline text, long text,
  names, and prefixes remain on the Python path.
- Assume `agents.json` is written by camc using `json.dump(..., indent=2)`.
- Extract only top-level `id`, `hostname`, `tmux_session`, legacy `session`,
  `tmux_socket`, and `tmux_bin`.
- If `tmux_socket` is empty, probe standard camc socket locations such as
  `/tmp/cam-sockets/<session>.sock`.
- If parsing fails, the host does not match, the session is missing, or tmux
  fails, fall back to the normal Python path.
- Do not claim to be a general JSON parser. This is a controlled fast-path
  extractor for camc-owned JSON only.

Validation already run for the extractor before implementation:

| Data source | Checked | Failures |
|---|---:|---:|
| Local real `~/.cam/agents.json` | 23 | 0 |
| PDX real `~/.cam/agents.json` | 68 | 0 |
| Synthetic standard / legacy / nested / missing-session cases | all | 0 |

Unsupported minified JSON intentionally misses and falls back to Python. Simple `send --text` uses the same metadata lookup, writes through `tmux send-keys -l`, and preserves the existing `Sent.` stdout contract. When `--no-enter` is absent, the prelude sends a trailing Enter after the text, matching the normal CLI behavior for short single-line inputs.

End-to-end timing validation on PDX agent `9e118b64`
(`pdx-container-xterm-098`, 20 iterations, `--lines 80`) after the
`agents.json` prelude implementation:

| Path | Min | Median | Mean | Max |
|---|---:|---:|---:|---:|
| raw `/bin/tmux capture-pane` | 0.000s | 0.000s | 0.011s | 0.070s |
| candidate `camc capture 9e118b64 --lines 80` | 0.010s | 0.080s | 0.063s | 0.090s |
| current installed `/home/hren/.cam/camc capture ...` | 1.090s | 1.130s | 1.176s | 1.400s |

This confirms that default exact-id capture can be fast without adding a
separate `capture.idx`: the generated shell prelude resolves tmux metadata
from the existing `agents.json` and falls back to Python when the record shape
is unsupported. The same lookup is now reused for conservative `send --text`
fast-path handling. Use `--no-fast-path` on capture/send when a caller needs to
force the original Python path for debugging or rollback.

Required release checks:

1. Generated `dist/camc capture <id>` must work from `agents.json` without
   `capture.idx`.
2. Generated `dist/camc send <id> --text ...` and `--no-enter` must work from
   `agents.json` for short single-line text.
3. `--no-fast-path` on capture/send must force the Python path and preserve argv.
4. Agents-json fast paths must still succeed with invalid `PYTHONHOME`,
   proving Python was not started.
5. Missing/unsupported records and unsupported send forms must fall back with
   original argv preserved before any tmux side effect.
6. PDX timing should remain in the tens-of-milliseconds range for a valid
   exact-id capture.

## 4. Architecture Direction

### 4.1 Control Plane vs Data Plane

`camc` should remain the canonical control-plane CLI, but hot data-plane operations must be explicitly designed.

Control-plane commands should stay in normal Python:

- lifecycle: `run`, `rm`, `kill`, `reboot`, `heal`, `prune`
- configuration: `api`, `machine`, `context`, `sync`, `env`
- scheduling writes: `cron add/rm`, loop add/rm
- complex protocols: `msg send/reply`, archive, migrate

Data-plane commands may use prelude hooks or small helper entrypoints when they meet all criteria:

- high-frequency or latency-sensitive
- tiny operation with stable argument shape
- required metadata available in a small index/cache
- safe fallback to Python for unsupported forms
- no complex writes, no adapter/proxy/API semantics

Current approved data-plane candidates:

- `capture <exact-8hex-id>` with an `agents.json` fast-path hit.
- `send <exact-8hex-id> --text/-t TEXT [--no-enter]` for short single-line text.

Possible future candidates only if measurements justify them:

- `status-lite <exact-id>` for Desktop sort/health refresh.
- `list-lite --json` from a precomputed snapshot if Desktop polls frequently.
- `mailbox-next <agent-id>` if monitor/Desktop needs high-frequency mailbox scanning.

Do not move complex commands into shell just because they are sometimes slow. `run`, `heal`, `api`, `cron add`, and `msg send/reply` are control-plane operations.

### 4.2 Registered Prelude Hooks

The prelude hook system is the right shape for the shell fast path:

- Hooks live under `src/camc_pkg/prelude/`.
- `manifest.json` registers enabled hooks.
- `build_camc.py` renders hooks into the generated polyglot prelude.
- `CAMC_PRELUDE_DISABLE=capture` disables the current tmux data-plane hook (capture + simple send text), and `CAMC_PRELUDE_DISABLE=all` disables all hooks at build time.

Hook return protocol:

| Code | Meaning |
|---:|---|
| `0` | handled success; generated `camc` exits 0 |
| `1` | not handled; try next hook or fall back to Python with original argv |
| `2` | usage error; exit 2 |
| other | handled failure; exit with that code |

Rule: there is no user-facing `--fast` enable switch. Fast path is automatic for the narrow supported forms, and `--no-fast-path` is the runtime escape hatch that forces the original Python path.

## 5. CLI Restructuring Plan

`cli.py` should become a thin parser/dispatcher, not a feature container.

Target module shape:

```text
src/camc_pkg/cli.py                 # main parser, common argument helpers, dispatch
src/camc_pkg/commands/run.py         # run/status/list/lifecycle CLI shims
src/camc_pkg/commands/capture.py     # capture/send/key CLI shims
src/camc_pkg/commands/msg.py         # msg parser + command shims
src/camc_pkg/messaging.py            # mailbox/thread service + ledger store
src/camc_pkg/commands/cron.py        # cron parser + command shims
src/camc_pkg/commands/api.py         # API command shims
src/camc_pkg/commands/archive.py     # archive command shims
src/camc_pkg/commands/machine.py     # machine/context/sync command shims
```

Refactor rule:

- Parser functions register arguments and call a service function.
- Service modules own behavior and tests.
- Do not change user-visible CLI syntax during this refactor.
- Keep Python 3.6 and stdlib-only constraints.
- Rebuild `src/camc` / `dist/camc` after each slice.

This refactor may not reduce generated line count immediately. Its purpose is reducing coupling, not shrinking the artifact at all costs.

## 6. Deletion / Archive Candidates

Deletion must be evidence-based. Candidate list for review, not immediate removal:

| Candidate | Why it may be removable | Required check before removal |
|---|---|---|
| `camc apply` / `scheduler.py` | DAG scheduling may belong to camflow, not camc | Search docs/tests/users; provide replacement path |
| Legacy `msg show/list/wait` | V0 mailbox API prefers `msg read/reply` | Keep one compatibility window; update skills/docs first |
| `machine/context/sync` | May overlap with newer remote/deploy flow | Confirm Desktop/remote workflows still depend on it |
| Archive summary/show internals | Large and specialized | Confirm UI/scripts/skills usage |
| Old migration helpers | One-time migration code may be stale | Confirm no active deployments need migration |

Do not delete:

- `runtime_env.py`, `transport.py`, tmux config hardening, or tool resolution. These protect PDX/DC stability.
- `monitor.py` / `monitor_features.py`; monitor is a core long-running subsystem.
- API/proxy modules while Claude/Codex custom API support is active.
- Fast data-plane prelude without replacing the latency solution.

## 7. Phased Implementation Plan

### P0 — Land agents.json fast data-plane correctness

- Remove the `capture.idx` side-index path from build/runtime code.
- Use a narrow shell extractor over camc-owned `agents.json` for exact-id capture and simple send text.
- Remove the public `--fast --tmux-*` capture surface; keep only automatic exact-id fast path plus `--no-fast-path` rollback.
- Add generated-artifact tests/smokes.
- Re-run PDX timing: agents-json fast capture should remain tens of milliseconds.

Exit criteria:

- `dist/camc capture <id>` succeeds with bad `PYTHONHOME` when `agents.json` has valid metadata.
- `dist/camc send <id> --text ...` succeeds with bad `PYTHONHOME` for both Enter and `--no-enter`.
- `--no-fast-path` for capture/send forces Python fallback with original argv preserved.
- Missing/unsupported `agents.json` and unsupported send forms fall back to Python with original argv preserved before side effects.
- Full pytest passes.

### P1 — Document and freeze hot-path contract

- Keep `docs/camc-refactor-development-plan.md` current.
- Update `docs/camc-spec.md` module map and doc map when slices land.
- Add release checklist item: no publish unless local and PDX capture fast path are verified.

Exit criteria:

- Release checklist includes prelude/index smoke.
- Skills/docs describe fast capture as an implementation detail, not a separate user workflow.

### P2 — Split CLI adapter layer by feature

Suggested order:

1. `commands/msg.py` + `messaging.py` because msg logic is currently dense and protocol-sensitive.
2. `commands/cron.py` because cron/loop already have service modules.
3. `commands/api.py` because API proxy logic is already mostly modular.
4. `commands/archive.py` because it is bulky and separable.
5. `commands/machine.py` / `commands/context.py` / `commands/capture.py`.

Exit criteria:

- `cli.py` owns parser/dispatch only.
- Each feature has focused tests against service modules.
- Generated `camc` behavior unchanged.

### P3 — Store/service cleanup

- Introduce a small shared store helper for fcntl lock + atomic JSON rewrite.
- Move `messages.jsonl` helpers out of `cli.py` into `messaging.py`.
- Make best-effort side indexes explicit and observable instead of silently swallowed.
- Keep core lifecycle resilient: index write failure should warn/event, not prevent agent start.

Exit criteria:

- `AgentStore`, cron job stores, machine/context stores use consistent locking patterns where practical.
- Message ledger operations are service-level APIs.

### P4 — Measure before adding more fast paths

Add or preserve simple benchmark recipes for:

- raw tmux capture
- `camc capture <id>` indexed fast
- `camc capture <id>` fallback
- `camc msg list/read`
- fake `camc run`
- Desktop output endpoint if applicable

Only add more prelude hooks if a command is both hot and safely indexable.

### P5 — Deprecate/remove with migration windows

For each removal candidate:

1. Mark deprecated in docs/help.
2. Update skills and Desktop/mobile callers.
3. Keep a compatibility release window.
4. Remove code only after usage is gone or explicitly accepted.

## 8. Testing and Release Gates

Minimum gates for camc releases touching runtime/build/fast path:

```bash
python3 -m py_compile build_camc.py src/camc_pkg/*.py
python3 build_camc.py
cp dist/camc src/camc
chmod +x dist/camc src/camc
cmp dist/camc src/camc
python3 -m pytest -q
git diff --check
```

Additional required smoke for data-plane/prelude changes:

- local generated fast capture reads `~/.cam/agents.json` without `capture.idx`
- local indexed capture succeeds with invalid `PYTHONHOME`
- local indexed `send --text` succeeds with invalid `PYTHONHOME` for Enter and `--no-enter`
- `--no-fast-path` capture/send forces Python fallback
- no-index capture and unsupported send forms fall back without argv corruption before side effects
- PDX candidate indexed capture median remains in tens of milliseconds

Do not publish to shared paths until these pass locally and on the target PDX/DC environment when the change affects tmux/tool/runtime behavior.

## 9. Non-Goals

- Do not optimize by blindly moving complex commands into shell.
- Do not delete code solely to reduce `dist/camc` line count.
- Do not break Python 3.6 or stdlib-only deployment.
- Do not make Desktop depend on private implementation details if a stable camc API can provide the same behavior.
- Do not hide release blockers behind broad `except Exception: pass` without tests that catch the failure mode.
