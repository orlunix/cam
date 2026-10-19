// Package camcwin implements the camc-win agent registry: a JSON store
// at %USERPROFILE%\.cam\agents.json (CAM_HOME override) mirroring the
// record shape of POSIX camc (src/camc_pkg), so the CAM hub's
// `_normalizeAgent` consumes `camc-win --json list` unchanged.
package camcwin

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Task mirrors camc's nested task object.
type Task struct {
	Name               string   `json:"name"`
	Tool               string   `json:"tool"`
	Prompt             string   `json:"prompt"`
	AutoConfirm        bool     `json:"auto_confirm"`
	AutoExit           bool     `json:"auto_exit"`
	AutoExitEnable     bool     `json:"auto_exit_enable"`
	Tags               []string `json:"tags"`
	SystemPrompt       string   `json:"system_prompt"`
	SystemPromptFile   string   `json:"system_prompt_file"`
	SystemPromptSource string   `json:"system_prompt_source"`
	RequestedTool      string   `json:"requested_tool,omitempty"`
}

// Agent mirrors camc's agents.json record (unified schema).
type Agent struct {
	ID            string   `json:"id"`
	SessionID     string   `json:"session_id"`
	Task          Task     `json:"task"`
	ContextID     string   `json:"context_id"`
	ContextName   string   `json:"context_name"`
	ContextPath   string   `json:"context_path"`
	TransportType string   `json:"transport_type"`
	Status        string   `json:"status"`
	State         string   `json:"state"`
	TmuxSession   string   `json:"tmux_session"`
	TmuxSocket    string   `json:"tmux_socket"`
	TmuxBin       string   `json:"tmux_bin"`
	TmuxVersion   string   `json:"tmux_version"`
	PID           *int     `json:"pid"`
	Hostname      string   `json:"hostname"`
	StartedAt     string   `json:"started_at"`
	CompletedAt   *string  `json:"completed_at"`
	ExitReason    *string  `json:"exit_reason"`
	RetryCount    int      `json:"retry_count"`
	CostEstimate  any      `json:"cost_estimate"`
	FilesChanged  []string `json:"files_changed"`
}

// Home returns the camc state dir: $CAM_HOME or ~/.cam.
func Home() string {
	if h := os.Getenv("CAM_HOME"); h != "" {
		return h
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".cam"
	}
	return filepath.Join(home, ".cam")
}

// SocketsDir holds the per-session control files.
func SocketsDir() string { return filepath.Join(Home(), "sockets") }

// SocketPath maps a session name to its control file path.
func SocketPath(session string) string {
	return filepath.Join(SocketsDir(), session+".sock")
}

// ArchivesDir holds removed-agent records (`rm --archive`).
func ArchivesDir() string { return filepath.Join(Home(), "archives") }

// NowISO returns the UTC timestamp format camc uses (_now_iso).
func NowISO() string { return time.Now().UTC().Format("2006-01-02T15:04:05Z") }

// NewID returns an 8-hex-char agent id (camc: uuid4().hex[:8]).
func NewID() string {
	b := make([]byte, 4)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// NewUUID returns a uuid4-shaped string.
func NewUUID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// Store is a JSON-file agent registry with lock-file mutual exclusion.
type Store struct {
	path string
}

// NewStore opens the registry at the default location.
func NewStore() *Store { return &Store{path: filepath.Join(Home(), "agents.json")} }

// List returns all records (missing/corrupt file → empty).
func (s *Store) List() []Agent {
	data, err := os.ReadFile(s.path)
	if err != nil {
		return nil
	}
	var agents []Agent
	if json.Unmarshal(data, &agents) != nil {
		return nil
	}
	return agents
}

// withLock takes the registry lock file (O_EXCL spin, 30s stale reap,
// 5s timeout), runs fn, and releases. POSIX camc uses fcntl; Windows
// has no byte-range flock in the stdlib, and O_EXCL create is atomic
// on NTFS — good enough for the hub-serialized write pattern here.
func (s *Store) withLock(fn func() error) error {
	lock := s.path + ".lock"
	if err := os.MkdirAll(filepath.Dir(s.path), 0755); err != nil {
		return err
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		f, err := os.OpenFile(lock, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
		if err == nil {
			f.Close()
			break
		}
		if !os.IsExist(err) {
			return err
		}
		if st, serr := os.Stat(lock); serr == nil && time.Since(st.ModTime()) > 30*time.Second {
			os.Remove(lock)
			continue
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("registry lock timeout: %s", lock)
		}
		time.Sleep(20 * time.Millisecond)
	}
	defer os.Remove(lock)
	return fn()
}

// modify reads, mutates via fn, and atomically rewrites the registry
// under the lock.
func (s *Store) modify(fn func([]Agent) []Agent) error {
	return s.withLock(func() error {
		agents := s.List()
		agents = fn(agents)
		tmp := s.path + ".tmp"
		data, err := json.MarshalIndent(agents, "", "  ")
		if err != nil {
			return err
		}
		if err := os.WriteFile(tmp, data, 0644); err != nil {
			return err
		}
		// Go ≥1.20: os.Rename replaces the destination on Windows too.
		return os.Rename(tmp, s.path)
	})
}

// Save inserts or replaces a record (keyed on id).
func (s *Store) Save(a Agent) error {
	return s.modify(func(agents []Agent) []Agent {
		for i := range agents {
			if agents[i].ID == a.ID {
				agents[i] = a
				return agents
			}
		}
		return append(agents, a)
	})
}

// UpdateFields applies patch to the record with the given exact id.
func (s *Store) UpdateFields(id string, patch func(*Agent)) error {
	return s.modify(func(agents []Agent) []Agent {
		for i := range agents {
			if agents[i].ID == id {
				patch(&agents[i])
				break
			}
		}
		return agents
	})
}

// Remove deletes the record with the given exact id.
func (s *Store) Remove(id string) error {
	return s.modify(func(agents []Agent) []Agent {
		out := agents[:0]
		for _, a := range agents {
			if a.ID != id {
				out = append(out, a)
			}
		}
		return out
	})
}

// Resolve finds an agent by camc's idiom: exact id → tmux session name →
// unique id prefix → case-insensitive name (exact, then unique prefix) →
// 1-based numeric index matching `list` order.
func (s *Store) Resolve(ref string) *Agent {
	agents := s.List()
	for i := range agents {
		if agents[i].ID == ref {
			return &agents[i]
		}
	}
	for i := range agents {
		if agents[i].TmuxSession == ref {
			return &agents[i]
		}
	}
	var prefix []*Agent
	for i := range agents {
		if strings.HasPrefix(agents[i].ID, ref) {
			prefix = append(prefix, &agents[i])
		}
	}
	if len(prefix) == 1 {
		return prefix[0]
	}
	lower := strings.ToLower(ref)
	for i := range agents {
		if strings.ToLower(agents[i].Task.Name) == lower && lower != "" {
			return &agents[i]
		}
	}
	var namePrefix []*Agent
	for i := range agents {
		if lower != "" && strings.HasPrefix(strings.ToLower(agents[i].Task.Name), lower) {
			namePrefix = append(namePrefix, &agents[i])
		}
	}
	if len(namePrefix) == 1 {
		return namePrefix[0]
	}
	var idx int
	if _, err := fmt.Sscanf(ref, "%d", &idx); err == nil && idx >= 1 && idx <= len(agents) {
		return &agents[idx-1]
	}
	return nil
}
