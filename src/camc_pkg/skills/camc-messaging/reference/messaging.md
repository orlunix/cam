# `camc msg` — inter-agent messaging protocol

Send a request to another running cam agent. The append-only ledger at
`~/.cam/messages.jsonl` is the source of truth; the tmux-pane injection
is a best-effort wake-up notification.

**msg_id is the THREAD id.** The original send writes `seq=1`; every
`camc msg reply <msg_id>` appends `seq=N+1` under the SAME msg_id
(no new logical id is minted). `camc msg read <msg_id>` replays the
whole thread sorted by seq.

**Mailbox = source of truth.** Every send/reply writes a `turn` record
plus a `delivery` record into the ledger BEFORE the tmux send. If the
pane injection fails, the message is still in the mailbox; the
receiver's `camc msg read` will see it. `camc msg read` (no args) lists
unread deliveries for the current mailbox; `--mark` appends `read`
records so subsequent reads omit them.

By default the sender blocks on the reply; with `--no-wait`, it returns
a message id immediately and a later `camc msg read <msg_id>` (or the
legacy `camc msg wait <msg_id>` for the first reply only) retrieves
the reply. The mechanism is a normal Bash subprocess — no daemon, no
special runtime.

Shape:
```
A's Claude → Bash tool → camc msg send <B> → poll B's tmux
                                            → extract reply
                                            → print(reply); exit 0
A's Bash tool ← stdout ← exit
A's Claude ← tool_result
```

Identical to `$(curl http://slow.example.com)` semantically — `camc msg
send` just refuses to exit until the reply is ready.

Async shape:
```
A's Claude → Bash tool → camc msg send <B> --no-wait
                                            → inject request
                                            → print MSG_ID + STATUS=sent; exit 0
... later ...
A's Claude → Bash tool → camc msg wait <msg_id> → poll B's tmux
                                                 → print(reply); exit 0
```

Phase 1 expected-reply async shape (deterministic):
```
A's Claude → Bash tool → camc msg send <B> --no-wait --expect-reply
                                            → inject request + receiver
                                              instruction
                                            → print MSG_ID + STATUS=sent
                                              + EXPECT_REPLY=yes; exit 0
B's Claude reads request → … does work … → Bash tool →
                       camc msg reply <orig_id> -t "<final answer>"
                                            → notify A back via no-wait
                                              + append `replied` ledger
                                              record on orig_id
... later ...
A's Claude → Bash tool → camc msg wait <orig_id> → poll LEDGER (not pane)
                                                  → print(reply); exit 0
```

## CLI

```bash
camc msg send <to> --text "..."           # synchronous, reply on stdout (default 600s)
camc msg send <to> --text "..." --timeout 300     # 5 min (integer seconds)
camc msg send <to> --text "..." --timeout 1800    # 30 min
camc msg send <to> --text "..." --no-wait # async: returns MSG_ID + STATUS=sent
camc msg send <to> --text "..." --no-wait --expect-reply
                                          # async + receiver MUST commit reply
camc msg reply <msg_id> --text "..."      # append next seq to the SAME thread
camc msg read                             # inbox: list unread for current mailbox
camc msg read --next                      # print body of oldest unread (msg_id+seq+from+ts+text)
camc msg read --next --mark               # …and mark it read
camc msg read --all                       # include already-read in the listing
camc msg read --for <agent-id-or-name>    # explicit mailbox (works outside tmux)
camc msg read --json                      # stable JSON for tests/automation
camc msg read <msg_id>                    # replay full thread, sorted by seq
camc msg read <msg_id> --mark             # …and mark unread deliveries in this thread
camc msg wait <msg_id>                    # legacy: first-reply only (compat)
camc msg wait <msg_id> --timeout 300      # wait up to 5 min
camc msg show <msg_id>                    # raw ledger history for one message
camc msg list [--for <to>] [-n 50]        # recent messages summary
```

Default `send`/`wait` timeout is 600 seconds (10 min) — chosen to
match the Claude bash tool's configurable maximum. `--timeout`
accepts integer seconds only; no `5m`/`2h` suffix parsing.

`--no-wait` stdout is intentionally minimal and parseable:

```text
MSG_ID=<8hex>
STATUS=sent
```

`STATUS=sent` means camc wrote the marker into the target tmux pane and
recorded `sent`/`delivered` ledger entries plus a V0 `turn(seq=1)` and
`delivery` for the recipient mailbox. It is not a read receipt and does
not prove the target understood the request.

With `--expect-reply` an additional line is emitted:

```text
EXPECT_REPLY=yes
```

The sent ledger record gains `expect_reply: true`. Subsequent
`camc msg wait <msg_id>` switches to polling the ledger for a
`replied` record (committed by the receiver via `camc msg reply`)
instead of pane-scrape. This trades minimal receiver compliance (one
extra Bash call) for full determinism: no flaky text extraction, no
pane scrollback issues. With `--expect-reply` and no `--no-wait`,
`send` just send-injects then ledger-polls in one process.

`camc msg reply <msg_id> -t "..."` appends a turn under the SAME
thread:
- looks up the thread (sent record OR any prior turn for msg_id);
  exits 1 if neither exists.
- computes `next_seq = max(turn.seq for msg_id) + 1`.
- recipient = the OTHER party of the highest-seq turn (falls back to
  the original sent record's `sender_*` for legacy ledgers).
- appends `turn(seq=next_seq, kind=message, from/to ids+names, text)`
  and `delivery(mailbox_id=…)` records — these are the source-of-truth
  for `camc msg read`.
- on FIRST reply only, also appends a legacy `status=replied` so
  existing `camc msg wait` keeps working. Subsequent replies append
  another turn unconditionally — no idempotency block.
- best-effort tmux notification reuses the SAME `[camc msg#<msg_id>]`
  thread marker (no new logical id, no `[reply_to:…]` block). Failure
  is non-fatal because the mailbox already has the message.
- stdout: `REPLIED_TO=<msg_id>`, `SEQ=<n>`, `MAILBOX=<mailbox_id>`.

`camc msg read` is the new conceptual API:
- No args → list unread deliveries for the current mailbox (table
  with msg_id / seq / ts / from / preview).
- `--next` → print body of oldest unread, header includes msg_id and
  seq so you can reply with `camc msg reply <msg_id>`.
- `--mark` → append `read` records for everything just listed/printed.
- `--all` → include already-read entries in the listing.
- `<msg_id>` → replay the full thread, ordered by seq (not insertion).
  Pure replay does NOT require a mailbox identity, so scripts running
  outside tmux can `camc msg read <msg_id>` without `--for`. `--mark`
  on the replay path DOES require an identity (or `--for`) since it
  writes mailbox-scoped read records.
- `--for <label>` → resolve mailbox via AgentStore (id / name) with
  fallback candidates for raw `agent:<id>`, `session:<tmux>`, and
  `label:<arg>`.
- `--json` → stable JSON output for tests/automation.

`camc msg wait <msg_id>` is now a compatibility helper for the FIRST
reply on a thread — it prints any existing `replied` record or polls
for one. For multi-turn threads (or to bypass the legacy semantics),
use `camc msg read <msg_id>`.

`camc msg show <msg_id>` and `camc msg list` remain raw views over
`~/.cam/messages.jsonl`. They include both legacy status records
(`sent`, `delivered`, `replied`, `timeout`) and V0 thread records
(`turn`, `delivery`, `read`). All V0 records carry
`"schema": "camc-msg/1"` for forward extensibility.

## Ledger record types (V0)

Beyond the legacy status records, the V0 schema appends three new
record kinds to `~/.cam/messages.jsonl`:

```jsonl
{"record":"turn","schema":"camc-msg/1","msg_id":"<thread>","seq":1,
 "kind":"message","from_id":"…","from_name":"…",
 "to_id":"…","to_name":"…","text":"…","ts":"…"}
{"record":"delivery","schema":"camc-msg/1","msg_id":"<thread>","seq":1,
 "mailbox_id":"agent:<to_id>","to_id":"…","to_name":"…","ts":"…"}
{"record":"read","schema":"camc-msg/1","msg_id":"<thread>","seq":1,
 "mailbox_id":"agent:<id>","ts":"…"}
```

`mailbox_id` preference is `agent:<id>` > `session:<tmux_session>` >
`label:<name-or-arg>`. `seq` increments per turn under the same
msg_id; `delivery` and `read` reference a turn by `(msg_id, seq)`
(no separate mail_id). Unread = delivery without a matching read for
the same mailbox_id/msg_id/seq.

## Wire format

When A sends, the request is injected into B's tmux pane via
`camc send <B> --text`:

```
[camc msg#abc12345]: [from:<name>#<id>][to:<name>#<id>] <user-supplied text>
```

`abc12345` = first 8 hex chars of uuid4. Marker is unique enough (32-bit
collision space) for ~100 concurrent in-flight messages with negligible
collision risk. B sees this as a normal user input line.

The `[from:<name>#<id>]` block is added automatically when `camc msg
send` is run from inside a registered camc agent tmux pane. `name` and
`id` come from `agents.json` via the sender's current tmux session.
The `[to:<name>#<id>]` block is added when the target identifier
resolves to a known agent in the same `agents.json` (so the receiver
can confirm the message was addressed to them by name/id, not just
dispatched to a raw tmux session). Each block is included independently
— either, both, or neither may be present. When both are present they
sit adjacent with no inter-block space; a single space precedes the
user text whenever at least one block is present. The `[camc msg#<id>]:`
anchor itself is invariant so older messages — including ones without
attribution blocks at all — still parse correctly. If neither identity
resolves the payload reduces to:

```
[camc msg#abc12345]: <user-supplied text>
```

Reply notifications generated by `camc msg reply` add a correlation block
after the available attribution blocks:

```
[camc msg#def67890]: [from:<name>#<id>][to:<name>#<id>][reply_to:abc12345] <reply text>
```

B replies the same way it would reply to any user — its full assistant
turn becomes the reply text.

## Reply extraction algorithm

V0 intentionally stays pane-level and tool-agnostic: it does not require
a positive Claude/Codex/Cursor "done" pattern. The only adapter hook is
an optional negative busy guard when the target is a known camc agent.

```
extract_response(capture, msg_id) -> text
  1. Split capture into lines.
  2. anchor = index of first line matching r"\[camc msg#<msg_id>\]:"
     If not found in the CURRENT capture → keep polling. A memoized
     "seen before" marker is not enough, because long replies can push
     the marker out of the 500-line window.
  3. response_lines = trim_footer(lines[anchor+1:])
     trim_footer drops trailing UI:
     - blank lines
     - bare ❯ / › / >
     - ❯/› prompt prefixes with placeholder text
     - "for shortcuts", "esc to interrupt", "context left",
       "Press enter to continue"
     - horizontal rules
     - indented status bars shaped like "model · ~/path"

     ASCII `>` is stripped only when it is the whole line; markdown
     blockquotes such as `> quote` are preserved.
  4. text = "\n".join(response_lines).rstrip()
  5. If text is empty → keep polling.
  6. Return text.
```

## Stable detection (the reply-is-done check)

Polling alone isn't enough — even after the agent stops generating, tmux
can redraw briefly. So waiter requires:

```
stable_for_N_captures(text, N=4, poll=5s):
  hash of text == previous hash for N consecutive ticks
```

V0 uses N=4 with a 5s poll interval, so the settle window is about 20s.
Before counting a tick as stable, the waiter also checks the visible tail
for busy signals:

- generic strong signals: `esc to interrupt`, spinner glyphs, or TUI
  progress labels such as `Working...`, `Thinking…`, `Working (12s)`
- optional target adapter `busy_pattern`, used only as a negative guard

Generic words like `working` in normal reply content are not busy signals.
For example, `working tree` must not block completion.

## Waiter loop (V0)

```python
def wait_for_response(agent_id, msg_id, *, timeout=600, poll=5.0,
                      stable_for=4):
    deadline = time.time() + timeout
    last_hash, stable_count = None, 0
    saw_marker_ever = False
    while time.time() < deadline:
        cap = capture_tmux(agent_id, lines=500)
        text = extract_response(cap, msg_id)
        if not text:
            stable_count, last_hash = 0, None
            time.sleep(poll)
            continue
        saw_marker_ever = True
        if generic_busy_tail(cap) or adapter_busy_tail(cap):
            stable_count, last_hash = 0, None
            time.sleep(poll)
            continue
        h = hashlib.md5(text.encode()).hexdigest()
        if h == last_hash:
            stable_count += 1
        else:
            stable_count, last_hash = 0, h
        if stable_count >= stable_for:
            return text, "complete"
        time.sleep(poll)
    if not saw_marker_ever:
        return None, "no_marker"
    return None, "timeout"
```

`camc msg wait <msg_id>` first checks the ledger; if a `replied` record
is already present, it prints that saved reply immediately and exits 0.
For `expect_reply` messages, `wait` polls the ledger until `camc msg
reply` commits that record. For legacy/non-expected-reply messages,
blocking `send` and `wait` use the pane-scrape waiter.

## Ledger (`~/.cam/messages.jsonl`)

Append-only — every send / delivery / reply / timeout writes one line
in the same JSONL file. Each line is a JSON record:

```json
{"msg_id":"abc12345","ts":"...","to":"<agent>","tmux_session":"cam-...","text":"...","sender_name":"<name>","sender_id":"<id>","sender_tmux_session":"cam-...","status":"sent","timeout_s":600}
{"msg_id":"abc12345","ts":"...","status":"delivered"}
{"msg_id":"abc12345","ts":"...","status":"replied","reply":"..."}
{"msg_id":"abc12345","ts":"...","status":"timeout","reason":"reply not stable","elapsed_s":600}
```

`status` transitions: `sent → delivered → (replied | timeout)`.

Query the ledger:

```bash
camc msg show <msg_id>      # all events for one message
camc msg wait <msg_id>      # block until reply, or print saved reply if already replied
camc msg list -n 20         # last 20 messages with latest status
camc msg list --for <to>    # filter by recipient
```

On timeout, blocking `camc msg send` and `camc msg wait` print to stdout:

```
[camc msg#<id>] timed out after <N>s (<reason> on '<to>').
  Check status later:  camc msg show <id>
  Recent messages:     camc msg list
  Peek target's pane:  camc capture <to>
```

…and exits 1. The calling Claude tool sees this as the tool result
and can react (retry, give up, peek the target's pane, etc.).

No `from` field is recorded; the marker doesn't carry it. If sender
identity matters, the sender writes it into `--text`.

## Failure modes & recovery

| Status | Cause | Recovery |
|---|---|---|
| `no_marker` | injection failed (camc send error) or B rotated buffer past it | retry; check B running with `camc status <to>` |
| `timeout` | B busy / stuck / slow | longer `--timeout`; check B with `camc capture <to>` |
| ledger `delivered`, no terminal status | async send, sender process killed mid-wait, or no waiter started yet | run `camc msg wait <msg_id>` |
| sender → self | A blocked, message in own pane unconsumed | times out cleanly; degenerate but not a hard deadlock |

## Why no `CAM_AGENT_ID`

Earlier draft auto-injected sender ID via env var. Dropped because:
- Receiver doesn't need it (just answers a tagged user message)
- Self-dispatch handled by timeout, not pre-flight
- Senders that DO want attribution can write it into `--text`

## Comparison to TeaSpirit `block_renderer`

| Concern | TeaSpirit | `camc msg` |
|---|---|---|
| Anchor | First 20 normalized chars of user prompt (fuzzy) | `[camc msg#<8-hex>]:` (deterministic; the `camc` keyword in the marker signals to receivers that this is legitimate inter-agent traffic, not prompt injection) |
| Output format | Adaptive Card blocks | Plain text |
| Streaming | Per-block flush during generation | Single final reply only |
| Stable check | Last block hash unchanged for 2 calls | Trimmed reply hash unchanged for 4 captures at 5s poll |
| Turn complete | Empty prompt + done verb + status idle | No positive done/prompt gate; uses stable trimmed reply plus negative busy-tail guards |
| Header trim | Find anchor line, drop everything before | Same (literal regex match) |
| Footer trim | Drop UI noise below content | Generic TUI trim; preserves markdown `> quote` and math/product lines with ` · ` |

Same broad idea, fewer transport dependencies: deterministic anchor,
plain text output, and conservative settle timing.

## Concurrency

V0: each blocking `camc msg send` or `camc msg wait` is its own
subprocess + own polling loop. `camc msg send --no-wait` only injects and
returns; no background waiter is started. N concurrent waits = N
subprocesses. Acceptable up to ~10 parallel without infrastructure
changes.

V1: when fan-out becomes common, consolidate into a per-host
`MessageRouter` daemon (started by `cam serve` or always-on) that
multiplexes captures and dispatches replies via msg_id correlation.

## Anti-patterns

- ❌ Writing literal `[camc msg#xxxxxxxx]:` in normal output (fakes a marker)
- ❌ Wrapping every Bash command in `camc msg send` (only use when delegating)
- ❌ Polling `camc capture <to>` instead of `camc msg send` (defeats the purpose)
- ❌ Replying via direct `camc send <a>` from B's shell instead of letting natural reply flow
