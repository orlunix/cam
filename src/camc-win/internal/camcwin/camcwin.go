// camc-win verb implementations. The CLI surface mirrors POSIX camc
// (src/camc_pkg/cli.py) for the subset CAM Desktop's hub drives over
// SSH: version, list, status, run, capture, send, key, stop, kill, rm,
// attach, msg send, and a cron stub. Output formats (ID: line, MSG_ID=
// block, --json record shape) are wire-compatible with camc so the hub
// needs no parsing changes.
package camcwin

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"camc-win/internal/attach"
	"camc-win/internal/ipc"
	"camc-win/internal/spawn"
)

// Version is the camc-win release version.
const Version = "0.1.0"

// spawnDaemon creates the session daemon. A package var so tests can
// substitute an in-process daemon (os.Executable is the test binary
// under `go test`, so the real self re-exec cannot work there).
var spawnDaemon = spawn.Daemon

// toolHints mirrors camc's _TOOL_HINTS for known coding-agent CLIs.
var toolHints = map[string]string{
	"claude": "npm install -g @anthropic-ai/claude-code",
	"codex":  "npm install -g @openai/codex",
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// sessionAlive probes the session daemon: control file present, IPC
// reachable, session still exists.
func sessionAlive(socketPath string) bool {
	if _, err := os.Stat(socketPath); err != nil {
		return false
	}
	resp, err := ipc.SendRequest(socketPath, &ipc.Request{Action: ipc.ActionHasSession})
	if err != nil {
		return false
	}
	return resp.OK && resp.Exists
}

// resolveSession maps a user/agent reference to a session name. Unknown
// refs fall through as bare session names (camc parity: capture/send
// accept raw tmux session names).
func resolveSession(store *Store, ref string) (*Agent, string) {
	if a := store.Resolve(ref); a != nil {
		return a, a.TmuxSession
	}
	return nil, ref
}

// refreshLiveness flips running records to completed when their daemon
// is gone, persisting the change (the monitor's job in POSIX camc).
func refreshLiveness(store *Store, a *Agent) {
	if a == nil || !strings.EqualFold(a.Status, "running") {
		return
	}
	if sessionAlive(a.TmuxSocket) {
		return
	}
	now := NowISO()
	reason := "Session ended"
	store.UpdateFields(a.ID, func(rec *Agent) {
		rec.Status = "completed"
		rec.State = ""
		rec.CompletedAt = &now
		rec.ExitReason = &reason
	})
	a.Status = "completed"
	a.State = ""
	a.CompletedAt = &now
	a.ExitReason = &reason
}

// computeUpdatedAt mirrors camc's dead-session fallback: completed_at,
// else started_at. (Live-session activity epochs come from tmux on
// POSIX; camc-win v0 has no activity feed.)
func computeUpdatedAt(a Agent) string {
	if a.CompletedAt != nil && *a.CompletedAt != "" {
		return *a.CompletedAt
	}
	return a.StartedAt
}

// agentJSON flattens one record to the camc --json output shape.
func agentJSON(a Agent) map[string]any {
	b, _ := json.Marshal(a)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	m["updated_at"] = computeUpdatedAt(a)
	return m
}

func printJSON(v any) {
	data, _ := json.MarshalIndent(v, "", "  ")
	fmt.Println(string(data))
}

// daemonPID reads the daemon pid from the control file (for the record).
func daemonPID(socketPath string) *int {
	info, err := ipc.ReadControlFile(socketPath)
	if err != nil {
		return nil
	}
	return &info.PID
}

// sendText writes literal text (+ optional Enter) to a session in 64KB
// chunks, pacing ConPTY input.
func sendText(socketPath, text string, sendEnter bool) error {
	const chunk = 64 * 1024
	for len(text) > 0 {
		n := len(text)
		if n > chunk {
			n = chunk
		}
		resp, err := ipc.SendRequest(socketPath, &ipc.Request{
			Action:  ipc.ActionSendKeys,
			Text:    text[:n],
			Literal: true,
		})
		if err != nil {
			return err
		}
		if !resp.OK {
			return fmt.Errorf("%s", resp.Error)
		}
		text = text[n:]
		if len(text) > 0 {
			time.Sleep(10 * time.Millisecond)
		}
	}
	if sendEnter {
		resp, err := ipc.SendRequest(socketPath, &ipc.Request{Action: ipc.ActionSendKey, Key: "Enter"})
		if err != nil {
			return err
		}
		if !resp.OK {
			return fmt.Errorf("%s", resp.Error)
		}
	}
	return nil
}

func capture(socketPath string, lines int) (string, error) {
	resp, err := ipc.SendRequest(socketPath, &ipc.Request{Action: ipc.ActionCapture, Lines: lines})
	if err != nil {
		return "", err
	}
	if !resp.OK {
		return "", fmt.Errorf("%s", resp.Error)
	}
	return resp.Output, nil
}

// ---------------------------------------------------------------------------
// verbs — each returns a process exit code
// ---------------------------------------------------------------------------

// CmdVersion prints the version line the hub parses (`v?(\d+)\.(\d+)\.(\d+)`).
func CmdVersion() int {
	fmt.Printf("camc-win v%s (wintmux/ConPTY)\n", Version)
	return 0
}

// CmdList implements `camc-win list [--json]`.
func CmdList(jsonOut bool) int {
	store := NewStore()
	agents := store.List()
	for i := range agents {
		refreshLiveness(store, &agents[i])
	}
	if jsonOut {
		out := make([]map[string]any, 0, len(agents))
		for _, a := range agents {
			out = append(out, agentJSON(a))
		}
		printJSON(out)
		return 0
	}
	fmt.Printf("%-10s %-14s %-7s %-11s %-19s %s\n", "ID", "NAME", "TOOL", "STATUS", "UPDATED", "PATH")
	for _, a := range agents {
		fmt.Printf("%-10s %-14s %-7s %-11s %-19s %s\n",
			a.ID, trunc(a.Task.Name, 13), a.Task.Tool, a.Status, computeUpdatedAt(a), a.ContextPath)
	}
	fmt.Printf("\n%d agent(s)\n", len(agents))
	return 0
}

// CmdStatus implements `camc-win status <id> [--json]`.
func CmdStatus(ref string, jsonOut bool) int {
	store := NewStore()
	a := store.Resolve(ref)
	if a == nil {
		fmt.Fprintf(os.Stderr, "Error: agent '%s' not found\n", ref)
		return 1
	}
	refreshLiveness(store, a)
	if jsonOut {
		printJSON(agentJSON(*a))
		return 0
	}
	fmt.Printf("ID:       %s\nName:     %s\nTool:     %s\nStatus:   %s\nPath:     %s\nSession:  %s\nStarted:  %s\n",
		a.ID, a.Task.Name, a.Task.Tool, a.Status, a.ContextPath, a.TmuxSession, a.StartedAt)
	if a.CompletedAt != nil {
		fmt.Printf("Finished: %s\n", *a.CompletedAt)
	}
	return 0
}

// RunOptions carries `run` flags.
type RunOptions struct {
	Tool     string
	Path     string
	Name     string
	Prompt   string
	AutoExit bool
	Cols     int
	Rows     int
}

// CmdRun implements `camc-win run -t <tool> -p <path> [-n name] [prompt]`.
// It prints camc's `  ID: <id>  Tool: ...  Session: ...` line, which the
// hub regex-parses from stdout.
func CmdRun(opts RunOptions) int {
	tool := opts.Tool
	if tool == "" {
		tool = "claude"
	}
	workdir := opts.Path
	if workdir == "" {
		wd, err := os.Getwd()
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: cannot determine working directory: %v\n", err)
			return 1
		}
		workdir = wd
	}
	if st, err := os.Stat(workdir); err != nil || !st.IsDir() {
		fmt.Fprintf(os.Stderr, "Error: path '%s' is not a directory\n", workdir)
		return 1
	}

	command, known := toolLaunchCommand(tool)
	if known && command == "" {
		fmt.Fprintf(os.Stderr, "Error: '%s' is not installed or not on PATH. Install: %s\n", tool, toolHints[tool])
		return 1
	}

	id := NewID()
	session := "cam-" + id
	socket := SocketPath(session)
	if err := os.MkdirAll(SocketsDir(), 0755); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		return 1
	}

	name := opts.Name
	if name == "" {
		name = filepath.Base(workdir)
	}
	hostname, _ := os.Hostname()

	fmt.Printf("Starting %s agent %s...\n", tool, id)
	cols, rows := opts.Cols, opts.Rows
	if cols <= 0 {
		cols = 120
	}
	if rows <= 0 {
		rows = 40
	}
	if err := spawnDaemon(socket, session, workdir, command, cols, rows); err != nil {
		fmt.Fprintf(os.Stderr, "Error: failed to create session: %v\n", err)
		return 1
	}

	// Wait for the daemon to come up (mirrors wintmux executeNewSession).
	ready := false
	for i := 0; i < 50; i++ {
		time.Sleep(100 * time.Millisecond)
		if resp, err := ipc.SendRequest(socket, &ipc.Request{Action: ipc.ActionPing}); err == nil && resp.OK {
			ready = true
			break
		}
	}
	if !ready {
		fmt.Fprintf(os.Stderr, "Error: session created but daemon not responding\n")
		return 1
	}

	rec := Agent{
		ID:            id,
		SessionID:     NewUUID(),
		Task:          Task{Name: name, Tool: tool, Prompt: opts.Prompt, AutoConfirm: known, AutoExit: opts.AutoExit && known, Tags: []string{}},
		ContextID:     "",
		ContextName:   "",
		ContextPath:   workdir,
		TransportType: "local",
		Status:        "running",
		State:         "",
		TmuxSession:   session,
		TmuxSocket:    socket,
		TmuxBin:       "camc-win",
		TmuxVersion:   "wintmux " + Version,
		PID:           daemonPID(socket),
		Hostname:      hostname,
		StartedAt:     NowISO(),
		RetryCount:    0,
		FilesChanged:  []string{},
	}
	if !known {
		rec.Task.RequestedTool = tool
		rec.Task.Tool = "others"
	}
	if err := NewStore().Save(rec); err != nil {
		fmt.Fprintf(os.Stderr, "Error: registry save failed: %v\n", err)
		return 1
	}

	// Prompt delivery: typed into the session after a startup wait — the
	// universal path that works for TUI tools and bare shells alike.
	if opts.Prompt != "" {
		wait := 2.0
		if v := os.Getenv("CAMC_WIN_STARTUP_WAIT"); v != "" {
			var f float64
			if _, err := fmt.Sscanf(v, "%g", &f); err == nil && f >= 0 {
				wait = f
			}
		}
		time.Sleep(time.Duration(wait * float64(time.Second)))
		if err := sendText(socket, opts.Prompt, true); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: prompt delivery failed: %v\n", err)
		}
	}

	fmt.Printf("  ID: %s  Tool: %s  Session: %s\n", id, tool, session)
	fmt.Printf("  Path: %s\n", workdir)
	return 0
}

// CmdCapture implements `camc-win capture <id> [--lines N] [--format plain]
// [--json]`. lines<=0 returns the full scrollback (camc default).
func CmdCapture(ref string, lines int, jsonOut bool) int {
	store := NewStore()
	_, session := resolveSession(store, ref)
	if session == "" {
		fmt.Fprintln(os.Stderr, "Agent has no tmux session")
		return 1
	}
	if lines <= 0 {
		lines = -1 // daemon: full scrollback
	}
	output, err := capture(SocketPath(session), lines)
	if err != nil {
		fmt.Fprintf(os.Stderr, "capture: %v\n", err)
		return 1
	}
	if jsonOut {
		h := md5.Sum([]byte(output))
		printJSON(map[string]any{"content": output, "hash": hex.EncodeToString(h[:])[:8], "format": "plain"})
		return 0
	}
	os.Stdout.WriteString(output)
	if !strings.HasSuffix(output, "\n") {
		fmt.Println()
	}
	return 0
}

// CmdCaptureAnsiRefused rejects --format ansi the way an old camc's
// argparse does, so the hub's fallback regex (`unrecognized arguments:
// --format`) retries in plain mode. camc-win v0 has no ANSI renderer.
func CmdCaptureAnsiRefused() int {
	fmt.Fprintln(os.Stderr, "camc-win: error: unrecognized arguments: --format ansi")
	return 2
}

// CmdSend implements `camc-win send <id> (-t text|--file f|--stdin) [--no-enter]`.
func CmdSend(ref, text string, noEnter bool) int {
	store := NewStore()
	_, session := resolveSession(store, ref)
	if session == "" {
		fmt.Fprintln(os.Stderr, "Agent has no tmux session")
		return 1
	}
	if err := sendText(SocketPath(session), text, !noEnter); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to send input: %v\n", err)
		return 1
	}
	fmt.Println("Sent.")
	return 0
}

// CmdKey implements `camc-win key <id> --key <K>` (tmux key names).
func CmdKey(ref, key string) int {
	store := NewStore()
	_, session := resolveSession(store, ref)
	if session == "" {
		fmt.Fprintln(os.Stderr, "Agent has no tmux session")
		return 1
	}
	resp, err := ipc.SendRequest(SocketPath(session), &ipc.Request{Action: ipc.ActionSendKey, Key: key})
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to send key: %v\n", err)
		return 1
	}
	if !resp.OK {
		fmt.Fprintf(os.Stderr, "Failed to send key: %s\n", resp.Error)
		return 1
	}
	fmt.Printf("Sent key: %s\n", key)
	return 0
}

// CmdStop implements `camc-win stop <id>`: mark stopped, session left
// alive. NOTE: unlike POSIX camc, camc-win v0 cannot kill only the tool
// process inside the ConPTY — use `kill` to tear down the session.
func CmdStop(ref string) int {
	store := NewStore()
	a := store.Resolve(ref)
	if a == nil {
		fmt.Fprintf(os.Stderr, "Error: agent '%s' not found\n", ref)
		return 1
	}
	now := NowISO()
	reason := "Stopped by user"
	store.UpdateFields(a.ID, func(rec *Agent) {
		rec.Status = "stopped"
		rec.State = ""
		rec.CompletedAt = &now
		rec.ExitReason = &reason
	})
	fmt.Printf("Stopped agent %s\n", a.ID)
	return 0
}

// CmdKill implements `camc-win kill <id>`: tear down the session.
func CmdKill(ref string) int {
	store := NewStore()
	a := store.Resolve(ref)
	if a == nil {
		fmt.Fprintf(os.Stderr, "Error: agent '%s' not found\n", ref)
		return 1
	}
	if a.TmuxSession != "" {
		_, _ = ipc.SendRequest(SocketPath(a.TmuxSession), &ipc.Request{Action: ipc.ActionKillSession})
	}
	now := NowISO()
	reason := "Force killed by user"
	store.UpdateFields(a.ID, func(rec *Agent) {
		rec.Status = "killed"
		rec.State = ""
		rec.CompletedAt = &now
		rec.ExitReason = &reason
	})
	fmt.Printf("Killed agent %s\n", a.ID)
	return 0
}

// CmdRm implements `camc-win rm <id> [--kill] [--archive]`: kills the
// session when alive, then removes the record. --kill is a deprecated
// no-op (camc parity); --archive stashes the record under ~/.cam/archives/.
func CmdRm(ref string, archive bool) int {
	store := NewStore()
	a := store.Resolve(ref)
	if a == nil {
		fmt.Fprintf(os.Stderr, "Error: agent '%s' not found\n", ref)
		return 1
	}
	if a.TmuxSession != "" && sessionAlive(a.TmuxSocket) {
		_, _ = ipc.SendRequest(a.TmuxSocket, &ipc.Request{Action: ipc.ActionKillSession})
	}
	if archive {
		if err := os.MkdirAll(ArchivesDir(), 0755); err == nil {
			data, _ := json.MarshalIndent(a, "", "  ")
			_ = os.WriteFile(filepath.Join(ArchivesDir(), a.ID+".json"), data, 0644)
		}
	}
	if err := store.Remove(a.ID); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		return 1
	}
	fmt.Printf("Removed agent %s\n", a.ID)
	return 0
}

// CmdAttach implements `camc-win attach <id>`: bridge the local terminal
// onto the session until detach/session end.
func CmdAttach(ref string) int {
	store := NewStore()
	_, session := resolveSession(store, ref)
	if session == "" {
		fmt.Fprintln(os.Stderr, "Agent has no tmux session")
		return 1
	}
	return attach.Run(SocketPath(session))
}

// CmdMsgSend implements `camc-win msg send <id> -t text [--no-wait]
// [--timeout N]`. --no-wait prints camc's MSG_ID/STATUS block. Blocking
// mode pane-scrapes until output stabilises and prints the new tail.
func CmdMsgSend(ref, text string, noWait bool, timeoutSec int) int {
	store := NewStore()
	_, session := resolveSession(store, ref)
	a := store.Resolve(ref)
	if session == "" || (a == nil) {
		fmt.Fprintf(os.Stderr, "camc msg: target '%s' has no tmux session\n", ref)
		return 1
	}
	socket := SocketPath(session)
	baseline, _ := capture(socket, -1)
	msgID := NewID()
	if err := sendText(socket, text, true); err != nil {
		fmt.Fprintf(os.Stderr, "camc msg: failed to deliver to '%s': %v\n", ref, err)
		return 1
	}
	if noWait {
		fmt.Printf("MSG_ID=%s\nSTATUS=sent\n", msgID)
		return 0
	}
	if timeoutSec < 5 {
		timeoutSec = 5
	}
	deadline := time.Now().Add(time.Duration(timeoutSec) * time.Second)
	prev := ""
	stable := 0
	for time.Now().Before(deadline) {
		time.Sleep(2 * time.Second)
		cur, err := capture(socket, -1)
		if err != nil {
			break
		}
		if cur == prev && cur != baseline {
			stable++
			if stable >= 2 {
				fmt.Print(replyTail(baseline, cur))
				return 0
			}
		} else {
			stable = 0
		}
		prev = cur
	}
	fmt.Fprintf(os.Stderr, "camc msg: timed out waiting for reply from '%s'\n", ref)
	return 1
}

// replyTail extracts the output appended after baseline, best-effort:
// when the capture grew past the baseline's line count, return the new
// lines; otherwise return the last 30 lines.
func replyTail(baseline, cur string) string {
	b := strings.Split(baseline, "\n")
	c := strings.Split(cur, "\n")
	if len(c) > len(b) {
		return strings.Join(c[len(b):], "\n") + "\n"
	}
	start := len(c) - 30
	if start < 0 {
		start = 0
	}
	return strings.Join(c[start:], "\n") + "\n"
}

// CmdCronList implements `camc-win cron list [--json]`: no scheduler on
// Windows v0 — always empty so the hub's automations tab just renders 0.
func CmdCronList(jsonOut bool) int {
	if jsonOut {
		fmt.Println("[]")
	} else {
		fmt.Println("No cron jobs.")
	}
	return 0
}

func trunc(s string, n int) string {
	if len(s) <= n {
		return s
	}
	if n <= 1 {
		return s[:n]
	}
	return s[:n-1] + "~"
}
