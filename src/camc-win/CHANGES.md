# Dev Notes

## Fixes (2026-02-26)

### 1. Daemon exits immediately when launched from bash / SSH session
**File:** `cmd/wintmux/spawn_windows.go`

**Problem:** The daemon process was killed immediately when wintmux was launched
from a bash shell or SSH session. OpenSSH wraps child processes in a Windows
Job Object and kills them when the session ends. Without `CREATE_BREAKAWAY_FROM_JOB`
the daemon was torn down with the parent.

**Fix:** Add `CREATE_BREAKAWAY_FROM_JOB (0x01000000)` to the `CreateProcess` flags
so the daemon escapes the SSH Job Object and stays alive independently.

---

### 2. Child process exits immediately inside daemon (exit code 0 or 5)
**File:** `internal/daemon/daemon.go`

**Problem:** `daemon.Run()` called `freeConsole()` (Win32 `FreeConsole`) before
creating the ConPTY. This detached the process from its console session, breaking
ConPTY internals and causing the spawned child (PowerShell, cmd) to exit immediately.

**Fix:** Remove the `freeConsole()` call. The daemon already runs with
`CREATE_NO_WINDOW` so there is no console to detach from.

---

### 3. `capture-pane` returns garbled output for full-screen TUI apps
**File:** `internal/daemon/daemon.go`, new `internal/screen/screen.go`

**Problem:** `capture-pane` was reading raw scrollback bytes and stripping VT
sequences with a regex. Full-screen apps (like the claude TUI) use cursor
movement and in-place rewrite sequences, so the scrollback contains overlapping
writes and the stripped output is garbled.

**Fix:** Add a virtual screen (`internal/screen`) that maintains a 2-D cell grid
by replaying VT cursor-movement sequences in real time. `capture-pane` now reads
the screen grid instead of the scrollback, returning the current visible state.

---

### 4. `tmux -u` flag causes parse error
**File:** `internal/cli/parser.go`

**Problem:** Some callers (e.g. CAM) pass `tmux -u` to enable UTF-8 mode.
wintmux rejected the unknown flag with a parse error.

**Fix:** Silently accept and ignore `-u`; wintmux is always UTF-8.

---

## Test coverage gaps
`daemon`, `pty`, and `cmd/wintmux` have no test files yet.
These should be added before a proper release.
