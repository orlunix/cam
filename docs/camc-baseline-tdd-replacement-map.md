# CamC baseline TDD replacement map

This map records every test removed or consolidated by the baseline TDD
change. No other existing test is deleted.

| Old test or block | Replacement test | Preserved invariant |
|---|---|---|
| `tests/test_cmd_run_wrapper_default.py::_rewrite_launch_cmd` and `TestLaunchCmdResolvedBinaryRewrite` (nine source-mirroring tests) | `tests/baseline/test_run_contract.py::test_run_selects_tool_and_creates_one_session_and_monitor` (Codex, Claude, and Cursor cases) | The real CLI resolves the selected binary into the correct launch-command slot, preserves the adapter's remaining arguments, does not mutate the configured command, defaults to inherited environment, creates one session and one monitor, and persists the selected tool. Defensive branches that cannot follow a successful preflight were implementation mirrors, not separate public contracts. |
| `tests/test_camc_hardening_pdx.py::_rewrite_launch_cmd` and `TestLaunchCmdRewrite` (three duplicate source-mirroring tests) | `tests/baseline/test_run_contract.py::test_run_selects_tool_and_creates_one_session_and_monitor` | The actual `cmd_run` path rewrites Codex argv 0, Claude's tool position after its `env` prefix, and Cursor's alias to the resolved absolute binary. |
| `tests/test_camc_hardening_pdx.py::TestRuntimeManifestShape` | `tests/baseline/test_run_contract.py::test_run_selects_tool_and_creates_one_session_and_monitor` | A record written by the real run command contains the `camc-runtime/1` schema plus Python, tmux, selected-tool, and shell runtime facts. The replacement asserts the durable record instead of reconstructing the dictionary in test code. |
| `tests/test_configurable_adapter.py::TestCursorConfig::test_trust_workspace_is_boot_only` | `tests/baseline/test_autoconfirm_contract.py::test_boot_trust_prompt_is_confirmed_only_while_initializing[cursor-...]` | Cursor workspace trust is handled only during initialization and produces the configured Enter key effect. The old assertion of literal `a` was stale against the current boot policy. |
| `tests/test_configurable_adapter.py::TestCamcCursorGuard::test_codex_runtime_confirm_ignores_stale_menu_outside_active_tail` | `tests/baseline/test_autoconfirm_contract.py::test_cursor_recent_tail_and_prose_guards_never_confirm` | The real monitor applies native-cursor and recent-tail safety guards, so stale menus and ordinary input prose do not emit confirmation. The old direct-helper case omitted the monitor cursor guard and placed the menu inside the configured 128-line active tail. |
| Exact launch-list assertion inside `tests/test_runtime_env.py::test_cmd_run_loads_context_for_metadata_only_after_launch` | `tests/baseline/test_run_contract.py::test_run_selects_tool_and_creates_one_session_and_monitor` | The baseline owns absolute binary placement and generated launch metadata. The retained focused test still proves that context is loaded only as record metadata and never supplies runtime `env_setup`; it now checks only the launch payload relevant to that invariant. |

## Distinct coverage retained

- `TestCodexReadyPatternAcceptsAllCursorMarkers` and
  `TestExternalCodexReadyPatternScrub` remain in
  `tests/test_cmd_run_wrapper_default.py`; they cover default and stale
  external configuration, not launch-command behavior.
- Auto-confirm parser, pattern, cooldown, and state-machine tests remain where
  they add focused failure localization. Only the two stale/duplicated cases
  listed above are removed.
- The activity-timestamp source scan remains because this change does not add
  a behavioral replacement for its invariant.
- Transport, runtime-environment, API resolution, ledger, and compatibility
  tests remain; the baseline contracts supplement rather than replace their
  distinct edge cases.

## Known pre-baseline failure classification

| Existing failure | Classification and disposition |
|---|---|
| `test_failed_startup_command_leaves_repairable_tmux_shell` reports `Operation not permitted` for a private tmux socket | Sandbox/environmental. Retained, with a separate private-socket capability probe that skips only the explicit EPERM case before exercising the integration assertion. |
| `test_cmd_run_loads_context_for_metadata_only_after_launch` expects an older launch-command shape | Stale expected shape. Its distinct context-metadata assertions remain; the duplicate launch-shape assertion is consolidated and mapped above. |
| `test_adapter_readiness_env_wrapper_binary_resolves_correctly` observes a host golden tool before temporary `PATH` | Non-hermetic test setup. The test now disables host golden paths so it deterministically exercises its intended adapter-readiness-versus-`env` invariant. |
| Cursor boot trust expects literal `a` | Stale test, replaced and mapped above. Current policy sends Enter. |
| Codex stale runtime menu calls the detection helper without the monitor's native-cursor guard | Stale/duplicated test, replaced and mapped above with the real monitor path. |
| Legacy `tests/test_api.py::TestHealth` stalls in the available Python 3.12/FastAPI environment | Optional legacy server/environment issue outside standalone CamC. The full suite is time-capped and reported; the offline API contract performs no network access. |
