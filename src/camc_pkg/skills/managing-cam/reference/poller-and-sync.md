# Poller and sync mechanics

`cam serve` runs a background `CamcPoller` that keeps cam's SQLite DB in
sync with each machine's `camc agents.json`. Understanding it explains
the "agent reappeared after I deleted it" class of issues.

## What the poller does (every 5s)

For each machine in `~/.cam/machines.json`:

1. SSH → `camc --json list` (ControlMaster pooled)
2. For each agent:
   - **Already in cam DB** → upsert all fields (`save()` / UPSERT)
   - **New, status terminal** (completed/failed/killed/timeout) → **skip**
     (don't pollute cam DB with already-done work)
   - **New, status running** → import (machine_host/user/port stamped)
3. After polling all machines: stale-agent cleanup (see below)

## Stale-agent cleanup

cam DB holds an aggregated view; camc per machine is the source of
truth. A running cam DB row whose ID is **not seen on any camc** for
`_MISS_THRESHOLD = 3` consecutive polls is **deleted**.

- Sightings reset the miss counter (transient SSH glitches don't flap-delete)
- ~15s window (3 × 5s) before deletion fires
- Emits an `AgentEvent` with `event_type="deleted"`,
  `detail={"reason": "missing from camc for 3 consecutive polls"}`

### Before this fix (pre-2026-04-26)

Stale rows were promoted to `status=completed` with
`exit_reason="Session gone (not in camc list)"`. This created two bugs:

1. `cam prune --all` deleted them, next poll re-created them — looked
   like prune wasn't sticking.
2. Workflows that camc-rm'd N agents accumulated N zombie completed
   rows in cam DB.

Now: gone in camc → gone in cam DB. No zombie rows.

## Three-layer machine_host correctness

NFS-shared `~/.cam/agents.json` clusters can leak agents between hosts
unless we're careful. cam handles this in three layers:

1. **Write at creation**: `AgentManager.run_agent()` stamps
   machine_host/user/port from the chosen context — new agents get the
   right host from the start.
2. **Hostname guard + self-healing backfill**: poller compares
   `agent.hostname` to the polled machine via `_is_same_host()` (matches
   `bpmpfw` to `bpmpfw.nvidia.com`). Mismatched agents are skipped and
   never imported. For agents that pass, the poller corrects
   machine_host/port if they don't match — self-heals legacy
   misassignments without manual intervention.
3. **Attach context fallback**: `cam attach <agent>` falls back to the
   context's SSH config when machine_host is missing or `localhost`,
   so attach still works on incomplete records.

## Shadow records

When cam DB has an agent A on machine X (its full record) and another
camc on machine Y reports a record with the same `tmux_session` name,
poller treats Y's record as a "shadow" — updates A's status only,
**never** demotes A to terminal based on Y's view. A's host (X) is
authoritative for A's status.

Why this matters: NFS clusters share `agents.json`, so every host can
see every host's agent. Without the shadow guard, polling host Y would
mark agent A (running on host X) as completed because Y can't see X's
tmux server.

## Events

Every status change emits an event written to `agent_events` table and
broadcast via `EventBus` (subscribed by WebSocket clients on `cam serve`):

- `status_change` — `{"from": "running", "to": "completed"}`
- `deleted` — `{"reason": "..."}` (stale cleanup)
- `imported` — first time poller saw this agent

## Disabling the poller

Currently no flag — kill `cam-serve-daemon.sh` to stop it. Useful when
debugging, otherwise leave running. Reads only — doesn't mutate camc
state, only cam DB.
