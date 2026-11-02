# Progressive VNC, NFS and LSF reconnection

This is an agent-executed workflow using existing CAM commands, not a claim
that the TUI already has a bulk-import button or an autonomous VNC scanner.
Use it when asked to reconnect/import all reachable environments. Do the work
in stages, keep completed work, and finish with a partial-success report when
some environments remain inaccessible. Do not abort an entire batch on one
failure or undo Stage 1 because any Stage 2.x fails.

## Stage 0: locate the current Client

Record the actual current config path, host, user and runtime; inspect
`cam-tui daemon status` and `cam-tui rpc snapshot '{}'` with the same `--config`.
Do not dump the whole snapshot: it contains private configuration.
The default config is `~/.config/cam-client/HOSTNAME.json` (or beneath
`XDG_CONFIG_HOME`); default runtime is local `/var/tmp/cam-client-UID`.

Multiple VNC displays on the same host/user share one local daemon. Reopen the
UI with that existing config; do not init a new identity or start one daemon
per display. Different hosts retain separate Client identities/configs and
host-local runtime/socket/lock/SSH-control files, even with shared NFS Home.
Do not share/copy live runtime directories between hosts. If opening a window
inside an existing VNC desktop, use its terminal application and valid DISPLAY/
XAUTHORITY; changing DISPLAY does not by itself create a usable TUI terminal.

A failed daemon/status/inventory probe must not prevent Stage 1. Record that
failure and continue independently with this Home's SSH authorization.

For targets with csh/tcsh as the login shell, send POSIX inspection scripts to
`ssh ... /bin/sh -s`; a shell syntax error is not an SSH login failure.

Build a deduplicated endpoint inventory from saved Nodes, SSH config, this
user's VNC records and owned RUN interactive LSF jobs (`bjobs -w` plus detailed
verification). Preserve real SSH port, optional username, selected key and
short alias. Do not assume VNC display N means SSH port 22 or a particular
container port. Hostname/display aliases alone do not prove common Home or
namespace. Include existing managed agents' owner Nodes, not just execution
hosts found in LSF.

## Stage 1: prepare this NFS Home independently

First make this user's own public key authorized for this user's own SSH login.
Prefer the existing selected key; do not overwrite it. If no key exists, use
native `ssh-keygen`/SSH-agent handling to create/unlock one under the user's
policy. Do not invent a password, store a passphrase, or copy a private key.

From this skill's directory:

```sh
python3 scripts/authorize_own_key.py --public-key "$HOME/.ssh/id_ed25519.pub"
python3 scripts/authorize_own_key.py --public-key "$HOME/.ssh/id_ed25519.pub" --check
```

The helper supports system Python 3.6+, appends only a missing public key,
preserves other entries/options and owned symlink targets, and makes `.ssh`
0700 / `authorized_keys` 0600. It reports a fingerprint and result, not key
contents. Select the actual public key path when a different key is used.
An existing restricted entry is not replaced by an unrestricted duplicate.
A permission error or managed/foreign file is a concrete blocker, not a reason
to chmod someone else's files. Check Home permissions if sshd rejects login;
do not recursively chmod Home.

Checkpoint the authorization result, key fingerprint and candidate NFS group.
Then test an actual saved SSH endpoint with bounded noninteractive OpenSSH:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=8 -o ConnectionAttempts=1 \
    -p "$PORT" "$TARGET" 'hostname; id -un; printf "%s\n" "$HOME"'
```

Add `-i` only for the chosen local private key. Use native OpenSSH/TUI handling
for an unknown host key, encrypted key or required first-login password. Verify
new host fingerprints through the user's trusted source; never disable host-key
checks, replace a changed known_hosts entry blindly or expose credentials in
argv/logs. Separate `authorization ready` from `SSH verified`; a failed endpoint
does not undo successful authorization. Never claim every NFS peer is reachable
from one successful SSH login.

## Stage 2.x: each remote Home/group, then the current Client

For every requested remote endpoint, proceed independently:

1. **2.1 Reach it.** Reuse the saved SSH profile and existing approved jump route
   (`ssh -J` if applicable). If keys are not yet accepted, native password login
   may bootstrap it. Without any working login, report that target blocked;
   continue to other targets and retain the Stage 1 result.
2. **2.2 Do Stage 1 there.** Run the same helper on that remote user's selected
   public key and `authorized_keys`. Shared remote NFS peers can then reuse that
   group's key. Upload this small helper if it is not installed there; no
   private key travels. Verify independently on each distinct Home/group.
3. **2.3 Authorize the current Client's access.** For distinct Home/key pairs,
   remote self-authorization alone does not let Client A log into remote B.
   Authorize A's public key at B as well, using native `ssh-copy-id -i PUBFILE`
   over the established login or another approved public-key transfer. Never
   transfer A's private key or silently enable key/agent forwarding.
4. **2.4 Attach B to the current Client.** Reuse an existing Node ID/profile;
   otherwise use Nodes → Add or `node.ssh_add`. Then connect and scoped-sync it
   in the current Client, not in a new remote Client config:
   ```sh
   cam-tui rpc node.ssh_add '{"name":"remote-b","host":"remote-b.example","port":22,"user":"","key":""}' --config "$CLIENT_CONFIG"
   cam-tui rpc node.connect '{"node_id":"RETURNED_NODE_ID"}' --config "$CLIENT_CONFIG"
   cam-tui rpc node.sync '{"node_id":"RETURNED_NODE_ID"}' --config "$CLIENT_CONFIG"
   ```
   `user` is optional; `key` is a private-key path on the current Client or blank
   for normal OpenSSH selection. For an existing Node, preserve identity and
   grants; use its saved profile and resolved ID rather than creating another.
   Verify the subsequent snapshot: an accepted connect request is not yet a
   ready tunnel. Use bounded read-only retries until the Node is `ready` and
   expected agent IDs are visible. A scoped sync response from current code has
   `agents_synced`; older Clients may ignore `node_id` and return only
   `members`/`updated_nodes` for NV peer sync. That response does not verify the
   target's status refresh. Keep a working connection, verify visibility through
   periodic snapshots, and report scoped sync unsupported until the Client is
   updated; do not blindly repeat setup or declare the whole workflow failed.
   The user chooses hosts/short names, not internal IDs. Before software sync,
   inspect bundle and remote component versions; retain newer remote binaries.
   If an older release would downgrade a newer camc, keep the existing Server/
   connection and report software sync blocked rather than copying the old file.
5. **2.5 Import/sync sessions and reattach.** Follow
   [import-sessions.md](import-sessions.md). Reconnect the existing agent/pane
   view; do not run/reboot the tool just because SSH or the UI disconnected.

### LSF namespace check

Use LSB_JOBID only as a hint. Verify RUN, current ownership, interactive job,
service class and execution host before treating an allocation as reusable.
A normal SSH login reaches its configured host environment; it does not prove
entry into a job container. A tool remotely launched from a tmux on another
Node stays owned by that tmux Node. Connect/import there.

For a tmux actually inside an allocation, probe its socket in that same job
context with supported `battach -L` when available. Do not nest SSH+battach as
an assumed requirement or run qsub to repair a Client connection. A job that
exits, lacks an accessible endpoint, or cannot expose its actual namespace to
the existing Server must be reported blocked. Do not start a second Server on
the same Node or claim a host Server can see a container's private sockets.

## Checkpoints, retries and final report

Keep a private per-run checkpoint under the current Client's host-local runtime
(or another owned local directory), mode 0600. Record only public fields:
Home/group label, host/port/user/alias, public-key fingerprint, Node/agent ID,
stage result, timestamp, bounded error and next action. Never store passwords,
private keys, pairing tokens, complete snapshots or arbitrary argv/env there.

- Record and verify Stage 1 before remote work. Keep it successful if 2.x fails.
- Advance each target to its highest verified stage. A failed Node does not
  prevent adopting a session on another Node or completing another connection.
- Repeat runs probe existing results first; do not duplicate keys, Nodes,
  daemons, Server instances or imported agents.
- Retry read-only connection/health probes with bounded backoff (for example
  1/2/4 seconds). Do not loop on bad credentials, changed host keys or exited jobs.
- An operation/setup timeout can mean unknown outcome. Inspect snapshot, Node
  version/setup logs or the original request result before repeating a mutation.
  Never solve a reconnection timeout by blindly repeating run/reboot/adoption.
- Continue safely independent work, then report even if partial. Cleanup only
  temporary probes/test fixtures; retain successful authorization and connections.

Final report must include local Stage 1 result, per-target 2.1–2.5 results,
connected/visible agent counts by tool, blocked/failed/unknown items with reason
and next step, retained successful changes and any temporary resources removed.
Use separate columns such as `local key`, `remote key`, `SSH`, `Node`, `sessions`;
no single green status may hide an unverified later stage.
