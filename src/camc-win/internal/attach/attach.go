// Package attach implements the client side of an attach session:
// it connects to a session daemon, repaints the screen, then bridges
// the local terminal to the remote ConPTY until either side ends.
package attach

import (
	"fmt"
	"io"
	"os"
	"time"

	"camc-win/internal/ipc"
)

// Run attaches the local stdin/stdout terminal to the session on
// socketPath. It blocks until the session ends or the local stream
// closes, then restores the terminal. Returns the process exit code.
func Run(socketPath string) int {
	conn, err := ipc.Connect(socketPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "attach: %v\n", err)
		return 1
	}
	defer conn.Close()

	cols, rows := terminalSize()
	req := &ipc.Request{Action: ipc.ActionAttach, Cols: cols, Rows: rows}

	raw, rawErr := makeRaw()
	if rawErr == nil {
		defer raw.restore()
	}

	if err := ipc.WriteMessage(conn, req); err != nil {
		fmt.Fprintf(os.Stderr, "attach: %v\n", err)
		return 1
	}
	var resp ipc.Response
	if err := ipc.ReadMessage(conn, &resp); err != nil {
		fmt.Fprintf(os.Stderr, "attach: handshake: %v\n", err)
		return 1
	}
	if !resp.OK {
		fmt.Fprintf(os.Stderr, "attach: %s\n", resp.Error)
		return 1
	}

	// Session output → stdout.
	outDone := make(chan struct{})
	go func() {
		defer close(outDone)
		for {
			typ, payload, err := ipc.ReadFrame(conn)
			if err != nil {
				return
			}
			if typ == ipc.FrameOutput {
				os.Stdout.Write(payload)
			}
		}
	}()

	// Watch for terminal resizes (poll: Windows has no SIGWINCH).
	go func() {
		prevC, prevR := cols, rows
		for {
			select {
			case <-outDone:
				return
			case <-time.After(500 * time.Millisecond):
			}
			c, r := terminalSize()
			if c > 0 && r > 0 && (c != prevC || r != prevR) {
				prevC, prevR = c, r
				payload := []byte{byte(c >> 8), byte(c), byte(r >> 8), byte(r)}
				if err := ipc.WriteFrame(conn, ipc.FrameResize, payload); err != nil {
					return
				}
			}
		}
	}()

	// Local input → session. Blocks until stdin EOF/errors or the
	// connection dies (session end).
	buf := make([]byte, 4096)
	for {
		n, err := os.Stdin.Read(buf)
		if n > 0 {
			if werr := ipc.WriteFrame(conn, ipc.FrameInput, buf[:n]); werr != nil {
				return 0
			}
		}
		if err != nil {
			if err != io.EOF {
				// terminal went away; treat as detach
			}
			return 0
		}
		select {
		case <-outDone:
			return 0
		default:
		}
	}
}
