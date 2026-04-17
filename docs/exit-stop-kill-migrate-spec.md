# Feature Spec: Exit / Stop / Kill / Migrate Redesign

## Overview

Clarify and separate agent lifecycle commands into three severity levels,
plus cross-machine migration.

## Commands — Three Levels (camc)

### `camc exit <id>` — Graceful exit (NEW)

Politely ask Claude to exit. tmux session stays alive.

```
1. Esc × 3 (interrupt sub-agents / tool calls)
2. Ctrl+C (interrupt generating response)
3. Wait 2s, verify ❯ prompt on screen
4. /exit (Claude's built-in exit command)
5. Wait up to 10s for Claude process to exit
6. If still alive → fall through to stop behavior
```

After exit: tmux session has a shell prompt. Can `camc run --resume` in it.

### `camc stop <id>` — Kill Claude process (CHANGED)

Kill the Claude process directly. tmux session stays alive.

```
1. Find Claude PID inside tmux (_find_claude_pid)
2. kill -9 <pid>
3. tmux session remains with shell prompt
```

Current behavior: kills tmux session. **Change to: kill process only.**

### `camc kill <id>` — Kill tmux session (UNCHANGED)

Nuclear option. Kill entire tmux session. Agent is gone.

```
1. Kill monitor
2. tmux kill-session
3. Mark agent as killed in agents.json
```

This is the current behavior of both stop and kill. After this change,
only kill does this.

### Summary

| Command | Claude | tmux | Can resume? |
|---------|--------|------|-------------|
| `exit`  | /exit (graceful) | alive | yes |
| `stop`  | kill -9 process | alive | yes (but unclean) |
| `kill`  | kill -9 process | **killed** | no |

## `camc run --resume <session-id>` (NEW flag)

Add `--resume` to `camc run` parser. When specified:
- Add `--resume <session-id>` to Claude launch command
- Skip prompt injection (Claude will resume conversation)
- Record session_id in agents.json

```bash
camc run --name l1tcm --path /home/hren --tag NR10 --resume d114c7c4-c500-4047-bd17-b42a57800b64
```

## `camc reboot <id>` (UPDATED)

Internally: `camc exit` + `camc run --resume` in same tmux.

```
1. camc exit <id>        → Claude exits, shell stays
2. cd <workdir>          → restore original path
3. claude --resume <session-id>  → start new Claude in same tmux
4. Update agents.json    → same ID, same session_id
```

Agent ID does not change. tmux session does not change.

## `cam migrate <id> --to <machine>` (NEW — cam server only)

Cross-machine migration. Only cam can do this (knows SSH info for all machines).

```
1. SSH to source machine: camc exit <id>
2. Session file is on NFS (shared) — no scp needed for PDX↔PDX
   For non-NFS: scp session .jsonl file to target machine
3. SSH to target machine: camc run --name <same> --tag <same> --path <same> --resume <session-id>
4. Update cam DB: agent now on target machine
```

`cam migrate` without `--to` = delegate to `camc reboot` on the agent's machine.

### Implementation

cam server side (src/cam/cli/agent_cmd.py):

```python
def migrate(agent_id, to=None):
    if to is None:
        # Local reboot — delegate to camc reboot
        delegate = resolve_delegate(agent)
        delegate._run(["reboot", agent_id])
    else:
        # Cross-machine
        src_delegate = resolve_delegate(agent)  # source machine
        dst_delegate = CamcDelegate(host=to_host, port=to_port, user=to_user)
        
        # Exit on source
        src_delegate._run(["exit", agent_id])
        
        # Get session info
        session_id = agent.session_id
        
        # Start on target
        dst_delegate._run(["run", "--name", name, "--path", workdir,
                          "--tag", tags, "--resume", session_id])
        
        # Update cam DB
        agent.machine_host = to_host
        agent.machine_port = to_port
        agent_store.save(agent)
```

## Implementation Order

### Phase 1 (camc — do first)
1. `camc exit` — new command using existing graceful_exit()
2. `camc stop` — change to kill process only (not tmux)
3. `camc run --resume` — add flag to parser
4. `camc reboot` — update to use exit + run --resume

### Phase 2 (cam — after phase 1)
5. `cam migrate --to` — delegate to source exit + target run --resume
6. `cam migrate` (no --to) — delegate to camc reboot

## Preflight Check Fix

`camc run` currently checks if `claude` is in PATH before starting.
This check uses the current shell's PATH, not the env_setup from context.
Fix: skip the check if env_setup is configured (it will set PATH in tmux).

## Testing

```bash
# exit
camc exit <id>              # Claude exits, tmux alive
camc capture <id>           # Should show shell prompt, not Claude

# stop
camc stop <id>              # Claude killed, tmux alive
camc capture <id>           # Should show shell prompt

# kill
camc kill <id>              # Everything gone
camc capture <id>           # Should fail (no tmux)

# run --resume
camc run --name test --path /tmp --resume <session-id>
camc send test -t "what did we discuss?"   # Should remember

# reboot
camc reboot <id>            # Same ID, same tmux, memory preserved

# cam migrate
cam migrate l1tcm --to pdx-098:3422   # Cross-machine, memory preserved
```

## Backward Compatibility

- `camc kill` behavior unchanged
- `camc stop` changes (kills process, not tmux) — announce in changelog
- Old agents without session_id: exit/stop/kill work normally, resume not available
