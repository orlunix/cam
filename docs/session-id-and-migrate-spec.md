# Feature Spec: Session ID Tracking + Agent Migrate/Reboot

## Overview

Two related features for camc:
1. **Session ID tracking** — each agent gets a deterministic Claude Code session ID
2. **Migrate/Reboot** — safely restart or move an agent with session resume

## 1. Session ID Tracking

### Design

Every camc agent launched with Claude Code gets a session ID derived from its agent ID:

```
agent_id  = "3e65a6d6"
session_id = "3e65a6d6-0000-0000-0000-000000000000"
```

Padding the 8-char agent ID into UUID format. This is deterministic — given an agent ID,
you always know its session ID. No extra lookup needed.

### Changes to `camc run` (cli.py cmd_run)

Add `--session-id` to the Claude launch command:

```python
# In cmd_run, when building the launch command for Claude:
session_uuid = "%s-0000-0000-0000-000000000000" % agent_id
# Add to launch args:
launch_args += ["--session-id", session_uuid]
```

Store in agents.json:

```json
{
  "id": "3e65a6d6",
  "session_id": "3e65a6d6-0000-0000-0000-000000000000",
  "task": { ... }
}
```

### Changes to `camc heal`

When checking agent health, verify session file exists:

```python
session_file = "~/.claude/projects/<project-dir>/<session-uuid>.jsonl"
if not os.path.exists(session_file):
    log.warning("Session file missing for agent %s", agent_id)
```

This is a WARNING only, not an error. Agent may still be functional
(e.g., session file on different NFS mount).

### Changes to `camc status`

Show session ID and session file info:

```
Agent: aicli (3e65a6d6)
Session: 3e65a6d6-0000-0000-0000-000000000000
  File: ~/.claude/projects/-home-hren-test-aicli/3e65a6d6-0000-0000-0000-000000000000.jsonl
  Size: 11 MB
  Last activity: 2 min ago
```

### Backward Compatibility

- Existing agents without session_id: field is empty/missing, treated normally
- session_id is only set for NEW agents launched after this change
- No migration needed for old agents
- All existing functionality unchanged when session_id is missing

## 2. Migrate / Reboot

### Commands

```bash
camc migrate <agent-id>                    # no --to = reboot on same machine
camc migrate <agent-id> --to <host:port>   # move to another machine
```

`migrate` without `--to` = reboot (restart on same machine). One command, one code path.

### Migrate Flow (local = reboot)

```
1. Verify agent is idle (state == "idle"). Refuse if working.
2. Read agent metadata: name, tags, workdir, session_id
3. Graceful exit:
   a. Send Esc × 3 (interval 0.5s)
   b. Wait 2s
   c. Send Esc × 2
   d. Wait 1s
   e. Send Ctrl+C × 2 (interval 0.5s)
   f. Wait for process exit (max 10s)
   g. If still alive → kill tmux session
4. Generate new session UUID (new agent ID)
5. Launch: camc run --name <same> --tag <same> --path <same>
   Claude gets: --resume <old-session-id> --session-id <new-session-uuid>
6. Update agents.json: old entry removed, new entry added
```

### Graceful Exit Function

Standalone function, does NOT touch agent metadata or agents.json:

```python
def graceful_exit(session_id, timeout=15):
    """Safely exit Claude Code in a tmux session.
    
    Sends Esc × 3, waits, Esc × 2, waits, Ctrl+C × 2.
    Returns True if exited cleanly, False if had to kill.
    """
```

This function can also be used by `camc stop` as a gentler alternative
to killing the tmux session directly. But do NOT change `camc stop`
behavior in this PR — keep it as-is.

### Migrate Flow (cross-machine)

Same as reboot, plus:

```
3b. After graceful exit, scp session file to target:
    scp ~/.claude/projects/<dir>/<old-session>.jsonl target:<same-path>/
4-6. Run on TARGET machine instead of local.
```

For NFS-shared machines (PDX ↔ PDX): skip scp, session file already shared.

### What `--resume` Does

Claude Code `--resume <session-id>` loads the conversation history from the
JSONL file and continues from where it left off. The agent retains:
- Full conversation history
- Memory of what it was working on
- Context from previous tool calls

What it does NOT retain:
- Open file handles / running processes
- tmux scrollback from the old session

## Implementation Scope

### camc changes (src/camc_pkg/)

| File | Change |
|------|--------|
| cli.py `cmd_run` | Add `--session-id` to launch command, store in agents.json |
| cli.py `cmd_status` | Show session ID + file info |
| cli.py `cmd_heal` | Check session file exists (warning) |
| cli.py NEW `cmd_migrate` | Graceful exit + optional scp + relaunch with --resume |
| cli.py NEW `graceful_exit()` | Esc × 3/2 + Ctrl+C × 2 sequence |
| transport.py | No changes |
| storage.py | No changes (schema-less JSON) |

### cam changes

None. cam delegates to camc. `cam reboot <id>` and `cam migrate <id> --to <host>`
will be added as thin wrappers that call camc on the appropriate machine.
But this is a SEPARATE PR — not in scope for this change.

### Parser additions

```python
# migrate (no --to = reboot on same machine)
migrate_p = sub.add_parser("migrate", help="Restart or move agent with session resume")
migrate_p.add_argument("id", help="Agent ID or name")
migrate_p.add_argument("--to", default=None, help="Target machine (host:port). Omit for local reboot.")
```

## Testing

### Session ID

```bash
# New agent gets session ID
camc run --name test-session --path /tmp "hello"
camc --json status test-session | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))"
# Should print: <agent-id>-0000-0000-0000-000000000000

# Session file exists
ls ~/.claude/projects/-tmp/<session-uuid>.jsonl

# Heal checks session
camc heal  # Should not warn for this agent
```

### Reboot

```bash
# Start agent, do some work
camc run --name reboot-test --path /tmp "remember the word 'pineapple'"
# Wait for response...

# Reboot
camc reboot reboot-test
# Agent restarts, new ID, same name

# Verify memory preserved
camc send reboot-test -t "what word did I ask you to remember?"
# Should answer "pineapple"
```

### Migrate (same machine = reboot)

```bash
camc migrate reboot-test --to localhost
# Same as reboot
```

## Constraints

- Do NOT change existing `camc stop` behavior
- Do NOT change existing `camc run` behavior for non-Claude tools (codex, cursor)
  - session_id only applies when tool == "claude"
- Do NOT break backward compatibility — old agents without session_id work normally
- Graceful exit must be a standalone function, not entangled with other logic
- All smoke tests must pass after changes
