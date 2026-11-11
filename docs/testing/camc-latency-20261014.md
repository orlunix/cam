# camc latency measurement and version fast path — 2026-10-14

The personal local executable was v1.2.162 before optimization. End-to-end
subprocess measurements included shell/Python startup and captured output.
The initial 15-run median for `camc version` was 249.8 ms (p95 269.7 ms);
a later 20-run measurement under concurrent test load was 298.3 ms.
Python startup alone had a 32.0 ms median.

The v1.2.163 candidate's ordinary executable `version` path had a 2.3 ms
median and 2.8 ms p95 over 20 runs. It uses shell builtins and build-time
metadata, checking external config file existence without parsing configs.
The Python/module entry also has an early version branch to avoid CLI,
bridge, capture, storage and tmux initialization. Direct `python3 <artifact>
version` still pays for compiling the large single-file artifact: its
measured median was 213.9 ms under the same load. No claim is made that the
shell improvement applies to all camc commands.

Output format, supported tool names, global --json/--verbose flags, version
help and invalid-argument handling have focused tests. HOME unset falls
back to Python for correct passwd-based home resolution. The build-time
CAMC_PRELUDE_DISABLE=all switch preserves the Python-only prelude.

252 relevant tests passed; Python 3.6 syntax and diff checks passed.
Version fast-path tests directly build candidates without advancing the
source version. Existing prelude tests which run the version-bumping build
script were not included in this validation run.

## Real run measurement

One profiled, no-prompt Codex run used the previously installed v1.2.162:

```text
camc run -t codex -n run-latency-1791969484 \
  -p /home/hren/.cam/run-latency-1791969484 \
  --tool-launcher auto --no-default-api
```

The command returned in 16,613.0 ms. LSF reported concurrent query pressure;
bjobs discovery timed out at 15 seconds and auto fell back to qsub. The
Codex banner and input prompt became visible at 65,218.0 ms from command
start, including scheduler and tool startup. This is one profiled sample,
not a median or a model-response measurement.

| Profiled function | Cumulative time |
|---|---:|
| resolve_named_tool_launcher | 15,328.4 ms |
| _launcher_process (3 calls; included above) | 15,325.1 ms |
| load_login_shell_env | 241.7 ms |
| create_tmux_session | 167.5 ms |
| configure_agent_runtime_home | 74.6 ms |
| install_manifest_skills | 26.3 ms |
| _preflight | 7.6 ms |

These numbers show that run latency in this sample was dominated by LSF,
not argparse/version output. The version optimization does not alter LSF
verification, launcher ordering, timeouts or submission behavior.

Test agent 813896a5 was killed and removed. Test qsub job 1909269709 had
already finished when cleanup called bkill. Existing allocations were not
killed. Evidence logs and reports remain under /tmp/camc-run-latency-*.

## Deployment

Installed exact v1.2.163 artifact to the local personal canonical entry,
venv entry and their aliases; SHA256:
`6fedd6cd8dad18b3c5b186322884dc37fb60ad1936d9f668ce94ec16dc74b864`.
Rollback backups: `/home/hren/.cam/backups/camc-local-163-20261014-022052/`.
Shared site_share, SITE-C and SITE-E entries were not updated by this change.
No commit or push was performed.
