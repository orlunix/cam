package session

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/openclaw/cam-agent/internal/screen"
)

const (
	socketDir = "/tmp/cam-agent-sockets"
	logDir    = "/tmp/cam-agent-logs"
)

// TmuxManager implements Manager using real tmux on Linux/macOS.
type TmuxManager struct{}

func (m *TmuxManager) socketPath(id string) string {
	return filepath.Join(socketDir, id+".sock")
}

func (m *TmuxManager) logPath(id string) string {
	return filepath.Join(logDir, id+".output.log")
}

func (m *TmuxManager) tmux(id string, args ...string) *exec.Cmd {
	sock := m.socketPath(id)
	all := append([]string{"-u", "-S", sock}, args...)
	return exec.Command("tmux", all...)
}

func (m *TmuxManager) runTmux(id string, args ...string) (string, error) {
	cmd := m.tmux(id, args...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func (m *TmuxManager) Create(id string, cmdArgs []string, workdir string, envSetup string) error {
	// Ensure socket directory exists
	os.MkdirAll(socketDir, 0755)

	// Build the shell command.
	// Wrap in login shell so user's env is loaded; include env_setup if given.
	cmdStr := strings.Join(quoteArgs(cmdArgs), " ")
	var shellCmd string
	if envSetup != "" {
		shellCmd = envSetup + " && exec " + cmdStr
	} else {
		shellCmd = "exec " + cmdStr
	}
	wrapped := fmt.Sprintf("bash -l -c %s", shellQuote(shellCmd))

	// Create detached tmux session. Session dies when process exits.
	args := []string{
		"new-session", "-d", "-s", id, "-c", workdir, wrapped,
	}
	if _, err := m.runTmux(id, args...); err != nil {
		return fmt.Errorf("tmux new-session failed: %w", err)
	}

	// Increase scrollback
	m.runTmux(id, "set-option", "-t", id, "history-limit", "50000")

	return nil
}

func (m *TmuxManager) Exists(id string) bool {
	cmd := m.tmux(id, "has-session", "-t", id)
	return cmd.Run() == nil
}

func (m *TmuxManager) Kill(id string) error {
	m.runTmux(id, "kill-session", "-t", id)
	// Clean up socket
	os.Remove(m.socketPath(id))
	return nil
}

func (m *TmuxManager) Capture(id string, lines int) (string, error) {
	target := id + ":0.0"
	lineArg := fmt.Sprintf("-%d", lines)

	// Primary capture
	out, err := m.runTmux(id, "capture-pane", "-p", "-J", "-t", target, "-S", lineArg)
	if err != nil {
		return "", fmt.Errorf("capture-pane failed: %w", err)
	}

	// Alternate screen fallback (Claude Code uses alternate buffer)
	if len(strings.TrimSpace(out)) < 20 {
		altOut, altErr := m.runTmux(id, "capture-pane", "-p", "-J", "-a", "-t", target, "-S", lineArg)
		if altErr == nil && len(strings.TrimSpace(altOut)) > len(strings.TrimSpace(out)) {
			out = altOut
		}
	}

	// Strip ANSI sequences
	return strings.TrimRight(screen.StripANSI(out), " \t\n\r"), nil
}

func (m *TmuxManager) SendText(id string, text string, enter bool) error {
	target := id + ":0.0"

	if text != "" {
		if _, err := m.runTmux(id, "send-keys", "-t", target, "-l", "--", text); err != nil {
			return fmt.Errorf("send-keys text failed: %w", err)
		}
	}

	if enter {
		if _, err := m.runTmux(id, "send-keys", "-t", target, "Enter"); err != nil {
			return fmt.Errorf("send-keys Enter failed: %w", err)
		}
	}

	return nil
}

func (m *TmuxManager) SendKey(id string, key string) error {
	target := id + ":0.0"
	_, err := m.runTmux(id, "send-keys", "-t", target, key)
	if err != nil {
		return fmt.Errorf("send-keys %s failed: %w", key, err)
	}
	return nil
}

func (m *TmuxManager) StartLogging(id string) (string, error) {
	os.MkdirAll(logDir, 0755)

	target := id + ":0.0"
	logPath := m.logPath(id)
	pipeCmd := fmt.Sprintf("cat >> %s", shellQuote(logPath))

	if _, err := m.runTmux(id, "pipe-pane", "-t", target, pipeCmd); err != nil {
		return "", fmt.Errorf("pipe-pane failed: %w", err)
	}
	return logPath, nil
}

func (m *TmuxManager) ReadLog(id string, offset int64, maxBytes int) ([]byte, int64, error) {
	logPath := m.logPath(id)

	f, err := os.Open(logPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, offset, nil
		}
		return nil, offset, err
	}
	defer f.Close()

	if offset > 0 {
		if _, err := f.Seek(offset, io.SeekStart); err != nil {
			return nil, offset, err
		}
	}

	buf := make([]byte, maxBytes)
	n, err := f.Read(buf)
	if err != nil && err != io.EOF {
		return nil, offset, err
	}

	newOffset := offset + int64(n)
	return buf[:n], newOffset, nil
}

// --- helpers ---

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'"
}

func quoteArgs(args []string) []string {
	out := make([]string, len(args))
	for i, a := range args {
		out[i] = shellQuote(a)
	}
	return out
}

// unused but available for future use
func parseInt(s string, def int) int {
	v, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return v
}
