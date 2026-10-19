package screen

import (
	"strings"
	"testing"
)

// fillLines writes n numbered lines (each terminated by \r\n) to the screen.
func fillLines(s *Screen, n int, prefix string) {
	for i := 0; i < n; i++ {
		s.Write([]byte(prefix + itoa(i) + "\r\n"))
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [12]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

func TestScrollbackHistory(t *testing.T) {
	s := New(20, 5)
	fillLines(s, 12, "line")

	// 12 lines through a 5-row screen: 8 scrolled into history, visible
	// = line8..line11 + blank cursor row; trailing blank trims → 12.
	all := s.Capture(-1)
	if len(all) != 12 {
		t.Fatalf("Capture(-1) = %d lines, want 12: %q", len(all), all)
	}
	if all[0] != "line0" || all[11] != "line11" {
		t.Fatalf("history order wrong: first=%q last=%q", all[0], all[11])
	}

	// Capture(N): N history lines + full visible (tmux -S -N).
	got := s.Capture(3)
	// history tail line5..7 + visible line8..11 + blank(trimmed) = 7
	if len(got) != 7 || got[0] != "line5" || got[6] != "line11" {
		t.Fatalf("Capture(3) = %q", got)
	}

	// Visible only.
	vis := s.Capture(0)
	if len(vis) != 4 || vis[0] != "line8" {
		t.Fatalf("Capture(0) = %q", vis)
	}
}

func TestHistoryCap(t *testing.T) {
	s := New(20, 5)
	s.SetHistoryCap(4)
	fillLines(s, 20, "x")

	all := s.Capture(-1)
	// 16 scrolled (x0..x15), capped to newest 4 → x12..x15 history +
	// x16..x19 visible (trailing blank trimmed) = 8.
	if len(all) != 8 {
		t.Fatalf("Capture(-1) = %d lines, want 8: %q", len(all), all)
	}
	if all[0] != "x12" {
		t.Fatalf("oldest retained = %q, want x12", all[0])
	}
}

func TestAltScreenNoHistory(t *testing.T) {
	s := New(20, 5)
	fillLines(s, 3, "main")
	s.Write([]byte("\x1b[?1049h")) // enter alt screen
	fillLines(s, 10, "alt")
	s.Write([]byte("\x1b[?1049l")) // leave alt screen

	all := s.Capture(-1)
	for _, l := range all {
		if strings.HasPrefix(l, "alt") {
			t.Fatalf("alt screen line leaked into history: %q", l)
		}
	}
	if len(all) != 3 || all[0] != "main0" {
		t.Fatalf("main capture = %q", all)
	}
}

func TestResizeShrinkPushesHistory(t *testing.T) {
	s := New(20, 6)
	fillLines(s, 6, "row")
	// 6 lines into 6 rows: row0 already scrolled off (cursor blank line).
	s.Resize(20, 3)

	all := s.Capture(-1)
	// history row0..row3 + visible row4,row5 (blank trimmed) = 6
	if len(all) != 6 {
		t.Fatalf("Capture(-1) = %d lines after shrink, want 6: %q", len(all), all)
	}
	vis := s.Capture(0)
	if len(vis) != 2 || vis[0] != "row4" || vis[1] != "row5" {
		t.Fatalf("visible after shrink = %q", vis)
	}
}

func TestResizeWidth(t *testing.T) {
	s := New(10, 3)
	s.Write([]byte("abcdefgh"))
	s.Resize(4, 3)
	if got := s.Capture(0)[0]; got != "abcd" {
		t.Fatalf("truncate: %q", got)
	}
	s.Resize(8, 3)
	if got := s.Capture(0)[0]; got != "abcd" {
		t.Fatalf("pad: %q", got)
	}
}
