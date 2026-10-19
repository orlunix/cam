//go:build !windows

package spawn

import (
	"os"
	"os/exec"
	"strconv"
	"syscall"
)

// DaemonArgv builds the argument vector that re-executes the current
// binary in daemon mode.
func DaemonArgv(socketPath, sessionName, workdir, command string, cols, rows int) []string {
	args := []string{"--daemon", "-S", socketPath, "new-session", "-d", "-s", sessionName}
	if workdir != "" {
		args = append(args, "-c", workdir)
	}
	if cols > 0 && rows > 0 {
		args = append(args, "-x", strconv.Itoa(cols), "-y", strconv.Itoa(rows))
	}
	if command != "" {
		args = append(args, command)
	}
	return args
}

// Daemon launches the daemon as a background process on Unix-like
// systems (used for development/testing on WSL2 and macOS).
func Daemon(socketPath, sessionName, workdir, command string, cols, rows int) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}

	cmd := exec.Command(exe, DaemonArgv(socketPath, sessionName, workdir, command, cols, rows)...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid: true,
	}
	cmd.Stdout = nil
	cmd.Stderr = nil

	return cmd.Start()
}
