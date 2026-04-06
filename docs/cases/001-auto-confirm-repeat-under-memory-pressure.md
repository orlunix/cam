# Case 001: Auto-Confirm Repeat Send Under Memory Pressure

**Date**: 2026-04-05
**Agent**: aicli (`3e65a6d6`), local, Claude tool
**Symptom**: Prompt shows `❯ 11111111` — multiple `1` characters typed at Claude's input prompt
**Root Cause**: Auto-confirm re-sends `1` when dialog persists beyond 5s cooldown due to system memory pressure
**Status**: Diagnosed, not yet fixed

## Timeline

```
20:46:18  Agent idle at prompt. Screen: "◐ medium · /effort | Checking for updates"
20:46:42  Bash confirmation dialog appears:
            "Permission rule Bash requires confirmation for this command.
             Do you want to proceed? ❯ 1. Yes / 2. No / Esc to cancel"
          Monitor auto-confirm fires: sends '1' (no Enter) — correct for Ink select menu
20:46:43  Cooldown starts (5s)
20:46:48  Cooldown expired. Dialog still on screen (hash unchanged). Monitor re-sends '1'
20:46:53  Same — re-sends '1' (3rd time)
20:46:58  Same — re-sends '1' (4th time)
20:47:01  Claude finally processes input, screen starts changing rapidly
20:47:26  Screen stabilizes at: ❯ 11111111
          (1 of the 4 '1's selected the menu, remaining 3+ landed as input at prompt)
```

## Environment

- Machine had **heavy memory pressure**: swap usage 3.1G / 3.8G
- No OOM kills, but Claude process likely swapped out
- Claude was in "Checking for updates" state when dialog appeared — additional slowness

## Monitor Log Excerpts

```
# Dialog detected, first confirm sent
2026-04-05 20:46:42,929 [monitor] INFO Auto-confirm: pattern='Do\s+you\s+want\s+to\s+proceed'
  matched='Do you want to proceed' -> '1' (enter=False)

# 5s cooldown, then re-send (dialog unchanged)
2026-04-05 20:46:48,287 [monitor] INFO Auto-confirm: pattern='Do\s+you\s+want\s+to\s+proceed'
  matched='Do you want to proceed' -> '1' (enter=False)

# And again...
2026-04-05 20:46:53,560 [monitor] INFO Auto-confirm: ...
2026-04-05 20:46:58,667 [monitor] INFO Auto-confirm: ...

# Finally agent processes, screen shows the damage
2026-04-05 20:47:26,436 [monitor] DEBUG screen: ... | ❯ 11111111 | ...
```

## Analysis

The auto-confirm logic works as follows:
1. Detect confirm pattern in last 8 screen lines
2. Send response (`1` for Ink select menus)
3. Set cooldown timer (5s)
4. After cooldown, check screen again — if pattern still matches, re-send

The flaw: **there is no mechanism to suppress re-sending when the screen hasn't changed**.
The monitor assumes that if the dialog is still visible after cooldown, it's a new dialog
or the previous send failed. But under memory pressure, the agent simply hasn't processed
the keystroke yet.

### Why 5s Cooldown Isn't Enough

Normal confirm cycle: send `1` → Claude processes in <1s → screen changes → no re-trigger.

Under memory pressure: send `1` → Claude swapped out → 5s passes → screen unchanged →
monitor thinks send failed → re-sends → repeat.

### Previous Mitigation (Removed)

An earlier version had "scrollback re-trigger suppression" that tracked whether the dialog
was successfully dismissed. This was removed, leaving only the raw cooldown as protection.

## Proposed Fix

**Track `last_confirm_hash`**: After sending a confirm, record the current screen hash.
On subsequent cycles, if the hash hasn't changed since the confirm was sent, do NOT re-send
even if the cooldown has expired. Only re-trigger when:
1. Hash changed (screen updated) AND
2. Hash changed back to showing a confirm dialog (new dialog appeared)

This is simpler and more robust than the removed scrollback suppression logic.

```python
# Pseudocode
if confirm_match:
    if screen_hash == last_confirm_hash:
        # Screen unchanged since last confirm — agent hasn't processed yet, don't re-send
        log("Confirm suppressed: screen unchanged since last send")
    else:
        send_keys(response)
        last_confirm_hash = screen_hash
        last_confirm = now
```

## Impact

- Agent left with garbage input at prompt (`11111111`)
- Agent becomes idle/stuck — needs manual intervention to clear the input
- Auto-exit disabled, so it just sits there indefinitely

## Related

- `docs/auto-confirm-strategy.md` — overall auto-confirm design
- `docs/auto-confirm-flow.md` — confirm flow diagrams
- Memory project note: this machine routinely runs 10+ local Claude agents
