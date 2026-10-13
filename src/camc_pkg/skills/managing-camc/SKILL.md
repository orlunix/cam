---
name: managing-camc
description: >
  Manage AI coding agents (Claude Code, Codex, Cursor, Pi) on a single
  machine with camc — the standalone, stdlib-only Python CLI. Use when
  the user wants to start an agent, send it a prompt, capture its
  output, attach to it, resume / reboot / move it, or clean up records.
  For inter-agent messaging use the camc-messaging skill. For cron/loops use
  camc-cron-loop. For diagnosing stuck agents use camc-diagnose.
  Cross-machine fleet operations are outside this built-in skill.
compatibility: Requires ~/.cam/camc deployed by the camc release.
metadata:
  author: hren
  tags: camc, cam, agent, agents, tmux, claude, claude-code, codex,
    cursor, pi, monitor, session, attach, capture, send, key, prune,
    reboot, resume, migrate, heal, single-machine, ai-agent,
    coding-agent, pm2, run, lifecycle
  category: infra
  requires-tools: camc, tmux
disable-model-invocation: false
argument-hint: "[run | list | capture <agent> | send <agent> -t '...' | reboot <agent> | attach <agent> | status <agent>]"
allowed-tools: Bash Read Glob Grep
---

# camc — single-machine agent manager

camc starts AI coding agents (claude / codex / cursor / pi) inside tmux
sessions, lets you send them prompts and read their output, and tracks
each agent's lifecycle. Every agent has its own tmux session, monitor
process, and Claude session-id, so multiple agents in the same
workspace are fully isolated at the conversation level.

> **When in doubt, run `--help` first — never guess.** Flags drift
> between releases; the binary is the source of truth. `~/.cam/camc --help`
> for subcommands; `camc <subcommand> --help` for flags.

Agents are addressable by name, ID prefix, or `#N` (1-based from `~/.cam/camc list`).

## Quick Reference

| Task | Command |
|---|---|
| Start agent | `~/.cam/camc run "task" -n <name>` |
| Start interactively | `~/.cam/camc run -n <name>` |
| Resume Claude session | `~/.cam/camc run -t claude -n <name> --resume <session-id>` |
| List agents | `~/.cam/camc list` (or `ls`) |
| Status detail | `~/.cam/camc status <agent>` |
| Capture screen | `~/.cam/camc capture <agent> --lines 200` |
| Send text + Enter | `~/.cam/camc send <agent> --text "msg"` |
| Send text only | `~/.cam/camc send <agent> --text "..." --no-enter` |
| Send special key | `~/.cam/camc key <agent> --key Escape` |
| Attach interactively | `~/.cam/camc attach <agent>` (Ctrl+B D to detach) |
| Logs | `camc logs <agent> -f` |
| Reboot (resume session) | `camc reboot <agent>` |
| Reboot through LSF | `camc reboot <agent> --tool-launcher auto` |
| Update name/tag | `camc update <agent> --name x --tag T` |
| Stop (graceful) | `~/.cam/camc stop <agent>` |
| Kill (force) | `~/.cam/camc kill <agent>` |
| Remove | `~/.cam/camc rm <agent>` (always kills tmux; `--archive` to save first) |
| Bulk cleanup | `camc prune --orphans` |
| Start with system prompt | `~/.cam/camc run "task" -n name --system-prompt "..."` or `--system-file <path>` |
| Run with an API profile | `~/.cam/camc run -t codex --api <name> --tool-launcher none -n <name> "task"` |
| Check/list API profiles | `~/.cam/camc api check` / `~/.cam/camc api list` |
| Show API defaults | `~/.cam/camc api default show` |
| Set a per-tool API default | `~/.cam/camc api default set -t codex <name>` |
| Clear a default (login) | `~/.cam/camc api default clear -t codex` |
| Use login for one run | `~/.cam/camc run -t codex --no-default-api -n <name>` |

**Other skill areas:**

| Area | Skill |
|------|-------|
| Inter-agent messaging (delegation) | camc-messaging |
| Cron jobs & prompt loops | camc-cron-loop |
| Diagnose stuck/failed agents | camc-diagnose |

## API profiles and login

`--api NAME` explicitly selects a profile from `~/.cam/api-models.json` for
Claude or Codex. Run `~/.cam/camc api check` before relying on a profile.
API profiles require `--tool-launcher none` because their proxy is local.
API defaults are per-tool and opt-in: `api default set` makes ordinary runs
use that profile; `api default show` displays the selection.

When a default is configured, use `--no-default-api` for one normal login
run. This skips the default without changing configuration. To permanently
return a tool to login, run `api default clear --tool <tool>`; an explicit
`--api NAME` still wins for a single run. Codex login and API runs use the
same thin `~/.codex/<agent-id>` home, linking safe donor state while retaining
a private config snapshot and host-local runtime paths. Only API runs add
provider/catalog settings to that snapshot.

## 1. Starting an agent

Always pass `--name`. The name is how you and other tools find this
agent later. Use the user's name if given, otherwise generate a short
clear one (`fix-ecc`, `bug5893270`, `regr-fn100`).

```bash
~/.cam/camc run "fix the ECC error" -n fix-ecc                # codex (default)
~/.cam/camc run "add tests" -t claude -n add-tests
~/.cam/camc run "review this tree" -t pi -n pi-review
~/.cam/camc run -n debug-mem                                   # interactive (no prompt)
~/.cam/camc run "build" -a -n nightly                          # auto-exit on completion
~/.cam/camc run "x" -n x --tag NR10 --tag WORK
~/.cam/camc run "..." -t claude -n redo --resume <session-id>            # resume Claude session
~/.cam/camc run "x" -p /path/to/proj -n proj-dev               # explicit path (default: CWD)
~/.cam/camc run "x" -n y --system-prompt "You are a reviewer." # inject system prompt inline
~/.cam/camc run "x" -n y --system-file path/to/SKILL.md        # inject system prompt from file
~/.cam/camc run "x" -n y --isolate-tool-home 0               # override adapter home-isolation default
~/.cam/camc run "x" -n auto                                   # direct -> battach -> SSH -> qsub -> local
~/.cam/camc run "x" -n batch --tool-launcher 'qsub -q o_cpu_4G -m rel8x -app r89docker --qsla autorun_sla -app affinity -Is'
```

When `nvprojectname` is available, every tool defaults to
`--tool-launcher auto`, including custom PATH tools. Outside that NVIDIA
environment, the default is local execution. Remote launcher choices are
`auto` or a custom argv prefix. The caller owns the behavior of a custom
prefix (for example, submitting its own qsub command); camc appends the tool
command. `none` is the local direct-launch sentinel, not a separate remote
launcher mode. Pass `--tool-launcher auto` to opt into the automatic path
explicitly.
With `auto`, camc first checks whether this process is inside its own RUN
interactive `autorun_sla` allocation. It verifies `LSB_JOBID` against
the LSF record, current user, service class, and local execution host. When
verified, it runs the tool directly in that allocation with the adapter's
`launcher_autorun_args`, avoiding a nested qsub or SSH hop. Missing or
mismatched allocation context falls through to normal launcher selection;
custom launcher templates remain authoritative. Otherwise, `auto` ranks the
caller's RUN interactive `autorun_sla` jobs by current project, matching
`QJT_AI_AGENT_TYPE`, generic shell, preferred interactive queue, and recency.
After rechecking each candidate's long job metadata and `EXEC_HOST`, it uses
LSF `battach -L` to run a private probe inside that job's container/cgroup.
The probe resolves the tool to an absolute path there, and the actual launch
uses the same job attachment and resolved executable. All battach candidates
are tried before independent SSH connections to the verified execution hosts.
SSH uses batch authentication and bounded connection timeouts; missing public
keys advance to qsub. SSH reaches the host shell and does not claim to enter
the job's container or cgroup. The tool must pass its version check in that
host environment before SSH is selected.
The order is current allocation direct -> battach -> SSH -> qsub -> local.
The qsub project is resolved and validated from the requested workdir with
`nvprojectname`, with the existing missing-project save policy. This save is
deferred until qsub is actually needed. Connection/submission failures before
the tool starts advance the chain, including qsub failures. Once a tool has
started, its exit ends the run; camc does not start a second agent on failure.
The final local fallback adds no adapter autorun arguments or monitor
auto-confirm. Explicit user tool arguments remain authoritative.
Selection and fallback decisions are printed and safe structural metadata is
recorded.

For auto/current-allocation Claude and Codex runs, an initial task is passed
as the tool's positional prompt, avoiding terminal paste/Enter races. The
monitor does not submit it again. Queue dispatch time does not consume the
tool startup timeout. Codex isolated homes exclude donor daemon socket/lock
directories so agents on different execution hosts cannot reuse those locks.

Probe and launch preserve the allocation's PATH and append target-visible
absolute submit-host directories plus `/home/nv/bin`, `/home/utils/bin`, and
`/usr/local/lsf/bin`. Duplicate, absent, relative, and transient submit paths
are skipped. Loader variables and arbitrary submit-host secrets are not
copied. Remote/current-allocation Codex home snapshots set
`allow_login_shell = false` so command shells retain that PATH; ordinary local
Codex settings are preserved. Already running agents need reboot to pick up
this environment repair.

Remote launchers are rejected while an explicit or default API profile is
active because the per-agent loopback proxy is not reachable from the attached
job or new allocation. A verified current `autorun_sla` allocation runs
directly and can use the API profile. Pass `--tool-launcher none` to use an API
profile locally, or `--no-default-api` to use normal tool login with a remote
launcher. `battach` runs directly from the submit host without SSH and places
the process inside the verified job context. SSH is a separate later fallback.

For a custom launcher prefix, `--tool-launcher LAUNCHER` parses `LAUNCHER`
with POSIX `shlex` rules and appends the complete POSIX-shell-quoted tool
command as one argv. A single standalone `{command}` remains accepted for
older templates. camc does not use `eval`. The launcher executable is checked
locally, while the tool name is deliberately left unresolved for the
qsub/SSH target environment.
Claude launcher runs add `--dangerously-skip-permissions` (without the local
`--permission-mode auto`); Codex launcher runs add `--yolo`; Pi launcher runs
add `--approve` so project-local resources cannot block in a remote trust TUI.
Cursor and custom tools add no launcher autorun option unless their adapter
config declares one.
Remote-launcher and verified current-allocation autorun runs disable the local
monitor's `auto_confirm`; the adapter's autorun option is the sole
confirmation policy in those modes.
With `--tool-launcher none`, ordinary local startup and Codex requirements-based autorun policy
are unchanged. A launcher/tool failure records its exit status and retains the
tmux shell for inspection.

`--isolate-tool-home 1|0` overrides the selected adapter's Boolean
`isolate_tool_home` default for one run. Codex defaults to
an isolated `~/.codex/<agent-id>` home. Claude, Cursor, and Pi default to their
ordinary shared homes; Pi can be explicitly isolated under `~/.pi/agent/`.
An adapter must declare its runtime-home layout before isolation can be enabled.

`--system-prompt` / `--system-file` writes the content into a
marker-delimited block inside the tool's auto-loaded config file in the
workdir (`CLAUDE.md` for claude, `AGENTS.md` for codex/cursor/pi).
The tool picks it up on startup via its own auto-load mechanism.

**Naming gotcha:** don't use `camflow-*` for personal dev agents.
camflow's pre-run cleanup matches `name.startswith("camflow-")` and
will kill them. Use `cf-dev`, `flowdev`, etc.

## 2. Send request / get response

Once an agent is running, you talk to it through three commands.

### Send a prompt

```bash
~/.cam/camc send <agent> --text "please refactor src/foo.py to ..."
```

`--text` payload is sent literally to the agent's tmux pane, then a
trailing Enter submits it. Use `--no-enter` to type without submitting.

### Read what the agent wrote

```bash
~/.cam/camc capture <agent>                    # last 100 lines (default)
~/.cam/camc capture <agent> --lines 500        # more
~/.cam/camc capture <agent> --lines 0          # full scrollback (60s timeout)
camc --json capture <agent>             # JSON with content hash
```

Capture reads the tmux pane buffer — exactly what a user attached to
the session would see.

### Special keys

```bash
~/.cam/camc key <agent> --key Escape           # cancel a tool call / interrupt
~/.cam/camc key <agent> --key Enter
~/.cam/camc key <agent> --key C-c              # SIGINT to foreground process
~/.cam/camc key <agent> --key C-d              # EOF
```

### Typical interaction loop

```bash
~/.cam/camc send <agent> --text "summarize the diff"
sleep 5                                  # let the model think
~/.cam/camc capture <agent> --lines 80          # read the answer
# ...iterate
```

When the agent is mid-tool-call and you want to stop it:

```bash
~/.cam/camc key <agent> --key Escape
```

### Attach for interactive work

```bash
~/.cam/camc attach <agent>     # join the tmux session; Ctrl+B D detaches
```

### Inter-agent messaging (delegation)

For asking another agent to do something, use the **camc-messaging** skill:

```bash
~/.cam/camc msg send <to> -t "..."             # block until reply
~/.cam/camc msg send <to> -t "..." --no-wait   # return message id immediately
~/.cam/camc msg reply <msg_id> -t "..."        # reply on same thread
~/.cam/camc msg read [--next] [--mark]         # read inbox
```

Full protocol, wire format, and patterns: see the camc-messaging skill.

## 3. Manage / lifecycle

### Inspect

```bash
~/.cam/camc list                          # all agents on this host
~/.cam/camc status <agent>                # detailed JSON-like state
camc logs <agent> -f               # follow monitor log
```

### Reboot / move

```bash
camc reboot <agent>                # inherit saved launcher, otherwise NV auto / non-NV local
camc reboot <agent> --tool-launcher auto # resume through direct/battach/SSH/qsub; no local fallback
camc reboot <agent> --tool-launcher none # explicitly resume locally
camc reboot <agent> --tool-launcher 'qsub -q o_cpu_4G --qsla autorun_sla -Is'
```

Reboot resolves the launcher before stopping the old agent. A saved `auto` or
`none` selection is inherited; legacy records use the environment default.
Custom prefixes must be supplied again, so secrets in templates are not saved.
Auto reboot never falls back to a local tool; a failed remote chain reports
failure. Local API profiles require an explicit local override unless already
inside a verified current allocation.

Remote Codex sessions without a local PID binding can be recovered only from
an idle pane with no pending input: reboot queries `/status` and validates that
exact UUID's transcript, workdir, and creation time. Managed Codex home
snapshots disable `check_for_update_on_startup` without changing the donor.

### Update

```bash
camc update <agent> --name new-name
camc update <agent> --tag SMOKE
```

### Stop / kill / remove

```bash
~/.cam/camc stop <agent>           # graceful: sends /exit; tmux stays alive (resumable)
~/.cam/camc kill <agent>           # force: tears down tmux session
~/.cam/camc rm <agent>             # remove record + always kills tmux + unlinks socket
~/.cam/camc rm <agent> --archive   # also tar.gz the history under ~/.cam/archives/
```

### Bulk cleanup

```bash
camc prune                  # drift-fix ONLY (status wrong but tmux alive)
camc prune --orphans        # actually delete dead-tmux records + stale files
camc prune --orphans --dry-run
```

### Adopt an existing tmux session

```bash
camc add my-existing-session --tool claude --name my-agent
```

## Files

```
~/.cam/
├── agents.json                          # agent registry
├── machines.json
├── contexts.json
├── messages.jsonl                       # inter-agent mailbox ledger
├── events.jsonl                         # append-only event log (auto-rotates 30d)
├── logs/monitor-<id>.log                # monitor stdout
├── pids/<id>.pid
├── archives/                            # tar.gz from `camc archive`
├── cron/jobs.d/<job_id>.json            # host cron job files
└── loops/<agent_id>/
    ├── agent.loop.json                  # per-agent prompt loop registry
    └── runs.jsonl                       # loop dispatch event log
/tmp/cam-sockets/                        # tmux sockets (per-machine)
```

## Troubleshooting

| Symptom | First thing to try |
|---|---|
| Status says `running` but tmux is dead | `~/.cam/camc heal` |
| Need to replace a healthy local monitor | `~/.cam/camc heal --restart` |
| Need to refresh the managed tmux configuration | `~/.cam/camc heal --tmux` |
| Agent not responding to `send` | `~/.cam/camc capture <agent> --lines 30` to see; then `~/.cam/camc attach` for manual |
| Want to find an agent's Claude JSONL | `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` — see `reference/sessions.md` |

### Heal modes

- `~/.cam/camc heal` and `~/.cam/camc heal --monitor` are the default monitor heal: they
  recover dead monitors without forcing healthy monitors to restart.
- `~/.cam/camc heal --restart` restarts verified local monitors one at a time, then
  runs the normal monitor heal.
- `~/.cam/camc heal --tmux` rewrites CAMC's managed `tmux.conf` and sources it in
  every local agent tmux socket; it does not run monitor or agent migration.
- `~/.cam/camc heal --agents` migrates only verified legacy agent records; it does
  not touch monitors.
- `~/.cam/camc heal --upgrade` is hidden and deprecated compatibility only; do not
  use it for normal maintenance.

For deeper diagnosis (stuck agents, exit reasons, heal details):
use the **camc-diagnose** skill.

## When NOT to use camc

camc only manages agents on its own host. Cross-machine operations require
the separate `cam` tooling and are outside this built-in skill.

## Reference (deep-dives — load on demand)

| File | When to read |
|---|---|
| `reference/sessions.md` | Find / resume a Claude session JSONL |
| `reference/machines-and-contexts.md` | Add SSH host, debug `env_setup` |

### Other skills

| Skill | Covers |
|-------|--------|
| camc-messaging | Delegation protocol, wire format, mailbox/read protocol |
| camc-cron-loop | Host cron jobs, per-agent prompt loops |
| camc-diagnose | Heal deep-dive, monitor details, stuck/failed fix workflows |
