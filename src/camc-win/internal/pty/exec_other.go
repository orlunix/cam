//go:build !windows

package pty

import (
	"os"
	"os/exec"
	"runtime"
)

// ExecTerminal runs the child under a real PTY via the `script` utility
// when available (faithful terminal semantics: ONLCR, ICRNL, echo), and
// falls back to plain pipes otherwise. Used for development and testing
// of daemon logic on non-Windows platforms.
type ExecTerminal struct {
	cmd    *exec.Cmd
	stdin  *os.File // write end of the pipe fed to child stdin
	stdout *os.File // read end of the pipe receiving child stdout+stderr
	done   chan struct{}
	code   int
}

// New starts command in workdir. cols/rows/env are accepted for
// interface compatibility but not applied (dev backend).
func New(cols, rows int, command string, workdir string, env []string) (Terminal, error) {
	var cmd *exec.Cmd
	if scriptPath, err := exec.LookPath("script"); err == nil {
		if runtime.GOOS == "darwin" {
			// BSD script: script -q /dev/null bash -c <command>
			cmd = exec.Command(scriptPath, "-q", "/dev/null", "bash", "-c", command)
		} else {
			// util-linux script: -q quiet, -e propagate child exit code
			cmd = exec.Command(scriptPath, "-qefc", command, "/dev/null")
		}
	} else {
		cmd = exec.Command("bash", "-c", command)
	}
	if workdir != "" {
		cmd.Dir = workdir
	}

	// Create pipes manually so stdout and stderr merge into one reader.
	outR, outW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	inR, inW, err := os.Pipe()
	if err != nil {
		outR.Close()
		outW.Close()
		return nil, err
	}

	cmd.Stdin = inR
	cmd.Stdout = outW
	cmd.Stderr = outW

	if err := cmd.Start(); err != nil {
		outR.Close()
		outW.Close()
		inR.Close()
		inW.Close()
		return nil, err
	}

	// Close child-side ends in the parent.
	outW.Close()
	inR.Close()

	t := &ExecTerminal{
		cmd:    cmd,
		stdin:  inW,
		stdout: outR,
		done:   make(chan struct{}),
	}

	go func() {
		_ = cmd.Wait()
		if cmd.ProcessState != nil {
			t.code = cmd.ProcessState.ExitCode()
		}
		close(t.done)
	}()

	return t, nil
}

func (t *ExecTerminal) Read(buf []byte) (int, error)   { return t.stdout.Read(buf) }
func (t *ExecTerminal) Write(data []byte) (int, error) { return t.stdin.Write(data) }
func (t *ExecTerminal) Resize(cols, rows int) error    { return nil }

func (t *ExecTerminal) Wait() error {
	<-t.done
	return nil
}

func (t *ExecTerminal) ExitCode() int { return t.code }

func (t *ExecTerminal) Close() error {
	t.stdin.Close()
	t.stdout.Close()
	if t.cmd.Process != nil {
		return t.cmd.Process.Kill()
	}
	return nil
}
