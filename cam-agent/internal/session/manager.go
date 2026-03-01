package session

import "runtime"

// NewManager returns a platform-appropriate session manager.
// Linux/macOS: tmux-based
// Windows: ConPTY-based (future)
func NewManager() Manager {
	switch runtime.GOOS {
	case "windows":
		// TODO: Phase 5 — ConPTY backend
		panic("cam-agent: Windows ConPTY backend not yet implemented")
	default:
		return &TmuxManager{}
	}
}
