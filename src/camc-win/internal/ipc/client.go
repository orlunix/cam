package ipc

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"time"
)

// ControlInfo is written to the socket path file by the daemon so that
// CLI clients can discover which TCP port to connect to.
type ControlInfo struct {
	Port int `json:"port"`
	PID  int `json:"pid"`
}

// ReadControlFile reads the daemon's control info from the socket path.
func ReadControlFile(path string) (*ControlInfo, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var info ControlInfo
	if err := json.Unmarshal(data, &info); err != nil {
		return nil, err
	}
	return &info, nil
}

// Connect establishes a TCP connection to the daemon identified by the
// given socket (control file) path. Returns an error if the control file
// doesn't exist or the daemon isn't reachable.
func Connect(socketPath string) (net.Conn, error) {
	info, err := ReadControlFile(socketPath)
	if err != nil {
		return nil, fmt.Errorf("session not found: %w", err)
	}

	addr := fmt.Sprintf("127.0.0.1:%d", info.Port)
	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		return nil, fmt.Errorf("session not running: %w", err)
	}

	return conn, nil
}

// SendRequest connects to the daemon, sends a request, and returns the response.
func SendRequest(socketPath string, req *Request) (*Response, error) {
	conn, err := Connect(socketPath)
	if err != nil {
		return nil, err
	}
	defer conn.Close()

	conn.SetDeadline(time.Now().Add(10 * time.Second))

	if err := WriteMessage(conn, req); err != nil {
		return nil, fmt.Errorf("send request: %w", err)
	}

	var resp Response
	if err := ReadMessage(conn, &resp); err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	return &resp, nil
}
