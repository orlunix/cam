// Package screen implements a virtual terminal emulator that maintains a
// rows×cols grid of characters. It parses ANSI/VT escape sequences from raw
// PTY output and tracks cursor position, enabling accurate capture-pane for
// full-screen TUI applications like Claude Code.
package screen

import (
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"
)

// Screen is a thread-safe virtual terminal emulator.
type Screen struct {
	mu   sync.RWMutex
	cols int
	rows int

	main  gridState
	alt   gridState
	inAlt bool

	// history holds lines scrolled off the top of the main screen
	// (tmux scrollback). The alternate screen never contributes.
	history    [][]rune
	historyCap int

	pState parserState
	pBuf   []byte // escape sequence accumulator
	uBuf   []byte // incomplete UTF-8 bytes from previous Write
}

type gridState struct {
	grid                    [][]rune
	row, col                int
	scrollTop, scrollBottom int
	savedRow, savedCol      int
}

type parserState byte

const (
	psNorm    parserState = iota
	psEsc                 // saw ESC
	psCSI                 // saw ESC[
	psOSC                 // saw ESC]
	psOSCEsc              // saw ESC inside OSC (expecting \)
	psEscSkip             // skip next byte (charset designation)
)

// New creates a virtual terminal screen with the given dimensions.
func New(cols, rows int) *Screen {
	s := &Screen{cols: cols, rows: rows, historyCap: 10000}
	s.main = newGrid(cols, rows)
	s.alt = newGrid(cols, rows)
	return s
}

// Rows reports the current grid height.
func (s *Screen) Rows() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.rows
}

// SetHistoryCap sets the maximum number of scrollback lines retained
// (0 disables history). Mirrors tmux's history-limit.
func (s *Screen) SetHistoryCap(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.historyCap = n
	s.trimHistoryLocked()
}

// trimHistoryLocked drops the oldest history lines when over capacity.
// Caller must hold s.mu.
func (s *Screen) trimHistoryLocked() {
	if s.historyCap < 0 {
		s.historyCap = 0
	}
	if excess := len(s.history) - s.historyCap; excess > 0 {
		s.history = append(make([][]rune, 0, s.historyCap), s.history[excess:]...)
	}
}

func newGrid(cols, rows int) gridState {
	g := gridState{
		grid:         make([][]rune, rows),
		scrollBottom: rows - 1,
	}
	for i := range g.grid {
		g.grid[i] = makeRow(cols)
	}
	return g
}

func makeRow(cols int) []rune {
	row := make([]rune, cols)
	for j := range row {
		row[j] = ' '
	}
	return row
}

func (s *Screen) st() *gridState {
	if s.inAlt {
		return &s.alt
	}
	return &s.main
}

// Write processes raw terminal output bytes, updating the screen grid.
func (s *Screen) Write(data []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Prepend any previously buffered incomplete UTF-8 bytes
	if len(s.uBuf) > 0 {
		data = append(s.uBuf, data...)
		s.uBuf = s.uBuf[:0]
	}

	i := 0
	for i < len(data) {
		b := data[i]

		// Inside escape sequence — byte-level parsing (all ASCII)
		if s.pState != psNorm {
			s.feedEsc(b)
			i++
			continue
		}

		// Control characters
		if b < 0x20 || b == 0x7f {
			s.feedCtrl(b)
			i++
			continue
		}

		// ASCII printable
		if b < 0x80 {
			s.putRune(rune(b))
			i++
			continue
		}

		// UTF-8 multi-byte
		r, size := utf8.DecodeRune(data[i:])
		if r == utf8.RuneError && size <= 1 {
			if len(data)-i < 4 {
				// Incomplete sequence — save for next Write
				s.uBuf = append(s.uBuf[:0], data[i:]...)
				return
			}
			// Invalid byte — skip
			i++
			continue
		}
		s.putRune(r)
		i += size
	}
}

// Capture returns screen content as newline-joined text rows, trailing
// spaces trimmed per line. historyLines selects how much scrollback
// precedes the visible grid (tmux capture-pane -S semantics): <0 = all
// history (`-S -`), 0 = visible screen only, >0 = that many history
// lines (`-S -N`). The alternate screen has no scrollback. Trailing
// blank rows are trimmed.
func (s *Screen) Capture(historyLines int) []string {
	s.mu.RLock()
	defer s.mu.RUnlock()

	g := s.st()
	out := make([]string, 0, len(s.history)+s.rows)
	if !s.inAlt && historyLines != 0 {
		h := s.history
		if historyLines > 0 && len(h) > historyLines {
			h = h[len(h)-historyLines:]
		}
		for _, row := range h {
			out = append(out, strings.TrimRight(string(row), " "))
		}
	}
	for r := 0; r < s.rows; r++ {
		out = append(out, strings.TrimRight(string(g.grid[r]), " "))
	}
	for len(out) > 0 && out[len(out)-1] == "" {
		out = out[:len(out)-1]
	}
	return out
}

// Resize changes the screen dimensions. Content is bottom-aligned (the
// cursor area survives); top rows dropped by a height shrink on the main
// screen move into scrollback history. Rows are truncated/padded to the
// new width. The scroll region resets to the full screen.
func (s *Screen) Resize(cols, rows int) {
	if cols <= 0 || rows <= 0 {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.main = s.resizeGridLocked(s.main, cols, rows, true)
	s.alt = s.resizeGridLocked(s.alt, cols, rows, false)
	s.cols, s.rows = cols, rows
}

// resizeGridLocked rebuilds g at the new size. Dropped top rows go to
// history only when useHistory (main screen). Caller holds s.mu.
func (s *Screen) resizeGridLocked(g gridState, cols, rows int, useHistory bool) gridState {
	ng := newGrid(cols, rows)
	src := g.grid
	if len(src) > rows {
		drop := len(src) - rows
		if useHistory && s.historyCap > 0 {
			s.history = append(s.history, src[:drop]...)
			s.trimHistoryLocked()
		}
		src = src[drop:]
	}
	n := len(src)
	if n > rows {
		n = rows
	}
	for i := 0; i < n; i++ {
		ng.grid[rows-n+i] = resizeRow(src[len(src)-n+i], cols)
	}
	ng.row = clamp(g.row-(len(g.grid)-n), 0, rows-1)
	ng.col = clamp(g.col, 0, cols-1)
	ng.savedRow = clamp(g.savedRow, 0, rows-1)
	ng.savedCol = clamp(g.savedCol, 0, cols-1)
	return ng
}

func resizeRow(row []rune, cols int) []rune {
	if len(row) == cols {
		return row
	}
	nr := make([]rune, cols)
	copy(nr, row)
	if len(row) < cols {
		for i := len(row); i < cols; i++ {
			nr[i] = ' '
		}
	}
	return nr
}

// --- Character output ---

func (s *Screen) putRune(r rune) {
	g := s.st()
	if g.col >= s.cols {
		// Auto-wrap
		g.col = 0
		s.linefeed()
	}
	g.grid[g.row][g.col] = r
	g.col++
}

// --- Control characters ---

func (s *Screen) feedCtrl(b byte) {
	g := s.st()
	switch b {
	case 0x1b: // ESC
		s.pState = psEsc
		s.pBuf = s.pBuf[:0]
	case '\r':
		g.col = 0
	case '\n':
		s.linefeed()
	case '\x08': // BS
		if g.col > 0 {
			g.col--
		}
	case '\t':
		g.col = (g.col/8 + 1) * 8
		if g.col >= s.cols {
			g.col = s.cols - 1
		}
	case '\x07': // BEL — ignore
	}
}

// --- Escape sequence parser ---

func (s *Screen) feedEsc(b byte) {
	switch s.pState {
	case psEsc:
		switch b {
		case '[':
			s.pState = psCSI
			s.pBuf = s.pBuf[:0]
		case ']':
			s.pState = psOSC
			s.pBuf = s.pBuf[:0]
		case 'M': // Reverse Index
			s.reverseIndex()
			s.pState = psNorm
		case '7': // Save Cursor (DECSC)
			g := s.st()
			g.savedRow = g.row
			g.savedCol = g.col
			s.pState = psNorm
		case '8': // Restore Cursor (DECRC)
			g := s.st()
			g.row = g.savedRow
			g.col = g.savedCol
			s.pState = psNorm
		case '(', ')': // Charset designation — skip next byte
			s.pState = psEscSkip
		default:
			s.pState = psNorm
		}

	case psCSI:
		if (b >= '0' && b <= '9') || b == ';' || b == '?' {
			s.pBuf = append(s.pBuf, b)
			return
		}
		// Final byte — execute
		params := string(s.pBuf)
		s.pState = psNorm
		s.pBuf = s.pBuf[:0]
		s.execCSI(b, params)

	case psOSC:
		if b == 0x07 { // BEL terminates
			s.pState = psNorm
			s.pBuf = s.pBuf[:0]
		} else if b == 0x1b {
			s.pState = psOSCEsc
		}
		// else: accumulate (ignored)

	case psOSCEsc:
		// ESC \ is String Terminator
		s.pState = psNorm
		s.pBuf = s.pBuf[:0]

	case psEscSkip:
		s.pState = psNorm
	}
}

// --- CSI command execution ---

func (s *Screen) execCSI(final byte, params string) {
	g := s.st()

	switch final {
	case 'H', 'f': // CUP — Cursor Position
		row, col := parseTwo(params, 1, 1)
		g.row = clamp(row-1, 0, s.rows-1)
		g.col = clamp(col-1, 0, s.cols-1)

	case 'A': // CUU — Cursor Up
		g.row = max0(g.row-parseOne(params, 1), g.scrollTop)

	case 'B': // CUD — Cursor Down
		g.row = min0(g.row+parseOne(params, 1), g.scrollBottom)

	case 'C': // CUF — Cursor Forward
		g.col = min0(g.col+parseOne(params, 1), s.cols-1)

	case 'D': // CUB — Cursor Backward
		g.col = max0(g.col-parseOne(params, 1), 0)

	case 'E': // CNL — Cursor Next Line
		g.row = min0(g.row+parseOne(params, 1), g.scrollBottom)
		g.col = 0

	case 'F': // CPL — Cursor Previous Line
		g.row = max0(g.row-parseOne(params, 1), g.scrollTop)
		g.col = 0

	case 'G': // CHA — Cursor Horizontal Absolute
		g.col = clamp(parseOne(params, 1)-1, 0, s.cols-1)

	case 'd': // VPA — Vertical Position Absolute
		g.row = clamp(parseOne(params, 1)-1, 0, s.rows-1)

	case 'J': // ED — Erase Display
		s.eraseDisplay(parseOne(params, 0))

	case 'K': // EL — Erase Line
		s.eraseLine(parseOne(params, 0))

	case 'X': // ECH — Erase Characters
		n := parseOne(params, 1)
		for i := 0; i < n && g.col+i < s.cols; i++ {
			g.grid[g.row][g.col+i] = ' '
		}

	case 'L': // IL — Insert Lines
		s.insertLines(parseOne(params, 1))

	case 'M': // DL — Delete Lines
		s.deleteLines(parseOne(params, 1))

	case '@': // ICH — Insert Characters
		s.insertChars(parseOne(params, 1))

	case 'P': // DCH — Delete Characters
		s.deleteChars(parseOne(params, 1))

	case 'S': // SU — Scroll Up
		s.scrollUp(parseOne(params, 1))

	case 'T': // SD — Scroll Down
		s.scrollDown(parseOne(params, 1))

	case 'r': // DECSTBM — Set Scroll Region
		top, bottom := parseTwo(params, 1, s.rows)
		g.scrollTop = clamp(top-1, 0, s.rows-1)
		g.scrollBottom = clamp(bottom-1, 0, s.rows-1)
		// Cursor moves to home after setting scroll region
		g.row = g.scrollTop
		g.col = 0

	case 'h': // SM — Set Mode
		if len(params) > 0 && params[0] == '?' {
			s.setPrivateMode(params[1:], true)
		}

	case 'l': // RM — Reset Mode
		if len(params) > 0 && params[0] == '?' {
			s.setPrivateMode(params[1:], false)
		}

	case 's': // SCP — Save Cursor Position
		g.savedRow = g.row
		g.savedCol = g.col

	case 'u': // RCP — Restore Cursor Position
		g.row = g.savedRow
		g.col = g.savedCol

	case 'm': // SGR — Select Graphic Rendition (ignore)
	case 'n': // DSR — Device Status Report (ignore)
	case 'c': // DA — Device Attributes (ignore)
	case 'q': // DECSCUSR — Set Cursor Style (ignore)
	}
}

// --- Private modes ---

func (s *Screen) setPrivateMode(params string, set bool) {
	for _, p := range strings.Split(params, ";") {
		n, _ := strconv.Atoi(p)
		switch n {
		case 47, 1047, 1049: // Alternate screen buffer
			if set && !s.inAlt {
				s.inAlt = true
				s.alt = newGrid(s.cols, s.rows)
			} else if !set && s.inAlt {
				s.inAlt = false
			}
		}
	}
}

// --- Scrolling & line operations ---

func (s *Screen) linefeed() {
	g := s.st()
	if g.row == g.scrollBottom {
		s.scrollUp(1)
	} else if g.row < s.rows-1 {
		g.row++
	}
}

func (s *Screen) reverseIndex() {
	g := s.st()
	if g.row == g.scrollTop {
		s.scrollDown(1)
	} else if g.row > 0 {
		g.row--
	}
}

func (s *Screen) scrollUp(n int) {
	g := s.st()
	top, bottom := g.scrollTop, g.scrollBottom
	span := bottom - top + 1
	if n > span {
		n = span
	}
	// Lines scrolled off the top of the full-width main scroll region
	// become scrollback history (tmux semantics: custom scroll regions
	// and the alternate screen do not contribute).
	if !s.inAlt && top == 0 && bottom == s.rows-1 && s.historyCap > 0 {
		s.history = append(s.history, g.grid[:n]...)
		s.trimHistoryLocked()
	}
	// Shift lines up within scroll region
	for r := top; r <= bottom-n; r++ {
		g.grid[r] = g.grid[r+n]
	}
	// Fill new lines at bottom with spaces
	for r := bottom - n + 1; r <= bottom; r++ {
		g.grid[r] = makeRow(s.cols)
	}
}

func (s *Screen) scrollDown(n int) {
	g := s.st()
	top, bottom := g.scrollTop, g.scrollBottom
	span := bottom - top + 1
	if n > span {
		n = span
	}
	// Shift lines down within scroll region
	for r := bottom; r >= top+n; r-- {
		g.grid[r] = g.grid[r-n]
	}
	// Fill new lines at top with spaces
	for r := top; r < top+n; r++ {
		g.grid[r] = makeRow(s.cols)
	}
}

func (s *Screen) insertLines(n int) {
	g := s.st()
	if g.row < g.scrollTop || g.row > g.scrollBottom {
		return
	}
	saved := g.scrollTop
	g.scrollTop = g.row
	s.scrollDown(n)
	g.scrollTop = saved
	g.col = 0
}

func (s *Screen) deleteLines(n int) {
	g := s.st()
	if g.row < g.scrollTop || g.row > g.scrollBottom {
		return
	}
	saved := g.scrollTop
	g.scrollTop = g.row
	s.scrollUp(n)
	g.scrollTop = saved
	g.col = 0
}

func (s *Screen) insertChars(n int) {
	g := s.st()
	row := g.grid[g.row]
	// Shift right from cursor
	for i := s.cols - 1; i >= g.col+n && i >= 0; i-- {
		row[i] = row[i-n]
	}
	// Fill inserted positions with spaces
	for i := g.col; i < g.col+n && i < s.cols; i++ {
		row[i] = ' '
	}
}

func (s *Screen) deleteChars(n int) {
	g := s.st()
	row := g.grid[g.row]
	// Shift left from cursor
	for i := g.col; i < s.cols-n; i++ {
		row[i] = row[i+n]
	}
	// Fill vacated positions with spaces
	for i := s.cols - n; i < s.cols; i++ {
		if i >= 0 {
			row[i] = ' '
		}
	}
}

// --- Erase operations ---

func (s *Screen) eraseDisplay(mode int) {
	g := s.st()
	switch mode {
	case 0: // Below (from cursor to end)
		for i := g.col; i < s.cols; i++ {
			g.grid[g.row][i] = ' '
		}
		for r := g.row + 1; r < s.rows; r++ {
			g.grid[r] = makeRow(s.cols)
		}
	case 1: // Above (from start to cursor)
		for r := 0; r < g.row; r++ {
			g.grid[r] = makeRow(s.cols)
		}
		for i := 0; i <= g.col && i < s.cols; i++ {
			g.grid[g.row][i] = ' '
		}
	case 2, 3: // Entire screen
		for r := 0; r < s.rows; r++ {
			g.grid[r] = makeRow(s.cols)
		}
	}
}

func (s *Screen) eraseLine(mode int) {
	g := s.st()
	switch mode {
	case 0: // Right (from cursor to end)
		for i := g.col; i < s.cols; i++ {
			g.grid[g.row][i] = ' '
		}
	case 1: // Left (from start to cursor)
		for i := 0; i <= g.col && i < s.cols; i++ {
			g.grid[g.row][i] = ' '
		}
	case 2: // Entire line
		g.grid[g.row] = makeRow(s.cols)
	}
}

// --- Parameter parsing helpers ---

func parseOne(params string, def int) int {
	params = strings.TrimPrefix(params, "?")
	if params == "" {
		return def
	}
	n, err := strconv.Atoi(params)
	if err != nil || n == 0 {
		return def
	}
	return n
}

func parseTwo(params string, def1, def2 int) (int, int) {
	parts := strings.SplitN(params, ";", 2)
	a, b := def1, def2
	if len(parts) >= 1 && parts[0] != "" {
		if n, err := strconv.Atoi(parts[0]); err == nil && n > 0 {
			a = n
		}
	}
	if len(parts) >= 2 && parts[1] != "" {
		if n, err := strconv.Atoi(parts[1]); err == nil && n > 0 {
			b = n
		}
	}
	return a, b
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func max0(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func min0(a, b int) int {
	if a < b {
		return a
	}
	return b
}
