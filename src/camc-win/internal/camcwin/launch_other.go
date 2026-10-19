//go:build !windows

package camcwin

import (
	"os"
	"os/exec"
	"strings"
)

// knownToolArgs: see launch_windows.go.
var knownToolArgs = map[string][]string{
	"claude": {"--dangerously-skip-permissions"},
	"codex":  {"--full-auto"},
}

// toolLaunchCommand builds the daemon command string for a tool (POSIX
// dev path: the daemon runs it through `bash -c`).
func toolLaunchCommand(tool string) (command string, known bool) {
	args, isKnown := knownToolArgs[tool]
	bin, err := exec.LookPath(tool)
	if err != nil {
		if isKnown {
			return "", true // known tool but missing: caller prints the hint
		}
		return defaultShell(), false
	}
	parts := append([]string{bin}, args...)
	return strings.Join(parts, " "), true
}

// defaultShell is the fallback for unrecognized tools.
func defaultShell() string {
	if sh := os.Getenv("SHELL"); sh != "" {
		return sh
	}
	return "/bin/bash"
}
