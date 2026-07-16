# Terminal Action Bar and Manual Re-attach Design

**Date:** 2026-07-15
**Scope:** CAM Desktop terminal mode

## Goal

Group the terminal attachment and navigation actions into one stable button
bar, and add an explicit control that reconnects a stuck or detached Desktop
terminal without stopping the remote agent or tmux session.

## Layout

The tmux window tabs remain at the top of the terminal's centered content
rail. The status output remains directly below the tabs. A four-slot action
bar sits at the existing bottom-left location and retains the current bottom
and left margins.

```text
┌─ tmux tabs ─────────────────────────────┐
│ status                                 │
│                                        │
│ terminal                               │
│                                        │
│ [Attach] [History] [Bottom] [Refresh]  │
└────────────────────────────────────────┘
```

The action bar is scoped to the same centered terminal width used by the tabs
and status, not the full application width.

## Button Presentation

The bar contains four fixed slots in this order:

1. Attach (`paperclip` icon)
2. History (`up arrow` icon)
3. To Bottom (`down arrow` icon)
4. Refresh (`circular arrow` icon)

All four controls retain the current `28px` square size and use the same
border, background, corner radius, shadow, hover treatment, and spacing.
Slots remain in place while Terminal mode is visible. An unavailable action
is disabled instead of hidden so the controls never shift horizontally.
Each icon button has a tooltip, an `aria-label`, and a screen-reader label.

## Button States

- **Attach** is enabled when the selected active agent has a usable Direct
  terminal bridge and no attachment upload is in progress.
- **History** is enabled when tmux controls are ready and the terminal is not
  already in tmux copy mode.
- **To Bottom** is enabled while tmux copy mode is active or the local xterm
  viewport is away from its live bottom.
- **Refresh** is enabled when an active selected agent can open a terminal and
  no terminal open, close, or refresh operation is in progress.

When no terminal action can run, the bar remains visible in Terminal mode and
the relevant buttons show their disabled state. Existing tooltip text should
explain each action; status or error details continue to use the status row.

## Refresh Behavior

Refresh performs a hard local re-attach for the currently selected agent:

1. Ignore repeated clicks while refresh is already in progress.
2. Disable all four action buttons and animate the Refresh icon to indicate
   progress.
3. Close the current Electron terminal channel using the existing
   `term:close` contract.
4. Dispose the current local xterm instance and its local scrollback.
5. Create a fresh xterm instance and call the existing `term:open` path for
   the same agent.
6. Fit and resize the new terminal, refresh tmux window/copy-mode metadata,
   focus the terminal, and restore normal button states.

This operation must not stop, kill, restart, or otherwise mutate the remote
agent or its tmux session. Clearing the Desktop-local xterm scrollback is an
accepted part of the hard re-attach; remote tmux history remains available
through History.

## Status and Failure Handling

The status row reports `Re-attaching terminal...` while the operation runs.
On success it reports `Terminal re-attached.` using the existing success
styling. On failure it reports the existing attach error detail, keeps the
fresh terminal entry in a detached/retryable state, restores the action-bar
controls, and permits another Refresh attempt.

A stale close/status event from the old channel must not clear or overwrite
the session id of the newly opened channel. Session-id ownership checks must
remain the boundary for terminal data and status events.

## Implementation Boundaries

- Refresh must call the existing `openTerminalForSelected({ force: true })`
  close/recreate/open sequence; do not introduce a second attach protocol or
  duplicate that lifecycle in the click handler.
- Reuse the existing `term:open` and `term:close` IPC contracts. No new main
  process IPC method is required.
- Keep tmux tabs at the top and the status row below them.
- Do not change Mobile terminal controls, camc, remote tmux configuration, or
  agent lifecycle behavior.

## Verification

Automated coverage must verify:

- one fixed action-bar container owns all four buttons in the required order;
- the buttons retain the current size and no longer use state-driven `hidden`
  behavior while Terminal mode is visible;
- disabled states match terminal, tmux copy-mode, scroll, and refresh state;
- one Refresh click closes the old session exactly once and opens the selected
  agent exactly once;
- repeated Refresh clicks cannot start overlapping re-attach operations;
- old-session status events cannot detach the new session;
- success and failure restore controls and publish the expected status.

Manual MSI verification must confirm the bar remains within the centered
terminal rail, keeps the current margins, does not shift as History/Bottom
state changes, and can recover a deliberately detached terminal while leaving
the remote agent running.
