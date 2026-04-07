# Case 002: send-keys Delayed in "Accept Edits" Mode

**Date**: 2026-04-06
**Agent**: teaspirit (`dfac113f`), local, Claude Code
**Symptom**: `camc send` and `tmux send-keys` appear to have no effect — text invisible for seconds/minutes
**Root Cause**: Ink TUI render lag — input is buffered but screen doesn't update until a subsequent keystroke triggers re-render
**Status**: Diagnosed, resolved (input does work, but with render delay)

## Timeline

```
~08:20    Agent completes task ("Sautéed for 18m 17s"), returns to ❯ prompt
          Status bar: ⏵⏵ accept edits on (shift+tab to cycle)
08:25     camc send dfac --text "hello, 你还活着吗？" → "Sent." (no error)
08:25+5s  Screen unchanged — text not visible at ❯ prompt
08:26     Direct tmux send-keys "hi" Enter → exit 0 (no error)
08:26+5s  Screen unchanged — still nothing visible

--- Further investigation ---
08:30     tmux send-keys BTab (Shift+Tab) → no visible effect
08:31     tmux send-keys ESC [Z (Shift+Tab escape sequence) → no visible effect
08:32     tmux send-keys Escape → no visible effect
08:33     tmux send-keys "test" (no Enter) → VISIBLE! "❯ test" appears
          Also visible: previous buffered input leaked as garbage text
          "sleep5tmux-S/tmp/cam-sockets/..." (from earlier bash commands)
          Status bar gains "ctrl+g to edit in Vim" — Claude detected input
08:34     Ctrl+C → clears garbage input, back to clean ❯
08:35     tmux send-keys "hello" Enter → SUCCESS
          Screen shows: ❯ hello / * Orbiting… / esc to interrupt
          Claude is processing the message normally
```

## Key Finding: Input Was NOT Ignored

Initial diagnosis was wrong. The Ink TUI was NOT dropping send-keys input. Instead:

1. **Input was buffered silently** — keystrokes went into Ink's input buffer but didn't trigger a screen re-render
2. **Chinese text handling** — the first `camc send` with Chinese characters (`你还活着吗`) may have had encoding issues with Ink's input parser
3. **Render only on certain events** — the screen only updated when a subsequent `send-keys "test"` was sent, at which point ALL buffered input became visible at once
4. **Once rendered, normal operation resumed** — after Ctrl+C to clear garbage and a clean `send-keys "hello" Enter`, Claude processed it immediately

## What Was Tested

| Input method | Result |
|---|---|
| `camc send --text "Chinese text"` | No visible effect (buffered but not rendered) |
| `tmux send-keys "hi" Enter` | No visible effect (buffered) |
| `tmux send-keys BTab` | No visible effect |
| `tmux send-keys ESC [Z` (Shift+Tab) | No visible effect |
| `tmux send-keys Escape` | No visible effect |
| `tmux send-keys "test"` (no Enter) | **Visible** — triggered render, showed all buffered input |
| `tmux send-keys C-c` | Cleared input line |
| `tmux send-keys "hello" Enter` | **Success** — Claude responded normally |

## Analysis

The `⏵⏵ accept edits on` mode is a display-only indicator. It does NOT block input. The actual issue is an Ink TUI render timing problem:

- Ink uses React-style rendering — screen updates are batched and may not flush immediately after every keystroke
- When the agent is idle at prompt with no pending re-render, incoming send-keys characters go into the buffer but don't trigger a screen refresh
- A subsequent character (or possibly a timer) eventually triggers the re-render and all buffered input appears at once

This means auto-confirm `1` keystrokes may ALSO appear delayed — the character reaches Claude but the screen doesn't reflect it. This could cause the monitor to think its confirm failed (screen unchanged) and re-send, leading to the same issue as Case 001.

## Impact

- **Lower than initially assessed** — send-keys does work, just with invisible delay
- Auto-confirm likely works but the delay could trigger re-send logic (see Case 001)
- `camc send` works but user gets no visual feedback that the message was received until render catches up

## Workaround

Send-keys works as-is. If text doesn't appear:
1. Wait a few more seconds — it may render on next Ink refresh cycle
2. Send an additional benign keystroke to trigger re-render
3. If garbage accumulated, send Ctrl+C to clear, then re-send clean message

## Implications for Monitor

The Case 001 fix (track `last_confirm_hash` to prevent re-send when screen unchanged) becomes even more important: the screen may legitimately stay unchanged for several seconds after a successful confirm send, because Ink hasn't re-rendered yet.

## Related

- `docs/cases/001-auto-confirm-repeat-under-memory-pressure.md` — re-send on unchanged screen
- `docs/auto-confirm-strategy.md` — overall auto-confirm design
