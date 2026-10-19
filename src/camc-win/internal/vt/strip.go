package vt

import "regexp"

// escapePattern matches ANSI/VT escape sequences:
//   - CSI sequences: ESC [ ... final_byte
//   - OSC sequences: ESC ] ... ST (or BEL)
//   - Simple ESC sequences: ESC + one char
var escapePattern = regexp.MustCompile(
	`\x1b\[[0-9;?]*[a-zA-Z]` + // CSI: ESC [ params letter
		`|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)` + // OSC: ESC ] ... BEL or ST
		`|\x1b[^[\]]`, // Simple: ESC + one non-bracket char
)

// Strip removes all ANSI/VT escape sequences from s, returning plain text.
func Strip(s string) string {
	return escapePattern.ReplaceAllString(s, "")
}
