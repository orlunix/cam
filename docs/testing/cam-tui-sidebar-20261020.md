# cam-tui sidebar and agent settings — 2026-10-20

## Behavior

- Filters and Agents have separate bordered regions. The list scrolls only
  when the pointer is over its region.
- Agent names share one bold text style; the status dot carries the color.
  The second line is muted: a fixed-width tool column and status on the left,
  with the short Node name aligned to the right. Long names are shortened by
  terminal-cell width, preserving space for a fixed `…` button.
- Left-click `…` or right-click an agent for Settings, Stop and Remove.
  More actions contains terminal, message, status, capture, reconnect, reboot
  and kill. Settings is a compact Name/Tags form with the Node in its title.
- Tags are comma-separated, support letters/digits/underscore/dash, and can
  be cleared. Search accepts `#tag`. Renaming updates open tab titles while
  preserving the original agent identity and tool process.

The new authenticated management action `agent.update` accepts only name
and tags for an exact local agent ID. It uses the existing store lock to
modify the current record, including legacy flat records, without saving an
old runtime snapshot. Foreign hosts, prefixes, redirected Nodes, invalid
fields and invalid tags are rejected. Returned identity is checked by the
daemon; its cache freshness is refreshed after a successful save. This does
not extend the forwarding message operation whitelist or invoke camc CLI.

## Verification

- Rust workspace: **24 passed** (18 TUI, 4 Server, 2 protocol).
- Python Server management/protocol: **65 passed**, 17.02 seconds.
- Full real UI/SDK/tmux suite: **11 passed**, 123.356 seconds.
- Final binary, after cache/dialog/alignment refinements: **3 focused real UI
  cases passed**, 31.849 seconds, covering settings, agent operations and
  filters.
- The settings test checks persistent rename/tags, tag clearing, tab-title
  updates, unchanged PID/launch ID, fresh metadata, invalid input and denial
  of a Client without management permission.
- Layout checks cover fixed menu/Node columns, wide characters, lowlight
  text, distinct regions and usable attribute fields down to 48×12 cells.
- Python 3.6 grammar and `git diff --check` passed.

Private authenticated Xvfb/xterm screenshots of the final welcome/list,
`…` menu, compact settings form and guide were inspected. Preview clicks
did not open a production agent terminal or save production attributes.
Test agents, Nodes and displays were reclaimed; the existing local client
daemon was restored after private tests.

## Deployment

Installed **cam-tui/clientd 0.1.3** and **cam-server 0.1.1**, plus the updated
Server SDK metadata module, on PDX046/064/086/106, KIX001/009 and DC7.
Release: `cam-tui-v0.1.3-1d60e16d5665`.
Package content SHA-256:
`1d60e16d56658578874e2fc161efb531d1122cb55b634e5ea95412d6bebc1345`.

Each node verified all 60 package files and binary versions. Server configs
preserved all fields except the SDK backend path. The existing local client
daemon was refreshed; no extra client daemon was started on the six nodes
without one. Client identities/configs and the camc binary were preserved.
All 65 production agent identities remain, all seven Nodes advertise
`agent.update` and are ready, and the daemon is healthy.

Reopen `cam-tui` to load the new interface. Mouse clipboard behavior remains
the previously recorded open issue; this sidebar release does not change it.
Logs, manifests, backups and PNGs:
`/tmp/cam-tui-sidebar-20261020/` and the personal release/backup directories.
