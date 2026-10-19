package pty

// Terminal abstracts a pseudo-terminal backed process.
// On Windows this is implemented via ConPTY; on other platforms via
// exec.Cmd with pipes (for development/testing).
type Terminal interface {
	// Read reads output produced by the child process.
	Read(buf []byte) (int, error)

	// Write sends input to the child process.
	Write(data []byte) (int, error)

	// Resize changes the terminal dimensions (cols × rows).
	Resize(cols, rows int) error

	// Wait blocks until the child process exits.
	Wait() error

	// ExitCode returns the child process exit code. Only valid after Wait returns.
	ExitCode() int

	// Close terminates the child process and releases resources.
	Close() error
}
