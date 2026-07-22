# Machines and contexts

Two related but distinct concepts in camc:

- **Machine** = a host you can SSH to (or local). Defined in `~/.cam/machines.json`.
- **Context** = a working directory, optionally bound to a machine. Defined
  in `~/.cam/contexts.json`. An agent's `--path` resolves through a context.

## Machines

```bash
camc machine list                                       # list all
camc machine add pdx --host pdx-xterm-098.nvidia.com \
  --user hren --port 3422                               # SSH machine
camc machine add pdx --type local                       # local entry
camc machine add pdx --env-setup "source ~/.bashrc"     # custom shell init
camc machine edit pdx --port 3500
camc machine ping pdx                                   # test SSH
camc machine rm pdx
```

Common fields:
- `name` — short ID
- `type` — `ssh` or `local`
- `host`, `user`, `port`
- `env_setup` — shell snippet sourced before each tmux command (e.g. set
  PATH so `claude` is findable inside the session)
- `shell` — `bash` (default) or `powershell` (Windows targets)

## Contexts

```bash
camc context list
camc context add myproject -p /path/to/project          # local context
camc context add remote-proj -m pdx -p /remote/path     # bound to machine
camc context rm myproject
```

`~/.cam/context.json` (singular) is a legacy single-machine config still
honored as a fallback for `env_setup` when the user has only one machine.

## How a `~/.cam/camc run` resolves machine + context

1. `--path` is canonicalized (CWD if omitted)
2. Optional context name → `contexts.json` → may override `path`
3. Machine resolved via context (if any) or default local
4. `env_setup` from the resolved machine wraps the launch command:
   ```
   bash -lc "<env_setup> && exec <tool> ..."
   ```

Skip `env_setup` (and the bash wrapper) with `--no-inherit-env` — useful
when you want a clean shell + the system tmux's PATH only.

## machines.json shape

```json
[
  {
    "name": "pdx-098",
    "type": "ssh",
    "host": "pdx-container-xterm-098.prd.it.nvidia.com",
    "user": "hren",
    "port": 3422,
    "env_setup": "export PATH=/home/prgn_share/tools/claude-code/bin:$PATH"
  },
  {"name": "local", "type": "local"}
]
```

`cam release` reads this same file to know where to ship dist/camc.
