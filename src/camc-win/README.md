# camc-win (née WinTmux)

The Windows-native [camc](https://github.com/orlunix/cam): manages
coding-agent sessions on a Windows node through a ConPTY-backed session
daemon, speaking the camc CLI subset that CAM Desktop's hub drives over
SSH. Lives in the cam repo (`src/camc-win/`); development of the
standalone wintmux repo has stopped — this is the canonical home.

Two binaries build from this module:

- **camc-win.exe** — the camc-compatible agent manager (what the hub
  deploys to Windows nodes at `%USERPROFILE%\.cam\camc-win.exe`).
- **wintmux.exe** — the tmux-compatible session CLI camc-win is built
  on (`new-session`/`send-keys`/`capture-pane`/`attach`/… over the same
  daemon). Useful for manual debugging; the hub never invokes it.

## What It Does

WinTmux replicates the subset of tmux commands that CAM relies on,
using the Windows **ConPTY** API as the backend. Each session runs
as an independent daemon process with its own pseudo-terminal, scrollback
buffer, and TCP-based IPC channel.

## Supported Commands

| Command | Description |
|---------|-------------|
| `new-session -d -s NAME -c DIR CMD` | Create a detached session |
| `send-keys -t TARGET -l -- TEXT` | Send literal text input |
| `send-keys -t TARGET Enter` | Send special key (Enter, Escape, etc.) |
| `capture-pane -p -J -t TARGET -S -N` | Capture last N lines of output |
| `has-session -t NAME` | Check if session exists (exit code) |
| `kill-session -t NAME` | Terminate a session |
| `set-option -t NAME history-limit N` | Set scrollback buffer size |
| `pipe-pane -t TARGET "cat >> PATH"` | Stream output to a log file |
| `attach [-t TARGET]` | Attach the terminal (repaint + live stream + resize) |
| `new-session ... -x COLS -y ROWS` | Set initial terminal size |
| `-V` | Print version |

## camc-win

`camc-win` is the Windows-native [camc](https://github.com/orlunix/cam):
it manages coding-agent sessions on a Windows node through the wintmux
daemon, speaking the camc CLI subset that CAM Desktop's hub drives over
SSH — `version`, `list`, `status`, `run`, `capture`, `send`, `key`,
`stop`, `kill`, `rm`, `attach`, `msg send`, `cron list` (stub).

- Wire-compatible output: `run` prints the `ID: <hex>` line the hub
  parses; `--json list` / `--json status` emit the same record shape as
  POSIX camc's `agents.json`; `msg send --no-wait` prints
  `MSG_ID=` / `STATUS=sent`.
- State lives in `%USERPROFILE%\.cam\` (`agents.json` + `sockets\`),
  `$CAM_HOME` overrides.
- `capture --format ansi` is rejected with camc's argparse-style message
  so the hub falls back to plain capture automatically.
- Differences from POSIX camc (v0): `stop` cannot kill only the tool
  process inside a ConPTY (use `kill`); no API proxy, cron scheduler,
  or monitor-based AI state.

```powershell
.\camc-win.exe run -t claude -p C:\work\proj -n fix-bug "fix the failing test"
.\camc-win.exe --json list
.\camc-win.exe capture <id> --lines 80
.\camc-win.exe send <id> -t "yes"
.\camc-win.exe attach <id>
```

## Building

### Prerequisites

- [Go 1.22+](https://go.dev/dl/)
- Windows 10 version 1809+ (for ConPTY support)

### Cross-compile from WSL2/Linux

```bash
cd wintmux

# Run unit tests (platform-independent)
make test

# Build Windows executable
make build-windows
# → produces wintmux.exe
```

### Build on Windows

```powershell
cd wintmux
go build -o wintmux.exe ./cmd/wintmux/
```

## Quick Start

```powershell
# Create a session running a PowerShell script
.\wintmux.exe -S C:\tmp\my-session.sock new-session -d -s agent1 -c C:\work "powershell -Command 'while($true){Get-Date; Start-Sleep 2}'"

# Check if it's running
.\wintmux.exe -S C:\tmp\my-session.sock has-session -t agent1

# Capture output
.\wintmux.exe -S C:\tmp\my-session.sock capture-pane -p -J -t agent1:0.0 -S -20

# Send input
.\wintmux.exe -S C:\tmp\my-session.sock send-keys -t agent1:0.0 -l -- "hello"
.\wintmux.exe -S C:\tmp\my-session.sock send-keys -t agent1:0.0 Enter

# Kill the session
.\wintmux.exe -S C:\tmp\my-session.sock kill-session -t agent1
```

## Integration Tests (Windows)

```powershell
cd wintmux

# Full CAM workflow test
.\scripts\test-cam-workflow.ps1
```

## Architecture

```
┌──────────────┐     TCP 127.0.0.1     ┌──────────────────┐
│  wintmux CLI │ ◄──────────────────► │  Session Daemon   │
│              │   JSON over TCP       │  ┌──────────────┐ │
│  new-session │                      │  │   ConPTY      │ │
│  send-keys   │                      │  │  ┌────────┐  │ │
│  capture-pane│                      │  │  │ child  │  │ │
│  has-session │                      │  │  │ process│  │ │
│  kill-session│                      │  │  └────────┘  │ │
│              │                      │  │  Scrollback   │ │
└──────────────┘                      │  └──────────────┘ │
                                      └──────────────────┘
```

Each `new-session` spawns a daemon process that:
1. Creates a ConPTY and starts the child process inside it.
2. Listens on `127.0.0.1:<random-port>` for IPC commands.
3. Writes a control file (JSON with port + PID) to the `-S` socket path.
4. Maintains a scrollback buffer fed by ConPTY output.
5. Exits when the child process terminates (after a 5-second grace period).

See [DESIGN.md](DESIGN.md) for the full technical specification.

## Project Structure

```
wintmux/
├── cmd/wintmux/
│   └── main.go              # wintmux CLI entry point + command dispatch
├── cmd/camc-win/
│   └── main.go              # camc-win CLI entry point (camc-compatible)
├── internal/
│   ├── cli/parser.go        # tmux-compatible argument parser
│   ├── scrollback/buffer.go # Thread-safe ring buffer
│   ├── ipc/                 # Length-prefixed JSON protocol + stream frames
│   ├── pty/                 # Terminal interface (ConPTY / script-PTY dev)
│   ├── screen/              # Virtual terminal (ANSI parse, history, resize)
│   ├── attach/              # Attach client (raw mode + frame bridge)
│   ├── spawn/               # Detached daemon spawn (per-platform)
│   ├── camcwin/             # camc-win registry + verb implementations
│   └── daemon/daemon.go     # Session daemon (IPC server, attach streams)
├── scripts/                 # PowerShell integration tests
├── DESIGN.md                # Technical design document
└── Makefile                 # Build automation
```

## License

MIT
