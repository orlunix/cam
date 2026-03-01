package session

// Manager abstracts platform-specific session management.
// Linux uses real tmux; Windows will use ConPTY.
type Manager interface {
	Create(id string, cmd []string, workdir string, envSetup string) error
	Exists(id string) bool
	Kill(id string) error
	Capture(id string, lines int) (string, error)
	SendText(id string, text string, enter bool) error
	SendKey(id string, key string) error
	StartLogging(id string) (string, error)
	ReadLog(id string, offset int64, maxBytes int) ([]byte, int64, error)
}
