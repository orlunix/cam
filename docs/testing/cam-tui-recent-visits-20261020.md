# CAM TUI recent visits — 2026-10-20

This records the earlier 0.1.4 iteration. Per-agent pane tabs, simplified SSH
Nodes and the later seven-Node 0.1.5 release are covered in
[the subsequent validation report](cam-tui-nodes-terminals-skills-20261020.md).

## Behavior

- The sidebar sorts visited agents first, newest visit first. Agents without
  visits sort by name, then Node and agent ID for stable ties.
- Clicking an agent or selecting its tab records a visit. Visits from each UI
  are queued in click order. Background output, status refreshes and filters
  do not update the visit history.
- The local Client daemon stores up to 1000 visits in its existing SQLite
  database, keyed by Node and full agent ID. Multiple UI windows share this
  history, which survives UI and daemon restarts. Nothing is forwarded to a
  Server or written to the remote agent store.
- Per-agent pane tabs are deferred under the user's optional scope: the
  current Server resolves pane0 and allows one terminal connection per agent.
  Supporting independent panes also needs attachment, writer ownership and
  geometry changes across the Server and daemon. Existing tabs are unchanged.

## Verification

- Rust workspace: **25 passed** (19 TUI, 4 Server, 2 protocol).
- Focused real UI/SDK/tmux tests: **5 passed**, 38.148 seconds. Coverage includes
  actual list and tab clicks, rapid tab switching, persistence after daemon
  restart, background output, filtering, same IDs on different Nodes, bounded
  storage, unchanged agent records, settings and two-window recovery.
- Static Linux x86_64 musl release build, Python syntax and diff checks passed.
  The socket test initially hit the sandbox restriction; the complete suite
  passed outside the sandbox. Disposable test agents were reclaimed and the
  production daemon was restored.

## Local deployment

`~/.local/bin/cam-tui` and `cam-clientd` are **0.1.4**. The local daemon was
restarted; all 65 original agent identities remain and all seven Nodes are
ready. No Server or camc binary update was needed. Other machines' TUI
binaries were not updated in this round.

Release: `~/.cam/releases/cam-tui-v0.1.4-2881aef77a76`.
Logs and deployment verification: `/tmp/cam-tui-recent-visits-20261020/`.
