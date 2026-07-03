# Contexts, machines, and nodes

cam has three related concepts:

| Term | Definition | Storage |
|---|---|---|
| **Context** | working directory + machine binding | SQLite `contexts` table |
| **Machine** | SSH/local host config | `~/.cam/machines.json` (also synced to camc) |
| **Node** | view of a machine + its agents | derived (no separate table) |

## Contexts

```bash
cam context list
cam context show <name>
cam context test <name>           # test SSH connectivity

cam context add myproject /path/to/project                    # local
cam context add pdx-work /home/scratch/project \
  --host pdx-container-xterm-098.prd.it.nvidia.com \
  --user hren --port 3422                                      # SSH

cam context add pdx-work /path \
  --host server --user dev \
  --env-setup "source /home/hren/.bashrc"                      # with env

cam context update <name> --env-setup "..."
cam context copy <name> <new-name>
cam context remove <name>
```

Fields:
- `name`, `path`
- `host`, `user`, `port` (SSH only; omit for local)
- `env_setup` — shell snippet sourced before launching agents
- `shell` — `bash` / `powershell` (Windows targets)
- `last_used_at` — bumped on every `cam run` against this ctx

## Machines vs Contexts

A **machine** is just connection info (host:port, user). A **context**
binds a working directory to a machine. Often there's a 1:1 mapping
(one project per host), but you can have N contexts → 1 machine when
you split work into subdirs.

`cam release` reads `machines.json` (the deployment list).
`cam run --ctx <name>` resolves ctx → machine → SSH transport.

## Nodes

```bash
cam node list                     # overview
cam node status <node>            # agents on one machine
```

A "node" is essentially `cam list --machine <X>` plus aggregated counts
(running, completed, failed). Read-only — there's no `cam node add`;
add machines via `cam context add` or `~/.cam/machines.json` directly.

## env_setup gotchas

`env_setup` is a shell snippet sourced **inside the tmux session's
shell** before exec'ing the agent tool:

```bash
# inside tmux, the launch ends up like:
bash -lc "<env_setup> && exec claude --allowed-tools ..."
```

Common pitfalls:
- Login shell init can be slow (NFS, network) — preflight does NOT run
  `command -v claude` through env_setup any more (was timing out at
  3s on heavy hosts).
- A bad env_setup that errors out kills the session immediately —
  agent shows up "completed" right away, look at
  `~/.cam/logs/monitor-<id>.stderr` for the bash error.
- env_setup is only consulted on agent **launch**. Modifying it
  doesn't affect already-running agents.

## SSH ControlMaster

Every cam → camc op (capture, send, kill, list, prune) goes over SSH.
ControlMaster sockets are shared between `SSHTransport`,
`CamcDelegate`, and `cam release` — same path:

```
/tmp/cam-ssh-<sha256(user@host:port)[:12]>
```

This makes 7-host operations near-instant after the first auth.

## Windows / non-bash shells

Set `--shell powershell` on a context to skip the `bash -l -c` wrapper.
SSHTransport then uses `cmd.exe`-style quoting for `send-keys` payloads.
Non-ASCII text is base64-encoded to survive POSIX-locale shells.
