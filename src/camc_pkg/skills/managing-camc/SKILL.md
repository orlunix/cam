---
name: managing-camc
description: >
  Manage AI coding agents (Claude Code, Codex, Cursor, Pi) locally or on
  connected CAM Servers with camc — the stdlib-only Python CLI. Use when
  the user wants to start an agent, send it a prompt, capture its
  output, attach to it, resume / reboot it, or clean up records.
  For inter-agent messaging use the camc-messaging skill. For cron/loops use
  camc-cron-loop. For diagnosing stuck agents use camc-diagnose.
  Cross-Server operations address a specific Node and agent; fleet listing
  and terminal attach use CAM TUI.
compatibility: Requires ~/.cam/camc deployed by the camc release.
metadata:
  author: hren
  tags: camc, cam, agent, agents, tmux, claude, claude-code, codex,
    cursor, pi, monitor, session, attach, capture, send, key, prune,
    reboot, resume, migrate, heal, server, node, forwarding, ai-agent,
    coding-agent, pm2, run, lifecycle
  category: infra
  requires-tools: camc, tmux
disable-model-invocation: false
argument-hint: "[run | list | capture <agent> | send <agent> -t '...' | reboot <agent> | attach <agent> | status <agent>]"
allowed-tools: Bash Read Glob Grep
---

# camc — local and cross-Server agent operations

camc starts AI coding agents (claude / codex / cursor / pi) inside tmux
sessions, lets you send them prompts and read their output, and tracks
each agent's lifecycle. Every agent has its own tmux session, monitor
process, and tool conversation identity. Reboot preserves the agent ID
but starts a new tool conversation.
With a local CAM Server and an authorized Client forwarding connection,
the same CLI can address agents on other Nodes. A missing local agent is
not proof that the target does not exist remotely.

> **When in doubt, run `--help` first — never guess.** Flags drift
> between releases; the binary is the source of truth. `~/.cam/camc --help`
> for subcommands; `camc <subcommand> --help` for flags.

Agents are addressable by name, ID prefix, or a numeric index (1-based from `~/.cam/camc list`).

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
| Logs | `~/.cam/camc logs <agent> -f` |
| Reboot (fresh session, same agent ID) | `~/.cam/camc reboot <agent>` |
| Reboot through LSF | `~/.cam/camc reboot <agent> --tool-launcher auto` |
| Update name/tag | `~/.cam/camc update <agent> --name x --tag T` |
| Stop pane 0 (keep tmux) | `~/.cam/camc stop <agent>` |
| Kill (force) | `~/.cam/camc kill <agent>` |
| Remove | `~/.cam/camc rm <agent>` (always kills tmux; `--archive` to save first) |
| Bulk cleanup | `~/.cam/camc prune --orphans` |
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
API profiles normally require `--tool-launcher none` because their proxy is
local. Verified current-allocation direct execution also supports that proxy.
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
~/.cam/camc run "x" -n x --tag NR10 --tag WORK
~/.cam/camc run "..." -t claude -n redo --resume <session-id>            # resume Claude session
~/.cam/camc run "x" -p /path/to/proj -n proj-dev               # explicit path (default: CWD)
~/.cam/camc run "x" -n y --system-prompt "You are a reviewer." # inject system prompt inline
~/.cam/camc run "x" -n y --system-file path/to/SKILL.md        # inject system prompt from file
~/.cam/camc run "x" -n y --isolate-tool-home 0               # override adapter home-isolation default
~/.cam/camc run "x" -n auto                                   # direct -> battach -> SSH -> qsub
~/.cam/camc run "x" -n batch --tool-launcher 'qsub -q o_cpu_4G -m rel8x -app r89docker --qsla autorun_sla -app affinity -Is'
```

### Startup supervision is part of the task

A successful `~/.cam/camc run` command means the launch request was handed to
tmux; it does not mean the tool is ready. Normal interactive startup supervision
continues through `status=alive, phase=started`. The same applies after `reboot`
or an explicit Claude resume.

For asynchronous qsub handoff, confirm that tmux exists and the pane has submitted
the job or is waiting for dispatch, then return the agent/job ID. The pane owns
the wait; leave `alive/running/initializing` until the tool actually starts.
Report "submitted/queuing", never "started", while resources are pending. Do
not wait for dispatch or kill a queued job merely because it takes a long time.

1. Keep the returned agent ID and poll its status and visible pane:
   ```bash
   ~/.cam/camc --json status <agent-id>
   ~/.cam/camc capture <agent-id> --lines 80
   ```
2. If initialization is waiting on workspace trust, first-run confirmations,
   theme/editor selection, or another routine startup choice, inspect the
   visible choices and select the appropriate one yourself. Trust the
   workspace the user asked you to run in; use the normal/default choice
   when the user gave no preference. Do not send the choice back to the user
   merely because the tool asks for it. Use the actual UI's key sequence:
   ```bash
   ~/.cam/camc key <agent-id> --key Enter
   ~/.cam/camc key <agent-id> --key Down
   ~/.cam/camc send <agent-id> --text "1"   # only if the visible menu accepts 1 + Enter
   ```
   Re-capture after each action; never blindly loop Enter/1. If the monitor
   already handled the screen, do not duplicate its input. Launcher autorun
   flags do not guarantee that first-run trust/onboarding screens disappear.
3. Continue through launcher/queue waiting and tool boot. Verify both the
   recorded `phase=started` and an actual ready input UI or tool activity.
   Do not send the initial task again when it was already passed on startup.
4. For missing credentials, unavailable resources, or an error that cannot be
   resolved within the requested scope, report the agent ID, exact blocker,
   and recorded failure reason. Do not claim started or force it into the
   registry. Authentication requiring the user remains a real blocker.

Launcher waiting has no startup deadline. A target-side receipt starts the
tool startup timers only after target resolution, immediately before exec,
for both auto and custom launcher prefixes. The worker's tool startup deadline
is at least 600s; the adapter also has its boot-readiness timeout. An early exit
or tool startup timeout is
`dead/failed`, with `exit_reason`. Keep the failed pane available for diagnosis.
When reporting a ready tool, include the final agent ID and observed started
state. An asynchronous submission is a separate, successful handoff.

When `nvprojectname` is available, every tool defaults to
`--tool-launcher auto`, including custom PATH tools. Outside that NVIDIA
environment, the default is local execution. Remote launcher choices are
`auto` or a custom argv prefix. The caller owns the behavior of a custom
prefix (for example, submitting its own qsub command); camc appends the tool
command. `none` is the local direct-launch sentinel, not a separate remote
launcher mode. Pass `--tool-launcher auto` to opt into the automatic path
explicitly.
Run uses an explicit launcher or the environment default. Reboot uses an
explicit override, otherwise the saved launcher (including a custom prefix),
and falls back to the environment default only for records without one.
All managed launches, including `--tool-launcher none`, use the adapter's
native autorun arguments: Claude `--dangerously-skip-permissions`, Codex
`--yolo`, Cursor `--force`, and Pi `--approve`. Workspace settings remain.
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
The order is current allocation direct -> battach -> SSH -> qsub. If none
can start the tool, the launch fails; it never silently starts locally.
The automatic qsub fallback requests `o_pri_interactive_cpu_16G` (16 GB),
with service class `autorun_sla`; the virtual queue maps to
`o_pri_interactive` on PDX. Custom launcher prefixes keep the caller's queue.

Auto startup prints brief `[launcher]` progress to stdout: self allocation
check, battach/SSH probes, qsub preparation, and final launch failure.
Messages show job/host and exit codes without raw commands or credentials.
After the detached tmux pane starts, actual transport attempts and fallback
messages appear in that pane; qsub submission/dispatch output stays there.
The qsub project is resolved and validated from the requested workdir with
`nvprojectname`, with the existing missing-project save policy. This save is
deferred until qsub is actually needed. Connection/submission failures before
the tool starts advance the chain, including qsub failures. Once a tool has
started, its exit ends the run; camc does not start a second agent on failure.
Use an explicit `--tool-launcher none` for local execution. Explicit user
tool arguments remain authoritative.
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
Managed Claude runs add `--dangerously-skip-permissions`; Codex adds `--yolo`,
Cursor adds `--force`, and Pi adds `--approve`. These same adapter options
apply to local, current-allocation, and remote launches. Custom tools only
receive autorun options if their adapter declares them.
Remote-launcher and verified current-allocation autorun runs disable the local
monitor's `auto_confirm`; the adapter's autorun option is the sole
confirmation policy in those modes.
With `--tool-launcher none`, tools run locally with native autorun. A
launcher/tool failure records its exit status and retains the
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
~/.cam/camc --json capture <agent>             # JSON with content hash
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
~/.cam/camc logs <agent> -f               # follow monitor log
```

### Reboot

```bash
~/.cam/camc reboot <agent>                # reuse saved launch settings
~/.cam/camc reboot <agent> --tool-launcher auto
~/.cam/camc reboot <agent> --tool-launcher none
~/.cam/camc reboot <agent> --tool-launcher 'qsub -q o_cpu_4G --qsla autorun_sla -Is'
```

Reboot destroys this agent's entire old tmux session (all windows/panes), then
creates a new tmux and starts a **fresh tool session with the same agent ID**.
It works with Claude, Codex, Cursor, Pi, and arbitrary tool executables, including
stopped agents whose tmux is gone. It does not recover or resume conversations,
reuse session UUIDs, or replay the previous task prompt. Old scrollback is lost.
Name, tool, working directory, tags, system prompt, API profile and tool-home
isolation policy are retained. Preparation and launcher progress run in the new
pane, just as for `run`; attach to see failures after terminal creation.

Reboot reuses `tool_launcher` (including custom prefixes), `tool_env`,
`tool_dir`, `tool_args`, and `use_env_tool` from `runtime.custom_launch` in
`agents.json` (mode 0600). Environment entries and launcher prefixes are saved
verbatim, including any secrets; environment references expand again at launch.
Explicit `--tool-env` overrides merge by variable name; `--tool-args` replaces
the saved argument list. Legacy records that lack values need them supplied once.
Auto reboot still fails if
the remote chain fails, without local fallback. Killing the local tmux does not
cancel a reused LSF allocation or prove that a detached remote process exited.
`migrate` remains an alias; `--to` is rejected (use a launcher instead).

### Update

```bash
~/.cam/camc update <agent> --name new-name
~/.cam/camc update <agent> --tag SMOKE
```

### Stop / kill / remove

```bash
~/.cam/camc stop <agent>           # reset pane 0 to an empty shell; tmux stays alive
~/.cam/camc kill <agent>           # force: tears down tmux session
~/.cam/camc rm <agent>             # remove record + always kills tmux + unlinks socket
~/.cam/camc rm <agent> --archive   # also tar.gz the history under ~/.cam/archives/
```

`stop` uses `respawn-pane -k` on window 0 / pane 0, independent of tool
type. It replaces the pane's program with an empty shell and marks the
agent `dead/stopped` only after tmux succeeds. Other panes are retained; the
reset pane's screen and scrollback are cleared. Capture/archive first if
needed. For remote launchers this stops the local pane/transport; it does
not confirm that detached remote processes have exited or cancel a reused
LSF allocation. Use `exit` when a graceful tool exit is wanted.

### Bulk cleanup

```bash
~/.cam/camc prune                  # preserve recorded lifecycle; no automatic revival
~/.cam/camc prune --orphans        # actually delete dead-tmux records + stale files
~/.cam/camc prune --orphans --dry-run
```

### Adopt an existing tmux session

```bash
~/.cam/camc add my-existing-session --tool claude --name my-agent
```

## Files

```
~/.cam/
├── agents.json                          # agent registry
├── messages.jsonl                       # inter-agent mailbox ledger
├── events.jsonl                         # append-only event log (auto-rotates 30d)
├── logs/monitor-<id>.log                # monitor stdout
├── pids/<id>.pid
├── archives/                            # tar.gz from `~/.cam/camc archive`
├── cron/jobs.d/<job_id>.json            # host cron job files
└── loops/<agent_id>/
    ├── agent.loop.json                  # per-agent prompt loop registry
    └── runs.jsonl                       # loop dispatch event log
/tmp/cam-sockets/                        # tmux sockets (per-machine)
```

## Troubleshooting

| Symptom | First thing to try |
|---|---|
| Status says `alive` but tmux is dead | `~/.cam/camc heal` |
| Need to replace a healthy local monitor | `~/.cam/camc heal --restart` |
| Need to refresh the managed tmux configuration | `~/.cam/camc heal --tmux` |
| Agent not responding to `send` | `~/.cam/camc capture <agent> --lines 30` to see; then `~/.cam/camc attach` for manual |
| Want to find an agent's Claude JSONL | `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` — see `reference/sessions.md` |

### Agent lifecycle

- `status`: `alive` or `dead`.
- `state`: alive → `idle`/`running`; dead → `stopped`/`failed`/`killed`.
- `phase`: `initializing` until readiness, then `started`; dead → null.
- `run` returns after creating tmux. The pane worker supervises startup;
  early exit or timeout becomes `dead/failed` with `exit_reason`.
- Work finishing returns to idle, not completed. Monitor errors alone do
  not imply the tool is dead.

### Heal modes

- `~/.cam/camc heal` and `~/.cam/camc heal --monitor` are the default monitor heal: they
  inspect local tool processes even for terminal records. A verified live tool
  repairs stale `complete`/`completed`/`stopped`/`failed`/`killed` labels to
  `alive/running` or `alive/idle`, with `phase=started`. Empty tmux shells and
  SSH/qsub/battach processes alone do not prove a live tool. Then heal recovers
  missing monitors without forcing healthy ones to restart.
  Use `~/.cam/camc reboot <agent>` to start the tool again.
  Cron repair, event maintenance, and same-HOME orphan discovery also run
  when no agent is currently running.
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

## Cross-Server operations

An unqualified agent name/ID resolves locally first, then through the local
CAM Server when no local match exists. Use `Node:agent` to choose the owner
explicitly, especially when names collide. Ambiguous targets fail instead
of selecting an arbitrary agent. Node means a configured Server hostname,
alias or internal ID; use the existing hostname/alias when available.

Examples assume the registered Node alias `node-b`:

```sh
~/.cam/camc status node-b:worker
~/.cam/camc capture node-b:worker --lines 100
~/.cam/camc send node-b:worker -t "Continue the authorized task"
~/.cam/camc run --node node-b --tool codex --path /work/repo -n worker
```

Remote operations are `msg`, `send`, `capture`, `status`, `run`, `stop`, `kill`,
`reboot`, `rm` and `key`. `run` uses `--node`; the others use `Node:agent`.
`msg reply` uses its saved message context. Output, errors and exit status
return to the caller; delivery is not a business reply or proof that a
newly submitted tool has started.

Cross-Server `list`, `attach`, `logs`, `update`, `heal`, cron and bulk operations
are not provided by this routing path. Use CAM TUI for fleet discovery and
terminal attach. A local registry, including one on shared Home, is not a
fleet inventory. LSF launcher execution does not change the agent's owner Node.

The local Server endpoint, connected forwarding Client and peer permissions
must be available. After submission, a timeout can mean an unknown outcome:
do not resend a mutation or switch to SSH/legacy execution automatically.
For connection checks, returned results and remote startup supervision, read
[reference/cross-server.md](reference/cross-server.md). The old `cam` CLI is retired.

## Reference (deep-dives — load on demand)

| File | When to read |
|---|---|
| `reference/cross-server.md` | Address remote agents, inspect routes, interpret results and recover from uncertainty |
| `reference/sessions.md` | Find / resume a Claude session JSONL |

### Other skills

| Skill | Covers |
|-------|--------|
| camc-messaging | Delegation protocol, wire format, mailbox/read protocol |
| camc-cron-loop | Host cron jobs, per-agent prompt loops |
| camc-diagnose | Heal deep-dive, monitor details, stuck/failed fix workflows |
