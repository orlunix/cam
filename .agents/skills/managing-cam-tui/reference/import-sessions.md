# Import current tool sessions into CAM TUI

Use for Claude, Codex, Cursor and Pi. Default scope is this user's current
running sessions on the requested Nodes/VNC/LSF environments. If history was
also requested, inventory saved conversations separately: a transcript is not
an alive agent. Discover installed resume support before planning a restart.

## Inventory first, without restarting tools

1. Read the current Client's `snapshot` and existing Node profiles. Inventory
   this user's managed records and tmux servers on each reachable owner Node.
   Inspect the default tmux server, `$TMUX`, known CAM socket directories and
   owned VNC/LSF environments. Do not scan other users' sessions.
2. Enumerate each actual socket explicitly:
   ```sh
   tmux -S "$SOCKET" list-sessions -F '#{session_name}'
   tmux -S "$SOCKET" list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_pid} #{pane_current_path} #{pane_current_command}'
   ```
   Omit `-S` only when inspecting the default server. Follow owned pane child
   processes to identify the real tool. `node`, `bash` or `ssh` alone does not
   identify Claude/Codex/Cursor/Pi. Extract executable identities; do not print
   full argv, environment or conversation content.
3. Track `(owner Node, namespace, socket, tmux session, pane)` and tool type,
   workdir and existing agent ID. Shared Home is not a machine-local inventory.
   Two VNC displays on one host can share the same tmux server; deduplicate it.
   An LSF tool's execution host is not necessarily its tmux/agent owner.

Classify every result:

| Existing session | Action |
|---|---|
| Already registered and reachable | Keep its ID; sync its owner Node and reattach |
| Unregistered tmux with tool at `0.0` | Adopt in place, then sync and verify |
| Custom socket or conflicting tmux name | Resolve socket/name compatibility first; otherwise report blocked |
| Tool in another pane | Preserve layout; report that primary-pane mapping is needed |
| Live process in an ordinary terminal, without tmux | Report live but not attachable by the current backend |
| Saved conversation only | Report as history/resume candidate, never as a live terminal |

Do not kill/reboot/resume a running session to make an import appear successful.
The current backend cannot move an arbitrary live terminal's process, stdin and
history into tmux merely from its PID. Invasive terminal reparenting is outside
this workflow. Plan a managed replacement only when the user authorizes that
transition, and report it as a replacement rather than an in-place import.

## Adopt eligible tmux sessions

Run on the actual owner Node, inside the namespace that owns the tmux socket.
Use that Node's current Python `~/.cam/camc`; Rust camc is development-only.
Verify executable versions first; never downgrade a newer camc during Node sync.

```sh
~/.cam/camc add --help
# Run with the existing pane's working directory as CWD:
~/.cam/camc add "$SESSION" --tool claude --name "$NAME"
```

Use `codex`, `cursor` or `pi` as appropriate. Record the returned ID. Existing
registered sessions need a sync, not repeated adoption. Before adoption inspect
records across the shared registry: this CLI deduplicates by tmux session name.
Never re-adopt another host's same-named session and change its ownership.
Resolve a collision with a unique tmux name only when that rename is authorized;
otherwise report that individual session as blocked and continue with others.

The current Python `add` accepts no `--socket` flag. For an owned custom socket,
a compatible alias at `/tmp/cam-sockets/SESSION.sock` can allow the existing
lookup to find it. Create that alias only after verifying socket ownership,
user-owned directory, safe session filename, and no conflicting record or alias.
Preserve the original socket; never replace an existing path. A matching alias
can be reused. If these conditions cannot be established, report blocked rather
than inventing `camc add --socket` or editing the registry blindly.

Capture and status must work from a fresh shell with no inherited `$TMUX`, not
only from inside the original session. This checks that Server-side attachment
can resolve the same socket. Verify monitor activity separately from tool
activity. A retained shell or an `alive` label alone does not prove a live tool.

## Bring imported agents into the current Client

Resolve the owner Node in the same current Client configuration, connect it,
then perform scoped sync:

```sh
cam-tui rpc node.sync '{"node_id":"RESOLVED_NODE_ID"}' --config "$CLIENT_CONFIG"
cam-tui rpc snapshot '{}' --config "$CLIENT_CONFIG"
```

Check the sync result as well as the snapshot. An older Client may return only
`members`/`updated_nodes` (NV peering), even with `node_id`; this is not a scoped
agent refresh. Retain adoption and a working connection, wait for normal
snapshot refresh, and report manual scoped sync unsupported on that release.
A successful request is not proof that the tunnel is ready or a terminal works.

Verify each ID appears under the correct Node, select it in the TUI and check
Terminal 0 against the original pane. Preserve tool conversation identity,
processes, working directory, scrollback and auxiliary panes. A second writer
may make this view read-only; do not evict another viewer automatically.

A successful adoption remains useful even if Client connection/sync fails:
record `adopted; visibility pending` and retry only the pending connection step.
Report counts by tool and Node: discovered, already visible, adopted, connected,
blocked and failed, with an individual reason and next step for every remainder.
