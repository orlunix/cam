# Locale-Independent tmux Control Parsing

## Problem

CAM Desktop identifies the attached tmux client correctly, but its control
probe fails on PDX098 with `client_state/tmux_parse_failed`. The probe formats
client state and window rows with literal tab characters.

The failure is reproducible at the transport boundary:

- OpenSSH with a UTF-8 locale returns `0<TAB>%0<TAB>0`.
- Desktop's `ssh2.Client.exec()` returns `0_%0_0`.
- OpenSSH with locale variables removed also returns `0_%0_0`.

tmux 2.7 sanitizes the tab characters to underscores when the remote command
has no locale. The Desktop parser only accepts tabs, so client-state parsing
fails and the renderer hides the tabs and History button. The same defect also
affects the tab-delimited `list-windows` format.

## Design

Use a printable colon as the field separator in all multi-field tmux control
formats. Printable ASCII survives OpenSSH, `ssh2`, locale-free shells, and tmux
2.7 without transformation.

Client state will use this fixed format:

```text
#{window_index}:#{pane_id}:#{pane_in_mode}
```

The parser will validate the complete row as three bounded fields: a numeric
window index, a `%`-prefixed numeric pane ID, and a `0` or `1` mode flag.

Window rows will use:

```text
#{window_index}:#{window_name}
```

The parser will split only at the first colon. This preserves window names
that contain additional colons, such as `node:server`.

The code will not accept underscore as an alternative separator. Underscores
are valid window-name content and cannot safely distinguish fields after tmux
has sanitized the original tabs.

## Components

- `apps/cam-desktop/electron/tmux-controls.cjs`
  - Add a pure client-state parser.
  - Update window-row parsing to consume the first printable separator while
    preserving the remaining window name.
- `apps/cam-desktop/electron/main.cjs`
  - Request colon-delimited client state and window rows.
  - Use the pure parser and keep the existing staged diagnostic errors.
- `apps/cam-desktop/test/tmux-controls.test.cjs`
  - Cover valid client state, malformed/sanitized state, copy-mode state, and
    window names containing colons.
- Existing Desktop terminal tests
  - Confirm the main process uses the printable formats and exposes no
    regression in window controls or diagnostics.

## Data Flow

1. Desktop opens a short exec channel over the existing pooled SSH connection.
2. tmux emits printable colon-delimited state.
3. The main process validates the structured response with pure parsers.
4. Validated window and copy-mode state is returned through the existing IPC
   bridge.
5. The renderer displays tabs, History, and To Bottom using the existing UI
   logic.

No SSH connection, client discovery, terminal PTY, or renderer protocol changes
are required.

## Error Handling

Malformed output remains a `client_state/tmux_parse_failed` or
`list_windows/tmux_parse_failed` failure. Invalid rows are not partially trusted.
The existing persistent diagnostic status remains available for future remote
compatibility failures.

## Verification

1. Add regression tests first and observe failure with the current tab-based
   implementation.
2. Implement the printable formats and parsers, then make the focused tests
   pass.
3. Run the Desktop terminal, tmux-control, hub, start-form, syntax, and lint
   checks.
4. Run a read-only ssh2 probe against PDX098 and confirm client state is
   returned as `0:%0:0` without relying on locale variables.
5. Build the next MSI and manually verify that dvnet shows window tabs and the
   History button after attach.

## Non-Goals

- Do not force or transmit `LANG`/`LC_*` values.
- Do not change SSH pooling, timeouts, or terminal attach behavior.
- Do not change History/copy-mode semantics in this fix.
- Do not add compatibility parsing for sanitized underscore output.
