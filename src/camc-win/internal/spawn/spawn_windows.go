//go:build windows

// Package spawn launches the session daemon as a detached background
// process. Shared by wintmux and camc-win.
package spawn

import (
	"fmt"
	"os"
	"strings"
	"syscall"
	"unsafe"
)

// DaemonArgv builds the argument vector that re-executes the current
// binary in daemon mode.
func DaemonArgv(socketPath, sessionName, workdir, command string, cols, rows int) []string {
	args := []string{"--daemon", "-S", socketPath, "new-session", "-d", "-s", sessionName}
	if workdir != "" {
		args = append(args, "-c", workdir)
	}
	if cols > 0 && rows > 0 {
		args = append(args, "-x", fmt.Sprintf("%d", cols), "-y", fmt.Sprintf("%d", rows))
	}
	if command != "" {
		args = append(args, command)
	}
	return args
}

// quoteArg wraps one argument for a Windows command line.
func quoteArg(s string) string {
	if s == "" {
		return `""`
	}
	if !strings.ContainsAny(s, " \t\"") {
		return s
	}
	return `"` + strings.ReplaceAll(s, `"`, `\"`) + `"`
}

// Daemon launches the daemon as a background process.
// Uses CREATE_BREAKAWAY_FROM_JOB so the daemon survives when the
// parent SSH session ends (OpenSSH uses Job Objects to kill children).
func Daemon(socketPath, sessionName, workdir, command string, cols, rows int) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}

	argv := DaemonArgv(socketPath, sessionName, workdir, command, cols, rows)
	parts := make([]string, 0, len(argv)+1)
	parts = append(parts, quoteArg(exe))
	for _, a := range argv {
		parts = append(parts, quoteArg(a))
	}
	cmdLine := strings.Join(parts, " ")

	cmdLinePtr, err := syscall.UTF16PtrFromString(cmdLine)
	if err != nil {
		return fmt.Errorf("cmd line: %w", err)
	}

	var si syscall.StartupInfo
	si.Cb = uint32(unsafe.Sizeof(si))

	var pi syscall.ProcessInformation
	// CREATE_NO_WINDOW (0x08000000): don't create a console window
	// CREATE_NEW_PROCESS_GROUP (0x00000200): separate Ctrl-C group
	// CREATE_BREAKAWAY_FROM_JOB (0x01000000): escape SSH's Job Object
	const flags = 0x08000000 | 0x00000200 | 0x01000000
	err = syscall.CreateProcess(
		nil,
		cmdLinePtr,
		nil, nil,
		false, // don't inherit handles
		flags,
		nil, nil,
		&si, &pi,
	)
	if err != nil {
		return fmt.Errorf("create process: %w", err)
	}
	syscall.CloseHandle(pi.Thread)
	syscall.CloseHandle(pi.Process)
	return nil
}
