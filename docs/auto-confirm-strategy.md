# Auto-Confirmation & Idle Detection Strategy

## Current State (v1.1.0): Screen Scraping

### How It Works

The monitor polls every ~1 second:

```
capture-pane → MD5 hash → compare with previous hash
  ├── hash changed → output changed, reset idle timer
  └── hash same for 5s → probe ("1" + BSpace) → idle or busy
```

Auto-confirm: regex on last 32 lines of capture → send "1" or Enter.

### Known Problems

#### Problem 1: Status Bar Causes Infinite Probe Loop

Claude Code's Ink TUI has a status bar on the **last line** that alternates:
```
  ? for shortcuts    ←→    esc to interrupt
```

This alternation changes the capture hash every 1-3 minutes, even when the agent is completely idle. The cycle:

```
Idle confirmed ✓
  → 2 min later: status bar text changes
  → hash changes → idle_confirmed = False
  → 5s stable → probe fires → sends "1" + BSpace
  → Idle confirmed ✓
  → repeat forever
```

**Evidence** from agent 75fb10bc (2026-04-03):
- Nighttime (no user): probe every 1-4 hours (only auto-update triggers)
- Morning (user active): probe every 50-210 seconds (status bar flicker + user input)
- Every `Probe: idle` was preceded by `Output changed while idle` with identical `❯` prompt content but different hash

The status bar is always the last line of `capture-pane` output:
```
────────────────────── (separator)
❯                      (prompt - actual content)
────────────────────── (separator)
  esc to interrupt     ← status bar, ALWAYS last line
```

**Quick fix**: Strip the last line before hashing. Status bar changes would no longer trigger idle reset.

#### Problem 2: Probe Feedback Loop ("1" Spam)

The probe mechanism sends "1" to test if the agent is at a prompt. This creates a feedback loop:

```
Send "1" → echoes on screen → hash changes
  → idle_confirmed reset → 5s later → probe again → send "1"
  → BSpace sometimes fails to erase → "1" accumulates
```

Root cause: **the observer changes what it observes**. Any character sent to the terminal either echoes (changing screen state) or gets consumed by a dialog (changing application state). Both affect subsequent observations.

Mitigations attempted (all partial):
- BSpace cleanup after probe → fails ~5% of the time (Ink TUI race)
- BSpace retry 3x → still sometimes fails
- Probe-caused output filter (Step 3) → helps but doesn't cover all edge cases
- has_worked gate → prevents premature probe but not the feedback loop itself

#### Problem 3: Scrollback False Positives

After auto-confirm dismisses a dialog, the dialog text remains in terminal scrollback. Next capture cycle may include it in the last 32 lines → regex matches again → sends "1" again.

Mitigations: `suppressing scrollback re-trigger`, checking only last 32 lines. Partially effective.

---

## Proposed Improvements

### Improvement 1: Strip Status Bar Before Hashing (Minimal Change)

**Effort**: Small (modify one function in monitor.py)

Strip the last line of `capture-pane` output before computing the MD5 hash. The status bar (`? for shortcuts` / `esc to interrupt`) changes don't affect the hash.

**Solves**: Problem 1 (status bar probe loop)
**Doesn't solve**: Problem 2 (probe feedback), Problem 3 (scrollback)

### Improvement 2: Use tmux `window_activity` for Idle Detection (Replace Probe)

**Effort**: Medium (rewrite idle detection logic)

tmux tracks `window_activity` — epoch timestamp of the last time the pane produced output. This is a passive observation with zero side effects.

```python
# Instead of hash comparison + probe:
activity = int(tmux_display(session, '#{window_activity}'))
idle_for = time.time() - activity
if idle_for > threshold:
    # Agent is idle — no "1" sent, no BSpace needed
```

**Verified working** on all remote environments:

| Machine | tmux Version | `window_activity` |
|---------|-------------|-------------------|
| Local | 3.4 | ✓ |
| bpmpfw | 3.4 | ✓ |
| YUBIO | 3.2a | ✓ |
| PDX-098 | 2.7 | ✓ |
| PDX-110 | 2.7 | ✓ |
| DC7 | 2.7 | ✓ |
| DC2 | 2.7 | ✓ |

Key behaviors verified:
- `window_activity` does NOT update from `send-keys ""` (empty send)
- `window_activity` DOES update from `send-keys "1"` (echo counts as output)
- On socket sessions (`/tmp/cam-sockets/*.sock`), the value is clean (no human input noise)
- Status bar animation does NOT affect `window_activity` (it's terminal-internal rendering, not process output)

**Wait — does status bar actually update `window_activity`?** This needs verification. The Ink TUI status bar is rendered via terminal escape codes which ARE process output. If it does update activity, we'd need the same "strip last line" approach but at the tmux level.

**Remaining question**: How to distinguish "idle at prompt" from "extended thinking" (both produce no output for minutes). Options:
- Use `has_worked` gate + longer timeout (30-60s instead of 5s). Extended thinking rarely goes 60s without any output.
- Accept the ambiguity — for auto-exit, waiting 60s is fine.

**Solves**: Problem 1, Problem 2 entirely. Probe mechanism removed.
**Doesn't solve**: Problem 3 (scrollback — but without probe, scrollback false positives are less harmful since no "1" gets sent)

### Improvement 3: Claude Code `PreToolUse` Hook (Replace Auto-Confirm)

**Effort**: Large (new subsystem)

Claude Code supports `PreToolUse` hooks — shell commands that run **before** any tool execution. The hook receives tool name and arguments as JSON via stdin, and can return `allow` or `deny`.

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/permission_handler.py"
      }]
    }]
  }
}
```

How it works:
1. Claude decides to use a tool (Bash, Write, etc.)
2. **Before** the permission dialog appears, `PreToolUse` hook fires
3. Hook script receives `{tool_name, input, ...}` on stdin
4. Script decides: return `allow` → tool executes silently, no dialog shown
5. Or return `deny` → tool skipped, no dialog shown

**This means the confirmation dialog never appears on screen.** No regex matching, no sending "1", no scrollback issues.

Passed to Claude CLI via: `claude --settings /tmp/camc-hooks-<id>.json`

**Implementation sketch**:
```
camc run → generate hook settings JSON → claude --settings <path>
         → PreToolUse hook → calls camc permission script
         → script checks auto_confirm rules → allow/deny
```

The permission script can use the same rules as current auto-confirm (tool name matching) but with 100% accuracy since it receives structured data, not regex-matched screen text.

**Solves**: Problem 1, 2, 3 entirely. Screen scraping for confirmation is eliminated.
**Limitation**: Only works with Claude Code (not Codex/Cursor). Other tools would still need screen-based confirm.

### Improvement 4: Additional tmux Metadata (Complement)

Other useful tmux features verified on all remotes (including tmux 2.7):

| Feature | Use Case |
|---------|----------|
| `pane_dead` / `pane_died` hook | Instant process death detection (replaces 15s health check polling) |
| `monitor-silence N` + `alert-silence` hook | tmux notifies when pane is silent for N seconds |
| `pipe-pane` | Stream all output to a file (alternative to periodic `capture-pane`) |
| `pane_pid` / `pane_current_command` | Check what process is running |

These can complement Improvement 2 for a fully event-driven monitor.

---

## Comparison: Happy Coder's Approach

Happy Coder (analyzed from `/home/hren/.openclaw/workspace/cam/happy/`) uses a fundamentally different architecture:

### Remote Mode (SDK)
- Uses `@anthropic-ai/claude-agent-sdk` `query()` function
- `canUseTool` callback intercepts tool execution at SDK level
- Structured events: tool name, arguments, permission result
- No screen scraping at all
- **Limitation**: Uses API key billing (per-token cost), not company subscription

### Interactive Mode (Terminal)
- Spawns `claude` CLI as child process
- Reads `~/.claude/projects/<path>/<session-id>.jsonl` for structured messages
- fd 3 pipe for fetch-start/fetch-end events (thinking state)
- **Does NOT handle permissions** — user handles them manually in terminal
- **Does NOT auto-confirm** — no screen scraping, no permission interception

### Session JSONL Files
Claude Code writes structured JSONL in ALL modes (not just headless) at:
```
~/.claude/projects/<path-hash>/<session-id>.jsonl
```

Contains: user messages, assistant messages with tool_use, tool_results, system events (api_error, turn_duration). Happy watches these files for state tracking.

**Key insight**: Happy's interactive mode is simpler than CAM because it doesn't try to auto-confirm. It gives the terminal to the user. CAM's auto-confirm is the source of all the complexity.

---

## Recommended Migration Path

```
Phase 1 (quick win):
  Strip last line before hashing
  → Eliminates status bar probe loop
  → ~10 lines of code change

Phase 2 (medium effort):
  Replace hash comparison with window_activity timestamp
  Remove probe mechanism entirely
  Use has_worked + 30s timeout for idle detection
  → Eliminates all "1" spam
  → Eliminates probe feedback loop

Phase 3 (larger effort):
  Add PreToolUse hook for Claude Code
  → Eliminates screen-based auto-confirm for Claude
  → Keep screen-based confirm as fallback for Codex/Cursor

Phase 4 (optional):
  Watch session JSONL files for structured state tracking
  → Replaces regex-based state detection (planning/editing/testing)
  → Only for Claude Code
```

Each phase is independently valuable. Phase 1 can ship today. Phase 2 removes the most bug-prone subsystem. Phase 3 makes auto-confirm 100% reliable. Phase 4 is nice-to-have for richer state information.
