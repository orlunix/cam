---
name: managing-cam-tui
description: >
  Operate and troubleshoot CAM TUI: connect Nodes over SSH, manage agent
  terminals, follow agent startup, use history and clipboard, and inspect
  the local Client daemon. Use for cam-tui/cam-clientd workflows.
metadata:
  author: hren
  category: infra
  tags: cam, cam-tui, terminal, agents
  requires-tools: cam-tui
---

# Managing CAM TUI

CAM TUI is the terminal Client for CAM Servers. One local `cam-clientd`
owns Node connections, shared agent metadata, forwarding and scheduled jobs;
multiple TUI windows share it. Each Server owns its machine's agent operations.

Agents can also use `~/.cam/camc` to address a specific remote agent through
these connections: `Node:agent` for `status`, `capture`, `send`, `msg`, `key`,
`stop`, `kill`, `reboot` and `rm`; `run --node Node` for creation. Bare names
prefer local matches. See managing-camc and camc-messaging for addressing and
replies. TUI fleet discovery/terminal attach and CLI per-agent routing are
different capabilities; camc does not provide remote fleet `list` or `attach`.

## Start and inspect

```sh
cam-tui version
cam-clientd version
cam-tui
cam-tui daemon status
```

The TUI starts its daemon when needed. `cam-tui daemon start|stop` controls
only the local Client background; stopping it disconnects its routes and
pauses its jobs, while agents and their tmux sessions keep running.
Use the existing installation and Client configuration; preserve Node
identities, credentials and runtime data during an update.

## Connect a Node

Open **Nodes / F2 → + Add**. Supply Host and, as needed, Port (default 22),
Username, SSH key file and an optional display name.

- Username is optional: OpenSSH uses its SSH config or the current user.
- The key is optional: OpenSSH can use an agent/default key or password login.
- Authentication temporarily uses the native terminal for OpenSSH host-key,
  password or encrypted-key passphrase prompts, then returns to the TUI.
  User passwords/passphrases are not saved to CAM config, argv, IPC or logs.
- CAM discovers the remote Server and pairs the Client automatically. A
  Server must already be installed and running. First-time Client pairing
  requires Server 0.1.2+; older Servers can reuse an existing management grant.
- The long Node ID is internal. Users select Host/display names; do not ask
  them to assemble a transport command, token file or Server ID.
- **Connect** can authenticate again after an SSH session expires. **Edit**
  changes a connection or its alias. Existing local/raw connections remain
  available through **Local / advanced**.

OpenSSH handles known_hosts and SSH authentication. Do not disable host-key
checks or copy login secrets into connection commands to bypass a failure.

## Agent list and terminals

The sidebar has separate Filters and Agents sections. Node/Tool/Status
filters combine with text search for names, IDs and `#tags`.
Agents sort by the Client's most recent visits. Background output and status
polling do not reorder them; visit history survives UI/daemon restarts.

Select an agent on the left. The right tab bar contains **that agent's
terminals**, rather than tabs for different agents:

- **Agent** is fixed to the primary tmux pane `0.0` and cannot be closed.
- **+** creates an auxiliary shell in the same agent session. Its tmux
  window has an independent pane, preserving the agent's screen area.
- **×** closes that auxiliary shell. It does not stop the agent pane.
- **↻** refreshes the list of existing panes. Switching agents restores its
  previous terminal; evicting an inactive view does not kill its shell.
- Auxiliary shells run on the owning Node. An agent launched through LSF
  may execute remotely; a new shell does not inherit that allocation.

Terminal panes need Server 0.1.2+ and a matching SDK. If unavailable, keep the
primary agent terminal usable and report the missing capability. Attach and
input ownership are per pane. A second view may be read-only; release/close
the writer view to hand over control rather than restarting the agent.
tmux windows in one session share geometry: the primary agent writer view
controls their size. Auxiliary views cannot resize another writer's agent.

Agent **…** opens name/tags settings, Stop, Remove and More actions. More
includes prompt, status, capture, reconnect, fresh reboot and kill. Reboot
keeps the agent ID and replaces the full tmux session; old auxiliary shells
end with it. Preserve run/reboot launcher, autorun and tool environment policy.

## Follow startup to an observable result

**New / F1** chooses Node, Tool, Workspace, optional launcher and prompt.
For an agent-start request, submitting the form/command is not completion:
follow its terminal and status until **started**, or report a specific
failure/queueing reason. Handle workspace-trust and ordinary tool startup
choices within the user's authorization, then verify the prompt is usable.

LSF/qsub waiting happens inside tmux. Queueing remains alive/running with
phase initializing; do not infer failure from a long dispatch wait or
submit duplicates. A returned/accepted operation alone does not prove the
tool is started. Check the actual terminal when metadata is stale.

Agent status is alive/dead. Alive is idle/running; initialization is a phase
of running. Dead reasons include stopped, failed or killed. Stale indicates
metadata freshness and is not a new tool lifecycle state.

## History, copy and exit

- PageUp/PageDown and the wheel browse CAM's terminal history; End returns live.
- Drag to select, release, then Ctrl+C copies. Without a selection Ctrl+C is
  sent to the tool. Ctrl+V/terminal Paste does not append Enter.
- The outer terminal's right-click Copy may be grey because it does not
  recognize CAM's selection. Use CAM's Ctrl+C and verify the native clipboard
  helper; do not assume mouse copy works everywhere.
- Native clipboard helpers are local to the TUI, not the remote Node. Do not
  silently enable OSC 52 when an unsupported terminal prints escape garbage.
- F4 opens Help; F8 toggles tool mouse passthrough. Ctrl+Q exits the UI and
  leaves agents, auxiliary shells and the daemon running.

## Automation and diagnosis

`cam-tui rpc ACTION 'JSON'` talks to the local daemon. Useful read-only actions
are `health`, `snapshot` and `terminal.list`; resolve a target's internal Node
and full agent ID from the snapshot before an operation. Extract only needed
public fields when reporting; snapshots also contain private configuration.

Agent actions use `agent.operation` with `node_id`, `agent_id`, `operation`
and `options`. Terminal create/close are explicit side effects; a timeout
may mean an unknown outcome. Refresh/query before retrying instead of
replaying a creation or reboot automatically.

Separate a failed SSH login, unavailable Server, missing management/pane
capability, stale agent snapshot and a closed tool. Inspect daemon health and
the Node's connection result first. Native SSH diagnostics live in private
`cam-ssh-*.log` files under the Client runtime directory. Avoid stopping a
healthy agent to repair a UI or connection problem. Reclaim disposable test
agents, terminals and SSH fixtures after verification.
