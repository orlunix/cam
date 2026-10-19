//go:build !windows

package camcwin

// End-to-end tests against the daemon's POSIX dev backend (a real PTY
// via `script`): spawn session daemons and drive them through the same
// IPC paths camc-win verbs use.

import (
	"strings"
	"testing"
	"time"

	"camc-win/internal/daemon"
	"camc-win/internal/ipc"
)

// startDaemon launches a session daemon on a throwaway socket running
// the given shell command, and waits for it to answer a ping.
func startDaemon(t *testing.T, sock, workdir, command string) {
	t.Helper()
	go func() {
		_ = daemon.Run(sock, "test-session", workdir, command, 80, 24)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionPing})
		if err == nil && resp.OK {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("daemon did not come up")
}

func TestSessionLifecycle(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("CAM_HOME", dir)
	sock := SocketPath("cam-test01")

	// A shell that echoes input lines back with a marker.
	startDaemon(t, sock, dir, "bash -c 'while read line; do echo GOT:$line; done'")

	// send literal text + Enter
	resp, err := ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionSendKeys, Text: "hello-win", Literal: true})
	if err != nil || !resp.OK {
		t.Fatalf("send keys: err=%v resp=%+v", err, resp)
	}
	resp, err = ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionSendKey, Key: "Enter"})
	if err != nil || !resp.OK {
		t.Fatalf("send enter: err=%v resp=%+v", err, resp)
	}

	// capture until the echo appears
	deadline := time.Now().Add(5 * time.Second)
	var out string
	for time.Now().Before(deadline) {
		r, err := ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionCapture, Lines: -1})
		if err == nil && r.OK && strings.Contains(r.Output, "GOT:hello-win") {
			out = r.Output
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if out == "" {
		t.Fatal("capture never showed the echoed input")
	}

	// attach stream: send input + read output over frames
	conn, err := ipc.Connect(sock)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := ipc.WriteMessage(conn, &ipc.Request{Action: ipc.ActionAttach, Cols: 80, Rows: 24}); err != nil {
		t.Fatal(err)
	}
	var hs ipc.Response
	if err := ipc.ReadMessage(conn, &hs); err != nil || !hs.OK {
		t.Fatalf("attach handshake: err=%v resp=%+v", err, hs)
	}
	// First frame should be the repaint.
	typ, payload, err := ipc.ReadFrame(conn)
	if err != nil || typ != ipc.FrameOutput || !strings.Contains(string(payload), "\x1b[2J") {
		t.Fatalf("repaint frame: typ=%d err=%v payload=%q", typ, err, payload)
	}
	// Send input through the stream; expect the echo to stream back.
	if err := ipc.WriteFrame(conn, ipc.FrameInput, []byte("stream-test\r")); err != nil {
		t.Fatal(err)
	}
	deadline = time.Now().Add(5 * time.Second)
	found := false
	for time.Now().Before(deadline) && !found {
		conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		typ, payload, err := ipc.ReadFrame(conn)
		if err != nil {
			break
		}
		if typ == ipc.FrameOutput && strings.Contains(string(payload), "GOT:stream-test") {
			found = true
		}
	}
	if !found {
		t.Fatal("attach stream never delivered the echoed input")
	}

	// kill
	resp, err = ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionKillSession})
	if err != nil || !resp.OK {
		t.Fatalf("kill: err=%v resp=%+v", err, resp)
	}
	// has-session should flip to false (daemon lingers 5s after exit).
	deadline = time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		r, err := ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionHasSession})
		if err != nil || !r.Exists {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("session still exists after kill")
}

func TestCaptureLineSemantics(t *testing.T) {
	dir := t.TempDir()
	sock := SocketPath("cam-test02")

	// Print 100 numbered lines then idle.
	startDaemon(t, sock, dir, "bash -c 'for i in $(seq 1 100); do echo L$i; done; sleep 30'")

	deadline := time.Now().Add(8 * time.Second)
	for {
		r, err := ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionCapture, Lines: -1})
		if err == nil && r.OK && strings.Contains(r.Output, "L100") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("session never printed L100")
		}
		time.Sleep(100 * time.Millisecond)
	}

	// Full scrollback: all 100 lines survived the 24-row screen.
	r, _ := ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionCapture, Lines: -1})
	if !strings.Contains(r.Output, "\nL1\n") && !strings.HasPrefix(r.Output, "L1\n") {
		t.Fatalf("full scrollback lost L1 (got %d bytes)", len(r.Output))
	}
	if !strings.Contains(r.Output, "L100") {
		t.Fatalf("full scrollback lost L100 (got %d bytes)", len(r.Output))
	}

	// Visible screen only: has the latest line, not the first.
	r, _ = ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionCapture, Lines: 0})
	if strings.Contains(r.Output, "L1\n") || !strings.Contains(r.Output, "L100") {
		t.Fatalf("visible capture wrong: %q", r.Output)
	}

	// -S -10 semantics: 10 history lines + the full visible screen.
	r, _ = ipc.SendRequest(sock, &ipc.Request{Action: ipc.ActionCapture, Lines: 10})
	lines := strings.Split(r.Output, "\n")
	// 24-row screen: 10 history + 24 visible, trailing blanks trimmed.
	if len(lines) < 25 || !strings.Contains(r.Output, "L100") || strings.Contains(r.Output, "L60\n") {
		t.Fatalf("-S -10 capture wrong (%d lines): %.120q", len(lines), r.Output)
	}
}

// TestRunVerbE2E drives the real CmdRun path — registry record, daemon
// spawn, prompt delivery, capture, kill, rm — with the spawn seam
// pointed at an in-process daemon (os.Executable is the test binary
// under `go test`, so the production self re-exec cannot run here).
func TestRunVerbE2E(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("CAM_HOME", dir)
	t.Setenv("CAMC_WIN_STARTUP_WAIT", "0.2")
	work := t.TempDir()

	orig := spawnDaemon
	spawnDaemon = func(socketPath, sessionName, workdir, command string, cols, rows int) error {
		go func() { _ = daemon.Run(socketPath, sessionName, workdir, command, cols, rows) }()
		return nil
	}
	defer func() { spawnDaemon = orig }()

	// An unrecognized tool falls back to the default shell; the prompt
	// is then typed into it (camc's unknown-tool behavior).
	rc := CmdRun(RunOptions{
		Tool:   "definitely-not-a-real-tool",
		Path:   work,
		Name:   "e2e",
		Prompt: "echo CAMCWIN_MARKER_$((40+2))",
	})
	if rc != 0 {
		t.Fatalf("CmdRun exit = %d", rc)
	}

	store := NewStore()
	agents := store.List()
	if len(agents) != 1 {
		t.Fatalf("registry has %d agents", len(agents))
	}
	a := agents[0]
	if a.Task.Tool != "others" || a.Task.RequestedTool == "" {
		t.Fatalf("unknown-tool record: %+v", a.Task)
	}
	if a.Status != "running" || a.TmuxSession != "cam-"+a.ID {
		t.Fatalf("record: %+v", a)
	}

	// The prompt should have been typed into the shell and executed.
	deadline := time.Now().Add(6 * time.Second)
	for {
		r, err := ipc.SendRequest(a.TmuxSocket, &ipc.Request{Action: ipc.ActionCapture, Lines: -1})
		if err == nil && r.OK && strings.Contains(r.Output, "CAMCWIN_MARKER_42") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("prompt never executed; capture=%q", r.Output)
		}
		time.Sleep(100 * time.Millisecond)
	}

	// kill + rm
	if rc := CmdKill(a.ID); rc != 0 {
		t.Fatalf("kill exit = %d", rc)
	}
	if got := store.Resolve(a.ID); got.Status != "killed" {
		t.Fatalf("status after kill: %s", got.Status)
	}
	if rc := CmdRm(a.ID, true); rc != 0 {
		t.Fatalf("rm exit = %d", rc)
	}
	if len(store.List()) != 0 {
		t.Fatal("registry not empty after rm")
	}
}
