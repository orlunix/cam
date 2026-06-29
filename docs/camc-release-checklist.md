# camc Release Checklist

This checklist is mandatory before publishing any new `camc` build to a shared path such as `/home/prgn_share/bin/camc`.

The rule is simple: **do not publish to shared if local smoke has not passed on the exact artifact being published.**

## 1. Build Artifact

```bash
python3 build_camc.py
cp dist/camc src/camc
cmp dist/camc src/camc
```

Record the version:

```bash
./dist/camc version
```

Do not publish a dirty or rebuilt artifact unless the dirty source diff is intentional and reviewed.

## 2. Static and Unit Checks

Run focused tests for the touched area plus the baseline camc safety checks:

```bash
python3 -m py_compile src/camc_pkg/*.py
python3 -m pytest tests/test_runtime_env.py tests/test_cmd_run_wrapper_default.py tests/test_camc_hardening_pdx.py -q
python3 -m pytest tests/test_camc_phase1_storage.py -q
```

Run adjacent tests when launch/runtime/adapters changed:

```bash
python3 -m pytest tests/test_configurable_adapter.py tests/test_adapters.py tests/test_custom_auto_confirm_config.py -q
```

Finish with:

```bash
git diff --check
```

If `python3.6` is available on the target class of machines, also run:

```bash
python3.6 -m py_compile src/camc_pkg/*.py
```

## 3. Local Three-Tool Smoke Gate

Before shared publish, the exact local artifact must be able to start all three supported agent tools locally, run a trivial prompt, and exit/cleanup.

Use a disposable directory and unique names:

```bash
SMOKE_DIR=/tmp/camc-release-smoke-$(date +%Y%m%d-%H%M%S)
mkdir -p "$SMOKE_DIR"
```

Run these with `./dist/camc` or the exact candidate path, not an older `camc` from `PATH`:

```bash
./dist/camc run -t claude -n smoke-claude --path "$SMOKE_DIR" --auto-exit "Reply exactly: CAMC_SMOKE_OK"
./dist/camc run -t codex  -n smoke-codex  --path "$SMOKE_DIR" --auto-exit "Reply exactly: CAMC_SMOKE_OK"
./dist/camc run -t cursor -n smoke-cursor --path "$SMOKE_DIR" --auto-exit "Reply exactly: CAMC_SMOKE_OK"
```

For each agent, verify:

```bash
./dist/camc list
./dist/camc capture <id> --lines 80
./dist/camc status <id>
```

Pass criteria:

- agent tmux session starts and remains manageable by camc
- prompt is delivered
- output contains the expected simple reply or equivalent successful completion signal
- agent exits or is cleaned up without stale tmux socket errors
- `camc status <id>` does not show `failed` due to launch/runtime failure

Cleanup:

```bash
./dist/camc rm <id> --kill
```

If any of Claude, Codex, or Cursor cannot be started locally, **do not publish to shared**. Fix the local issue first or explicitly mark the release as not eligible for shared deployment.

## 4. Shared Publish Guard

Never copy directly over `/home/prgn_share/bin/camc` as the first deployment step.

Preferred flow:

```text
1. copy candidate to a versioned release file
2. smoke test the versioned file on the target host class
3. atomically update the shared symlink/pointer
4. keep the previous known-good artifact for rollback
```

Example shape:

```text
/home/prgn_share/tools/camc/releases/camc-<commit>-<date>
/home/prgn_share/bin/camc -> ../tools/camc/releases/camc-<known-good>
```

Shared publish is allowed only after:

- local three-tool smoke passed
- target node tmux sanity passed
- candidate artifact version was recorded
- rollback target is known

## 5. Target Node Sanity

On PDX/DC style hosts, verify tmux isolation before publishing:

```bash
/bin/tmux -f /dev/null -S /tmp/camc-release-smoke.sock new-session -d -s camc-release-smoke
/bin/tmux -S /tmp/camc-release-smoke.sock has-session -t camc-release-smoke
/bin/tmux -S /tmp/camc-release-smoke.sock kill-server
```

The candidate `camc` should use its camc-owned tmux config and private socket. User `~/.tmux.conf` must not be required for the smoke to pass.

## 6. Failure Policy

If a publish attempt fails at any gate:

- stop
- do not update shared path
- keep the existing shared `camc` unchanged
- record the failed command and output
- fix and rerun the full checklist from the top

If a bad shared publish happened anyway:

- immediately restore the previous known-good shared artifact
- verify `camc version` on the affected host
- run one local smoke agent before resuming deployment
