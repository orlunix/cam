package scrollback

import (
	"sync"
)

// Buffer is a thread-safe ring buffer that stores terminal output lines.
// It handles raw byte streams from a PTY, splitting on newlines and
// stripping carriage returns.
type Buffer struct {
	mu       sync.RWMutex
	lines    []string
	capacity int
	head     int // next write position
	count    int // number of committed lines
	partial  []byte
}

// New creates a scrollback buffer with the given line capacity.
// If capacity <= 0, defaults to 2000 (matching tmux default).
func New(capacity int) *Buffer {
	if capacity <= 0 {
		capacity = 2000
	}
	return &Buffer{
		lines:    make([]string, capacity),
		capacity: capacity,
	}
}

// Write processes raw bytes from terminal output, splitting into lines
// on newline characters and stripping carriage returns.
func (b *Buffer) Write(data []byte) {
	b.mu.Lock()
	defer b.mu.Unlock()

	for _, c := range data {
		switch c {
		case '\n':
			b.commitLine()
		case '\r':
			continue
		default:
			b.partial = append(b.partial, c)
		}
	}
}

func (b *Buffer) commitLine() {
	line := string(b.partial)
	b.partial = b.partial[:0]

	b.lines[b.head] = line
	b.head = (b.head + 1) % b.capacity
	if b.count < b.capacity {
		b.count++
	}
}

// Last returns the most recent n committed lines (excludes any partial line).
func (b *Buffer) Last(n int) []string {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.getLinesLocked(n)
}

// LastWithPartial returns the most recent n lines, including the current
// partial (uncommitted) line if one exists. This matches tmux capture-pane
// behavior where the current line content is included even without a trailing newline.
func (b *Buffer) LastWithPartial(n int) []string {
	b.mu.RLock()
	defer b.mu.RUnlock()

	if n <= 0 {
		return nil
	}

	partial := string(b.partial)
	hasPartial := len(partial) > 0

	committed := n
	if hasPartial {
		committed = n - 1
	}

	result := b.getLinesLocked(committed)

	if hasPartial {
		result = append(result, partial)
	}

	return result
}

// SetCapacity resizes the buffer. If shrinking, the oldest lines are discarded.
func (b *Buffer) SetCapacity(n int) {
	b.mu.Lock()
	defer b.mu.Unlock()

	if n <= 0 || n == b.capacity {
		return
	}

	old := b.getLinesLocked(b.count)

	b.capacity = n
	b.lines = make([]string, n)
	b.head = 0
	b.count = 0

	start := 0
	if len(old) > n {
		start = len(old) - n
	}
	for _, line := range old[start:] {
		b.lines[b.head] = line
		b.head = (b.head + 1) % b.capacity
		b.count++
	}
}

// Count returns the number of committed lines in the buffer.
func (b *Buffer) Count() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.count
}

// Capacity returns the maximum number of lines the buffer can hold.
func (b *Buffer) Capacity() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.capacity
}

func (b *Buffer) getLinesLocked(n int) []string {
	if n <= 0 {
		return nil
	}
	if n > b.count {
		n = b.count
	}

	result := make([]string, n)
	start := (b.head - n + b.capacity) % b.capacity
	for i := 0; i < n; i++ {
		result[i] = b.lines[(start+i)%b.capacity]
	}
	return result
}
