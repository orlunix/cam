# Desktop Terminal tmux Interactions Spec

**Status:** draft (2026-07-14)  
**Scope:** CAM-Desktop Direct/Electron Terminal mode.  
**Out of scope:** Relay mode, Mobile UI, Rich/Plain output behavior, and
changing CAMC's message/send fast path.

## Goal

Make the attached terminal feel like a useful tmux client, without replacing
the long-lived SSH PTY:

1. expose tmux windows as tabs and create a window with `+`;
2. expose tmux copy-mode history through mouse-first controls.

An existing `camc attach <agent>` channel remains open throughout a tab switch
or history browse. `term:data` remains raw ANSI/VT output only; it is not a
control protocol.

## Current constraints

- Desktop's live terminal is implemented in
  `web/js/desktop/agent-console.js`; `web/js/shared/terminal-mount.js` is a
  separate shared/mobile-oriented implementation. Do not silently change both
  in the first Desktop pass.
- The Electron terminal bridge already writes arbitrary input to the existing
  PTY (`term:input`). Thus entering copy mode needs no CAMC process, no new
  SSH connection, and no `camc` command latency.
- xterm normally consumes mouse-wheel and PageUp for its local scrollback. In
  tmux copy-mode these events must instead be sent to the attached PTY.

## Feature A — tmux window tabs

### User behavior

- On Terminal attach, render a tab strip for the agent's tmux session; window
  `0` is selected initially when it is the active tmux window.
- Clicking a tab selects that tmux window in the **same attached client**.
  The SSH connection, PTY, xterm instance, and terminal scrollback stay alive.
- `+` creates a tmux window and selects it, equivalent in outcome to
  `Ctrl-b c`.
- Tab labels use tmux's window name and index; the active tab is visually
  distinct. A failed control action leaves the current tab selected and shows
  a non-blocking terminal status.

### Design

Electron establishes the attached tmux *client* deterministically, without
parsing ANSI and without guessing: it snapshots `list-clients` immediately
before opening the SSH PTY, polls it after `camc attach` succeeds, and accepts
the one newly added `client_tty` only. The snapshot/list commands use the
already-resolved agent record's `tmux_session`, `tmux_socket`, and `tmux_bin`
over the existing pooled SSH control connection.

- If exactly one new client does not appear within the bounded discovery
  window, or required session/socket/binary metadata is unavailable, tmux
  controls stay disabled with a status. They must never guess and risk
  switching a different user's view.
- With the discovered client TTY, Electron runs tmux 2.7-compatible,
  internally constructed commands: `list-windows`, client-scoped
  `display-message` for the active window/pane, `switch-client -c` to select,
  and `new-window -P -F` followed by `switch-client -c` for `+`.
- Renderer input is limited to an opaque terminal session ID and a validated
  window index. It never supplies a shell fragment, tmux target, socket, or
  client TTY.

The renderer receives structured window metadata and must not infer windows
or active state from ANSI text. This approach does not depend on a user's tmux
prefix or key bindings.

## Feature B — mouse-first copy-mode history

### State model

```
Live / follow output -- click "↑ History" --> tmux copy-mode / browsing
tmux copy-mode / browsing -- click "↓ To bottom" --> Live / follow output
```

The Terminal canvas remains full width, matching the current Terminal mode.
Its controls do **not** span that full canvas: tabs, History, Attach, status,
and To bottom live in a centered Terminal control rail with the exact width
and horizontal edges of the existing agent header and Plain/Rich content
rail (`min(100%, var(--desktop-rail-wide))`). This is the column whose
top-left holds the agent name/status and whose top-right holds the
Plain/Rich/Terminal/Browse mode buttons.

The control rail is an overlay over the full-width Terminal canvas; it never
changes the canvas size, xterm fit geometry, or output width. At widths below
the rail maximum, it shrinks to the available canvas width. If a tmux tab
strip is present, it is fixed chrome inside this rail; `↑ History` begins
below the tab strip and never overlaps a tab.

```text
terminal control rail: same horizontal edges as agent header / Plain-Rich rail
top-left                                                top-right
[status: attach / action result]                        [↑ History]

                 (terminal canvas / tmux output)

[Attach icon]                                         [↓ To bottom]
bottom-left                                               bottom-right
```

- `↑ History` is a floating Terminal-only button at the top-right.
- `↓ To bottom` is a floating Terminal-only button at the bottom-right.
- The existing text `Attach` control becomes an icon-only button at the
  bottom-left, with an accessible label and tooltip.
- The transient attach/action status is a compact pill in the upper-left,
  immediately below the tab strip. It is not a full-width banner and does not
  overlap tabs or `↑ History`.
- Attach and `↓ To bottom` remain on the bottom-left/right corners of the
  control rail but sit above its edge by 1.5× the configured terminal font
  size, rather than touching the canvas boundary.

The buttons and status are overlays and do not alter terminal layout. In live
state `↑ History` is shown; in copy-mode it is hidden and `↓ To bottom` is
shown. Attach and the compact status are independent of that state.

### Enter browsing

When a connected Terminal user clicks `↑ History`:

1. use the discovered active client/pane to run tmux `copy-mode -t <pane>` on
   the pooled SSH control connection;
2. mark that terminal entry as `copyBrowsing` immediately;
3. hide `↑ History`, show `↓ To bottom`, and keep terminal focus;
4. stop local auto-follow: incoming `term:data` is still written to xterm but
   must not call `scrollToBottom` while `copyBrowsing` is true.

This introduces no CAMC CLI/Python invocation and no new SSH connection. The
tmux control operation is a short command on the already pooled connection;
the long-lived attach PTY remains the data and keyboard channel.

### Browse input

While `copyBrowsing` is true:

- mouse-wheel events over xterm are captured before xterm's local viewport
  handler and are converted into bounded Up/Down terminal sequences for tmux;
- Up/Down continue to reach tmux normally;
- PageUp/PageDown are intercepted so xterm does not locally scroll and their
  standard escape sequences reach tmux;
- existing Ctrl-V attachment handling continues unchanged;
- output remains visible but does not force the viewport down.

The UI control is the supported entry/exit path in v1. Normal tmux keyboard
copy-mode shortcuts still work, but v1 does not infer manual mode transitions
from ANSI output. It may reconcile state only by querying `pane_in_mode` for
the already discovered client pane; it must not guess from ANSI or an
unscoped `tmux list-clients` result.

### Return to live output

Clicking `↓ To bottom` runs tmux `send-keys -X cancel -t <active-pane>` through
the pooled control connection, then calls xterm `scrollToBottom()`, clears
`copyBrowsing`, restores the usual follow policy, hides the bottom button,
and shows `↑ History` again. It must **not** blindly write `q`: if the user
left copy-mode through a keyboard shortcut first, a raw `q` would reach the
agent prompt. The tmux cancel command is harmless when copy-mode has already
ended. If client discovery or the PTY has failed, both controls are
disabled/hidden; they must never reattach it.

## Non-functional requirements

- No SSH reconnect, no `camc attach` restart, and no Python/CAMC invocation
  for Feature B interactions.
- Feature A actions operate on the pooled SSH control connection and should
  complete in well under one second on a healthy host; record timings during
  verification.
- Do not break xterm selection, ordinary terminal input, Ctrl-V attachment,
  terminal resizing, fast agent switching, or Rich/Plain `More +` and
  `Jump to bottom` controls.
- Do not install a second xterm custom-key handler: xterm supports one. The
  history behavior composes with the existing Ctrl-V attachment handler.
- The upper-left status must remain content-width, ellipsize before it can
  cover its control rail, retain the complete message in its title or
  accessible description, and remain below rather than over the tab strip.
- Terminal controls use the same compact tab-shaped corners as the Desktop
  mode buttons, rather than fully pill-shaped floating controls.
- Terminal tabs, History, Attach, status, and To bottom must share the
  centered agent-control rail with the header's name/status and mode buttons;
  they must not use the full-width Terminal canvas edges at wide viewport
  sizes.
- tmux 2.7 is the compatibility floor for commands and formatting used by the
  Desktop control API. PDX tmux 2.7 has been isolated-tested for client
  discovery, client-scoped switch, `copy-mode`, `send-keys -X cancel`, and
  `new-window -P -F`.

## Acceptance criteria

1. Attach one agent, enter history with the mouse, scroll up with wheel, Up,
   and PageUp, then click `↓ To bottom`: the display returns to current live
   output without reconnecting the PTY.
2. New output received during history browsing does not pull the viewport to
   the bottom. After return, new output follows normally.
3. Repeating history enter/exit does not duplicate input, event listeners,
   floating controls, CAMC processes, or SSH connections.
4. Clicking a tmux window tab and `+` preserves the existing PTY session;
   no additional `camc attach` is observed.
5. Tab/create/control failures retain the prior UI state and show a clear,
   non-modal error.
6. A manually exited copy-mode followed by clicking `↓ To bottom` never sends
   a literal `q` to the agent.
7. With a tab strip, `↑ History` remains in the Terminal control rail and does
   not overlap tabs; the upper-left status remains below tabs and a long
   status does not cover the control rail.
8. At wide Desktop sizes, the outer edges of terminal tabs and bottom controls
   align with the agent header / Plain-Rich control rail rather than the
   full-width Terminal canvas. At narrow sizes, the rail shrinks with the
   canvas without clipping any control.
