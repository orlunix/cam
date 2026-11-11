# Auto launcher verification, 2026-10-11

Source checkout: `/home/scratch.hren_gpu/test/gitlab/cam`.
The initial source verification did not build or install. A later authorized
local build/install is recorded below. No commit or push was performed.

## Implemented behavior

`auto` tries verified current RUN autorun allocation directly, then direct
LSF `battach`, independent SSH, qsub, and finally local without injected
autorun flags or auto-confirm. Each remote target resolves and version-checks
an absolute tool executable. A private executable script handles transport
fallback and exit status without a nested shell expression in the tmux pane.
Once a target starts the tool, later tool failure never launches another agent.

Qsub project creation is deferred until that stage is needed. An empty missing
project uses `nvprojectname save ./ gpu_gr102g_hw_mmplex_riscv`; other project
errors fall back locally. Custom launcher prefixes keep user-owned behavior.

Claude and Codex initial tasks are positional tool arguments on auto/verified
current-allocation paths; the monitor does not paste the task a second time.
Queue waiting does not consume the tool boot timeout. Codex isolated homes
exclude shared daemon control/socket/lock directories; the donor is untouched.

## Real agent results

All tasks requested only an exact reply and prohibited tools/file edits.

| Tool / transport | Agent | Job / host | Actual reply |
| --- | --- | --- | --- |
| Codex / battach | `5023faf9` | `1895876238`, site-a3-sim-g46-033 | `CAMC_CODEX_PRIVATE_DAEMON_OK` |
| Codex / qsub | `0b4c372d` | `1895891607`, site-a3-sim-e14-038 | `CAMC_CODEX_QSUB_PRIVATE_OK` |
| Claude / battach | `c84e08d2` | `1761147411`, site-a3-sim-g46-005 | `CAMC_CLAUDE_BATTACH_INLINE_OK` |
| Claude / qsub | `723a58c6` | `1895876238`, site-a3-sim-g46-033 | `CAMC_CLAUDE_QSUB_INLINE_OK` |

Codex was 0.159.2 with `--yolo`; Claude was 2.1.296 with
`--dangerously-skip-permissions`. Neither test needed manual Enter submission.
Qsub tests used a temporary source entrypoint suppressing reuse discovery to
reach the real qsub stage; transport and agent execution were real, not mocked.
Terminal evidence is saved in `/tmp/camc-auto-e2e-20261011/<agent-id>.txt`.

Independent SSH probing was also exercised live, with battach discovery
disabled in the temporary entrypoint. Existing account authentication or host
key verification failed on all discovered hosts; auto submitted qsub instead.
Claude agent `933b1f8c`, job `1895897686`, returned `CAMC_CLAUDE_SSH_OK` via that
qsub fallback. This does **not** certify successful SSH agent execution.
No SSH key/known-host policy was changed. Current-allocation direct and final
local fallback have focused test coverage, not new live agent verification.
Cursor launcher execution was not tested; use `--tool-launcher none` for now.

Cleanup completed: all twelve test agents were stopped and their tmux sessions
removed. Seven new qsub test jobs are DONE/EXIT. The pre-existing reused jobs
`1761147411` and `1796124140` remain RUN; the original launcher-smoke session
was preserved.

## Automated checks

442 passed in the focused launcher, adapter, runtime-home, runtime-env, run/API,
and monitor suites. Python 3.6 grammar checks and changed-file diff checks pass.

The full `tests` run returned 1310 passed, 93 failed, 2 skipped, 1 teardown
error. It is not green. Checked failure examples include the environment's
missing async pytest plugin, Cursor boot trust expectations, existing rm mock
signatures rejecting `cleanup_home`, and tmux probe timeouts. No claim is made
that every full-suite failure is pre-existing. Recheck logs:
`/tmp/camc-auto-full-failures-20261011.log` and
`/tmp/camc-auto-independent-failures-20261011.log`.

Focused command:

```sh
PYTHONPATH=src python3 -m pytest -q tests/test_tool_launcher.py tests/test_adapters.py tests/test_configurable_adapter.py tests/test_runtime_env.py tests/test_runtime_home.py tests/test_arbitrary_tool.py tests/test_camc_api.py tests/baseline/test_run_contract.py tests/test_monitor_features.py
```

Review the changes with `git diff -- src/camc_pkg/tool_launcher.py
src/camc_pkg/cli.py src/camc_pkg/monitor.py src/cam/adapters/configs/codex.toml
tests/test_tool_launcher.py tests/test_runtime_home.py tests/test_runtime_env.py
tests/baseline/test_run_contract.py src/camc_pkg/skills/managing-camc/SKILL.md`.
Those files also contain earlier workspace modifications; unrelated dirty
changes were preserved.

## Authorized local update

Built v1.2.158 from the checkout (`9f90391-dirty`, 2026-10-11 16:30).
161 artifact/build/runtime-home/launcher tests passed. Python 3.6 grammar and
source/artifact version agree. The build's `--verify` matched all three help
commands but reports a version text mismatch because source says `(dev)` and
the artifact has its build stamp; the release version itself matches.

The exact built artifact started Codex `50d2050b` and Claude `ef8f6be1` via
battach job `1796124140`. They automatically returned `CAMC_BUILD158_CODEX_OK`
and `CAMC_BUILD158_CLAUDE_OK`. Evidence: `/tmp/camc-build158-evidence/`.
Both test agents were then exited and their tmux sessions removed.

Atomically updated `/home/hren/.cam/camc` and
`/home/scratch.hren_gpu/doc/riscv-ias/doc-template/tools/venv/bin/camc`.
Verified those files and existing aliases `/home/hren/.local/bin/camc`,
`/home/hren/bin/camc`, `/home/scratch.hren_gpu/bin/camc` report v1.2.158 and
have the same SHA256:
`0693986afc6032b19f28659f90de1fc106b1328ad85da8d54afa6135995e16cb`.
Old copies: `/home/hren/.cam/backups/camc-local-158-20261011-163203/`.
Shared `/home/site_share/bin/camc` remains v1.2.155; no shared publication.

## Reboot integration, v1.2.159

`reboot` and its `migrate` alias accept `--tool-launcher`. With no explicit
option, they inherit saved auto/none selection; older records use the same
NV-auto/non-NV-none default as run. Custom prefixes must be resupplied.
Auto reboot resolves targets before stopping the old process and fails closed
instead of launching locally when the remote chain is exhausted. Explicit
`none` remains a local override. API rejection occurs before stopping.

Remote Codex lacking a bound session queries the idle pane's `/status` and
validates the exact UUID's transcript, workdir, and creation time; busy or
pending input is preserved. Reboot persists recovered session IDs, suppresses
duplicate monitor prompt injection, and uses adapter autorun flags once.

Real default reboot tests preserved these sessions on LSF job `1796124140`:

- Claude `18ae6407`, session `18ae6407-0000-0000-0000-000000000000`, replied
  `REBOOT_CLAUDE_AFTER_159` after reboot.
- Codex `33f8b3d7`, session `01a12d56-e3d3-7683-b499-c698fae3f311`, replied
  `REBOOT_CODEX_AFTER_159` after reboot.

An early Codex resume update menu was accidentally submitted during testing;
npm was interrupted and the installed Codex remained 0.159.2. Managed Codex
snapshots now set `check_for_update_on_startup = false`, with no change to the
donor, and the final live reboot proceeded without the menu. Existing owned
overlay daemon links excluded by current policy are also removed safely.

413 related tests passed, then 9 final artifact/build tests passed. Python
3.6 grammar and changed-file diff checks passed. Evidence is in
`/tmp/camc-reboot159-evidence/`. Both temporary agents were cleaned up.

Installed v1.2.159 to both personal canonical files and verified all five local
entry paths. Backup: `/home/hren/.cam/backups/camc-local-159-20261011-164903/`.
Artifact SHA256:
`d353b80f1ab93a4c97eb5180a0277b779a21b79fa1ee2bfd81b3dae4b1e60154`.
Shared deployment, commit, and push were not performed.

Then default `camc reboot dvnet-cc` resumed `ffcd0477` on job `1796124140`,
host `site-a3-sim-g46-042`, using battach and Claude's autorun flag. Status is
running/idle; saved launcher mode is auto, session remains
`3605c0a5-572a-4c27-b919-196c1db5174b`. Its missing nvproject mapping did not
prevent reuse, and no qsub or project-save was needed.

## Allocation command PATH repair, v1.2.161

The live battach probe on job `1796124140` had a nonempty PATH and resolved
git/python3/node/camc, but could not find qsub/nvprojectname/p4/smake/nvip.
The submit login PATH also omitted `/home/nv/bin` and `/home/utils/bin`.

Launcher probes and actual target launches now use the same PATH supplement:
keep the allocation PATH first, append target-visible absolute submit paths
and NVIDIA/LSF tool directories, deduplicate added paths, and reject relative
or transient submit entries. The allocation's loader variables and job ID
remain unchanged; arbitrary submit secrets are not copied.

Real Claude `c0932688` resolved all five commands in its Bash tool and returned
`PATH_CLAUDE_161_OK`. Initial Codex `608eacb0` still failed because its login
command shell reset the prepared PATH. Remote/current-allocation isolated
Codex snapshots now set `allow_login_shell = false`; ordinary local snapshots
and the donor are unchanged. The same Codex session was rebooted, then its
shell_command resolved all five paths and returned `PATH_CODEX_161_FIXED_OK`.
The setting's semantics are documented in the
[official Codex config reference](https://developers.openai.com/codex/config-reference/).

417 related tests and 9 final artifact/build checks passed. Python 3.6 grammar
and diff checks passed. Evidence: `/tmp/camc-path161-evidence/`. Both temporary
agents and the probe script were cleaned up; the reused allocation was not
terminated. Running agents need reboot to adopt the repair.

Installed v1.2.161 to all five local entry paths and site-c's personal
`.cam/camc`, `.local/bin/camc`, and `bin/camc`. site-c Python 3.6.8 compiled the
candidate, ran version/run-help/reboot-help checks, and verified the same hash
before/after atomic promotion. SHA256:
`5de5cfa43e14c74eaa6cf122f4c62d2eab499113665aa826e1e96c499c971ac2`.
Backups: local `~/.cam/backups/camc-local-161-20261011-193106/`; site-c
`~/.cam/backups/camc-before-v1.2.161-20261011-193111`.
No shared release, commit, or push was performed.

## v1.2.162 shared release candidate (2026-10-12)

Native Claude/Codex/Cursor initial prompts now travel once in positional
argv, avoiding a lost pasted Enter. Cursor prefers the company latest
installation before the older site_share copy.

Candidate SHA256: `e976b7fe14d7bc4418366e5d2e4440da637cb970f8bc93f0721589aeaa261873`.
428 release-related tests and 3 built-artifact tests passed. The touched
source/test files pass diff whitespace checks; the pre-existing BUILD_LOG
contains conflict-marker-like text and trailing whitespace, so the whole
worktree diff check is not clean.

Real native launches inside verified allocation 1796124140 produced the
expected Claude and Codex replies. Cursor remains at its login screen even
with company CLI 2026.10.01-e373342. Shared publication is pending the
user's explicit exception to the three-tool smoke gate or successful
Cursor login. No production entry was changed in this release attempt.

The versioned candidate is staged at
`/home/site_share/tools/camc/v1.2.162-9f90391-dirty/camc`; site-c and
node.example.com have `/tmp/camc-v1.2.162-candidate`. Both remote hosts
passed the candidate hash, Python compile and isolated tmux sanity checks.
