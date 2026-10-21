# cam-tui guide and sidebar — 2026-10-20

This UI work follows the launcher/clipboard checkpoint `fdeb302` on
`cam-tui`.

## Changes

- The welcome screen and F4 / Help share a colored guide for history,
  selection, clipboard, quitting, filters, aliases and common actions.
- Agent detail rows show `tool · status · node`. Explicit connection names
  remain aliases, including dots; otherwise hostnames become short labels
  such as `pdx-046`. Nodes → Edit → Name saves an alias. Routing still uses
  the original Server identity.
- Node, Tool and Status dropdowns combine with text search. Clear filters
  resets all four conditions. Short windows use a compact Filters panel.
  Open choice lists retain their original identities during snapshot updates.
- UI text uses a fixed RGB palette alongside the existing dark backgrounds.
  In the private xterm preview, indexed foreground colors appeared black
  despite correct ANSI output; RGB foregrounds restored readable contrast.
  Agent terminal output still uses its own parsed colors.

Mouse copying remains open: the user confirmed Ctrl+C works, but the outer
terminal's mouse-copy path still fails. This release does not claim to fix it.

## Verification

- Rust cam-tui tests: **16 passed**, including combined filters, stable
  identities, alias labels, colored guide and small-window rendering.
- Full real UI/SDK/tmux tests: **10 passed**, 113.936 seconds. This includes
  New, Nodes, status/capture, stop/kill/reboot/remove, tasks, clipboard,
  multiple windows, recovery and the new sidebar flow.
- After the final palette change, the new real UI scenario passed again
  against the final binary: **1 passed**, 6.879 seconds.
- Private authenticated Xvfb/xterm previews of the welcome page and F4 guide
  were inspected. No production agent terminal was opened for these previews.
- A sandbox denied the Rust input-order test's temporary Unix socket; the
  complete suite passed when rerun with socket access. The initial UI run
  also exposed test-label ambiguity and a missing asynchronous operation
  wait; those test interactions were corrected before the passing full run.

Private clipboard testing used a separate X display. Temporary agents and
displays were reclaimed, and the production client daemon was restored.

## Deployment

**cam-tui 0.1.2**, static Linux x86_64 musl, SHA-256:
`9bb9ab93a04e3e6494312ba4b0490d3821455929a9b71011dc04b27748b6efb6`.

Installed to `~/.local/bin/cam-tui` on PDX046/064/086/106, KIX001/009 and DC7.
All seven verified the same version/hash and the existing native clipboard
helper. TUI-only deployment preserved all 65 production agent identities;
seven Nodes are ready and the client daemon is healthy. Server and camc/SDK
binaries were not changed by this UI deployment.

Reopen the TUI to load the new binary. Private logs, screenshots, manifests and backups are
recorded in `/tmp/cam-tui-guide-filters-20261020/` and each personal release
directory.
