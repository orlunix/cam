# CAM Server Router

Rust Router implementing the [Client/Server Spec](../../docs/cam-client-server-spec.md)
and [forwarding protocol](../../docs/cam-client-forwarding-spec.md). Agent state,
tmux, launcher and monitors remain in the existing native camc backend.

`cam-server` is the single machine/user service. `cam-forwarder` is a headless
reference implementation of a **Client's forwarding component**, intended for
testing and embedding; it is not a third product role or a new hub service.
Existing Desktop/Mobile transports are unchanged and have not yet embedded it.
The Python package's old `cam-server` entry point is unchanged; use the new
binary's absolute path during migration.

## Build on Linux

Rust 1.71.1 and a C compiler suffice for development:

```sh
cargo build --locked --manifest-path components/cam-router/Cargo.toml
cargo test --locked --manifest-path components/cam-router/Cargo.toml
```

Distributable binaries use **musl static linking**, bundled SQLite, a baseline
CPU target, and no OpenSSL, Python, NVIDIA, LSF or systemd dependency in the
Router. Like [Herdr's Linux releases](https://github.com/herdrdev/herdr/blob/master/.github/workflows/release.yml),
x86_64 and aarch64 need separate artifacts. This removes the runtime glibc
dependency; it does not guarantee every CPU, kernel, filesystem or distribution.

On the matching CPU, install the official Rust musl target and a musl C compiler
(for example the distribution's `musl-tools` package), then:

```sh
rustup target add x86_64-unknown-linux-musl
export CC_x86_64_unknown_linux_musl=musl-gcc
sh components/cam-router/scripts/build-linux.sh x86_64
# On ARM64: rustup target add aarch64-unknown-linux-musl
# export CC_aarch64_unknown_linux_musl=musl-gcc
# sh components/cam-router/scripts/build-linux.sh aarch64
```

The script rejects dynamic dependencies, smoke-tests native `version`, and
produces both binaries plus SHA256SUMS. It does not install, publish or overwrite
legacy camc. Cross builds also require a matching C compiler/linker; an ARM
binary must be tested on ARM before claiming support.

## Start one Server

```sh
/absolute/path/cam-server init --config /private/config/node-a.json --node-id node-a
# Edit the generated private JSON to add peers and Client credentials.
/absolute/path/cam-server serve --config /private/config/node-a.json
```

The default config is `$XDG_CONFIG_HOME/cam-server/<hostname>.json` or
`~/.config/cam-server/<hostname>.json`. A generated UUID is retained as Node ID;
hostnames and aliases do not replace identity. Foreground `serve` works without
a service manager. A supervisor may restart it, keeping the same config/state.
SIGINT/SIGTERM closes the Router without stopping tools already in tmux.

A machine/user-wide advisory lock rejects a second production Server, including
one configured with another Node ID or runtime directory. Sockets, locks and
SQLite state stay on a node-local filesystem; NFS/SMB runtime paths are rejected.
Default runtime: `/tmp/cam-server-<uid>-<node-hash>/runtime`. **For persistence
across reboot or /tmp cleanup, set `runtime_dir` to a private, node-local durable
path** (short enough for Unix sockets). Deleting state loses deduplication and
queued results; do not wipe it to resolve an unknown operation.

Example Node A config; use a separate Node B config with the identities reversed:

```json
{
  "node_id": "node-a",
  "alias": "a",
  "runtime_dir": "/var/tmp/cam-router-1000/a",
  "clients": [
    {"client_id": "desktop-1", "token_file": "desktop-1.token", "source_nodes": ["node-b"]}
  ],
  "peers": [
    {"node_id": "node-b", "aliases": ["b"], "operations": [
      "message.send", "agent.send", "agent.capture", "agent.run", "agent.stop",
      "agent.remove", "agent.kill", "agent.reboot", "agent.key", "agent.status"
    ]}
  ]
}
```

Config/token files must be owned by the current user with mode 0600, directories
0700; tokens must contain at least 32 random bytes. Provision a distinct token
per Client–Server pair, shared only between those two endpoints. Each grant
explicitly lists which other authenticated source Nodes that Client may relay.
Peer operation lists authorize both outbound and inbound operations; internal
results/receipts are allowed for registered peers and checked against the
original request. No arbitrary shell operation or remote `list` is available.

## Connect a reference Client

The private Client config specifies directly connected Servers. A local socket
and a long-lived SSH stdio channel use the same framed protocol:

```json
{
  "client_id": "desktop-1",
  "endpoints": [
    {"node_id": "node-a", "token_file": "node-a.token", "socket": "/var/tmp/cam-router-1000/a/server.sock"},
    {"node_id": "node-b", "token_file": "node-b.token", "command": [
      "ssh", "-T", "-o", "BatchMode=yes", "host-b",
      "/absolute/path/cam-server", "connect", "--socket", "/var/tmp/cam-router-1000/b/server.sock"
    ]}
  ],
  "agents_file": "agents.json",
  "agent_ttl_seconds": 60
}
```

```sh
/absolute/path/cam-forwarder --config /private/config/client.json
```

The argv transport is spawned once per connection, never once per message.
`connect` bridges stdio to an **already-running** Server; it does not start
another Server or run camc. SSH provides encryption/host authentication, and
the forwarding handshake independently checks the expected Node and token.
Use absolute, shell-safe remote paths; SSH interprets its remote command.
There is no exposed TCP listener or assumption of passwordless SSH on every host.

The optional private metadata file is a JSON array:

```json
[{"node_id":"node-b","id":"beefcafe","name":"worker","status":"dead"}]
```

It reuses a Client's existing agent table. File mtime or each entry's `updated_at`
(Unix seconds) bounds freshness. Offline/expired/ambiguous entries cannot resolve
a name; dead agents remain valid targets for status/rm/reboot. Explicit
`Node:8-hex-ID` bypasses name lookup, with real existence checked at the target.
Refresh the file as the existing Client cache changes. Router does not read or
write `agents.json` to build a second authority.

## Enable existing native agent operations

Without a backend, the Router needs no Python; unregistered agent operations
return `UNSUPPORTED_OPERATION`. Optional `"echo": true` registers `test.echo`
for independent Router tests. It is off by default.

To use the native camc backend, add to the target Server's config:

```json
"backend": {"python":"/absolute/path/python3", "module_path":"/absolute/path/cam/src"}
```

This is a Server-supervised private **stdio SDK worker**, not a separate
listening daemon. Business messages invoke `CamOperationReceiver` and existing
backend functions; the Server never dispatches `camc <business argv>` or parses
CLI output. Python 3.6+ and tmux are needed only for the optional native adapter;
each tool still needs its own installation/environment. The worker terminates
with the Server; existing tmux tools and monitors remain independent.

The Server writes `~/.cam/server/endpoint-<hostname>.json` for opt-in camc
discovery (disable with `discover_camc:false`). An explicit `CAM_SERVER_SOCKET`
also works. Local targets retain local-first behavior; a selected Server path
never retries via legacy transport after uncertain submission.

```sh
camc capture b:beefcafe --lines 100
camc --json status b:beefcafe
camc run --node b --tool codex --path /shared/workspace
camc msg send b:beefcafe -t 'please inspect' --no-wait
camc msg wait MESSAGE_ID --timeout 600
# On the receiver, using its local Server:
camc msg reply MESSAGE_ID -t 'result'
```

See [camc integration](../../docs/camc-server-integration.md) for the complete
whitelist, stdout behavior and LSF callback limitation.

## Delivery, recovery and limits

- Every request, reply, operation result and delivery receipt is an ordinary
  addressed message with its own ID. Client forwards original encoded bytes;
  replies can return over another Client. No Client business pending map exists.
- Ready, authenticated direct connections advertise snapshots with connection
  generation, increasing sequence and TTL. Old snapshots/connections cannot
  revive or delete a replacement route. No recursive/multi-hop relay.
- Server SQLite WAL queues/dedup are durable before acknowledgement. Keys are
  `(source Node, message ID)` plus a canonical content digest. Transport retries
  preserve IDs. Interrupted execution reports `unknown`, never blindly replays
  native effects. The SDK also deduplicates at the execution boundary.
- `not_forwarded` means not enqueued/written to the destination. Partial writes
  report `unknown`; missing confirmation is also unknown. These transport
  statuses do not fabricate a business reply. Server decides when to use an
  alternate Client. A lost receipt is recovered by retrying the original ID.
- Frames are bounded to 4 MiB, connection queues to 32 frames and a shared
  16 MiB budget by default. Server defaults: 32 connections, 8 executing native
  operations, 10,000 stored messages and a 64 MiB admission budget. Bounded
  completion reserve adds at most `2*max_messages+16` rows and 64 MiB for
  receipts/results of admitted work. Queue-full rejects admission explicitly.
- Deadlines stop undelivered execution, not already-started side effects.
  Dedup/result tombstones remain seven days after expiry; budget exhaustion may
  reject new work until old tombstones expire. Capacity is configurable.
  Expired-but-unconfirmed state is visible rather than silently discarded.
- Default logs contain connection/error metadata, never bodies or tokens.
  `cam-server status --socket PATH` shows identity, epoch, clients/routes and
  queue states; `query --socket PATH --message-id ID` shows local delivery state
  and saved results. Query output includes the requested business result.

## Verification and boundaries

```sh
cargo build --locked --manifest-path components/cam-router/Cargo.toml \
  --features test-harness --bins --examples
CAM_ROUTER_BIN_DIR=components/cam-router/target/debug \
  python3 -m unittest discover -s components/cam-router/tests -v
```

The unshipped `router_test_node` example simulates independent machines on one
host. Production `cam-server` always enforces the machine/user singleton lock,
even in a build with the test feature. Tests use private Homes, sockets,
credentials and disposable real tmux tools, then remove them. They cover three
Servers/two Clients, unchanged frames, concurrency, two-way mailbox turns,
alternate reply routes, restart recovery, partial-write status, authentication,
route generation/TTL, quota/expiry and all ten agent-specific operations.

This validates separate processes on one Linux host and stdio transport. It
does not claim physical cross-host SSH, packaged Electron, Mobile UI, ARM or
every Linux distribution has been tested. No Desktop connection/crypto code is
changed. Owner-Node callbacks from a tool moved to another LSF machine still
need a cross-host callback transport; a Unix socket path cannot provide that.
