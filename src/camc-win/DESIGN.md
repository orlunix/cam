# WinTmux — Design Document

## Overview

WinTmux is a Windows-native tmux-compatible session manager designed to enable
[CAM (Coding Agent Manager)](https://github.com/orlunix/cam) to run on native
Windows PowerShell without requiring WSL or Cygwin.

CAM uses tmux as its process isolation and I/O control layer. WinTmux replicates
the exact subset of tmux commands that CAM relies on, backed by the Windows
**ConPTY** (Console Pseudo Terminal) API.

## Problem Statement

- tmux requires Unix PTY and cannot run on native Windows.
- CAM's entire architecture (agent lifecycle, output monitoring, input injection)
  is built on tmux.
- Windows 10 1809+ provides ConPTY, which offers equivalent PTY functionality.
- A tmux-compatible CLI wrapper around ConPTY would let CAM run unmodified on
  Windows.

## Architecture

```
┌──────────────────┐        TCP 127.0.0.1        ┌────────────────────────┐
│   wintmux CLI    │  ◄───────────────────────►   │   Session Daemon       │
│                  │    length-prefixed JSON       │                        │
│  new-session     │                              │  ┌──────────────────┐  │
│  send-keys       │                              │  │     ConPTY       │  │
│  capture-pane    │                              │  │  ┌────────────┐  │  │
│  has-session     │                              │  │  │  child     │  │  │
│  kill-session    │                              │  │  │  process   │  │  │
│  set-option      │                              │  │  └────────────┘  │  │
│  pipe-pane       │                              │  │                  │  │
│  attach          │                              │  │  Scrollback Buf  │  │
└──────────────────┘                              │  └──────────────────┘  │
                                                  └────────────────────────┘
```

### Per-Session Daemon Model

Each session runs as an independent daemon process, matching CAM's per-socket
(`-S`) tmux architecture:

1. `wintmux -S <path> new-session ...` spawns a daemon process.
2. The daemon creates a ConPTY, starts the child process, and listens on a
   TCP port on `127.0.0.1`.
3. The daemon writes a **control file** to `<path>` containing `{"port": N, "pid": M}`.
4. Subsequent commands (send-keys, capture-pane, etc.) read the control file,
   connect to the daemon via TCP, and exchange length-prefixed JSON messages.
5. When the child process exits, the daemon keeps listening for 5 seconds
   (grace period for final capture-pane), then shuts down and removes the
   control file.

### Why TCP Instead of Named Pipes?

- TCP works cross-platform, allowing tests on WSL2/Linux.
- Simpler implementation (Go `net` package vs. Win32 named pipe API).
- Localhost-only binding provides equivalent security to Unix domain sockets.
- Named pipes can be added later as an optimization if needed.

## Supported Commands

All commands follow tmux CLI syntax. The `-S <path>` global flag identifies the
session (maps to the control file path).

### 1. `new-session`

```
wintmux -S <socket> new-session [-d] [-s <name>] [-c <workdir>] [shell-command]
```

- Creates a ConPTY with default size 120×40.
- Starts the shell command as the initial process.
- When the process exits, the session terminates (remain-on-exit OFF).
- `-d` (detached) is always implied; included for tmux compatibility.

### 2. `send-keys`

```
wintmux -S <socket> send-keys [-t <target>] [-l] [--] <keys...>
```

- **Literal mode** (`-l`): Sends text bytes directly to ConPTY stdin.
  Keys are joined with spaces before sending.
- **Key mode** (no `-l`): Interprets key names (Enter, Escape, BSpace, C-c, etc.)
  and sends the corresponding byte sequences.
- `--` ends option parsing (prevents text starting with `-` from being parsed as flags).
- Target (`-t`) is accepted for tmux compatibility but ignored (single-pane model).

### 3. `capture-pane`

```
wintmux -S <socket> capture-pane [-p] [-J] [-a] [-t <target>] [-S <-lines>]
```

- `-p`: Print captured output to stdout.
- `-J`: Join wrapped lines (accepted for compatibility; output is always line-based).
- `-a`: Capture alternate screen buffer (currently returns same as primary).
- `-S -N`: Capture last N lines from scrollback buffer.
- Default: last 50 lines.

### 4. `has-session`

```
wintmux -S <socket> has-session [-t <target>]
```

- Exit code 0: session exists and child process is running.
- Exit code 1: session does not exist (daemon not running, process exited, or
  control file missing).

### 5. `kill-session`

```
wintmux -S <socket> kill-session [-t <target>]
```

- Terminates the child process and shuts down the daemon.
- Cleans up the control file.

### 6. `set-option`

```
wintmux -S <socket> set-option [-t <target>] <option> <value>
```

Supported options:
- `history-limit <N>`: Set scrollback buffer capacity (default: 2000 lines).

### 7. `pipe-pane`

```
wintmux -S <socket> pipe-pane [-t <target>] "cat >> <path>"
```

- Streams all ConPTY output to the specified file (append mode).
- Only `cat >> <path>` syntax is supported (matching CAM's usage).
- Call with no command to disable.

### 8. `attach`

```
wintmux -S <socket> attach [-t <target>]
```

- Connects current terminal's stdin/stdout to the ConPTY session.
- *Not yet implemented in v0.1.*

### 9. `-V`

```
wintmux -V
```

- Prints version string: `wintmux <version>`.

## IPC Protocol

All client-daemon communication uses **length-prefixed JSON over TCP**.

### Wire Format

```
[4 bytes: message length (big-endian uint32)] [N bytes: JSON payload]
```

Maximum message size: 10 MB.

### Request Schema

```json
{
  "action": "send_keys | send_key | capture_pane | has_session | kill_session | set_option | pipe_pane | ping",
  "text": "literal text to send",
  "key": "Enter | Escape | BSpace | ...",
  "literal": true,
  "send_enter": true,
  "lines": 50,
  "alternate": false,
  "join": true,
  "option": "history-limit",
  "value": "50000",
  "shell_cmd": "cat >> /path/to/log"
}
```

### Response Schema

```json
{
  "ok": true,
  "error": "error message if ok=false",
  "output": "captured pane content",
  "exists": true
}
```

## Scrollback Buffer

- **Implementation**: Thread-safe ring buffer with configurable capacity.
- **Default capacity**: 2000 lines (matches tmux default).
- **CAM typically sets**: 50000 lines via `set-option history-limit`.
- **Write path**: Raw bytes from ConPTY → split by `\n` → store lines.
- **Read path**: Return last N committed lines + current partial line.
- **Carriage returns** (`\r`) are stripped during write.

## ConPTY Integration (Windows)

Key Windows APIs used:

| API | Purpose |
|-----|---------|
| `CreatePseudoConsole` | Create virtual terminal |
| `ResizePseudoConsole` | Change terminal dimensions |
| `ClosePseudoConsole` | Destroy virtual terminal |
| `CreatePipe` | Create I/O pipes for ConPTY |
| `InitializeProcThreadAttributeList` | Set up process attributes |
| `UpdateProcThreadAttribute` | Attach ConPTY to process |
| `CreateProcess` | Start child process in ConPTY |
| `WaitForSingleObject` | Monitor child process exit |

### Process Lifecycle

1. Create two pipe pairs (input, output).
2. `CreatePseudoConsole(size, inputReadEnd, outputWriteEnd)`.
3. Close pipe ends now owned by ConPTY.
4. `CreateProcess` with `EXTENDED_STARTUPINFO_PRESENT` and ConPTY attribute.
5. Read loop: ConPTY output pipe → scrollback buffer (+ optional pipe-pane file).
6. Write path: IPC send-keys → ConPTY input pipe.
7. On child exit: `WaitForSingleObject` returns → close daemon after grace period.

### Non-Windows Fallback

On Linux/macOS, `exec.Cmd` with stdin/stdout pipes replaces ConPTY. This enables
development and unit testing on non-Windows platforms (e.g., WSL2).

## Key Mapping

| tmux Key Name | Byte Sequence |
|---------------|---------------|
| Enter | `\r` |
| Escape | `\x1b` |
| BSpace | `\x7f` |
| Tab | `\t` |
| Space | ` ` |
| C-c | `\x03` |
| C-d | `\x04` |
| C-z | `\x1a` |
| Up | `\x1b[A` |
| Down | `\x1b[B` |
| Right | `\x1b[C` |
| Left | `\x1b[D` |
| Home | `\x1b[H` |
| End | `\x1b[F` |
| DC (Delete) | `\x1b[3~` |
| PageUp | `\x1b[5~` |
| PageDown | `\x1b[6~` |

## Security

- The TCP listener binds to `127.0.0.1` only (no remote access).
- Commands are always `[]string` lists — never shell-interpreted strings.
- Control files are created with user-only permissions (0644).
- No authentication on the TCP channel (same trust model as tmux Unix sockets).

## Build & Test

```bash
# Build for current platform (Linux — for unit tests)
make build

# Cross-compile for Windows
make build-windows

# Run unit tests (scrollback, protocol, CLI parser)
make test

# On Windows PowerShell — run integration tests
.\scripts\test-cam-workflow.ps1
```

## Future Enhancements

- `attach` command (bidirectional stdin/stdout proxying).
- Named pipe transport (replace TCP for lower latency on Windows).
- Full VT100 terminal emulator for accurate `capture-pane` rendering.
- `list-sessions` command (scan control files in a directory).
- `resize-pane` command (calls `ResizePseudoConsole`).
- Authentication token for the TCP channel.
