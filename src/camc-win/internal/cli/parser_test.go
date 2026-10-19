package cli

import (
	"strings"
	"testing"
)

func TestParseNewSession(t *testing.T) {
	args := strings.Fields("-S /tmp/test.sock new-session -d -s mysession -c /work/dir echo hello")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdNewSession {
		t.Errorf("expected CmdNewSession, got %d", cmd.Type)
	}
	if cmd.SocketPath != "/tmp/test.sock" {
		t.Errorf("expected socket /tmp/test.sock, got %s", cmd.SocketPath)
	}
	if !cmd.Detached {
		t.Error("expected detached=true")
	}
	if cmd.SessionName != "mysession" {
		t.Errorf("expected session mysession, got %s", cmd.SessionName)
	}
	if cmd.StartDir != "/work/dir" {
		t.Errorf("expected dir /work/dir, got %s", cmd.StartDir)
	}
	if cmd.ShellCmd != "echo hello" {
		t.Errorf("expected cmd 'echo hello', got %q", cmd.ShellCmd)
	}
}

func TestParseSendKeysLiteral(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock send-keys -t sess:0.0 -l -- hello world")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdSendKeys {
		t.Errorf("expected CmdSendKeys, got %d", cmd.Type)
	}
	if cmd.Target != "sess:0.0" {
		t.Errorf("expected target sess:0.0, got %s", cmd.Target)
	}
	if !cmd.Literal {
		t.Error("expected literal=true")
	}
	if len(cmd.Keys) != 2 || cmd.Keys[0] != "hello" || cmd.Keys[1] != "world" {
		t.Errorf("expected keys [hello world], got %v", cmd.Keys)
	}
}

func TestParseSendKeysEnter(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock send-keys -t sess:0.0 Enter")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdSendKeys {
		t.Errorf("expected CmdSendKeys, got %d", cmd.Type)
	}
	if len(cmd.Keys) != 1 || cmd.Keys[0] != "Enter" {
		t.Errorf("expected keys [Enter], got %v", cmd.Keys)
	}
	if cmd.Literal {
		t.Error("expected literal=false for Enter key")
	}
}

func TestParseCapturePaneBasic(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock capture-pane -p -J -t sess:0.0 -S -50")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdCapturePane {
		t.Errorf("expected CmdCapturePane, got %d", cmd.Type)
	}
	if !cmd.Print {
		t.Error("expected print=true")
	}
	if !cmd.JoinLines {
		t.Error("expected join=true")
	}
	if cmd.Target != "sess:0.0" {
		t.Errorf("expected target sess:0.0, got %s", cmd.Target)
	}
	if cmd.StartLine != -50 {
		t.Errorf("expected startLine -50, got %d", cmd.StartLine)
	}
}

func TestParseCapturePaneAlternate(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock capture-pane -p -J -a -t sess:0.0 -S -50")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if !cmd.Alternate {
		t.Error("expected alternate=true")
	}
}

func TestParseHasSession(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock has-session -t mysession")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdHasSession {
		t.Errorf("expected CmdHasSession, got %d", cmd.Type)
	}
	if cmd.Target != "mysession" {
		t.Errorf("expected target mysession, got %s", cmd.Target)
	}
}

func TestParseKillSession(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock kill-session -t mysession")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdKillSession {
		t.Errorf("expected CmdKillSession, got %d", cmd.Type)
	}
	if cmd.Target != "mysession" {
		t.Errorf("expected target mysession, got %s", cmd.Target)
	}
}

func TestParseSetOption(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock set-option -t mysession history-limit 50000")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdSetOption {
		t.Errorf("expected CmdSetOption, got %d", cmd.Type)
	}
	if cmd.Target != "mysession" {
		t.Errorf("expected target mysession, got %s", cmd.Target)
	}
	if cmd.Option != "history-limit" {
		t.Errorf("expected option history-limit, got %s", cmd.Option)
	}
	if cmd.Value != "50000" {
		t.Errorf("expected value 50000, got %s", cmd.Value)
	}
}

func TestParsePipePane(t *testing.T) {
	args := []string{"-S", "/tmp/s.sock", "pipe-pane", "-t", "sess:0.0", "cat >> /tmp/log"}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdPipePane {
		t.Errorf("expected CmdPipePane, got %d", cmd.Type)
	}
	if cmd.PipeCmd != "cat >> /tmp/log" {
		t.Errorf("expected pipe cmd 'cat >> /tmp/log', got %q", cmd.PipeCmd)
	}
}

func TestParseAttach(t *testing.T) {
	args := strings.Fields("-S /tmp/s.sock attach -t mysession")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdAttach {
		t.Errorf("expected CmdAttach, got %d", cmd.Type)
	}
	if cmd.Target != "mysession" {
		t.Errorf("expected target mysession, got %s", cmd.Target)
	}
}

func TestParseListSessions(t *testing.T) {
	args := strings.Fields("list-sessions")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdListSessions {
		t.Errorf("expected CmdListSessions, got %d", cmd.Type)
	}
}

func TestParseLsAlias(t *testing.T) {
	args := strings.Fields("ls")
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdListSessions {
		t.Errorf("expected CmdListSessions, got %d", cmd.Type)
	}
}

func TestParseNoCommand(t *testing.T) {
	_, err := Parse([]string{})
	if err == nil {
		t.Fatal("expected error for empty args")
	}
}

func TestParseUnknownCommand(t *testing.T) {
	_, err := Parse([]string{"nonexistent"})
	if err == nil {
		t.Fatal("expected error for unknown command")
	}
}

func TestParseMissingSArg(t *testing.T) {
	_, err := Parse([]string{"-S"})
	if err == nil {
		t.Fatal("expected error for -S without argument")
	}
}

func TestParseDaemonMode(t *testing.T) {
	args := []string{"--daemon", "-S", "/tmp/s.sock", "new-session", "-d", "-s", "test"}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if !cmd.DaemonMode {
		t.Error("expected daemon mode")
	}
	if cmd.Type != CmdNewSession {
		t.Errorf("expected CmdNewSession, got %d", cmd.Type)
	}
}

// --- Tests matching exact CAM command lines ---

func TestParseCAMNewSession(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"new-session", "-d", "-s", "agent-123", "-c", "/work/dir",
		"env -u CLAUDECODE claude --print",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.SocketPath != "/tmp/cam-sockets/agent-123.sock" {
		t.Errorf("wrong socket: %s", cmd.SocketPath)
	}
	if cmd.SessionName != "agent-123" {
		t.Errorf("wrong session: %s", cmd.SessionName)
	}
	if cmd.StartDir != "/work/dir" {
		t.Errorf("wrong workdir: %s", cmd.StartDir)
	}
	if cmd.ShellCmd != "env -u CLAUDECODE claude --print" {
		t.Errorf("wrong shell cmd: %q", cmd.ShellCmd)
	}
}

func TestParseCAMSetOption(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"set-option", "-t", "agent-123", "history-limit", "50000",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdSetOption {
		t.Errorf("expected CmdSetOption, got %d", cmd.Type)
	}
	if cmd.Target != "agent-123" {
		t.Errorf("wrong target: %s", cmd.Target)
	}
	if cmd.Option != "history-limit" || cmd.Value != "50000" {
		t.Errorf("wrong option: %s=%s", cmd.Option, cmd.Value)
	}
}

func TestParseCAMSendKeysLiteral(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"send-keys", "-t", "agent-123:0.0", "-l", "--", "implement the feature",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if !cmd.Literal {
		t.Error("expected literal=true")
	}
	if cmd.Target != "agent-123:0.0" {
		t.Errorf("wrong target: %s", cmd.Target)
	}
	// "implement the feature" is a single arg, so it becomes one key
	if len(cmd.Keys) != 1 || cmd.Keys[0] != "implement the feature" {
		t.Errorf("expected ['implement the feature'], got %v", cmd.Keys)
	}
}

func TestParseCAMSendEnter(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"send-keys", "-t", "agent-123:0.0", "Enter",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Literal {
		t.Error("expected literal=false")
	}
	if len(cmd.Keys) != 1 || cmd.Keys[0] != "Enter" {
		t.Errorf("expected [Enter], got %v", cmd.Keys)
	}
}

func TestParseCAMCapture(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"capture-pane", "-p", "-J", "-t", "agent-123:0.0", "-S", "-100",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdCapturePane {
		t.Errorf("expected CmdCapturePane, got %d", cmd.Type)
	}
	if !cmd.Print || !cmd.JoinLines {
		t.Error("expected print and join flags")
	}
	if cmd.Target != "agent-123:0.0" {
		t.Errorf("wrong target: %s", cmd.Target)
	}
	if cmd.StartLine != -100 {
		t.Errorf("expected startLine -100, got %d", cmd.StartLine)
	}
}

func TestParseCAMCaptureAlternate(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"capture-pane", "-p", "-J", "-a", "-t", "agent-123:0.0", "-S", "-100",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if !cmd.Alternate {
		t.Error("expected alternate=true")
	}
}

func TestParseCAMHasSession(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"has-session", "-t", "agent-123",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdHasSession {
		t.Errorf("expected CmdHasSession, got %d", cmd.Type)
	}
	if cmd.Target != "agent-123" {
		t.Errorf("expected target agent-123, got %s", cmd.Target)
	}
}

func TestParseCAMKillSession(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"kill-session", "-t", "agent-123",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdKillSession {
		t.Errorf("expected CmdKillSession, got %d", cmd.Type)
	}
}

func TestParseCAMPipePane(t *testing.T) {
	args := []string{
		"-S", "/tmp/cam-sockets/agent-123.sock",
		"pipe-pane", "-t", "agent-123:0.0",
		"cat >> /tmp/cam-logs/agent-123.output.log",
	}
	cmd, err := Parse(args)
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if cmd.Type != CmdPipePane {
		t.Errorf("expected CmdPipePane, got %d", cmd.Type)
	}
	expected := "cat >> /tmp/cam-logs/agent-123.output.log"
	if cmd.PipeCmd != expected {
		t.Errorf("expected %q, got %q", expected, cmd.PipeCmd)
	}
}
