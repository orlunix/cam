# cam-tui clipboard fix — 2026-10-20

The reported outer terminal menu showed Copy disabled after a CAM selection,
and Ctrl+C printed `]52;c;...` plus base64 text. The outer menu does not own
CAM's selection. The second symptom came from the automatic OSC 52 fallback
when no native clipboard helper could be launched. An inherited TMUX
environment also made the old UI emit a tmux passthrough wrapper into a PTY
that could not interpret it.

The new real UI regression reproduces the old binary printing
`tmux;]52;c;VFVJX1JFQURZXw==` into its frame. It fails against the old binary
and passes after the change.

## Change

- Native clipboard helpers are resolved from PATH and beside cam-tui.
- X11 supports both xclip and xsel; Wayland and macOS helpers are preserved.
- Missing/broken native clipboards return a visible error by default.
- OSC 52 requires explicit `CAM_TUI_CLIPBOARD=osc52`; no default escape
  payload is sent to an unsupported terminal.
- Ctrl+C copies a CAM selection; without one it remains a tool interrupt.
  Releasing a drag retains the selection. The outer terminal's Copy item
  may stay disabled; use CAM Ctrl+C or a right-click delivered to CAM.

## Verification and deployment

cam-tui Rust tests: **14 passed**. Full real UI/SDK/tmux process tests:
**9 passed**, 109.401 seconds. Clipboard testing used a separate authenticated
Xvfb display, not the user's clipboard. Unicode/multiline contents were
written and read back through real xclip; a sibling helper was required by
omitting helpers from the UI's PATH. Paste, no added Enter, copy versus
interrupt, and repeated Ctrl+C without a clipboard were checked.

Published **cam-tui 0.1.1**, static Linux x86_64 musl, SHA-256:
`bb7448b1c54a4ac205d5639eb908e627c95da3fa5bfff9e2ddc6b6abee77e698`.
Installed to `~/.local/bin/cam-tui` on PDX046/064/086/106, KIX001/009 and DC7.
Each node verified the version and matching hash. An xclip entry beside the
binary points to the existing site installation `/home/utils/xclip-0.13/bin/xclip`;
all seven checked its executable dependencies successfully.

The deployment did not restart Servers, daemons or existing agents. The local
daemon was briefly stopped for private test instances and restored afterward.
All test agents and the private display were reclaimed; production retains
the same 65 agents, seven ready Nodes and a healthy daemon. camc/Server SDK
remain v1.2.191. Reopen existing TUI windows to load the new binary.

Private evidence: `/tmp/cam-tui-clipboard-fix-20261020/`. No commit or push.

## User follow-up

The user confirmed that Ctrl+C now works in their desktop terminal. Mouse
copy still does not work and remains an open issue. The synthetic mouse-event
test does not prove that the outer desktop terminal delivers right-clicks to
CAM; do not report the user's mouse-copy path as fixed.
