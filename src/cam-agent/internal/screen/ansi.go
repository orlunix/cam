package screen

import "regexp"

// ansiPattern matches ANSI escape sequences (CSI, OSC, and simple escapes).
var ansiPattern = regexp.MustCompile(`\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][AB012]|\x1b[=>]`)

// StripANSI removes all ANSI escape sequences from the input string.
func StripANSI(s string) string {
	return ansiPattern.ReplaceAllString(s, "")
}
