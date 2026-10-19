package vt

import "testing"

func TestStripCSI(t *testing.T) {
	input := "\x1b[?9001h\x1b[?1004hHELLO\r\n"
	want := "HELLO\r\n"
	if got := Strip(input); got != want {
		t.Errorf("Strip(%q) = %q, want %q", input, got, want)
	}
}

func TestStripOSC(t *testing.T) {
	input := "\x1b]0;C:\\Windows\\cmd.exe\x07text"
	want := "text"
	if got := Strip(input); got != want {
		t.Errorf("Strip(%q) = %q, want %q", input, got, want)
	}
}

func TestStripMixed(t *testing.T) {
	input := "\x1b[?25l\x1b[2J\x1b[m\x1b[H\x1b]0;ping.exe\x07\x1b[?25hPinging 127.0.0.1\r\n"
	want := "Pinging 127.0.0.1\r\n"
	if got := Strip(input); got != want {
		t.Errorf("Strip(%q) = %q, want %q", input, got, want)
	}
}

func TestStripPlainText(t *testing.T) {
	input := "Hello World\nLine2\n"
	if got := Strip(input); got != input {
		t.Errorf("Strip(%q) = %q, want unchanged", input, got)
	}
}

func TestStripEmpty(t *testing.T) {
	if got := Strip(""); got != "" {
		t.Errorf("Strip(\"\") = %q", got)
	}
}

func TestStripOnlyEscapes(t *testing.T) {
	input := "\x1b[?9001h\x1b[?1004h"
	if got := Strip(input); got != "" {
		t.Errorf("Strip(%q) = %q, want empty", input, got)
	}
}
