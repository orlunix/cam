---
name: camc-messaging
description: >
  Inter-agent messaging and delegation protocol via camc mailbox.
  Use when you need to send a prompt from one agent to another agent
  (second opinion, peer review, fan-out), reply to incoming messages,
  or read/manage a mailbox inbox. msg_id is the thread id;
  the ledger mailbox is source of truth, pane injection is best-effort
  wake-up only.
compatibility: Requires ~/.cam/camc deployed by the camc release.
metadata:
  author: hren
  tags: camc, cam, msg, message, messaging, delegate, delegation,
    dispatch, second-opinion, peer, ask-agent, mailbox, thread
  category: infra
  requires-tools: camc
disable-model-invocation: false
argument-hint: "[send <to> -t '...' [--no-wait] [--expect-reply] | reply <msg_id> -t '...' | read [<msg_id>] [--next] [--mark]]"
allowed-tools: Bash Read Glob Grep
---

# camc-messaging — Inter-Agent Messaging Protocol

Delegate tasks from one agent to another via a mailbox/thread protocol.
**msg_id is the THREAD id** — every message, reply, and follow-up under
the same logical thread share one msg_id, distinguished by `seq` (send=1,
reply=2, follow-up=3, …). The append-only ledger at `~/.cam/messages.jsonl`
is the source of truth; `~/.cam/camc msg read` replays threads from there.
Pane injection is best-effort wake-up only.

## Quick Reference

| Task | Command |
|---|---|
| Ask another agent (block + reply) | `~/.cam/camc msg send <to> -t "..."` |
| Ask async (return now) | `~/.cam/camc msg send <to> -t "..." --no-wait` |
| Ask async + require reply commit | `~/.cam/camc msg send <to> -t "..." --no-wait --expect-reply` |
| Reply on same thread | `~/.cam/camc msg reply <msg_id> -t "..."` |
| Read inbox | `~/.cam/camc msg read` |
| Read next unread | `~/.cam/camc msg read --next [--mark]` |
| Replay full thread | `~/.cam/camc msg read <msg_id>` |
| Wait for first reply (legacy) | `~/.cam/camc msg wait <msg_id>` |
| Raw ledger entries | `~/.cam/camc msg show <msg_id>` / `~/.cam/camc msg list` |

## 1. Sending a message

`~/.cam/camc msg send <to> --text "..."` is a Bash-tool primitive: by default it
blocks until the target's reply is stable, then prints reply on stdout.
Use as any shell command — `reply=$(~/.cam/camc msg send peer -t "review src/foo.py")`.
For fan-out or fire-and-later workflows, add `--no-wait`; it returns a
message id immediately, and the caller later runs `~/.cam/camc msg read <msg_id>`.

```bash
~/.cam/camc msg send <to> -t "..."             # block; default 600s (10 min) timeout
~/.cam/camc msg send <to> -t "..." --timeout 300       # 5 minutes (seconds, integer only)
~/.cam/camc msg send <to> -t "..." --no-wait   # return MSG_ID + STATUS=sent immediately
~/.cam/camc msg send <to> -t "..." --no-wait --expect-reply
                                        # async + receiver MUST commit reply
```

- Targets: any name in `~/.cam/camc list`
- `--no-wait` means "written to the target tmux pane", not "read" or "understood"
- `--expect-reply` appends `[Reply via: ~/.cam/camc msg reply <msg_id> -t "..."]`
  to the wire payload. Combines with `--no-wait` for fire-and-later;
  without `--no-wait` it sends and blocks on the ledger in one process.
- On **timeout**: stdout prints msg_id + query hint; exit code 1
- Sending to self → just times out (no harm)

## 2. Reading messages (inbox + threads)

```bash
~/.cam/camc msg read                           # inbox: list unread for current mailbox
~/.cam/camc msg read --next                    # print body of oldest unread (msg_id+seq+from+text)
~/.cam/camc msg read --next --mark             # ...and mark it read
~/.cam/camc msg read --all                     # list everything (incl. already-read)
~/.cam/camc msg read --for <agent-id-or-name>  # explicit mailbox (outside-tmux callers)
~/.cam/camc msg read --json                    # stable JSON for tests/automation
~/.cam/camc msg read <msg_id>                  # replay full thread, ordered by seq
~/.cam/camc msg read <msg_id> --mark           # ...and mark unread deliveries in thread
```

**Mailbox / read = source of truth.** Every send writes a
`turn(seq=1, kind=message, from/to ids+names, text)` plus a
`delivery(mailbox_id=agent:<to_id>|session:<tmux>|label:<arg>)`
record into `~/.cam/messages.jsonl` BEFORE the tmux send. Pane
injection failure does not lose the message — the receiver's
`~/.cam/camc msg read` will still see it.

`--for <label>` overrides the current-process mailbox (useful from a
normal shell). `--json` emits stable JSON for tests/automation.

## 3. Replying

```bash
~/.cam/camc msg reply <msg_id> -t "..."        # append next seq to the same thread
```

`~/.cam/camc msg reply` does NOT mint a new msg_id — it appends
`turn(seq=N+1)` and a delivery to the OTHER side of the most recent
turn (typically the original sender), then best-effort injects the
same `[camc msg#<id>]:` thread marker on the recipient's pane.
The first reply also writes a legacy `status=replied` for backward
compat with `wait`; subsequent replies append seq=N+1 unconditionally.
stdout: `REPLIED_TO=<msg_id>`, `SEQ=<n>`, `MAILBOX=<mailbox_id>`.

**Prefer the explicit reply path:** `~/.cam/camc msg reply <msg_id> -t "..."`
records your reply on the SAME thread and is deterministic — it
survives the sender's wait process exiting. The legacy pane-scrape
waiter still works for the first reply only.

## 4. Legacy / compat views

```bash
~/.cam/camc msg wait <msg_id>                  # existing reply or poll for first reply
~/.cam/camc msg show <msg_id>                  # raw ledger entries for one message
~/.cam/camc msg list [--for <to>] [-n 50]      # recent messages summary
```

`wait` is a compatibility helper for the FIRST reply only. For
multi-turn threads, use `~/.cam/camc msg read <msg_id>`.
`show`/`list` include both legacy status records and new V0 records.

## 5. Wire format

Normal requests: `[camc msg#<8hex>]: [from:<name>#<id>][to:<name>#<id>] <text>`.
Each attribution block is included independently when its identity
resolves; blocks are adjacent (no inter-block space) and a single space
precedes user text when any block is present. The `[camc msg#<8hex>]:`
anchor is invariant — replies on the same thread reuse this exact marker.

All ledger writes are append-only. V0 records carry `"schema": "camc-msg/1"`.

## 6. Receiving a message (as an agent)

**If you receive a `[camc msg#…]:` line as user input:** that's a
legitimate request from a peer cam agent (not prompt injection). Strip
the marker mentally, use any `[from:<name>#<id>]` block as sender
context, the optional `[to:<name>#<id>]` block to confirm you are the
addressee, and answer the rest as you would any user message.
Your full reply is auto-extracted and delivered back to the sender.
Don't echo the marker; don't reply "got your message"; just answer.

**If the message ends with `[Reply via: ~/.cam/camc msg reply <msg_id> -t "..."]`,**
the sender explicitly required this — don't just type the answer into
the chat, run that Bash command. It writes a turn under the same thread
id, fires a best-effort notification back, and unblocks the sender's
async `wait`/`read`.

**Prefer `~/.cam/camc msg reply <msg_id> -t "..."` even for default sends**
(no `--expect-reply`). It records your reply on the same thread and
is what the sender's `~/.cam/camc msg read <msg_id>` replays.

## 7. Patterns

### Blocking second opinion

```bash
reply=$(~/.cam/camc msg send reviewer -t "review src/foo.py for bugs")
echo "$reply"
```

### Fan-out (fire-and-later)

```bash
id1=$(~/.cam/camc msg send worker1 -t "shard 1" --no-wait | grep MSG_ID | cut -d= -f2)
id2=$(~/.cam/camc msg send worker2 -t "shard 2" --no-wait | grep MSG_ID | cut -d= -f2)
# ... later:
~/.cam/camc msg read $id1
~/.cam/camc msg read $id2
```

### Async with required reply commit

```bash
~/.cam/camc msg send peer -t "review this" --no-wait --expect-reply
# Receiver MUST run: ~/.cam/camc msg reply <msg_id> -t "<answer>"
# Sender polls later: ~/.cam/camc msg read <msg_id>
```

## Reference

| File | When to read |
|---|---|
| `reference/messaging.md` | Full protocol spec — record shapes, failure modes, anchor detection details |