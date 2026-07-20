# Desktop NodeTransport and local WSL design

## Scope

CAM Desktop will route every node operation through one `NodeTransport`
contract. Features such as Start, agent synchronization, CAMC commands,
Skills, Todos, Browse, uploads, and Terminal will no longer decide for
themselves whether a node is local or SSH.

The existing automatic `local` node is retained. On Windows it means the
default WSL Linux environment; it does not mean running the bundled POSIX
CAMC script directly with Windows `spawn`.

This design supersedes the Windows `local_runtime_unsupported` behavior in
`2026-07-15-start-local-runtime-design.md`.

## Persisted node model

The persisted topology discriminator remains `machine.type`:

- `"local"` selects the runtime supplied by the Desktop operating system.
- `"ssh"` selects the configured SSH endpoint, even when its host is
  `127.0.0.1`, `localhost`, or `::1`.

There is no `local: true` flag, persisted `transport_driver`, or WSL distro
field. Hostname inspection must not determine transport behavior.

Example local record:

```json
{
  "machine": {
    "type": "local",
    "host": "",
    "user": "",
    "port": null
  }
}
```

Example SSH record:

```json
{
  "machine": {
    "type": "ssh",
    "host": "pdx-container-xterm-098.prd.it.nvidia.com",
    "user": "hren",
    "port": 3422
  }
}
```

Existing records remain compatible. Missing `machine.type` may be decoded
once using the legacy record shape, but all feature code receives a
normalized `local` or `ssh` value.

## Runtime resolution

Only the NodeTransport factory maps topology and native OS to an adapter:

| `machine.type` | Desktop OS | Adapter |
| --- | --- | --- |
| `ssh` | any | existing pooled `ssh2` adapter |
| `local` | Windows | default-WSL adapter |
| `local` | Linux/macOS | native-process adapter |

The WSL adapter invokes `wsl.exe --exec ...` without `-d`, so Windows uses
the user's configured default distribution. WSL2 is the expected and
validated Windows runtime. The app does not install, select, or manage a
distribution.

WSL is a Windows-only adapter. Linux and macOS local contexts use the
existing native-process behavior behind the same contract and must never
detect, invoke, or depend on `wsl.exe`. The WSL module is required lazily
only after the factory selects `wsl` on `win32`; shared Hub and renderer
code remains platform-neutral.

No renderer, API route, or feature module may branch on hostnames, a node key
named `local`, or native OS. They resolve a context or agent and call the
factory. Adapter-internal platform handling is allowed.

## NodeTransport contract

Each adapter exposes equivalent operations and normalized result shapes:

- execute CAMC or another approved tool with argv, optional stdin, timeout,
  and environment setup;
- list, stat, read, write, upload, and download files;
- open, resize, write to, and close an interactive agent terminal;
- dispose underlying resources.

Feature code receives `{ ok: true, ... }` or
`{ ok: false, error, detail, ... }` regardless of the adapter. Secrets and
native command construction remain in the Electron main process.

The SSH adapter preserves the existing long-lived `ssh2` pool. The WSL and
native-process adapters may keep lightweight process/runtime metadata, but
must not introduce one shell launch merely to classify each request.

## Windows local runtime

The local WSL environment is assumed to be provisioned by the user. Desktop
performs a focused preflight and reports which requirement is missing:

1. WSL2 and a usable default Linux distribution;
2. Python;
3. `tmux`;
4. `~/.cam/camc` or the packaged CAMC deployment path;
5. the selected agent tool, such as Claude, Codex, Cursor, or Aider.

Desktop does not install these dependencies or test tool authentication.
Authentication is not a Start precondition: the executable-presence check
must not invoke a probe that can fail merely because login or licensing is
incomplete. Desktop prepares and opens the tmux-backed agent session, and the
user may complete login, licensing, or other interactive setup inside that
session. Start errors identify only runtime or executable failures that make
session creation impossible.

Commands are passed as fixed argv through `wsl.exe --exec` whenever possible.
Shell text is used only for an existing `env_setup` contract and must be
quoted in one adapter-owned helper. User prompt/input is delivered through
stdin, not interpolated into a command string.

## Terminal behavior

Remote terminals continue to use the pooled SSH PTY channel. Windows-local
terminals use a Windows pseudoconsole to launch `wsl.exe` and attach CAMC to
the agent's tmux session. The implementation may use `node-pty`/ConPTY and
must be included in the Electron packaging and MSI verification path.

Both terminal adapters feed the same renderer events and support the same
open, input, resize, close, warm-session reuse, and error behavior. Electron
main asks NodeTransport to open a terminal and does not decode SSH options
itself.

## UI behavior

The automatic local Node remains visible and is labeled as local. On Windows,
its supporting text explains that it uses the default WSL2 Linux environment.
It has no SSH host, port, key, password, or distro fields.

SSH node creation remains explicit. Entering a loopback host still creates an
SSH node and requires normal SSH credentials.

To keep the first migration thin, existing Nodes grouping and display code is
left unchanged except for the Windows-local WSL label. Transport-neutral UI
cleanup is a separate change unless a regression test requires it.

## Verification

Automated tests cover:

- the single topology/OS-to-adapter factory mapping;
- loopback addresses remaining SSH;
- legacy record normalization;
- default WSL invocation without `-d`;
- argv/stdin handling and normalized errors;
- local preflight failure details;
- operation parity through fake SSH, WSL, and native adapters;
- Electron Terminal delegation and session lifecycle;
- a source guard against transport branching outside approved adapter files;
- unchanged remote SSH behavior and existing Hub/API response shapes.

Windows verification additionally covers `node-pty` rebuild/packaging,
interactive WSL terminal input and resize, Start with an empty prompt, CAMC
send/capture, Browse file operations, and MSI installation.
