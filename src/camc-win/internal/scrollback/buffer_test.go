package scrollback

import (
	"fmt"
	"sync"
	"testing"
)

func TestNew(t *testing.T) {
	b := New(100)
	if b.Capacity() != 100 {
		t.Errorf("expected capacity 100, got %d", b.Capacity())
	}
	if b.Count() != 0 {
		t.Errorf("expected count 0, got %d", b.Count())
	}
}

func TestNewDefaultCapacity(t *testing.T) {
	b := New(0)
	if b.Capacity() != 2000 {
		t.Errorf("expected default capacity 2000, got %d", b.Capacity())
	}
}

func TestWriteAndLast(t *testing.T) {
	b := New(10)
	b.Write([]byte("line1\nline2\nline3\n"))

	lines := b.Last(3)
	if len(lines) != 3 {
		t.Fatalf("expected 3 lines, got %d", len(lines))
	}
	expected := []string{"line1", "line2", "line3"}
	for i, line := range lines {
		if line != expected[i] {
			t.Errorf("line %d: expected %q, got %q", i, expected[i], line)
		}
	}
}

func TestWriteCarriageReturn(t *testing.T) {
	b := New(10)
	b.Write([]byte("hello\r\nworld\r\n"))

	lines := b.Last(2)
	if len(lines) != 2 {
		t.Fatalf("expected 2 lines, got %d", len(lines))
	}
	if lines[0] != "hello" || lines[1] != "world" {
		t.Errorf("expected [hello, world], got %v", lines)
	}
}

func TestPartialLine(t *testing.T) {
	b := New(10)
	b.Write([]byte("line1\npartial"))

	committed := b.Last(10)
	if len(committed) != 1 {
		t.Fatalf("expected 1 committed line, got %d", len(committed))
	}
	if committed[0] != "line1" {
		t.Errorf("expected 'line1', got %q", committed[0])
	}

	withPartial := b.LastWithPartial(10)
	if len(withPartial) != 2 {
		t.Fatalf("expected 2 lines with partial, got %d", len(withPartial))
	}
	if withPartial[0] != "line1" {
		t.Errorf("expected 'line1', got %q", withPartial[0])
	}
	if withPartial[1] != "partial" {
		t.Errorf("expected 'partial', got %q", withPartial[1])
	}
}

func TestOverflow(t *testing.T) {
	b := New(3)
	for i := 0; i < 10; i++ {
		b.Write([]byte(fmt.Sprintf("line%d\n", i)))
	}

	lines := b.Last(3)
	if len(lines) != 3 {
		t.Fatalf("expected 3 lines, got %d", len(lines))
	}
	expected := []string{"line7", "line8", "line9"}
	for i, line := range lines {
		if line != expected[i] {
			t.Errorf("line %d: expected %q, got %q", i, expected[i], line)
		}
	}
}

func TestLastMoreThanAvailable(t *testing.T) {
	b := New(10)
	b.Write([]byte("only\n"))

	lines := b.Last(100)
	if len(lines) != 1 {
		t.Fatalf("expected 1 line, got %d", len(lines))
	}
	if lines[0] != "only" {
		t.Errorf("expected 'only', got %q", lines[0])
	}
}

func TestLastZero(t *testing.T) {
	b := New(10)
	b.Write([]byte("line\n"))

	lines := b.Last(0)
	if lines != nil {
		t.Errorf("expected nil, got %v", lines)
	}
}

func TestLastWithPartialZero(t *testing.T) {
	b := New(10)
	b.Write([]byte("line\n"))

	lines := b.LastWithPartial(0)
	if lines != nil {
		t.Errorf("expected nil, got %v", lines)
	}
}

func TestSetCapacityShrink(t *testing.T) {
	b := New(10)
	for i := 0; i < 8; i++ {
		b.Write([]byte(fmt.Sprintf("line%d\n", i)))
	}

	b.SetCapacity(3)
	if b.Capacity() != 3 {
		t.Errorf("expected capacity 3, got %d", b.Capacity())
	}

	lines := b.Last(10)
	if len(lines) != 3 {
		t.Fatalf("expected 3 lines after shrink, got %d", len(lines))
	}
	expected := []string{"line5", "line6", "line7"}
	for i, line := range lines {
		if line != expected[i] {
			t.Errorf("line %d: expected %q, got %q", i, expected[i], line)
		}
	}
}

func TestSetCapacityGrow(t *testing.T) {
	b := New(3)
	for i := 0; i < 5; i++ {
		b.Write([]byte(fmt.Sprintf("line%d\n", i)))
	}

	b.SetCapacity(10)
	lines := b.Last(10)
	if len(lines) != 3 {
		t.Fatalf("expected 3 lines (only 3 fit before grow), got %d", len(lines))
	}
	expected := []string{"line2", "line3", "line4"}
	for i, line := range lines {
		if line != expected[i] {
			t.Errorf("line %d: expected %q, got %q", i, expected[i], line)
		}
	}
}

func TestSetCapacitySameNoOp(t *testing.T) {
	b := New(5)
	b.Write([]byte("test\n"))
	b.SetCapacity(5)
	if b.Count() != 1 {
		t.Errorf("expected count preserved, got %d", b.Count())
	}
}

func TestConcurrentAccess(t *testing.T) {
	b := New(1000)
	var wg sync.WaitGroup

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 1000; i++ {
			b.Write([]byte(fmt.Sprintf("line%d\n", i)))
		}
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 500; i++ {
			b.Last(50)
			b.LastWithPartial(50)
		}
	}()

	wg.Wait()
}

func TestIncrementalWrite(t *testing.T) {
	b := New(10)
	b.Write([]byte("hel"))
	b.Write([]byte("lo\nwor"))
	b.Write([]byte("ld\n"))

	lines := b.Last(2)
	if len(lines) != 2 {
		t.Fatalf("expected 2 lines, got %d", len(lines))
	}
	if lines[0] != "hello" || lines[1] != "world" {
		t.Errorf("expected [hello, world], got %v", lines)
	}
}

func TestEmptyBuffer(t *testing.T) {
	b := New(10)
	lines := b.Last(5)
	if len(lines) != 0 {
		t.Errorf("expected empty for empty buffer, got %v", lines)
	}
	lines = b.LastWithPartial(5)
	if len(lines) != 0 {
		t.Errorf("expected empty for empty buffer with partial, got %v", lines)
	}
}

func TestOnlyNewlines(t *testing.T) {
	b := New(10)
	b.Write([]byte("\n\n\n"))

	lines := b.Last(5)
	if len(lines) != 3 {
		t.Fatalf("expected 3 empty lines, got %d", len(lines))
	}
	for i, line := range lines {
		if line != "" {
			t.Errorf("line %d: expected empty, got %q", i, line)
		}
	}
}

// Simulates the output pattern of a long-running AI coding agent:
// periodic status lines interspersed with pauses.
func TestAgentOutputPattern(t *testing.T) {
	b := New(100)

	b.Write([]byte("Agent started\n"))
	b.Write([]byte("[10:00:01] Planning...\n"))
	b.Write([]byte("  Reading file: config.yaml\n"))
	b.Write([]byte("[10:00:04] Editing src/main.py...\n"))
	b.Write([]byte("  Applying changes to 3 files...\n"))
	b.Write([]byte("[10:00:07] Running tests...\n"))
	b.Write([]byte("  Test results: 5 passed, 0 failed\n"))
	b.Write([]byte("[10:00:10] Done! Task completed successfully.\n"))

	lines := b.Last(50)
	if len(lines) != 8 {
		t.Fatalf("expected 8 lines, got %d", len(lines))
	}
	if lines[0] != "Agent started" {
		t.Errorf("first line: expected 'Agent started', got %q", lines[0])
	}
	if lines[7] != "[10:00:10] Done! Task completed successfully." {
		t.Errorf("last line: expected done message, got %q", lines[7])
	}
}
