//go:build windows

package camcwin

import (
	"os"
	"os/exec"
	"strings"
)

// knownToolArgs: launch flags that force the tool's unattended /
// auto-confirm mode (camc run forces auto-confirm on; the hub depends
// on agents never parking at a trust/permission prompt).
var knownToolArgs = map[string][]string{
	"claude": {"--dangerously-skip-permissions"},
	"codex":  {"--full-auto"},
}

// toolLaunchCommand builds the daemon command string for a tool.
// known=false means the tool is unrecognized → the returned command is
// the default shell. known=true with command="" means the tool is a
// recognized name but its binary was not found on PATH (caller prints
// the install hint). npm-installed CLIs resolve to .cmd shims, which
// CreateProcess cannot exec directly — wrap those in `cmd.exe /c`.
func toolLaunchCommand(tool string) (command string, known bool) {
	args, isKnown := knownToolArgs[tool]
	bin, err := exec.LookPath(tool)
	if err != nil {
		if isKnown {
			return "", true
		}
		return defaultShell(), false
	}
	if strings.HasSuffix(strings.ToLower(bin), ".cmd") || strings.HasSuffix(strings.ToLower(bin), ".bat") {
		parts := append([]string{bin}, args...)
		return `cmd.exe /c "` + strings.Join(parts, " ") + `"`, true
	}
	parts := append([]string{bin}, args...)
	return strings.Join(parts, " "), true
}

// defaultShell is the fallback for unrecognized tools.
func defaultShell() string {
	if cs := os.Getenv("COMSPEC"); cs != "" {
		return cs
	}
	return "cmd.exe"
}
