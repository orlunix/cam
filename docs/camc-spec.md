# camc — Canonical Spec

> Status: canonical / current as of 2026-05-25.
> Scope: the `camc` standalone agent manager (`src/camc_pkg/`, built to `dist/camc`).
> See [Doc map](#doc-map) at the bottom for how this relates to other docs.

`camc` is the per-machine, single-file, stdlib-only Python CLI that owns the
local lifecycle of AI coding agents (Claude Code, Codex, Cursor, …). It is the
**source of truth** for agent execution on its host. The optional `cam` server
is a multi-machine API/UI aggregator that talks to camc over SSH and may cache
or present camc state, but camc owns the local runtime truth.

## 1. Design principles

- **camc-first.** Source of truth for local agent runtime state lives in
  `~/.cam/` JSON files. `cam` server is an aggregator/cache/UI surface for
  those runtime records; local execution state is born in camc.
- **Standalone.** Zero hard dependencies. Python 3.6+, stdlib only.
  Optional `rich` is feature-detected at runtime for pretty tables; ANSI
  fallback otherwise.
- **Thin CLI, fat services.** This is the direction, not the current state:
  today `cli.py` still contains substantial command logic. New work should
  push logic into service modules (`monitor`, `cron`, `scheduler`,
  `transport`, `storage`, future `messaging`, …) and leave argparse handlers
  as thin shims.
- **External tick model.** Monitor is a long-lived **observer** of a tmux
  pane. Cron is a **driven** module — the OS crontab calls `camc cron tick`
  on a fixed schedule; camc never schedules itself.
- **Stores own durable state.** JSON snapshots use fcntl-protected
  read-modify-write plus atomic rename where a store exists. Some legacy
  ledgers are still inline helpers in `cli.py`; the target is to move them
  behind service/store APIs.
- **Append-only ledgers.** `events.jsonl`, `messages.jsonl`, and
  `cron-runs.jsonl` are append-only records. JSON snapshots are rewritten
  atomically.

## 2. File layout (`~/.cam/`)

| Path | Owner | Schema | Notes |
|---|---|---|---|
| `agents.json` | `AgentStore` | list-of-dicts, canonical schema below | Locked write, atomic rename |
| `agents.json.lock` | `AgentStore` | empty file, fcntl lock target | Held only during read-modify-write |
| `events.jsonl` | `EventStore` | one JSON record per line | Append-only; `rotate(max_age_days=30)` helper |
| `messages.jsonl` | inline (`cli.py`) | ledger of msg envelopes + turn records | Append-only best-effort today; see §6 |
| `cron.json` | `CronStore` | `{"version": 1, "jobs": [...]}` | Locked write, atomic rename, refuses corrupt overwrite |
| `cron-config.json` | `CronConfig` | global cron defaults | Auto-created on first cron add |
| `cron-runs.jsonl` | `cron.py` | per-fire history | Append-only |
| `cron.state.json` | `cron.py` | last tick state | Single-record |
| `cron.lock` | `cron.py` | fcntl lock target | Prevents concurrent ticks |
| `cron-archive/` | `cron.py` | recycled job records | Removed/expired jobs are archived here |
| `configs/*.toml` | adapter loader | TOML adapter rules | Optional override of embedded configs |
| `context.json` | `_load_default_context` | legacy single-machine context | Optional env_setup |
| `contexts.json` | `ContextStore` | list of workspace contexts | Locked write, atomic rename |
| `machines.json` | `machine_store` | remote machine registry | Used by `cam sync` |
| `archives/*.tar.gz` | `cli.cmd_archive_*` | transcript + agent metadata bundle | Created by `archive`, `rm --archive`, or pruning flows |
| `logs/monitor-<id>.log` | monitor subprocess | monitor loop log | Main monitor logger output |
| `logs/monitor-<id>.stderr` | monitor subprocess launcher | monitor subprocess stderr | Created by `run` / `heal` launch paths |
| `logs/cron.log` | system crontab block | cron tick stdout/stderr | Human-readable cron log |
| `pids/<id>.pid` | monitor subprocess | monitor process pid | Self-cleanup on exit |

`/tmp/cam-sockets/cam-<id>.sock` holds the tmux server socket for each
agent. This is **machine-local** even on NFS-shared `~/.cam/` — that's how
camc isolates a session to one host across a cluster.

## 3. Canonical agent record

The single normative schema for agent records. Lifecycle-related store / CLI
commands produce or consume records in this shape. (See also
`src/cam/core/agent_schema.py` for the unified version on the cam side.)

```jsonc
{
  "id":             "8-char hex",                  // uuid5(hostname+ts+rand)[:8]
  "session_id":     "<id>-0000-0000-0000-000000000000 OR real Claude UUID",
  "task": {
    "name":         "human label",
    "tool":         "claude|codex|cursor|...",
    "prompt":       "initial prompt or empty for interactive",
    "auto_confirm": true,
    "auto_exit":    false,
    "auto_exit_enable": false,
    "tags":         []
  },
  "context_id":     "",                            // cam-side UUID (empty in pure camc)
  "context_name":   "",
  "context_path":   "/abs/workdir",
  "transport_type": "local|ssh",                   // how cam deployed it; NOT where tmux lives
  "status":         "running",                       // common: pending|starting|running|completed|failed|killed|stopped
  "state":          "initializing|planning|editing|testing|committing|idle",
  "tmux_session":   "cam-<id>",
  "tmux_socket":    "",                            // override; default /tmp/cam-sockets/<session>.sock
  "tmux_bin":       "/usr/bin/tmux",               // pinned per-session
  "tmux_version":   "tmux 3.4",
  "pid":            12345,                         // monitor subprocess pid
  "hostname":       "short-or-fqdn",               // tmux session host (machine-local!)
  "started_at":     "2026-05-25T00:00:00Z",
  "completed_at":   null,
  "exit_reason":    null,
  "retry_count":    0,
  "cost_estimate":  null,
  "files_changed":  []
}
```

**Legacy fields tolerated** (read-only, never written): `session` →
`tmux_session`, `path` → `context_path`, `monitor_pid` → `pid`, flat
`tool/prompt/name/auto_exit` → nested `task.*`. The `_sf()` / `_tf()`
helpers in `cli.py` provide transparent access across both shapes.

## 4. Module map (`src/camc_pkg/`)

| Module | LOC | Responsibility |
|---|---|---|
| `cli.py` | 5371 | Argparse dispatcher + thin command handlers (`cmd_<name>`) |
| `cron.py` | 1068 | `CronStore`, schedule parsing, tick loop, crontab install/uninstall |
| `monitor.py` | 391 | Per-agent observer loop; capture → detect → auto-confirm → state |
| `scheduler.py` | 371 | DAG runner for `camc apply` |
| `transport.py` | 362 | Tmux primitives (`capture_tmux`, `tmux_send_input`, …) |
| `adapters.py` | 283 | TOML parser + embedded configs + `AdapterConfig` |
| `formatters.py` | 269 | Table / JSON output (rich-optional) |
| `storage.py` | 242 | `AgentStore`, `EventStore` |
| `migrate.py` | 229 | SQLite → JSON migration |
| `remote.py` | 205 | SSH helpers for `cam sync` |
| `utils.py` | 156 | ANSI strip, pattern compile, subprocess helpers, `_now_iso` |
| `machine_store.py` | 100 | `machines.json` accessor |
| `context_store.py` | 92 | `contexts.json` accessor |
| `detection.py` | 89 | `detect_state`, `should_auto_confirm`, `is_ready_for_input` |

**Service boundary today.** `cli.py` is still 5K lines because many `cmd_*`
handlers also contain logic. The simplification target (§9) is to demote
`cmd_*` to ~20 lines each that delegate to a service module.

## 5. Monitor — observer-only contract

`run_monitor_loop(session, agent_id, config, store, …)` in `monitor.py`.
Spawned as `camc _monitor <id>` subprocess (`start_new_session=True`) so
it survives CLI exit.

Loop, every ~1s:

1. **Health check** (every 15s): `tmux_session_exists(session)`. Gone →
   `completed` if `has_worked`, else `failed`. Exit.
2. **Capture** (`capture_tmux`) → hash (last status-bar line stripped to
   avoid Claude's spinner-alternation false changes).
3. **Aux signals** (optional, per-adapter): `busy_pattern` (working),
   `done_pattern` (just finished). Tuned per tool in `<tool>.toml`.
4. **Auto-confirm** (5s cooldown):
   - Build `tail_lines` from last 5 non-empty lines.
   - Compute `bare_prompt` (whole line is `❯` / `>` / `›`).
   - Run `should_auto_confirm(output, config)` — scans `lines[-config.confirm_recent_lines:]` against `[[confirm]]` rules.
   - **1-spam guard**: if response would be `1` and `tail_lines` already
     contain `1{3,}`, suppress and apply ~60s cooldown. Prevents
     blasting `1`s when an earlier confirm misfired into prose.
   - Dispatch via `tmux_send_input` / `tmux_send_key`.
5. **State detection** (`detect_state`): regex on recent ~2KB →
   `planning|editing|testing|committing`. Sets `has_worked=True` on
   first non-None state.
6. **Idle detection**: `has_worked` + hash stable 60s + prompt visible →
   `idle_confirmed`. Fast-track to 5s if `done_pattern` matched + bare prompt.
7. **Stuck fallback**: `has_worked` + hash frozen 120s + prompt NOT visible →
   send `probe_char` (default `1`).
8. **Auto-exit** (opt-in): `idle_confirmed` + nobody attached +
   `auto_exit=true` → kill session, mark completed.

**Self-heal**: monitor catches exceptions and restarts itself up to 5
times with exponential backoff (5s, 10s, 15s…). Crashes beyond that need
external heal.

**What monitor does NOT do**: it does not write to `messages.jsonl`, does
not run cron jobs, does not invoke other agents, does not modify
`agents.json` beyond status / state / pid / completed_at. That separation
is the "observer" half of "monitor as observer only".

## 6. Mailbox — `camc msg` protocol

Ledger: `~/.cam/messages.jsonl` (append-only). Pane injection is a
best-effort wake-up; **the ledger is source of truth**.

### 6.1 Record kinds

```jsonc
// On send (envelope):
{ "msg_id": "e03371f8",  "to": "<id|name>",  "tmux_session": "cam-<id>",
  "text": "...", "status": "sent", "timeout_s": 600,
  "target_id": "...", "target_name": "...", "expect_reply": true,
  "ts": "2026-05-25T08:25:24Z" }

// On delivery (envelope flip):
{ "msg_id": "e03371f8", "status": "delivered", "ts": "..." }

// Thread turn (also written for each send and reply):
{ "record": "turn", "schema": "camc-msg/1", "msg_id": "e03371f8",
  "seq": 1, "kind": "message", "from_id": "...", "from_name": "...",
  "to_id":  "...", "to_name":  "...", "text": "...", "ts": "..." }

// Delivery sidecar (used by `msg read`):
{ "record": "delivery", "schema": "camc-msg/1", "msg_id": "...",
  "seq": N, "mailbox_id": "agent:<id>", "to_id": "...", "to_name": "...",
  "ts": "..." }

// Read marker (set by `msg read --mark`):
{ "record": "read", "schema": "camc-msg/1", "msg_id": "...", "seq": N,
  "mailbox_id": "agent:<id>", "ts": "..." }
```

### 6.2 Commands

```
camc msg send <to> -t "<text>"             # blocks until reply / timeout
camc msg send <to> -t "<text>" --no-wait   # fire-and-forget
camc msg wait <msg_id>                     # later wait on a prior --no-wait send
camc msg reply <msg_id> -t "<text>"        # append reply to thread (from receiver side)
camc msg read [<msg_id>] [--next] [--mark] # inbox; --next pops oldest unread
camc msg show <msg_id>                     # raw ledger records for one id
camc msg list [--for <to>] [--limit N]     # recent legacy summary
```

`<to>` resolves via `AgentStore.get()`, which tries exact id, exact
`tmux_session`, id prefix, exact name, name prefix, and numeric `camc list`
index. Mailbox id is derived as `agent:<id>` when the receiver resolves,
otherwise `session:<tmux_session>` and finally `label:<arg>`.

### 6.3 State machine (current vs target)

Current (implemented):

```
SEND → ledger has {status:sent} + thread turn (seq=1)
DELIVER → injection succeeded → ledger has {status:delivered}
READ (--mark) → ledger has {record:read, mailbox_id, seq}
REPLY → ledger has thread turn (seq=2, kind=message) + first legacy status=replied
SENDER WAIT detects an existing status=replied, ledger-polls expected replies,
or legacy pane-scrapes and records the first reply; `msg read <id>` replays all turns
```

Target (**F-04**, see §9.4): add an explicit `claim → pending → reply`
state machine — the receiver explicitly *claims* a message before
processing, exposing a `pending` set so a crashed receiver can be
detected by the sender. The current model has no claim record; the
wait loop times out after `timeout_s` (default 600s) with no way to
tell "still processing" from "dead".

### 6.4 Known gaps

- **No claim phase.** Sender can't distinguish slow vs dead receiver. → **F-04**
- **Pane injection is brittle.** If the receiver is mid-Bash, the injected
  text is consumed by the running command. Mitigation today: best-effort
  retry + ledger-as-truth.
- **No fan-out.** `to` is single-recipient. Broadcast requires N sends.
- **No back-pressure.** Sender sees no queue depth.

## 7. Cron — external tick contract

`cron.py` + `cmd_cron_*` in `cli.py`. Backed by `~/.cam/cron.json`.

### 7.1 Commands

```
camc cron add --name N --every 10m  -- <command and args>
camc cron add --name N --daily HH:MM -- ...
camc cron add --name N --at  '2026-05-26T03:00Z' -- ...
camc cron add --name N --in  '30m' -- ...
camc cron list
camc cron rm <name|id>
camc cron tick                             # the external tick entry point
```

### 7.2 The tick

camc never schedules itself. The OS crontab carries a single line
installed by `install_tick()` (block-managed; see `_build_tick_block`):

```
* * * * * HOME=/home/<user> PATH=<captured PATH> /home/<user>/.cam/camc cron tick >> ~/.cam/logs/cron.log 2>&1
```

`camc cron tick` then:

1. Acquires `~/.cam/cron.lock` (fcntl). If held, exit immediately.
2. Loads `cron-config.json` and `cron.json`; corrupt files fail closed.
3. For each `is_due(job, now)` → fork+exec the job's command.
4. `_append_runs(record)` writes to `cron-runs.jsonl` for each fire.
5. `_write_state(...)` updates `cron.state.json` (last fire ts, etc.).
6. `recycle_job(...)` archives one-shot, expired, or over-failed jobs.

### 7.3 Hostname filtering

A job has an optional `host` field. On NFS-shared `~/.cam/cron.json`,
only the host whose `socket.gethostname()` matches the `host` field
actually fires. This mirrors the agent-level `hostname` filter.

### 7.4 Self-heal hook

`ensure_tick_if_needed()` is also called by `camc heal`. If active jobs
exist and the crontab block has been wiped (NFS reset, user crontab edit),
heal re-installs it. If no enabled jobs remain, cron removal may remove the
owned block.

## 8. Tmux transport contract

All session operations go through `transport.py`. Sockets in
`/tmp/cam-sockets/cam-<id>.sock`. tmux binary is pinned at session
creation (`tmux_bin` field) so a per-host tmux upgrade doesn't break
existing sessions.

| Op | Function | Notes |
|---|---|---|
| Create | `create_tmux_session(id, cmd, cwd, env_setup, inherit_env)` | Unsets `TMUX`/`TMUX_PANE` and `CLAUDECODE`; starts a 220x50 session with history-limit 50000 |
| Exists | `tmux_session_exists(id)` | Uses the session's pinned tmux binary and socket when available |
| Capture | `capture_tmux(id, lines=100)` | Captures visible/scrollback content, strips ANSI, falls back to `-a` on sparse output |
| Send text | `tmux_send_input(id, text, send_enter)` | Chunks long payloads; wraps multiline text in bracketed-paste markers |
| Send key | `tmux_send_key(id, key)` | Single-key dispatch |
| Attached | `tmux_is_attached(id)` | "Human in pane" check; suppresses auto-exit |
| Kill | `tmux_kill_session(id)` | Kills the tmux session; higher-level cleanup handles stale sockets/records |

**Multi-tmux-binary support**: `_tmux_bin_for_session(id)` reads the
running server's `argv[0]` so capture/send can target a session that was
created with a different `tmux` binary path than the one in `$PATH` today.
The *choice* of which tmux binary to use at session creation is today
implicit (`_detect_tmux_bin()` first match in `$PATH`); **F-06** makes
that choice explicit per machine — see §9.6.

## 9. Planned features (simplification roadmap)

Forward-looking work items. Each gets a stable feature ID (`F-NN`) so it
can be referenced from issues, commits, and other docs without re-typing
the description. §9.6 is **not** numbered — it documents the current
design that is already in place.

### 9.0 Planned features index

| ID | Feature | Section | Touches | Status |
|---|---|---|---|---|
| **F-01** | Thin CLI (move logic out of `cmd_*` handlers) | §9.1 | `cli.py`, new `messaging.py` / `archive.py` / `preflight.py` / `heal.py` | planned |
| **F-02** | Service module shape (uniform `XService` interface) | §9.2 | all service modules | planned |
| **F-03** | Common `BaseJsonStore` mixin (fcntl + atomic rename) | §9.3 | `storage.py`, `cron.py`, `machine_store.py`, `context_store.py` | planned |
| **F-04** | Mailbox `claim → pending → reply` state machine | §9.4 + §6.3/§6.4 | `messages.jsonl` schema, new `messaging.py` | planned |
| **F-05** | Monitor observer-only + pluggable `rule_engine` | §9.5 + §5 | `monitor.py`, new `rule_engine.py` | planned |
| **F-06** | Per-machine tool resolution (Tier A / Tier B) | §9.6 + §8 | `machines.json` schema, `_preflight` in `cli.py`, `transport.py` tmux discovery | planned |

Cross-refs from earlier sections:

- §6.3 **Target** state machine → **F-04**
- §6.4 **Known gaps** (no claim phase) → **F-04**
- §5 **Monitor loop** (today: detection + auto-confirm + idle in one loop) → **F-05**
- §2 `messages.jsonl` row owner = "inline (`cli.py`)" → **F-01** + **F-04** move it behind a service
- §8 **Multi-tmux-binary support** (per-session pin) → **F-06** supplies the resolution layer that decides which tmux to pin

### 9.1 F-01 — Thin CLI

Today `cli.py` has 5371 lines with logic inside `cmd_*` handlers.
Target: each handler ≤ 20 lines, delegating to a service.

Move targets (already factored or pending):

| `cmd_*` | Move to |
|---|---|
| `cmd_msg`, `_msg_wait_loop`, `_msg_inject`, `_msg_ledger_*` | new `messaging.py` |
| `cmd_heal`, `_kill_all_monitors`, orphan-scan | `monitor.py` (or new `heal.py`) |
| `cmd_archive_*`, `_jsonl_summary` | new `archive.py` |
| `cmd_prune`, `_find_session_in_use` | `storage.py` |
| Preflight (`_preflight`) | new `preflight.py` |

Target: CLI remains pure dispatch; services become independently testable.

### 9.2 F-02 — Service modules

Stable interface for each service:

```python
class XService:
    def __init__(self, store, events, config): ...
    # public API mirrors the camc CLI surface
```

Today the only ones close to this shape are `CronStore` / `AgentStore` /
`EventStore`. The rest are loose-function modules.

### 9.3 F-03 — Common stores (`BaseJsonStore` mixin)

All stores should share:

- `~/.cam/<file>.json` for state, `~/.cam/<file>.json.lock` for fcntl, and
  `<file>.tmp` + atomic rename for writes.
- Optional `.bak` recovery files only if we deliberately add that feature
  later; current stores do not create backups.
- Append-only `.jsonl` for event ledgers.
- Atomic rename on every write.
- `from_dict` / `to_dict` schema accessors.

Target: extract a `BaseJsonStore` mixin. `AgentStore`, `CronStore`,
`MachineStore`, `ContextStore` would all inherit it. Today each
re-implements the locking dance.

### 9.4 F-04 — Mailbox `claim → pending → reply` state machine

Today: `sent → delivered → (turn:reply) → resolved` with no explicit
"receiver is processing".

Target:

```
              ┌──── timeout ────────────┐
              ▼                          │
SENT ──► DELIVERED ──► CLAIMED ──► REPLIED
              │            │
              └─ orphan ───┴─► CANCELLED
```

New ledger record types:

```jsonc
{ "record": "claim",   "msg_id": "...", "by_id": "...", "ts": "..." }
{ "record": "cancel",  "msg_id": "...", "reason": "...", "ts": "..." }
```

Sender's `wait_loop` returns:

- `replied(text)` — normal completion
- `claimed_but_silent(elapsed)` — receiver claimed but exceeded
  `timeout_s` without reply → suggests stuck (not dead)
- `never_claimed(elapsed)` — receiver never claimed → suggests dead

### 9.5 F-05 — Monitor as observer only (pluggable rule engine)

Today `monitor.py` runs detection + auto-confirm + state + idle + auto-exit
+ stuck fallback inside one loop. Target:

```
monitor.capture(session)  ──┐
                            ├─► rule_engine.evaluate(output) → Actions
monitor.last_state          ┘                                  │
                            ┌──────────────────────────────────┘
                            ▼
                  transport.dispatch(Action)
```

`rule_engine` is pluggable — confirm rules, state rules, idle rules all
become discrete evaluators. Monitor only schedules captures and
dispatches actions. Easier to test, easier to extend with new tools.

### 9.6 F-06 — Per-machine tool resolution (Tier A / Tier B)

Today the resolution of external tools is implicit: `claude` / `codex` /
`cursor` are looked up via `$PATH` (after `env_setup`), and `tmux` is
discovered by `_detect_tmux_bin()` then pinned per-session as `tmux_bin`.
This produces hard-to-debug drift when one container has a different
PATH ordering, a stale claude install, or an unexpected tmux version.
F-06 makes the resolution explicit per machine.

**Rationale**: the same agent prompt produces different results on
different machines. Without a registry, `camc run` fails opaquely —
or, worse, runs against the wrong binary version and produces subtly
different output. Blind debugging is expensive; the registry surfaces
drift up front.

**Scope** (intentionally narrow):

- `tmux` (resolution + version expectation)
- The agent tool family (`claude`, `codex`, `cursor`) — search-path list + version expectation

Everything else (`git`, `ssh`, `scp`, `python3`, `bash`, plus anything an
agent itself invokes via Bash) is **out of scope** for F-06. They use
system PATH; missing → standard shell error.

#### Two tiers

| Tier | Definition | Resolution | Missing |
|---|---|---|---|
| **A — managed** | `machines.json` entry has a `tools` field | Explicit `path` / `search_paths` from config | **ERROR** (refuse to launch) |
| **B — standalone** | `machines.json` entry has **no** `tools` field | Fall back to `which <tool>` (after `env_setup`) | **ERROR** for hard deps (tmux + the agent's own tool); **WARN** for anything else |

The presence/absence of `tools` is the **only** discriminator. No new
flag, no new file. NVIDIA `*-container-xterm-*` hosts opt in to Tier A;
local / `bpmpfw` / `hlren` / `vdi-wsl` stay zero-config Tier B.

#### Schema (extension of `machines.json` entry)

```jsonc
{
  "name": "pdx-container-xterm-098",
  "host": "...",
  "port": 3422,
  "env_setup": "source ~/.bashrc",
  "tools": {                                       // optional; presence → Tier A
    "tmux":   { "path": "/usr/bin/tmux",
                "version_expect": "3.4" },          // optional; mismatch → WARN
    "claude": { "search_paths": [                   // ordered; first existing wins
                  "/home/prgn_share/tools/claude-code/bin/claude",
                  "claude"                          // sentinel: "claude" == fall back to which
                ],
                "version_expect": "2.1.x" },
    "codex":  { "search_paths": ["codex"] },
    "cursor": { "search_paths": ["cursor"] }
  }
}
```

- `path` — single pinned location (Tier A `tmux` convention)
- `search_paths` — ordered list, first existing wins; a literal string
  equal to the tool name (e.g. `"claude"`) means "fall back to
  `which claude`" so an entry can mix pinned paths with PATH fallback
- `version_expect` — optional, prefix-glob (`"2.1.x"` matches any
  version starting with `2.1.`); mismatch → WARN + event, never blocks

#### Runtime behavior

- `_preflight(tool, machine)` resolves the tool **before** session
  creation. Resolved path is recorded in the agent record (mirroring
  today's per-session `tmux_bin`; F-06 extends to also pin the agent
  tool path so later capture/send always knows the binary that was
  actually used).
- Each resolution writes an `events.jsonl` entry of type `tool_resolve`
  so drift is visible in history.
- Tier A version mismatch → **WARN** + event, continue.
- Tier A missing path → **ERROR**, refuse to start agent.
- Tier B missing hard-dep (tmux or the agent's own tool) → **ERROR**.
- Tier B missing soft-dep → **WARN**.

#### New CLI surface

```
camc machine fingerprint <name>     # SSH, discover paths/versions for tmux and
                                    # claude/codex/cursor, print a diff vs the
                                    # current machines.json. Does NOT auto-edit
                                    # machines.json — Tier A registry stays
                                    # authoritative; human reviews + commits.
```

Read-only on purpose: in Tier A the registry **is** the contract.
Auto-rewrite would silently mask drift instead of surfacing it.

#### Out-of-scope for F-06 v1 (future)

- **Tool profiles** (`~/.cam/tool_profiles.json` + `tools_profile:
  "<name>"` reference). Container hosts will likely share identical
  `tools` blocks; once the duplication actually hurts, add a profile
  layer. Until then, duplicate the block across the 3–4 NVIDIA
  containers — honest > clever.
- **Auto-rewrite of `machines.json`** by `fingerprint`. Same reasoning.
- **Fingerprinting tools agents invoke via Bash** (git/p4/nvip/make/…).
  Trust system PATH; out of scope.

### 9.7 Cron as external tick — (current design, no work item)

Already the design today. Document as canonical:

- camc owns `cron.json` and `cron tick` behavior.
- camc does **not** own scheduling. The OS (crontab / systemd timer / cam-flow)
  calls `cron tick` at whatever cadence it wants.
- `install_tick()` is a convenience for the common "I want a 1-minute OS cron
  entry" case. It is not required.

## 10. Drift from older docs

Decisions taken while writing this spec:

| Older doc | Status | Action |
|---|---|---|
| `docs/architecture-full.md` (2026-03-27) | Still mostly accurate for cam-side | Keep; this spec supersedes for camc-side specifics |
| `docs/migration-v3-unified.md` | The roadmap that produced today's state | Keep; this spec is the *current state* version of that roadmap |
| `docs/heal-hooks-spec.md` | Implemented; details in §5 (self-heal) + `cmd_heal`; future move per **F-01** | Keep; pointer added |
| `docs/camc-cron-spec.md` | Implemented; details in §7 | Keep; pointer added |
| `docs/exit-stop-kill-migrate-spec.md` | Implemented; commands in §4 + `cli.py` subparsers | Keep |
| `docs/session-id-and-migrate-spec.md` | Implemented; canonical schema in §3 | Keep |
| `docs/auto-confirm-flow.md` + `docs/auto-confirm-strategy.md` | Implemented + extended (1-spam, anchors); §5 | Keep |
| `docs/archive/*` | Already labeled legacy | No action |

No docs were moved or deleted as part of this reorganization. The new
canonical entry point is `docs/camc-spec.md` (this file).

## 11. Quick-reference

```
# Lifecycle
camc init                                # first-time setup
camc run "<prompt>" -n <name> --tool claude --path ...
camc list                                # current host only
camc status <id>
camc attach <id>                         # tmux attach
camc exit  <id>                          # graceful /exit, tmux stays
camc stop  <id>                          # SIGTERM tool, tmux stays
camc kill  <id>                          # tear down session
camc reboot <id>                         # restart with --resume
camc migrate <id> [--to host:port]       # move/restart with --resume

# Interact
camc capture <id> [--lines N]
camc send <id> -t "<text>"
camc key <id> --key Enter

# Inter-agent
camc msg send <to> -t "..."
camc msg read [<id>] [--next] [--mark]
camc msg reply <id> -t "..."

# Cleanup / maintenance
camc heal                                # restart dead monitors on this host
camc heal --upgrade                      # kill all monitors so they pick up a new binary
camc prune                               # drift fix; --orphans also removes records
camc rm <id> [--archive]
camc archive list
camc archive info <id-or-archive>
camc archive summary <id-or-archive>
camc archive show <id-or-archive>

# Scheduling
camc cron add --name <n> --every 10m -- <cmd>
camc cron tick                           # called by OS crontab
camc cron list
camc cron rm <id-or-name>

# Multi-machine
camc machine list
camc machine add <name>
camc machine rm <name>
camc machine edit <name>
camc machine ping [name]
camc context list
camc context add <name>
camc context rm <name>
camc sync                                # push binary + configs to remote machines
```

## 12. Doc map

This file is the **canonical current spec** for camc. It cites the existing
detailed docs in place rather than copying their content:

- Architecture diagram (full system, cam + camc + transport): `docs/architecture-full.md`
- Mermaid version: `docs/architecture-mermaid.md`
- Roadmap that produced today's state: `docs/migration-v3-unified.md`
- Heal details (Phase 1 / 2 / 2.5 / 3): `docs/heal-hooks-spec.md`
- Cron details (parsing grammar, lock, hostname filter): `docs/camc-cron-spec.md`
- Exit / stop / kill / migrate semantics: `docs/exit-stop-kill-migrate-spec.md`
- Session ID + migrate edge cases: `docs/session-id-and-migrate-spec.md`
- Auto-confirm rules & patterns: `docs/auto-confirm-flow.md`, `docs/auto-confirm-strategy.md`, `docs/claude-permission-patterns.md`
- Postmortems: `docs/postmortem-agent-lifecycle-bugs.md`, `docs/cases/`
- Legacy (do not depend on): `docs/archive/`
