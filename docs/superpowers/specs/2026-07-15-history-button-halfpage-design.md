# History Button Half-Page Design

**Date:** 2026-07-15
**Scope:** CAM Desktop terminal mode

## Goal

Make repeated History-button clicks useful while tmux copy mode is active.
The first click keeps the existing entry behavior; each later click moves
upward by one native tmux half-page.

## Interaction

- When the selected terminal is not in copy mode, clicking History enters
  copy mode through the existing `copy-mode -u` operation. This retains the
  current initial one-page-up behavior.
- When the selected terminal is already in copy mode, clicking History runs
  the native tmux `halfpage-up` copy-mode command.
- History remains enabled while copy mode is active unless a History request,
  terminal Refresh, or other blocking terminal operation is in progress.
- While copy mode is active, the History tooltip and accessible label describe
  the action as `Half page up`. Outside copy mode they describe `History`.
- To Bottom remains the only mouse control that exits copy mode and returns to
  live output.
- Wheel behavior remains unchanged: each History wheel event sends the current
  five line-up or line-down keys through the live PTY.
- PageUp and PageDown keyboard behavior remains unchanged.

## Architecture

Reuse the existing renderer `bridge.copyMode({ sessionId })` call and the
existing Electron `term:copyMode` IPC handler. No new preload or main-process
IPC channel is added.

The main-process handler remains the authority for the current pane state:

1. Resolve and validate the renderer-owned terminal session.
2. Read the active pane and `pane_in_mode` through `_tmuxClientState()`.
3. If the pane is not in copy mode, execute:

   ```text
   copy-mode -u -t <pane-id>
   ```

4. If the pane is already in copy mode, execute:

   ```text
   send-keys -X -t <pane-id> halfpage-up
   ```

5. Return `{ ok: true, copyMode: true, paneId, action }`, where `action` is
   `enter` or `halfpage-up`. On command failure, return the existing staged
   tmux failure payload.

Using `send-keys -X halfpage-up` makes the action independent of tmux
`mode-keys` (`emacs` or `vi`) and works with the supported tmux 2.7 target.

## Renderer State and Failure Handling

The renderer continues to wait for `bridge.copyMode()` before asserting local
`copyBrowsing = true`. It disables History only for the duration of that one
request and restores the derived enabled state afterward.

If `halfpage-up` fails, the status row displays the returned tmux failure and
the renderer leaves `copyBrowsing` unchanged. Passive tmux refresh continues
to reconcile `copyBrowsing` with `pane_in_mode`, including manual keyboard
entry or exit from copy mode.

## Boundaries

- Do not change wheel scroll distance or throttle behavior.
- Do not change PageUp, PageDown, Up, or Down keyboard mappings.
- Do not change To Bottom or Refresh behavior.
- Do not modify tmux configuration or user key bindings.
- Do not add a new IPC route or a second SSH control path.
- Do not change Mobile or camc.

## Verification

Automated tests must verify:

- History is not disabled merely because `copyBrowsing` is true.
- The renderer still sends exactly one `bridge.copyMode()` request per click.
- The first click still reaches `copy-mode -u` when `pane_in_mode` is false.
- A repeated click reaches `send-keys -X -t <pane-id> halfpage-up` when
  `pane_in_mode` is true, with tmux 2.7-compatible option ordering.
- No new preload/main IPC channel is introduced.
- The button label/tooltip changes between History and Half page up.
- Existing wheel, keyboard, To Bottom, Refresh, tab, and attachment regression
  tests remain green.

Manual MSI verification must enter History, click History repeatedly to move
up by half-pages, then click To Bottom and confirm live input/output resumes.
