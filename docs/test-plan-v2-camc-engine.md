# Test Plan: CAM Architecture v2 (camc as Engine)

> Date: 2026-03-25
> Corresponds to: [architecture-v2-camc-engine.md](./architecture-v2-camc-engine.md)

## 1. Overview

This test plan ensures no feature loss or regression during the architecture v2 migration. Each phase in the architecture doc has a corresponding test section below.

Testing happens at three levels:
1. **Unit tests** — pytest on the `src/camc/` package (multi-file)
2. **Build verification** — pytest on the built single-file `camc` (same tests, different target)
3. **Integration tests** — real tmux sessions, real SSH, real agent launches

## 2. Pre-Migration Baseline

Before any refactoring, capture the current behavior as the baseline.

### 2.1 Snapshot current camc behavior

```bash
# Record current outputs for diff comparison after refactor
camc --help > baseline/help.txt
camc run --help > baseline/run-help.txt
camc list --help > baseline/list-help.txt
camc --json list > baseline/list-json.txt
camc version > baseline/version.txt
```

### 2.2 Existing cam test suite

```bash
pytest                          # 444 tests must still pass
pytest tests/test_camc.py       # camc-specific tests
```

Any test that fails before migration is documented as pre-existing (currently 5 probe-related failures).

## 3. Phase 0: Package Split (no new features)

> Ref: architecture-v2 §9 "Migration Path"

Goal: Split `src/camc` single file into `src/camc/` package. Build script merges back to single file. **Zero behavior change.**

### 3.1 Package unit tests

```
tests/test_camc/
  test_cli.py              # argparse, command dispatch
  test_storage.py          # agents.json read/write, file lock, prefix match
  test_transport_local.py  # tmux create/capture/send/kill
  test_detection.py        # state patterns, completion, auto-confirm
  test_monitor.py          # monitor loop, crash restart, auto-exit
  test_probe.py            # idle probe
  test_adapters.py         # TOML parse, config load (claude/codex/cursor)
  test_models.py           # agent data structures
```

### 3.2 Build verification

```bash
python build_camc.py                    # Merge to single file
diff <(python src/camc --help) <(python dist/camc --help)    # Must be identical
pytest tests/test_camc/ --camc-binary=dist/camc              # Same tests, built file
```

### 3.3 CLI output regression

Every `camc` subcommand must produce identical output before and after the split:

| Command | Verify |
|---------|--------|
| `camc --help` | diff with baseline |
| `camc run --help` | diff with baseline |
| `camc list --help` | diff with baseline |
| `camc list` | same table format |
| `camc --json list` | same JSON schema (field names, types, nesting) |
| `camc --json status <id>` | same JSON schema |
| `camc version` | same output |
| `camc logs <id>` | same output |
| `camc logs <id> -f` | same follow behavior |

### 3.4 Functional regression (mock tmux)

| Test | What it verifies |
|------|-----------------|
| `test_run_creates_session` | tmux session created with correct command, workdir, env |
| `test_run_inherit_env` | Default mode: tmux started without command, send-keys used |
| `test_run_no_inherit_env` | Legacy mode: bash -c wrapping with env_setup |
| `test_run_prompt_after_launch` | Startup wait → auto-confirm → ready detect → send prompt |
| `test_run_auto_exit` | auto_exit flag stored and passed to monitor |
| `test_list_empty` | "No agents." when agents.json is empty |
| `test_list_table_format` | Column headers, alignment, color codes |
| `test_list_json_format` | cam-compatible JSON (task.name, task.tool, tmux_session, etc.) |
| `test_list_status_filter` | `--status running` filters correctly |
| `test_list_last_n` | `--last 5` limits output |
| `test_stop_agent` | Kills monitor, kills tmux, updates status |
| `test_kill_agent` | Same as stop but exit_reason says "Force killed" |
| `test_logs_output` | Captures tmux pane content |
| `test_logs_follow` | Polls and refreshes output |
| `test_logs_tail` | `--tail 20` limits capture lines |
| `test_attach_by_id` | Resolves agent, execs tmux attach |
| `test_attach_no_id` | Attaches to most recent running agent |
| `test_status_detail` | Shows full agent info for single agent |
| `test_status_json` | JSON output in cam-compatible format |
| `test_status_hash` | Returns `{"unchanged": true}` when hash matches |
| `test_rm_agent` | Removes from agents.json |
| `test_rm_kill` | Removes and kills session |
| `test_add_session` | Adopts existing tmux session, spawns monitor |
| `test_heal_restart_monitor` | Detects dead monitor PID, restarts |
| `test_heal_mark_dead_session` | Session gone → mark completed/failed |
| `test_init_creates_configs` | Writes TOML files to ~/.cam/configs/ |
| `test_version_output` | Shows version and supported tools |

### 3.5 Storage regression

| Test | What it verifies |
|------|-----------------|
| `test_agents_json_roundtrip` | Save → load → same data |
| `test_file_lock` | Concurrent writes don't corrupt |
| `test_short_id_match` | `store.get("a889")` finds `a889245f` |
| `test_update_agent` | Partial update preserves other fields |
| `test_remove_agent` | Agent deleted from file |

### 3.6 Adapter regression

| Test | What it verifies |
|------|-----------------|
| `test_load_claude_config` | Parses claude.toml correctly |
| `test_load_codex_config` | Parses codex.toml correctly |
| `test_load_cursor_config` | Parses cursor.toml correctly |
| `test_embedded_configs` | Embedded TOML strings match file configs |
| `test_state_patterns` | Each pattern matches expected input |
| `test_completion_detection` | Prompt count strategy works |
| `test_auto_confirm_patterns` | Trust dialog, permission, y/n patterns match |
| `test_probe_config` | Probe char, wait time, threshold loaded correctly |

### 3.7 Monitor regression

| Test | What it verifies |
|------|-----------------|
| `test_monitor_poll_cycle` | capture → detect state → check confirm → check completion |
| `test_monitor_auto_confirm` | Sends response with cooldown |
| `test_monitor_completion` | Detects completion, marks agent done |
| `test_monitor_auto_exit` | Kills session after completion when auto_exit=true |
| `test_monitor_crash_restart` | Restarts up to 5 times with backoff |
| `test_monitor_session_died` | Marks agent completed/failed when session gone |

## 4. Phase 1: Event History

> Ref: architecture-v2 §9 "Phase 1"
>
> Note: camc is a pure local tool. SSH transport lives in cam serve, not camc.
> cam serve SSHes into remote machines and calls `camc --json ...` commands.

### 4.1 Event history tests

| Test | What it verifies |
|------|-----------------|
| `test_events_append` | Monitor appends to events.jsonl |
| `test_events_state_change` | State transitions recorded |
| `test_events_auto_confirm` | Auto-confirm events recorded |
| `test_events_completion` | Completion event recorded |
| `test_history_command` | `camc history <id>` shows filtered events |
| `test_history_json` | `camc --json history <id>` outputs JSON array |
| `test_history_since` | `--since <timestamp>` filters by time |
| `test_events_rotation` | Old events cleaned up (>30 days) |

### 4.3 Backward compatibility

```bash
# All Phase 0 tests must still pass
pytest tests/test_camc/

# cam still works as-is
pytest tests/                   # Full cam test suite
cam list                        # Still works, no change
cam run -t claude "test"        # Still works via direct tmux management
```

## 5. Phase 2: DAG Scheduler

> Ref: architecture-v2 §9 "Phase 2"

### 5.1 DAG tests

| Test | What it verifies |
|------|-----------------|
| `test_dag_parse_yaml` | Reads task YAML with defaults, tasks, depends_on |
| `test_dag_topo_sort` | Correct topological ordering |
| `test_dag_cycle_detection` | Rejects cyclic dependencies |
| `test_dag_missing_dep` | Rejects reference to non-existent task |
| `test_dag_parallel` | Independent tasks run concurrently |
| `test_dag_serial` | Dependent tasks wait for predecessors |
| `test_dag_failure_stops` | Downstream tasks skipped when dependency fails |
| `test_dag_yaml_compat` | Same YAML format as cam's `cam apply` |

### 5.2 Backward compatibility

```bash
# Existing cam apply YAML files work with camc apply
cam apply tasks.yaml            # Still works
camc apply tasks.yaml           # Same result

# All previous phase tests pass
pytest tests/test_camc/
pytest tests/
```

## 6. Phase 3: cam Delegates to camc

> Ref: architecture-v2 §9 "Phase 3"

### 6.1 Delegation tests

| Test | What it verifies |
|------|-----------------|
| `test_cam_run_delegates` | `cam run` calls camc internally |
| `test_cam_stop_delegates` | `cam stop` calls camc internally |
| `test_cam_list_reads_camc` | `cam list` data sourced from camc |
| `test_cam_logs_delegates` | `cam logs` output comes from camc |
| `test_cam_heal_delegates` | `cam heal` calls camc heal |

### 6.2 API regression

| Test | What it verifies |
|------|-----------------|
| `test_api_list_agents` | `GET /api/agents` same JSON schema |
| `test_api_agent_detail` | `GET /api/agents/{id}` same JSON schema |
| `test_api_start_agent` | `POST /api/agents` still works |
| `test_api_stop_agent` | `POST /api/agents/{id}/stop` still works |
| `test_api_agent_output` | `GET /api/agents/{id}/output` same format |
| `test_ws_events` | WebSocket events same format |
| `test_relay_passthrough` | Relay REST-over-WS unchanged |

### 6.3 Migration test

```bash
# Simulate migration on test environment
cam migrate --dry-run           # Show what would happen
cam migrate                     # Execute migration
cam list                        # Same agents as before
cam --json list                 # Same JSON as before
cam history                     # Same events as before

# Rollback test
cam migrate --rollback          # Restore direct management
cam list                        # Still works
```

### 6.4 Full interface compatibility matrix

| Interface | Test method | Pass criteria |
|-----------|-------------|---------------|
| cam CLI | diff output before/after | Identical |
| REST API | JSON schema diff | Identical |
| WebSocket | Event format diff | Identical |
| Web UI | Manual smoke test | All pages load, data shows |
| Mobile App | Manual smoke test (no app update) | All features work |
| Teams Bot | Send test command | Same response format |
| camc CLI | diff output before/after | Identical |

## 7. Phase 4: cam Slims Down

> Ref: architecture-v2 §9 "Phase 4"

### 7.1 Removed code verification

| Check | What it verifies |
|-------|-----------------|
| `test_no_direct_tmux` | cam serve code has no direct tmux calls |
| `test_no_agent_manager` | AgentManager removed or empty wrapper |
| `test_no_agent_monitor` | AgentMonitor removed or empty wrapper |
| `test_no_transport_local` | LocalTransport not used for agent management |

### 7.2 Full regression

```bash
# Everything still works
pytest tests/                   # All cam tests
pytest tests/test_camc/         # All camc tests

# Manual smoke test checklist
[ ] cam run — starts agent via camc
[ ] cam list — shows all agents across machines
[ ] cam logs -f — follows agent output
[ ] cam attach — attaches to tmux
[ ] cam stop / kill — stops agent
[ ] cam history — shows events
[ ] cam heal — restarts dead monitors
[ ] cam apply — runs DAG
[ ] Web UI — dashboard, agent detail, start agent
[ ] Mobile App — same features via relay
[ ] cam sync — deploys latest camc to all remotes
```

## 8. Continuous Regression

After migration is complete, ongoing protection:

### 8.1 CI pipeline

```bash
# On every commit:
pytest tests/                         # cam tests
pytest tests/test_camc/               # camc package tests
python build_camc.py                  # Build single file
pytest tests/test_camc/ --camc-binary=dist/camc  # Single file tests
```

### 8.2 Deploy verification

```bash
# After cam sync:
cam sync                              # Deploy to all remotes
cam sync --verify                     # Run camc version + camc --json list on each remote
```

### 8.3 Compatibility gate

Before any release, the full interface compatibility matrix (§6.4) must pass. No release if any interface produces different output than the previous version.

## 9. Test Environment

### Local testing
- Mock tmux via subprocess mock (for unit tests)
- Real tmux for integration tests (temp sessions, auto-cleanup)
- Temp `~/.cam/` directory per test (isolated agents.json)

### Remote testing
- Use `ssh-test` context (localhost SSH) for SSH transport tests
- Use PDX context for real remote integration tests (manual)

### Test fixtures (from existing tests/conftest.py)
- `tmp_db` — temporary SQLite for cam tests
- `sample_context` — test context fixture
- `sample_adapter` — test adapter fixture
- NEW: `tmp_cam_dir` — temporary `~/.cam/` for camc tests
- NEW: `mock_tmux` — mock tmux commands for unit tests
