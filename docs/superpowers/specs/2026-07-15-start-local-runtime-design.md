# Start form and local-runtime feedback

## Scope

- Keep the automatic `local` Node/context; it remains the ownership anchor
  for agents launched on the Desktop host.
- Move the optional Task name field out of Advanced and place it immediately
  above the optional Prompt field.
- Allow an empty Prompt to create an interactive agent.
- Replace the raw Windows `spawn ... camc ENOENT` launch failure with a
  clear explanation of the supported runtime and required configuration.

## Behavior

The Start form sends an empty prompt unchanged. The embedded Hub accepts it
and invokes `camc run` with an empty positional prompt; CAMC then launches
the selected CLI in interactive mode.

On Windows, a Start request targeting the automatic local Node returns a
stable `local_runtime_unsupported` error before attempting to execute the
bundled POSIX CAMC script. Its message explains that Local uses the Desktop
host runtime and requires a POSIX environment with `/bin/sh`, Python 3,
`tmux`, CAMC, and the selected CLI (Claude, Codex, Cursor, or Aider) installed
and authenticated. It directs the user to select a configured Linux SSH node.
WSL execution is intentionally not implied or attempted by this change.

## Error handling and verification

The UI keeps its existing inline status/toast behavior, but receives the
actionable error detail rather than a Windows `ENOENT` path. Existing remote
SSH Start behavior and local POSIX Start behavior are unchanged.

Tests will cover the Start layout contract, empty-prompt acceptance, and the
Windows-local preflight. Focused Hub and Start tests plus Electron syntax
checks will be run after implementation.
