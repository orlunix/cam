# Finding the Claude session for an agent

A Claude agent normally stores its conversation JSONL at:

```
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
```

The path encoding replaces `/`, `.`, `_` with `-`. Example:

```
/home/hren/.openclaw/workspace/camflow
              ↓
-home-hren--openclaw-workspace-camflow      (`. `_` `/` all → `-`)
```

## List every session for a path

```bash
ENCODED=$(echo "/home/hren/.openclaw/workspace/camflow" | sed 's/[/._]/-/g')
ls -lt ~/.claude/projects/$ENCODED/*.jsonl
```

Check the agent’s recorded session ID and runtime home first. Isolated/API
homes may place projects under a different root. Modification time alone
cannot bind a transcript when multiple agents share a workspace. Each file is one session;
its name is the session-id (UUID).

## Map agent → session-id (camc-managed agents)

```bash
~/.cam/camc --json list | python3 -c "
import json, sys, os
for a in json.load(sys.stdin):
    sid = a.get('session_id') or '?'
    path = a.get('context_path','')
    enc = path.replace('/', '-').replace('.', '-').replace('_', '-')
    f = os.path.expanduser(f'~/.claude/projects/{enc}/{sid}.jsonl')
    print(a['id'][:8], a.get('status'), sid, 'OK' if os.path.exists(f) else 'MISSING')
"
```

Synthetic session-ids look like `<agent-id>-0000-0000-0000-000000000000`
(8-char agent ID padded to UUID). Real ones look like
`8c84ca2e-1343-47ca-a87d-ca275fdd440e`.

## Inspect a session (last assistant message)

```bash
SID=940969a8-0000-0000-0000-000000000000
ENCODED=-home-hren--openclaw-workspace-camflow
tail -1 ~/.claude/projects/$ENCODED/$SID.jsonl | python3 -m json.tool | head -30
```

Or extract the conversation in Q/A order via:

```bash
~/.cam/camc archive show <archive-name> | less
```

## Resume a session in a fresh agent

```bash
~/.cam/camc run --tool claude \
  --path /home/hren/.openclaw/workspace/camflow \
  --name <name> \
  --resume <session-id>
```

The `--resume` flag passes through to `claude --resume`, which reads
the JSONL and rebuilds the agent's full memory before accepting new
input. The new agent gets a new agent ID and tmux session, but its
conversation history is the old one.

## Multiple sessions in the same workspace

`~/.claude/projects/<encoded-cwd>/` may contain many `.jsonl` files —
one per session that's ever run there. They don't overwrite each other
because each has a unique session-id. `claude --resume` without an
explicit ID picks the most recent.

After explicit resume, supervise startup through alive/started as described in
managing-camc. `reboot` starts a fresh tool conversation and does not resume.
