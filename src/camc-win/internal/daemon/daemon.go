package daemon

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"camc-win/internal/ipc"
	"camc-win/internal/pty"
	"camc-win/internal/screen"
	"camc-win/internal/scrollback"
)

// ControlInfo is written to the socket path file so CLI clients can
// discover the daemon's TCP port.
type ControlInfo struct {
	Port int `json:"port"`
	PID  int `json:"pid"`
}

// Daemon manages a single session: one ConPTY process, a scrollback
// buffer, and a TCP server for IPC.
type Daemon struct {
	socketPath   string
	sessionName  string
	terminal     pty.Terminal
	buffer       *scrollback.Buffer
	screen       *screen.Screen
	listener     net.Listener
	pipePaneMu   sync.Mutex
	pipePaneFile *os.File
	subsMu       sync.Mutex
	subs         map[chan []byte]struct{}
	done         chan struct{} // closed when child process exits
}

// Run is the main entry point for a daemon process. It creates the
// terminal, starts the IPC server, and blocks until the child exits
// and the grace period elapses.
func Run(socketPath, sessionName, workdir, command string, cols, rows int) error {
	term, err := pty.New(cols, rows, command, workdir, nil)
	if err != nil {
		return fmt.Errorf("create terminal: %w", err)
	}

	d := &Daemon{
		socketPath:  socketPath,
		sessionName: sessionName,
		terminal:    term,
		buffer:      scrollback.New(2000),
		screen:      screen.New(cols, rows),
		subs:        make(map[chan []byte]struct{}),
		done:        make(chan struct{}),
	}

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		term.Close()
		return fmt.Errorf("listen: %w", err)
	}
	d.listener = listener

	addr := listener.Addr().(*net.TCPAddr)
	info := ControlInfo{Port: addr.Port, PID: os.Getpid()}
	if err := writeControlFile(socketPath, info); err != nil {
		listener.Close()
		term.Close()
		return fmt.Errorf("write control file: %w", err)
	}

	// Redirect log output to a file next to the control file for debugging.
	logPath := socketPath + ".log"
	if lf, err := os.OpenFile(logPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0644); err == nil {
		log.SetOutput(lf)
		defer lf.Close()
	}

	log.Printf("daemon: session=%s pid=%d port=%d socket=%s", sessionName, info.PID, info.Port, socketPath)

	go d.readOutput()
	go d.watchProcess()

	d.acceptConnections()
	d.cleanup()
	return nil
}

// readOutput continuously reads from the terminal and feeds data into
// the scrollback buffer, the virtual screen, optional pipe-pane file,
// and any attached streaming clients.
func (d *Daemon) readOutput() {
	buf := make([]byte, 4096)
	for {
		n, err := d.terminal.Read(buf)
		if n > 0 {
			data := buf[:n]
			d.buffer.Write(data)
			d.screen.Write(data)

			d.pipePaneMu.Lock()
			if d.pipePaneFile != nil {
				d.pipePaneFile.Write(data)
			}
			d.pipePaneMu.Unlock()

			d.broadcast(data)
		}
		if err != nil {
			if err != io.EOF {
				log.Printf("daemon: read error: %v", err)
			}
			return
		}
	}
}

// broadcast fans a raw output chunk out to attach subscribers. A
// subscriber whose queue is full is dropped (its terminal reconnects)
// rather than allowed to render corrupted output.
func (d *Daemon) broadcast(data []byte) {
	d.subsMu.Lock()
	defer d.subsMu.Unlock()
	if len(d.subs) == 0 {
		return
	}
	cp := append([]byte(nil), data...)
	for ch := range d.subs {
		select {
		case ch <- cp:
		default:
			delete(d.subs, ch)
			close(ch)
		}
	}
}

// subscribe registers a new output subscriber. The returned channel is
// closed by unsubscribe, by broadcast backpressure, or on child exit.
func (d *Daemon) subscribe() chan []byte {
	ch := make(chan []byte, 64)
	d.subsMu.Lock()
	d.subs[ch] = struct{}{}
	d.subsMu.Unlock()
	return ch
}

func (d *Daemon) unsubscribe(ch chan []byte) {
	d.subsMu.Lock()
	if _, ok := d.subs[ch]; ok {
		delete(d.subs, ch)
		close(ch)
	}
	d.subsMu.Unlock()
}

// closeSubscribers terminates all attach streams (session ending).
func (d *Daemon) closeSubscribers() {
	d.subsMu.Lock()
	for ch := range d.subs {
		delete(d.subs, ch)
		close(ch)
	}
	d.subsMu.Unlock()
}

// watchProcess waits for the child to exit, then shuts down the daemon
// after a grace period.
func (d *Daemon) watchProcess() {
	d.terminal.Wait()
	log.Printf("daemon: child exited with code %d", d.terminal.ExitCode())
	close(d.done)
	// Session is gone — attached clients exit immediately, like tmux.
	d.closeSubscribers()
	time.Sleep(5 * time.Second)
	d.listener.Close()
}

func (d *Daemon) acceptConnections() {
	for {
		conn, err := d.listener.Accept()
		if err != nil {
			return
		}
		go d.handleConnection(conn)
	}
}

func (d *Daemon) handleConnection(conn net.Conn) {
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(10 * time.Second))

	var req ipc.Request
	if err := ipc.ReadMessage(conn, &req); err != nil {
		log.Printf("daemon: read request: %v", err)
		return
	}

	// Attach upgrades the connection to a long-lived binary stream —
	// it owns the connection from here on (no request/response deadline).
	if req.Action == ipc.ActionAttach {
		d.handleAttach(conn, req)
		return
	}

	resp := d.dispatch(req)
	if err := ipc.WriteMessage(conn, resp); err != nil {
		log.Printf("daemon: write response: %v", err)
	}
}

// handleAttach bridges a client onto the live session: optional resize,
// handshake, full-screen repaint, then bidirectional frame streaming
// until either side disconnects.
func (d *Daemon) handleAttach(conn net.Conn, req ipc.Request) {
	conn.SetDeadline(time.Time{}) // clear the request deadline

	if req.Cols > 0 && req.Rows > 0 {
		d.resize(req.Cols, req.Rows)
	}
	if err := ipc.WriteMessage(conn, ipc.Response{OK: true}); err != nil {
		return
	}

	// Repaint: clear the client screen and redraw the current grid so an
	// attach lands on the same view a tmux attach-session would show.
	repaint := "\x1b[2J\x1b[H" + strings.Join(d.screen.Capture(d.screenHeight()), "\r\n")
	if err := ipc.WriteFrame(conn, ipc.FrameOutput, []byte(repaint)); err != nil {
		return
	}

	ch := d.subscribe()
	defer d.unsubscribe(ch)

	// Writer: stream session output to the client. Exits when the
	// channel closes (unsubscribe, backpressure drop, or child exit).
	writeDone := make(chan struct{})
	go func() {
		defer close(writeDone)
		for data := range ch {
			if err := ipc.WriteFrame(conn, ipc.FrameOutput, data); err != nil {
				return
			}
		}
	}()

	// Reader: client input + resize frames. When the session ends, close
	// the conn to unblock this read so the attach exits promptly.
	doneSig := make(chan struct{})
	go func() {
		select {
		case <-d.done:
			conn.Close()
		case <-doneSig:
		}
	}()
	defer close(doneSig)

	for {
		typ, payload, err := ipc.ReadFrame(conn)
		if err != nil {
			return
		}
		switch typ {
		case ipc.FrameInput:
			if len(payload) > 0 {
				if _, err := d.terminal.Write(payload); err != nil {
					return
				}
			}
		case ipc.FrameResize:
			if len(payload) >= 4 {
				cols := int(payload[0])<<8 | int(payload[1])
				rows := int(payload[2])<<8 | int(payload[3])
				d.resize(cols, rows)
			}
		}
	}
}

// screenHeight reports the current grid height for a full repaint.
func (d *Daemon) screenHeight() int {
	return d.screen.Rows()
}

// resize applies a new terminal size to both the pty and the screen.
func (d *Daemon) resize(cols, rows int) {
	if cols <= 0 || rows <= 0 {
		return
	}
	if err := d.terminal.Resize(cols, rows); err != nil {
		log.Printf("daemon: pty resize: %v", err)
	}
	d.screen.Resize(cols, rows)
}

func (d *Daemon) dispatch(req ipc.Request) ipc.Response {
	switch req.Action {
	case ipc.ActionPing:
		return ipc.Response{OK: true}
	case ipc.ActionSendKeys:
		return d.handleSendKeys(req)
	case ipc.ActionSendKey:
		return d.handleSendKey(req)
	case ipc.ActionCapture:
		return d.handleCapture(req)
	case ipc.ActionHasSession:
		return d.handleHasSession()
	case ipc.ActionKillSession:
		return d.handleKillSession()
	case ipc.ActionSetOption:
		return d.handleSetOption(req)
	case ipc.ActionPipePane:
		return d.handlePipePane(req)
	case ipc.ActionResize:
		if req.Cols <= 0 || req.Rows <= 0 {
			return ipc.Response{OK: false, Error: "resize requires cols and rows"}
		}
		d.resize(req.Cols, req.Rows)
		return ipc.Response{OK: true}
	default:
		return ipc.Response{OK: false, Error: fmt.Sprintf("unknown action: %s", req.Action)}
	}
}

func (d *Daemon) handleSendKeys(req ipc.Request) ipc.Response {
	if req.Text != "" {
		if _, err := d.terminal.Write([]byte(req.Text)); err != nil {
			return ipc.Response{OK: false, Error: err.Error()}
		}
	}
	if req.SendEnter {
		if _, err := d.terminal.Write([]byte("\r")); err != nil {
			return ipc.Response{OK: false, Error: err.Error()}
		}
	}
	return ipc.Response{OK: true}
}

// keyMap translates tmux key names to the VT byte sequences expected by
// terminal applications.
var keyMap = map[string]string{
	"Enter":    "\r",
	"Escape":   "\x1b",
	"BSpace":   "\x7f",
	"Tab":      "\t",
	"Space":    " ",
	"C-c":      "\x03",
	"C-d":      "\x04",
	"C-z":      "\x1a",
	"Up":       "\x1b[A",
	"Down":     "\x1b[B",
	"Right":    "\x1b[C",
	"Left":     "\x1b[D",
	"Home":     "\x1b[H",
	"End":      "\x1b[F",
	"DC":       "\x1b[3~",
	"PageUp":   "\x1b[5~",
	"PageDown": "\x1b[6~",
}

func (d *Daemon) handleSendKey(req ipc.Request) ipc.Response {
	seq, ok := keyMap[req.Key]
	if !ok {
		return ipc.Response{OK: false, Error: fmt.Sprintf("unknown key: %s", req.Key)}
	}
	if _, err := d.terminal.Write([]byte(seq)); err != nil {
		return ipc.Response{OK: false, Error: err.Error()}
	}
	return ipc.Response{OK: true}
}

func (d *Daemon) handleCapture(req ipc.Request) ipc.Response {
	// Lines semantics: <0 = full scrollback + screen, 0 = visible screen,
	// >0 = last N rows of scrollback + screen (tmux capture-pane -S -N).
	lines := req.Lines
	if lines == 0 {
		lines = d.screenHeight()
	}
	// Use virtual screen for capture — handles full-screen TUI apps correctly.
	captured := d.screen.Capture(lines)
	output := strings.Join(captured, "\n")
	return ipc.Response{OK: true, Output: output}
}

func (d *Daemon) handleHasSession() ipc.Response {
	select {
	case <-d.done:
		return ipc.Response{OK: true, Exists: false}
	default:
		return ipc.Response{OK: true, Exists: true}
	}
}

func (d *Daemon) handleKillSession() ipc.Response {
	if err := d.terminal.Close(); err != nil {
		return ipc.Response{OK: false, Error: err.Error()}
	}
	return ipc.Response{OK: true}
}

func (d *Daemon) handleSetOption(req ipc.Request) ipc.Response {
	switch req.Option {
	case "history-limit":
		n, err := strconv.Atoi(req.Value)
		if err != nil || n <= 0 {
			return ipc.Response{OK: false, Error: "invalid history-limit value"}
		}
		d.buffer.SetCapacity(n)
		d.screen.SetHistoryCap(n)
		return ipc.Response{OK: true}
	default:
		return ipc.Response{OK: false, Error: fmt.Sprintf("unknown option: %s", req.Option)}
	}
}

func (d *Daemon) handlePipePane(req ipc.Request) ipc.Response {
	d.pipePaneMu.Lock()
	defer d.pipePaneMu.Unlock()

	if d.pipePaneFile != nil {
		d.pipePaneFile.Close()
		d.pipePaneFile = nil
	}

	if req.ShellCmd == "" {
		return ipc.Response{OK: true}
	}

	path := extractPipePath(req.ShellCmd)
	if path == "" {
		return ipc.Response{OK: false, Error: "unsupported pipe-pane command (only 'cat >> path' supported)"}
	}

	os.MkdirAll(filepath.Dir(path), 0755)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		return ipc.Response{OK: false, Error: err.Error()}
	}
	d.pipePaneFile = f
	return ipc.Response{OK: true}
}

func (d *Daemon) cleanup() {
	d.pipePaneMu.Lock()
	if d.pipePaneFile != nil {
		d.pipePaneFile.Close()
	}
	d.pipePaneMu.Unlock()

	d.terminal.Close()
	os.Remove(d.socketPath)
	log.Printf("daemon: cleaned up session %s", d.sessionName)
}

func writeControlFile(path string, info ControlInfo) error {
	os.MkdirAll(filepath.Dir(path), 0755)
	data, err := json.Marshal(info)
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0644)
}

// extractPipePath parses "cat >> /path/to/file" and returns the file path.
func extractPipePath(cmd string) string {
	cmd = strings.TrimSpace(cmd)
	if !strings.HasPrefix(cmd, "cat") {
		return ""
	}
	cmd = strings.TrimPrefix(cmd, "cat")
	cmd = strings.TrimSpace(cmd)
	if !strings.HasPrefix(cmd, ">>") {
		return ""
	}
	cmd = strings.TrimPrefix(cmd, ">>")
	cmd = strings.TrimSpace(cmd)
	cmd = strings.Trim(cmd, "'\"")
	return cmd
}
