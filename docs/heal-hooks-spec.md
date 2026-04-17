# Feature Spec: Unified Heal + Hook Management

## Overview

Consolidate all agent health and maintenance operations under `camc heal`.
Add hook injection capability for agent-to-agent communication.

## Commands

```bash
camc heal                    # default: fix dead monitors + restore stale agents
camc heal --upgrade          # kill all monitors, restart with current binary
camc heal --hooks            # inject/update hooks to all running agents
camc heal --session          # validate session files exist (warning only)
camc heal --all              # all of the above
```

## Hook Management

### Architecture

```
~/.cam/hooks/
├── claude.json              ← default hooks for Claude Code agents
├── codex.json               ← default hooks for Codex agents (future)
└── custom/                  ← user-defined hooks (future)
```

### Default Hook Template (~/.cam/hooks/claude.json)

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '{\"event\":\"prompt\",\"agent_id\":\"{agent_id}\",\"ts\":\"'$(date -Iseconds)'\"}' >> ~/.cam/events/{agent_id}.jsonl",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '{\"event\":\"stop\",\"agent_id\":\"{agent_id}\",\"ts\":\"'$(date -Iseconds)'\",\"message\":'$(cat | python3 -c \"import json,sys;print(json.dumps(json.load(sys.stdin).get('last_assistant_message','')))\")'}' >> ~/.cam/events/{agent_id}.jsonl",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### Injection Mechanism

`camc heal --hooks` does:

1. Read hook template from `~/.cam/hooks/claude.json`
   - If not found, generate default template and save it
2. For each running agent:
   a. Find agent's workdir
   b. Read existing `<workdir>/.claude/settings.local.json` (if any)
   c. Merge hook config (don't overwrite user's existing hooks)
   d. Write merged config back
3. Hooks take effect on next message — no restart needed

### Hot Reload (verified by experiment)

Claude Code re-reads `.claude/settings.local.json` on each message.
Injecting hooks does NOT require agent restart. Verified:

```
- Message 1 (no hooks) → no hook fired
- Inject hooks to settings.local.json
- Message 2 (hooks present) → UserPromptSubmit + Stop both fired
```

### Merge Strategy

When injecting, don't overwrite existing hooks. Merge:

```python
def merge_hooks(existing, new):
    """Merge new hooks into existing config without overwriting."""
    for event, handlers in new.get("hooks", {}).items():
        if event not in existing.get("hooks", {}):
            existing.setdefault("hooks", {})[event] = handlers
        # If event already has handlers, check if ours is already there
        # by looking for "camc event" in command strings
        else:
            existing_cmds = [h["command"] for hl in existing["hooks"][event] 
                           for h in hl.get("hooks", [])]
            for handler_list in handlers:
                for h in handler_list.get("hooks", []):
                    if h["command"] not in existing_cmds:
                        existing["hooks"][event].append(handler_list)
    return existing
```

### Event Output

Hooks write to `~/.cam/events/<agent-id>.jsonl`:

```jsonl
{"event":"prompt","agent_id":"3e65a6d6","ts":"2026-04-16T12:00:00-07:00"}
{"event":"stop","agent_id":"3e65a6d6","ts":"2026-04-16T12:05:00-07:00","message":"Done. Committed abc1234."}
```

### Reading Events

```bash
camc events <agent-id>              # show recent events
camc events <agent-id> --follow     # tail -f style
camc wait <agent-id>                # block until next "stop" event
camc wait <agent-id> --timeout 300  # with timeout
```

## Heal Registry

| Flag | What it does | Status |
|------|-------------|--------|
| (default) | Restart dead monitors, restore stale agents | Existing |
| `--upgrade` | Kill all monitors, restart with current binary | Existing |
| `--hooks` | Inject/update hooks to all running agent workdirs | New |
| `--session` | Validate session files exist for all agents | New |
| `--all` | Run all of the above in sequence | New |

## Implementation (camc only)

### Files to change

| File | Change |
|------|--------|
| cli.py `cmd_heal` | Add `--hooks`, `--session`, `--all` flags |
| cli.py NEW `_inject_hooks()` | Read template, merge into settings.local.json |
| cli.py NEW `_validate_sessions()` | Check session files, warn if missing |
| cli.py NEW `cmd_events` | Read/tail event JSONL files |
| cli.py NEW `cmd_wait` | Block until stop event |

### Parser additions

```python
# heal flags
heal_p.add_argument("--hooks", action="store_true", help="Inject/update hooks")
heal_p.add_argument("--session", action="store_true", help="Validate session files")
heal_p.add_argument("--all", action="store_true", help="Run all heal operations")

# events
events_p = sub.add_parser("events", help="Show agent events")
events_p.add_argument("id", help="Agent ID or name")
events_p.add_argument("--follow", "-f", action="store_true")

# wait
wait_p = sub.add_parser("wait", help="Wait for agent to complete current task")
wait_p.add_argument("id", help="Agent ID or name")
wait_p.add_argument("--timeout", type=int, default=0, help="Timeout in seconds (0=forever)")
```

## Testing

```bash
# Inject hooks to all agents
camc heal --hooks
# Verify: check any agent's workdir
cat <workdir>/.claude/settings.local.json | python3 -m json.tool

# Send a message, check events
camc send aicli -t "hello"
cat ~/.cam/events/3e65a6d6.jsonl
# Should see: prompt event + stop event

# Wait for completion
camc send cam-dev -t "fix the bug"
camc wait cam-dev --timeout 300
echo "cam-dev finished: $(tail -1 ~/.cam/events/dec2f7e7.jsonl)"
```

## Backward Compatibility

- `camc heal` without flags: unchanged behavior
- `camc heal --upgrade`: unchanged behavior
- Agents without hooks: work normally, just no events emitted
- Existing settings.local.json: merged, not overwritten
- Old agents without session_id: heal --session skips them

## Dependencies

- Session ID tracking (from session-id-and-migrate-spec.md) — needed for --session
- No dependency on cam server changes — this is camc-only
