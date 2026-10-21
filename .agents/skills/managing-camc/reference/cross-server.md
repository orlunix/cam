# Address agents on connected CAM Servers

camc keeps its standalone local behavior and can use a local CAM Server for
specific remote-agent operations. The path is:

```text
camc -> local CAM Server -> connected Client forwarding -> owner CAM Server
```

The Client can be cam-clientd or another authorized relay. It must connect
both Nodes; a Server running on each machine alone does not provide a route.
Requests and replies each use an available authorized path, which need not
be the same Client connection.

## Find and address the owner

Bare names/IDs prefer a local match. Only a local lookup miss goes to Server
resolution. A local ambiguity/error is not a reason to dispatch elsewhere.
Use a full agent ID or qualify the owner when names or prefixes are ambiguous.

`Node:agent` selects a configured Server hostname, peer alias or internal
Node ID. A TUI display-only nickname is not necessarily a Server peer alias.
Prefer configured hostnames/aliases rather than asking users to type IDs.
An explicit Node and full eight-character agent ID avoids the name cache.

Use CAM TUI's agent list or a filtered Client snapshot to find the owner.
`~/.cam/camc list` is not a cross-Server discovery API; remote `list --all`
is unsupported when the Server routing path is active. Shared `agents.json`
records do not make remote tmux sessions local.

For automation, `cam-tui rpc snapshot '{}'` returns Client metadata. Extract
only needed public agent/Node names, IDs and freshness fields: the full
snapshot also contains private connection configuration. Do not paste it
unfiltered.

## Commands

Examples assume `node-b` is a configured alias and `worker` is on that Node.
Perform mutations only within the user's task.

```sh
~/.cam/camc status node-b:worker
~/.cam/camc --json status node-b:worker
~/.cam/camc capture node-b:worker --lines 100
~/.cam/camc send node-b:worker -t "Continue the authorized task"
~/.cam/camc send node-b:worker -t "draft text" --no-enter
~/.cam/camc key node-b:worker --key Enter
~/.cam/camc msg send node-b:worker -t "Report the result" --no-wait
```

Supported lifecycle commands use the same qualifier:

```sh
~/.cam/camc run --node node-b --tool codex --path /work/repo -n worker
~/.cam/camc stop node-b:worker
~/.cam/camc reboot node-b:worker
~/.cam/camc kill node-b:worker
~/.cam/camc rm node-b:worker
```

These are separate choices, not a sequence to run against an existing agent.
The workspace and explicit tool paths must exist on the target Node. An
omitted launcher is resolved in the target environment. Preserve the usual
autorun/launcher policy: `--node` selects the management owner, while a tool
launcher can execute the tool in a different LSF allocation.

## Results and startup

Target stdout, stderr and exit status return to the caller. Capture returns
terminal output; `--json status` returns structured state. `send`/`key`
success confirms input delivery, not task completion. `msg send` normally
waits for an explicit business reply; `--no-wait` returns delivery information.
Reply on the receiving Node with `~/.cam/camc msg reply <msg_id> -t "result"`;
saved context supplies the destination. See camc-messaging for thread details.

After `run` or `reboot`, follow the returned ID with remote `status` and
`capture` until `status=alive, phase=started`, or report a specific failure
or queueing reason. Resolve ordinary startup confirmations within the user's
authorization. A long LSF dispatch wait can remain initializing; do not
submit another run just because the queue is slow.

## Connection failures and uncertain results

camc discovers its machine-specific local Server endpoint automatically;
`CAM_SERVER_SOCKET` can select an existing local endpoint. Do not guess a
remote Unix socket path or start a second production Server.
Use `cam-tui daemon status` and filtered `cam-tui rpc health '{}'` output to
distinguish a missing Server, disconnected Client, stale name cache, unknown
alias and missing operation permission. Health alone does not prove
end-to-end forwarding.

Server errors include correlation information when available. After
submission, camc does not automatically retry through a legacy path. A
timeout/disconnection can leave the outcome unknown: inspect the existing
target/result before retrying a run, reboot, removal or message. For an async
message, use `~/.cam/camc msg wait <msg_id> --timeout 600` on its source Node
to wait on the existing request without resubmitting it.

Unsupported remote operations include fleet list, attach, logs, update,
heal, cron and bulk cleanup. Use CAM TUI for multi-Node listing/attach;
perform local maintenance on the owner Node when authorized.
