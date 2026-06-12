# camc Agent Loop Jobs Spec

## Status

Draft for implementation.

This spec defines agent-owned scheduled prompt loops. These are related
to `camc cron` because they reuse the same schedule and policy concepts,
but they are not host cron jobs and are not executed by `camc cron tick`.

## Summary

An agent loop is a recurring prompt owned by exactly one camc agent. The
agent's own monitor process checks the loop file, waits until the agent
is idle, and delivers the prompt to that same owner through the fixed
`camc msg send` protocol.

Conceptually:

```text
agent monitor loop
  -> load this agent's agent.loop.json
  -> find due enabled prompt loops
  -> if owner agent is idle, send prompt by camc msg send <owner>
  -> record msg_id and advance next_due_at
```

The action surface is intentionally narrow: a loop can only send prompt
text. Slash commands, skill invocations, and subagent instructions are
just prompt text. No shell command, arbitrary argv, Python callback, or
cron worker is supported in P0.

## Goals

- Add recurring per-agent prompts without adding another daemon.
- Require every loop to have one agent owner.
- Reuse cron schedule and policy field shapes where possible.
- Store multiple loop entries in one per-agent `agent.loop.json` file.
- Execute loops only from the owner's monitor process.
- Deliver loop prompts through the fixed `camc msg send` path so message
  delivery remains ledger-backed and replayable.
- Keep host cron jobs and agent loop jobs separated in storage and
  execution.

## Non-Goals

- Do not execute loop entries from `camc cron tick`.
- Do not support shell commands, argv actions, or arbitrary scripts.
- Do not allow loops to target arbitrary other agents in P0. The target
  is always the owner agent.
- Do not add a new top-level command such as `camc loop` in P0.
- Do not require users or agents to hand-edit JSON files.
- Do not implement dependency graphs, conditionals, branching, or a DSL.
- Do not backfill every missed fire after downtime or long busy periods.

## Relationship To Existing Cron

Host cron jobs:

```text
storage:  ~/.cam/cron/jobs.d/<job_id>.json
executor: camc cron tick -> camc cron run <run_id>
action:   opaque argv or shell command
owner:    host, with created_by metadata
```

Agent loop jobs:

```text
storage:  ~/.cam/loops/<agent_id>/agent.loop.json
executor: host scheduler tick (V0); future revision may move
          execution to the agent's monitor process
action:   prompt text only, sent via camc msg send <agent_id>
owner:    required agent id/name/session
```

V0 implementation note (2026-06-12, CAM-DESK-CRON-010):
``camc cron tick`` services BOTH host cron jobs (under
``~/.cam/cron/jobs.d/``) AND agent loops (under ``~/.cam/loops/``)
in the same pass. This keeps Desktop on a single tick installer
without spawning a separate monitor-side daemon. The earlier
"tick must never scan ~/.cam/loops/" stance is **obsolete** — see
``src/camc_pkg/cron.py:tick()`` calling
``cron_loop.tick_loops()`` after the host-job pass.

The execution semantics still preserve the "loops belong to one
owner agent" contract: ``tick_loops`` resolves the owner from the
agent store and **defers dispatch unless the owner is currently
``status=running`` and ``state=idle``**. Deferred fires emit
``loop_deferred`` events and leave both ``next_due_at`` and
``state.attempts`` untouched, so a busy or missing agent burns
neither schedule slots nor retry budget.

The monitor-side execution path described elsewhere in this spec
is a future evolution, not a V0 requirement. The host scheduler
path keeps the storage boundary intact: jobs.d/ files are still
the cron registry, loops/ files are still per-agent, and
``camc cron tick`` never confuses one for the other (separate
modules, separate stores).

## Files

```text
~/.cam/loops/<agent_id>/agent.loop.json       active loop registry for one agent
~/.cam/loops/<agent_id>/agent.loop.lock       fcntl lock for that file
~/.cam/loops/<agent_id>/runs.jsonl            append-only loop event log for that agent
~/.cam/loops/<agent_id>/archive/              archived removed/expired loop entries
```

Why not `~/.cam/cron/`:

- It prevents accidental pickup by `cron tick`.
- It makes the owner boundary clear.
- It allows monitor tests to use temporary loop roots without touching
  system cron state.

The filename is always `agent.loop.json` so humans can find it easily,
but camc owns the file. Normal users and agents should create/update it
through `camc cron --loop` commands.

## User Command Surface

P0 should extend `camc cron` with a loop mode rather than add a new
command family.

### Add Loop

```bash
camc cron add --loop --owner <agent-id-or-name> --name <name> \
  (--every DUR | --daily HH:MM | --at TIME | --in DUR) \
  --prompt TEXT

camc cron add --loop --owner cam-dev --name daily-status --daily 09:00 \
  --prompt "/status\nSummarize blockers and next actions."

camc cron add --loop --owner cam-dev --name skill-check --every 30m \
  --prompt "Use the managing-camc skill and check whether any agents are stuck."
```

Optional P0 convenience:

```bash
camc cron add --loop --owner cam-dev --name review --every 2h \
  --prompt-file prompt.md
```

Validation:

- `--loop` requires `--owner`.
- `--loop` requires exactly one schedule preset.
- `--loop` requires `--prompt` or `--prompt-file`.
- `--loop` rejects `--shell` and `-- COMMAND...`.
- Owner must resolve to an existing camc agent record.
- The persisted owner id is the resolved full agent id, not the label.
- Duplicate loop names are rejected within the same owner file.

Output:

```text
added agent loop daily-status (a1b2c3d4)
owner: cam-dev (f1a1a661)
executor: monitor
```

### List Loops

```bash
camc cron list --loop --owner cam-dev
camc cron list --loop --owner cam-dev --json
```

Human output should be a compact table:

```text
ID        NAME          SCHED       EN  NEXT                 LAST       MSG
A1B2C3D4  daily-status  daily 09:00 y   2026-06-10 09:00    sent       9e3244af
```

JSON output should include the raw loop entries plus a top-level count:

```json
{
  "agent_id": "f1a1a661",
  "count": 1,
  "loops": []
}
```

### Remove Loop

```bash
camc cron rm --loop --owner cam-dev <id-or-name>
```

Removal archives the loop entry under the owner archive directory and
writes a `loop_removed` event. It must not remove OS crontab entries.

### Future But Not P0

```bash
camc cron enable --loop --owner cam-dev <id-or-name>
camc cron disable --loop --owner cam-dev <id-or-name>
camc cron run-now --loop --owner cam-dev <id-or-name>
```

These are useful, but P0 can ship with add/list/rm only.

## JSON Schema

The top-level file is one JSON object with multiple loop entries.

```json
{
  "schema": "camc-agent-loop/1",
  "version": 1,
  "agent_id": "f1a1a661",
  "agent_name": "cam-dev",
  "tmux_session": "cam-f1a1a661",
  "updated_at": "2026-06-10T09:00:00Z",
  "loops": [
    {
      "id": "a1b2c3d4",
      "name": "daily-status",
      "enabled": true,
      "executor": "monitor",
      "owner": {
        "type": "agent",
        "agent_id": "f1a1a661",
        "agent_name": "cam-dev",
        "tmux_session": "cam-f1a1a661"
      },
      "schedule": {
        "type": "daily",
        "time": "09:00",
        "timezone": "local",
        "next_due_at": "2026-06-10T09:00:00-07:00"
      },
      "action": {
        "type": "prompt",
        "text": "/status\nSummarize blockers and next actions.",
        "delivery": {
          "method": "camc-msg-send",
          "to": "f1a1a661",
          "no_wait": true,
          "expect_reply": false
        }
      },
      "policy": {
        "ttl_days": null,
        "expires_at": null,
        "max_attempts": 3,
        "busy_policy": "defer",
        "misfire_policy": "skip"
      },
      "state": {
        "attempts": 0,
        "created_at": "2026-06-09T09:00:00-07:00",
        "updated_at": "2026-06-09T09:00:00-07:00",
        "last_due_at": null,
        "last_dispatched_at": null,
        "last_msg_id": null,
        "last_status": null,
        "last_error": null
      }
    }
  ]
}
```

### Field Rules

- `schema` must be `camc-agent-loop/1`.
- Top-level `agent_id` is authoritative. Every loop owner must match it.
- `executor` must be `monitor`.
- `action.type` must be `prompt`.
- `action.delivery.method` must be `camc-msg-send`.
- `action.delivery.to` must equal the owner agent id.
- `schedule.next_due_at` is the source of truth for due checks.
- `state.last_msg_id` is the `MSG_ID` returned by `camc msg send`.
- Unknown fields must be preserved by read/modify/write operations.

## Schedule Semantics

Reuse the cron schedule presets:

```text
--every 30m      interval loop
--every 2h       interval loop
--daily 09:30    daily loop at local time
--at TIME        one-time loop
--in 45m         one-time loop after delay
```

A loop is due when:

```text
enabled == true
and schedule.next_due_at != null
and now >= schedule.next_due_at
```

After a successful dispatch, advance `next_due_at`:

- interval: advance to the first time after `now`
- daily: advance to the next local daily occurrence after `now`
- once: set `next_due_at = null`, then archive or disable after success

P0 misfire policy is `skip`: if a loop was due many times while the
agent was busy or offline, dispatch once when eligible and advance from
current time. Do not enqueue a backlog.

## Monitor Execution

Implement a real monitor feature by replacing or renaming the placeholder
`CronFeature` in `src/camc_pkg/monitor_features.py`.

Preferred class:

```python
@register_feature
class AgentLoopFeature(MonitorFeature):
    name = "agent_loop"
    order = 40
    enabled = True
```

If preserving the placeholder name is less invasive, `CronFeature` may be
implemented with `name = "cron"`, but logs and docs should call the
behavior "agent loop" to avoid confusion with host cron.

### Hook Point

Use `after_confirm()`.

Reasoning:

- `StateManagerFeature.after_confirm()` runs first and updates
  `runtime.idle_confirmed`.
- `AutoConfirmationFeature.confirm()` can halt a cycle; if it does,
  loop dispatch is skipped that cycle.
- Dispatching only after confirm prevents sending loop prompts while a
  permission/trust dialog is active.

### Eligibility

A loop may dispatch only when:

```text
runtime.idle_confirmed == true
snap.screen_busy == false
snap.prompt_visible == true
loop is due
loop is enabled
owner agent id == runtime.agent_id
```

If the loop is due but the agent is busy, do not increment attempts and
do not advance `next_due_at`. Leave it due and try again on a later
monitor cycle.

### Dispatch

P0 must use fixed message delivery semantics equivalent to:

```bash
camc msg send <owner-agent-id> -t <prompt> --no-wait
```

No shell string is allowed. If implemented through subprocess, use an
argv list:

```python
[camc_path, "msg", "send", owner_id, "-t", prompt, "--no-wait"]
```

The implementation may call an internal messaging helper instead of
spawning a subprocess, but the behavior must remain identical to
`camc msg send`:

- writes `messages.jsonl`
- creates a new `msg_id`
- appends turn/delivery records
- best-effort injects the prompt into the owner pane
- returns `MSG_ID=<id>` and `STATUS=sent` semantics

### Sender Identity Requirement

Current `camc msg send` infers `[from:<agent>]` from the caller's tmux
environment. The monitor process is not running inside the agent's tmux
pane, so P0 implementation must avoid losing sender attribution.

Acceptable implementation choices:

1. Add an internal helper such as `_msg_inject_with_sender(...)` and call
   it directly from `AgentLoopFeature` with `sender = owner`.
2. Add private environment variables used only by camc, for example
   `CAMC_SENDER_AGENT_ID` and `CAMC_SENDER_AGENT_NAME`, and teach
   `_msg_sender_identity()` to prefer them over tmux inference.
3. Add hidden CLI flags for internal use only, for example
   `camc msg send --from-agent <id>`, but do not document them as public
   user API in P0.

Recommended: option 2. It keeps the fixed command shape and avoids
large refactors. The monitor subprocess can set:

```text
CAMC_SENDER_AGENT_ID=<owner_id>
CAMC_SENDER_AGENT_NAME=<owner_name>
CAMC_SENDER_TMUX_SESSION=<owner_session>
```

Then `camc msg send <owner_id> ...` produces both `[from:owner#id]` and
`[to:owner#id]` attribution. Self-message is intentional for loops.

### Idempotency

Although there should normally be one monitor per agent, heal/restart
races can temporarily create duplicate monitors. Use the per-agent
`agent.loop.lock` around due check, dispatch record update, and schedule
advance.

For each loop, compute an idempotency key:

```text
loop_id + due_at
```

Before dispatching, check whether `runs.jsonl` already contains a
terminal or queued record for that key. If so, skip. After successful
`msg send`, append:

```json
{
  "event": "loop_dispatched",
  "loop_id": "a1b2c3d4",
  "due_at": "2026-06-10T09:00:00-07:00",
  "msg_id": "9e3244af",
  "agent_id": "f1a1a661",
  "ts": "2026-06-10T09:00:01Z"
}
```

Then update the loop state and advance `next_due_at` in the same locked
modify operation where practical.

## Store API

Add a small module rather than putting JSON handling in `monitor_features.py`.

Preferred file:

```text
src/camc_pkg/agent_loop.py
```

Responsibilities:

- path helpers
- schedule parser reuse or wrappers around cron schedule helpers
- `AgentLoopStore(agent_id)`
- locked load/save/modify
- add/list/remove
- archive removed/expired loops
- append per-agent run events
- due check and schedule advancement

Keep it Python 3.6 compatible and stdlib-only.

Suggested API:

```python
class AgentLoopStore(object):
    def __init__(self, agent_id): ...
    def load(self): ...
    def add(self, loop): ...
    def list(self): ...
    def remove(self, key): ...
    def due(self, now): ...
    def dispatch_due(self, agent_record, now, sender_env): ...
```

`dispatch_due` may be split if monitor feature should own subprocess
calls. The store should own locking and state mutation.

## CLI Implementation

`cmd_cron_add` should branch early when `args.loop` is true.

Pseudocode:

```python
if args.loop:
    owner = AgentStore().get(args.owner)
    prompt = args.prompt or read_file(args.prompt_file)
    schedule = build_schedule_from_args(args)
    loop = build_agent_loop(owner, args.name, schedule, prompt, args.policy)
    AgentLoopStore(owner["id"]).add(loop)
    print("added agent loop ...")
    return
```

Argparse changes:

```text
cron add:
  --loop
  --owner AGENT
  --prompt TEXT
  --prompt-file PATH

cron list:
  --loop
  --owner AGENT

cron rm:
  --loop
  --owner AGENT
```

Validation matrix:

| Command | Required With `--loop` | Rejected With `--loop` |
|---|---|---|
| add | `--owner`, schedule, prompt/prompt-file | `--shell`, `-- COMMAND...` |
| list | `--owner` | none |
| rm | `--owner`, id/name | none |

Do not install or remove crontab blocks for `--loop` operations.

## Monitor Configuration

P0 can enable agent loops unconditionally once implemented, because an
agent without `agent.loop.json` does nothing.

Optional future context setting:

```json
{
  "monitor_features": {
    "agent_loop": true
  }
}
```

But this should not be required for P0.

## Events And Logging

Append per-agent loop events to:

```text
~/.cam/loops/<agent_id>/runs.jsonl
```

Events:

```text
loop_added
loop_removed
loop_due_deferred_busy
loop_dispatch_started
loop_dispatched
loop_dispatch_failed
loop_archived
```

Also mirror important events into `EventStore` with `agent_id = owner_id`:

```text
camc history <agent>
```

should be able to show loop dispatches in the future.

Monitor logs should include concise lines:

```text
[agent_loop] due daily-status a1b2c3d4
[agent_loop] deferred daily-status: screen_busy
[agent_loop] sent daily-status msg_id=9e3244af
```

## Failure Handling

- Owner missing during `cron add --loop`: fail without writing.
- Owner removed after loop exists: monitor will no longer run; `camc rm`
  should archive or disable that agent's loop file in a future cleanup
  step. P0 may leave the file orphaned but `camc prune --orphans` should
  be extended later.
- Corrupt `agent.loop.json`: monitor logs an error and skips loops for
  that agent. It must not overwrite the file.
- `camc msg send` returns non-zero: increment attempts, record
  `last_error`, do not advance `next_due_at` unless attempts reached
  `max_attempts` and the loop is archived/disabled.
- Loop prompt is empty after file read: reject at add time.
- Prompt too large: P0 should reject above a fixed limit, for example
  64 KiB, to avoid huge tmux injection surprises.

## Security And Safety

- No shell execution.
- No arbitrary target in P0.
- No direct JSON editing in the normal workflow.
- Agent owner is resolved and pinned to full agent id at creation time.
- All file writes use fcntl lock + atomic rename.
- Unknown JSON fields are preserved.
- V0 host scheduler tick scans loop files after the host-job pass; a
  future monitor executor may narrow this to one owner file per monitor.

## Tests

Unit tests:

- Build a loop from `cron add --loop` arguments.
- Reject `--loop` without owner.
- Reject `--loop` with `--shell` or command argv.
- Reject duplicate loop names for one owner.
- Allow same loop name for different owners.
- `agent.loop.json` stores multiple loop entries.
- `cron list --loop --owner` reads only that owner's loop file.
- `cron rm --loop --owner` archives one entry and leaves others.
- Due calculation reuses interval/daily/once semantics.
- Busy or missing owner agent defers due loop without advancing
  `next_due_at` or incrementing attempts.
- Idle owner agent dispatches exactly one due loop.
- Duplicate tick race does not double-dispatch the same due_at.
- Stale/corrupt `agent.loop.json` is skipped and preserved.
- `camc cron tick` services both host jobs and due agent loops without
  mixing the two stores.

Integration-style tests with mocks:

- Fake agent record + fake monitor runtime + fake msg sender returns
  `MSG_ID=...`; assert state records `last_msg_id`.
- Simulate `camc msg send` failure and assert attempts increment.
- Simulate once-loop success and assert loop is disabled or archived.

Live smoke after implementation:

```bash
camc run -n loop-smoke -t codex -p /tmp/loop-smoke
camc cron add --loop --owner loop-smoke --name ping --in 1m \
  --prompt "Reply READY."
# wait for monitor cycle after due time
camc msg read --for loop-smoke --all
camc capture loop-smoke --lines 80
```

Expected:

- prompt is delivered by mailbox/msg path
- loop state records `last_msg_id`
- `next_due_at` advances or once loop completes
- no `~/.cam/cron/jobs.d/` file is created
- no crontab entry is installed

## Implementation Plan

1. Add `src/camc_pkg/agent_loop.py` with paths, store, schema helpers,
   due calculation, archive, and run-event append.
2. Reuse or wrap schedule parsing from `cron.py`. Avoid copy/paste when
   simple imports are possible, but do not couple loop storage to
   `CronJobStore`.
3. Add `--loop`, `--owner`, `--prompt`, and `--prompt-file` to cron
   subcommands in `cli.py`.
4. Implement loop branches in `cmd_cron_add`, `cmd_cron_list`, and
   `cmd_cron_rm`. Ensure these branches never call crontab helpers.
5. Extend message sender identity for monitor-owned sends, preferably by
   supporting `CAMC_SENDER_AGENT_ID` / name / session env vars.
6. Implement `AgentLoopFeature.after_confirm()` in
   `monitor_features.py`, enabled by default.
7. Update `build_camc.py` module order to include `agent_loop` before
   `monitor_features` or before `cli`, depending on imports.
8. Add focused tests in `tests/test_agent_loop.py` and monitor feature
   tests in `tests/test_monitor_features.py`.
9. Rebuild `dist/camc`, sync `src/camc`, run focused tests and full
   non-integration tests.
10. Update `docs/camc-cron-spec.md` with a short pointer to this spec,
    clarifying that loop jobs are not host cron jobs.

## Open Questions

- Should once loops be archived after success or left disabled in the
  owner file? Recommendation: archive after success, matching cron job
  recycle semantics.
- Should `--expect-reply` be enabled by default for loop prompts?
  Recommendation: no. A loop is a prompt injection mechanism; if the
  prompt wants a recorded reply, it should include that instruction or a
  later P1 flag can add it.
- Should loop dispatch wait for idle confirmation or merely prompt
  visibility? Recommendation: require `runtime.idle_confirmed` in P0 to
  avoid interrupting long-running work.
- Should loops survive agent reboot to a new id? Recommendation: not in
  P0. They are owned by agent id. A future `camc reboot` enhancement can
  migrate `~/.cam/loops/<old_id>/` to the new id when resume semantics
  are well-defined.
